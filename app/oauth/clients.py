"""Client registration for the MCP authorization server.

Three ways a client gets a client_id (MCP spec 2026-07-28, "Client
Registration"):

* Client ID Metadata Documents (CIMD) — the client_id *is* an https URL. We
  fetch it, check it describes itself, and cache name/redirect URIs. Claude and
  ChatGPT use this when our AS metadata advertises support.
* Dynamic Client Registration (RFC 7591) — POST /oauth/register. Deprecated by
  the spec but still what some clients speak; kept as a fallback.
* Pre-registered — a row inserted by hand.

Everything that touches the network lives here so it can be mocked in tests.
"""

import datetime
import ipaddress
import json
import logging
import secrets
import socket
from typing import Optional
from urllib.parse import urlsplit

import requests
from fastapi_sqlalchemy import db

from oauth.models import OAuthClient, utcnow

logger = logging.getLogger(__name__)

CIMD_FETCH_TIMEOUT = 5          # seconds; Claude gives the whole flow 10 s
CIMD_MAX_BYTES = 64 * 1024
CIMD_CACHE_TTL = datetime.timedelta(hours=24)

# Redirect URIs a client may use without appearing in its metadata: none.
# Loopback URIs are matched with the port ignored (RFC 8252 §7.3), which is
# what Claude Code and other native clients rely on.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class ClientError(Exception):
    """A client_id or its metadata is unusable. Message is safe to show."""


# ---------------------------------------------------------------------------
# Redirect URI matching
# ---------------------------------------------------------------------------

def is_loopback_redirect(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def redirect_uri_matches(candidate: str, registered: str) -> bool:
    """Exact match, except loopback URIs ignore the port.

    Everything else — scheme, host, path, query — must be byte-identical.
    Fragments are not allowed in redirect URIs at all.
    """
    if candidate == registered:
        return True
    if not (is_loopback_redirect(candidate) and is_loopback_redirect(registered)):
        return False
    c, r = urlsplit(candidate), urlsplit(registered)
    return (
        c.scheme == r.scheme
        and (c.hostname or "") == (r.hostname or "")
        and c.path == r.path
        and c.query == r.query
        and not c.fragment
        and not r.fragment
    )


def client_allows_redirect(client: OAuthClient, redirect_uri: str) -> bool:
    return any(redirect_uri_matches(redirect_uri, r) for r in (client.redirect_uris or []))


def only_loopback_redirects(client: OAuthClient) -> bool:
    uris = client.redirect_uris or []
    return bool(uris) and all(is_loopback_redirect(u) for u in uris)


# ---------------------------------------------------------------------------
# CIMD
# ---------------------------------------------------------------------------

def is_cimd_client_id(client_id: str) -> bool:
    try:
        parts = urlsplit(client_id)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.netloc) and not parts.fragment


def _host_is_public(hostname: str) -> bool:
    """Refuse to fetch metadata from private, loopback or link-local addresses.

    The client_id is attacker-controlled input that we turn into an outbound
    request, so this is the SSRF guard. Resolution happens here, before the
    fetch; `requests` will resolve again, which leaves a small DNS-rebinding
    window we accept for a 5-second, 64 KB, GET-only fetch of public JSON.
    """
    try:
        infos = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            return False
    return True


def fetch_cimd(client_id: str) -> dict:
    """Fetch and validate a Client ID Metadata Document. Raises ClientError."""
    parts = urlsplit(client_id)
    if not _host_is_public(parts.hostname or ""):
        raise ClientError("client_id host is not publicly reachable.")

    try:
        with requests.get(
            client_id,
            timeout=CIMD_FETCH_TIMEOUT,
            headers={"Accept": "application/json", "User-Agent": "Packstack-OAuth/1.0"},
            stream=True,
            allow_redirects=False,
        ) as resp:
            if resp.status_code != 200:
                raise ClientError(f"client_id metadata returned HTTP {resp.status_code}.")
            body = resp.raw.read(CIMD_MAX_BYTES + 1, decode_content=True)
    except requests.RequestException as exc:
        logger.info("CIMD fetch failed for %s: %s", client_id, exc)
        raise ClientError("client_id metadata could not be fetched.")

    if len(body) > CIMD_MAX_BYTES:
        raise ClientError("client_id metadata document is too large.")

    try:
        doc = json.loads(body)
    except ValueError:
        raise ClientError("client_id metadata is not valid JSON.")
    if not isinstance(doc, dict):
        raise ClientError("client_id metadata must be a JSON object.")

    # The document must claim to be the URL it was fetched from.
    if doc.get("client_id") != client_id:
        raise ClientError("client_id metadata does not match its URL.")

    redirect_uris = doc.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris or not all(
        isinstance(u, str) and u for u in redirect_uris
    ):
        raise ClientError("client_id metadata has no redirect_uris.")
    for uri in redirect_uris:
        u = urlsplit(uri)
        if u.fragment or u.scheme not in ("https", "http"):
            raise ClientError("client_id metadata has an invalid redirect_uri.")
        if u.scheme == "http" and not is_loopback_redirect(uri):
            raise ClientError("client_id metadata uses http for a non-loopback redirect_uri.")

    methods = doc.get("token_endpoint_auth_methods_supported") or doc.get("token_endpoint_auth_method")
    if isinstance(methods, str):
        methods = [methods]
    if methods and "none" not in methods:
        # We only issue tokens to public clients from CIMD. private_key_jwt is
        # a v2 candidate.
        raise ClientError("Only public (token_endpoint_auth_method=none) CIMD clients are supported.")

    return doc


def _apply_cimd(client: OAuthClient, doc: dict) -> None:
    client.kind = "cimd"
    client.client_name = (doc.get("client_name") or "")[:200] or None
    client.client_uri = (doc.get("client_uri") or "")[:1000] or None
    client.logo_uri = (doc.get("logo_uri") or "")[:1000] or None
    client.redirect_uris = doc["redirect_uris"]
    client.token_endpoint_auth_method = "none"
    client.metadata_fetched_at = utcnow()


def resolve_client(client_id: str, redirect_uri: Optional[str] = None) -> OAuthClient:
    """Return the OAuthClient for a client_id, fetching CIMD as needed.

    If `redirect_uri` is given and the cached document does not allow it, the
    document is refetched once before failing — clients add redirect URIs.
    Raises ClientError for anything the caller should surface as
    invalid_client / invalid_request.
    """
    if not client_id:
        raise ClientError("client_id is required.")

    client = db.session.query(OAuthClient).filter_by(client_id=client_id).first()

    if is_cimd_client_id(client_id):
        stale = (
            client is None
            or client.metadata_fetched_at is None
            or utcnow() - client.metadata_fetched_at > CIMD_CACHE_TTL
            or (redirect_uri is not None and not client_allows_redirect(client, redirect_uri))
        )
        if stale:
            doc = fetch_cimd(client_id)
            if client is None:
                client = OAuthClient(client_id=client_id)
                db.session.add(client)
            _apply_cimd(client, doc)
            db.session.commit()
        return client

    if client is None:
        raise ClientError("Unknown client_id.")
    return client


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------

def register_client(metadata: dict) -> OAuthClient:
    """Create a public client from an RFC 7591 registration request.

    We ignore anything that would make it a confidential client; the spec
    marks DCR as deprecated and Claude only uses it as a fallback, so the
    simplest correct behavior wins.
    """
    redirect_uris = metadata.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris or not all(
        isinstance(u, str) and u for u in redirect_uris
    ):
        raise ClientError("redirect_uris is required.")
    for uri in redirect_uris:
        u = urlsplit(uri)
        if u.fragment or u.scheme not in ("https", "http"):
            raise ClientError("Invalid redirect_uri.")
        if u.scheme == "http" and not is_loopback_redirect(uri):
            raise ClientError("http redirect URIs are only allowed for loopback hosts.")

    grant_types = metadata.get("grant_types") or ["authorization_code"]
    if "authorization_code" not in grant_types:
        raise ClientError("authorization_code grant is required.")

    client = OAuthClient(
        client_id="dcr_" + secrets.token_urlsafe(24),
        kind="dcr",
        client_name=(metadata.get("client_name") or "")[:200] or None,
        client_uri=(metadata.get("client_uri") or "")[:1000] or None,
        logo_uri=(metadata.get("logo_uri") or "")[:1000] or None,
        redirect_uris=redirect_uris,
        token_endpoint_auth_method="none",
    )
    db.session.add(client)
    db.session.commit()
    return client


def client_display_name(client: OAuthClient) -> str:
    if client.client_name:
        return client.client_name
    if is_cimd_client_id(client.client_id):
        return urlsplit(client.client_id).hostname or client.client_id
    return "an application"
