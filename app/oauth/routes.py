"""OAuth 2.1 authorization server endpoints + RFC 8414 / RFC 9728 discovery.

Mounted only when MCP_ENABLED. See claude/mcp-server-spec.md §4 for the
design; this file follows it section by section.

Flow: /oauth/authorize validates the request and parks it in
oauth_authorization_request, then redirects the browser to the web app's
consent page. The page (logged in via the normal cookie) reads the parked
request through GET /oauth/consent/{id}, and on Allow POSTs /oauth/consent,
which mints the code and returns the redirect URL for the page to follow.
"""

import collections
import datetime
import logging
import secrets
import threading
import time
from typing import Optional
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi_sqlalchemy import db
from pydantic import BaseModel

from models.base import User
from oauth import clients, tokens
from oauth.models import OAuthAuthorizationRequest, OAuthGrant, utcnow
from utils.auth import authenticate
from utils.consts import (
    MCP_AUTH_REQUEST_TTL, MCP_CONSENT_URL, MCP_DOCS_URL, MCP_ISSUER,
    MCP_RESOURCE_URL,
)

logger = logging.getLogger(__name__)

route = APIRouter()

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
PUBLIC_CORS = {"Access-Control-Allow-Origin": "*"}


# ---------------------------------------------------------------------------
# A small in-process rate limiter for the unauthenticated endpoints. Good
# enough for one API instance; swap for Redis if the service is scaled out.
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                raise HTTPException(429, "Too many requests. Try again shortly.",
                                    headers={"Retry-After": str(self.window)})
            q.append(now)
            if len(self._hits) > 10000:      # crude memory cap
                self._hits.clear()


_limit_token = _RateLimiter(limit=20, window_seconds=60)
_limit_register = _RateLimiter(limit=10, window_seconds=60)
_limit_authorize = _RateLimiter(limit=30, window_seconds=60)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@route.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata():
    """RFC 8414. Claude selects CIMD only when both
    client_id_metadata_document_supported and "none" auth are advertised."""
    return JSONResponse({
        "issuer": MCP_ISSUER,
        "authorization_endpoint": f"{MCP_ISSUER}/oauth/authorize",
        "token_endpoint": f"{MCP_ISSUER}/oauth/token",
        "registration_endpoint": f"{MCP_ISSUER}/oauth/register",
        "revocation_endpoint": f"{MCP_ISSUER}/oauth/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [tokens.SCOPE_READ, tokens.SCOPE_WRITE, tokens.SCOPE_OFFLINE],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": MCP_DOCS_URL,
    }, headers={**PUBLIC_CORS, "Cache-Control": "public, max-age=3600"})


@route.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource_metadata():
    """RFC 9728 for the MCP endpoint. Lists both scopes: clients that ignore
    the 401 `scope` hint fall back to this list, and hosted Claude does not
    step up from read to write later (see server.RequireAuth)."""
    return JSONResponse({
        "resource": MCP_RESOURCE_URL,
        "authorization_servers": [MCP_ISSUER],
        "scopes_supported": [tokens.SCOPE_READ, tokens.SCOPE_WRITE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Packstack",
        "resource_documentation": MCP_DOCS_URL,
    }, headers={**PUBLIC_CORS, "Cache-Control": "public, max-age=3600"})


# ---------------------------------------------------------------------------
# Authorize
# ---------------------------------------------------------------------------

def _redirect_error(redirect_uri: str, error: str, description: str, state: Optional[str]) -> RedirectResponse:
    params = {"error": error, "error_description": description, "iss": MCP_ISSUER}
    if state is not None:
        params["state"] = state
    return RedirectResponse(_with_query(redirect_uri, params), status_code=302, headers=NO_STORE)


def _with_query(url: str, params: dict) -> str:
    sep = "&" if urlsplit(url).query else "?"
    return f"{url}{sep}{urlencode(params)}"


@route.get("/oauth/authorize")
def authorize(
    request: Request,
    response_type: Optional[str] = None,
    client_id: Optional[str] = None,
    redirect_uri: Optional[str] = None,
    scope: Optional[str] = None,
    state: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
    resource: Optional[str] = None,
):
    _limit_authorize.check(_client_ip(request))

    # --- Things we cannot report via redirect: bad client or bad redirect_uri.
    # Never bounce the browser to an unverified URL.
    try:
        client = clients.resolve_client(client_id or "", redirect_uri)
    except clients.ClientError as exc:
        return PlainTextResponse(f"Invalid client: {exc}", status_code=400, headers=NO_STORE)

    if not redirect_uri or not clients.client_allows_redirect(client, redirect_uri):
        return PlainTextResponse(
            "Invalid redirect_uri: it is not registered for this client.",
            status_code=400, headers=NO_STORE)

    # --- From here on, errors go back to the client via redirect.
    if response_type != "code":
        return _redirect_error(redirect_uri, "unsupported_response_type",
                               "Only response_type=code is supported.", state)
    if not code_challenge or code_challenge_method != "S256":
        return _redirect_error(redirect_uri, "invalid_request",
                               "PKCE with code_challenge_method=S256 is required.", state)
    if not (43 <= len(code_challenge) <= 128):
        return _redirect_error(redirect_uri, "invalid_request", "Malformed code_challenge.", state)
    if not resource:
        return _redirect_error(redirect_uri, "invalid_target",
                               f"The resource parameter is required and must be {MCP_RESOURCE_URL}.", state)
    if resource.rstrip("/").lower() != MCP_RESOURCE_URL.lower():
        return _redirect_error(redirect_uri, "invalid_target",
                               f"Unknown resource. This server issues tokens for {MCP_RESOURCE_URL} only.", state)
    try:
        scopes = tokens.parse_scope(scope)
    except tokens.TokenError as exc:
        return _redirect_error(redirect_uri, exc.error, exc.description, state)

    pending = OAuthAuthorizationRequest(
        id=secrets.token_urlsafe(32),
        client_id=client.client_id,
        redirect_uri=redirect_uri,
        scope=tokens.scope_string(scopes),
        state=state,
        code_challenge=code_challenge,
        resource=MCP_RESOURCE_URL,
        expires_at=utcnow() + datetime.timedelta(seconds=MCP_AUTH_REQUEST_TTL),
    )
    db.session.add(pending)
    db.session.commit()

    return RedirectResponse(_with_query(MCP_CONSENT_URL, {"request": pending.id}),
                            status_code=302, headers=NO_STORE)


# ---------------------------------------------------------------------------
# Consent (called by the web app with the user's session cookie)
# ---------------------------------------------------------------------------

def _load_pending(request_id: str) -> OAuthAuthorizationRequest:
    pending = db.session.query(OAuthAuthorizationRequest).filter_by(id=request_id).first()
    if pending is None:
        raise HTTPException(404, "This connection request has expired or was already completed. Start again from your AI assistant.")
    if pending.expires_at < utcnow():
        db.session.delete(pending)
        db.session.commit()
        raise HTTPException(410, "This connection request has expired. Start again from your AI assistant.")
    return pending


SCOPE_DESCRIPTIONS = {
    tokens.SCOPE_READ: {
        "title": "Read your trips, packs and gear",
        "detail": "See your trips, packing lists, gear closet, kits and categories. Never your email, payment details or hiker body measurements.",
    },
    tokens.SCOPE_WRITE: {
        "title": "Make changes on your behalf",
        "detail": "Create and edit trips, packs, gear and kits, and archive or restore gear. It cannot delete anything. Requires a Packstack subscription.",
    },
    tokens.SCOPE_OFFLINE: {
        "title": "Stay connected",
        "detail": "Keep the connection working without asking you to sign in again. You can disconnect any time from Connected apps.",
    },
}


@route.get("/oauth/consent/{request_id}")
def consent_details(request_id: str, user: User = Depends(authenticate)):
    """What the consent page renders."""
    pending = _load_pending(request_id)
    client = pending.client
    scopes = tokens.parse_scope(pending.scope)
    existing = db.session.query(OAuthGrant).filter_by(
        user_id=user.id, client_id=client.client_id, revoked_at=None).first()
    return {
        "request_id": pending.id,
        "client": {
            "name": clients.client_display_name(client),
            "uri": client.client_uri,
            "logo_uri": client.logo_uri,
            "kind": client.kind,
        },
        "redirect_host": urlsplit(pending.redirect_uri).hostname,
        "loopback_only": clients.only_loopback_redirects(client),
        "scopes": [{"scope": s, **SCOPE_DESCRIPTIONS[s]} for s in scopes],
        "requests_write": tokens.SCOPE_WRITE in scopes,
        "already_connected": existing is not None,
        "account": {"username": user.username, "is_subscribed": bool(user.is_subscribed)},
        "expires_at": pending.expires_at.isoformat(),
    }


class ConsentDecision(BaseModel):
    request_id: str
    approve: bool


@route.post("/oauth/consent")
def consent_decide(payload: ConsentDecision, user: User = Depends(authenticate)):
    pending = _load_pending(payload.request_id)
    redirect_uri, state = pending.redirect_uri, pending.state

    if not payload.approve:
        db.session.delete(pending)
        db.session.commit()
        return {"redirect_to": _with_query(redirect_uri, {
            "error": "access_denied", "error_description": "The user declined the request.",
            "iss": MCP_ISSUER, **({"state": state} if state is not None else {}),
        })}

    scopes = tokens.parse_scope(pending.scope)
    grant = tokens.upsert_grant(user.id, pending.client_id, scopes)
    code = tokens.issue_code(grant, pending.client_id, redirect_uri, scopes,
                             pending.code_challenge, pending.resource)
    db.session.delete(pending)
    db.session.commit()

    logger.info("oauth consent granted user=%s client=%s scope=%s", user.id, pending.client_id, pending.scope)
    params = {"code": code, "iss": MCP_ISSUER}
    if state is not None:
        params["state"] = state
    return {"redirect_to": _with_query(redirect_uri, params)}


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def _token_error(exc: tokens.TokenError, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": exc.error, "error_description": exc.description},
                        status_code=status, headers={**NO_STORE, **PUBLIC_CORS})


def _authenticate_client(client_id: Optional[str], client_secret: Optional[str], request: Request) -> clients.OAuthClient:
    """Public clients identify with client_id alone; a pre-registered
    confidential client must present its secret (body or HTTP Basic)."""
    if request.headers.get("authorization", "").lower().startswith("basic "):
        import base64
        try:
            raw = base64.b64decode(request.headers["authorization"][6:]).decode("utf-8")
            basic_id, _, basic_secret = raw.partition(":")
            client_id = client_id or basic_id
            client_secret = client_secret or basic_secret
        except Exception:
            raise tokens.TokenError("invalid_client", "Malformed Basic credentials.")
    if not client_id:
        raise tokens.TokenError("invalid_client", "client_id is required.")
    try:
        client = clients.resolve_client(client_id)
    except clients.ClientError as exc:
        raise tokens.TokenError("invalid_client", str(exc))
    if client.client_secret_hash:
        if not client_secret or not secrets.compare_digest(
                tokens.hash_secret(client_secret), client.client_secret_hash):
            raise tokens.TokenError("invalid_client", "Client authentication failed.")
    return client


@route.post("/oauth/token")
def token(
    request: Request,
    grant_type: Optional[str] = Form(None),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
    scope: Optional[str] = Form(None),
    resource: Optional[str] = Form(None),
):
    _limit_token.check(_client_ip(request))
    try:
        client = _authenticate_client(client_id, client_secret, request)
        if grant_type == "authorization_code":
            if not code:
                raise tokens.TokenError("invalid_request", "code is required.")
            issued = tokens.exchange_code(code, client.client_id, redirect_uri, code_verifier, resource)
        elif grant_type == "refresh_token":
            if not refresh_token:
                raise tokens.TokenError("invalid_request", "refresh_token is required.")
            issued = tokens.refresh_tokens(refresh_token, client.client_id, scope, resource)
        else:
            raise tokens.TokenError("unsupported_grant_type",
                                    "grant_type must be authorization_code or refresh_token.")
    except tokens.TokenError as exc:
        return _token_error(exc, status=401 if exc.error == "invalid_client" else 400)

    body = {
        "access_token": issued.access_token,
        "token_type": "Bearer",
        "expires_in": issued.expires_in,
        "scope": issued.scope,
    }
    if issued.refresh_token:
        body["refresh_token"] = issued.refresh_token
    return JSONResponse(body, headers={**NO_STORE, **PUBLIC_CORS})


@route.post("/oauth/revoke")
def revoke(
    request: Request,
    token: str = Form(...),
    client_id: Optional[str] = Form(None),
    token_type_hint: Optional[str] = Form(None),
):
    _limit_token.check(_client_ip(request))
    tokens.revoke_token(token, client_id)
    return Response(status_code=200, headers={**NO_STORE, **PUBLIC_CORS})


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------

@route.post("/oauth/register", status_code=201)
async def register(request: Request):
    _limit_register.check(_client_ip(request))
    try:
        metadata = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_client_metadata", "error_description": "Body must be JSON."},
                            status_code=400, headers={**NO_STORE, **PUBLIC_CORS})
    if not isinstance(metadata, dict):
        return JSONResponse({"error": "invalid_client_metadata", "error_description": "Body must be a JSON object."},
                            status_code=400, headers={**NO_STORE, **PUBLIC_CORS})
    try:
        client = clients.register_client(metadata)
    except clients.ClientError as exc:
        return JSONResponse({"error": "invalid_redirect_uri" if "redirect" in str(exc).lower() else "invalid_client_metadata",
                             "error_description": str(exc)},
                            status_code=400, headers={**NO_STORE, **PUBLIC_CORS})
    return JSONResponse({
        "client_id": client.client_id,
        "client_id_issued_at": int(client.created_at.timestamp()),
        "client_name": client.client_name,
        "client_uri": client.client_uri,
        "logo_uri": client.logo_uri,
        "redirect_uris": client.redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201, headers={**NO_STORE, **PUBLIC_CORS})


# ---------------------------------------------------------------------------
# Connected apps (web app, cookie auth)
# ---------------------------------------------------------------------------

@route.get("/oauth/grants")
def list_grants(user: User = Depends(authenticate)):
    rows = (db.session.query(OAuthGrant)
            .filter(OAuthGrant.user_id == user.id, OAuthGrant.revoked_at.is_(None))
            .order_by(OAuthGrant.created_at.desc()).all())
    return [{
        "id": g.id,
        "client": {
            "name": clients.client_display_name(g.client),
            "uri": g.client.client_uri,
            "logo_uri": g.client.logo_uri,
        },
        "scopes": tokens.parse_scope(g.scope),
        "created_at": g.created_at.isoformat(),
        "last_used_at": g.last_used_at.isoformat() if g.last_used_at else None,
    } for g in rows]


@route.delete("/oauth/grants/{grant_id}", status_code=204)
def revoke_grant(grant_id: int, user: User = Depends(authenticate)):
    grant = db.session.query(OAuthGrant).filter_by(id=grant_id, user_id=user.id).first()
    if grant is None:
        raise HTTPException(404, "Connection not found.")
    tokens.revoke_grant(grant)
    db.session.commit()
    logger.info("oauth grant revoked user=%s client=%s", user.id, grant.client_id)
    return Response(status_code=204)
