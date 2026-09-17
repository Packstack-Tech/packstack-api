"""Token and authorization-code issuance, verification and rotation.

Every secret handed to a client is 32 random bytes, base64url-encoded, and
stored only as its SHA-256 hash. Verification is a single primary-key lookup.
"""

import base64
import datetime
import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi_sqlalchemy import db

from oauth.models import (
    OAuthAuthorizationCode, OAuthGrant, OAuthToken, utcnow,
)
from utils.consts import (
    MCP_ACCESS_TOKEN_TTL, MCP_AUTH_CODE_TTL, MCP_REFRESH_TOKEN_IDLE_TTL,
    MCP_REFRESH_TOKEN_MAX_TTL,
)

SCOPE_READ = "packstack:read"
SCOPE_WRITE = "packstack:write"
SCOPE_OFFLINE = "offline_access"
KNOWN_SCOPES = {SCOPE_READ, SCOPE_WRITE, SCOPE_OFFLINE}

# Scopes that mean something to the resource server. offline_access only
# affects whether a refresh token is issued and is never stored on tokens.
RESOURCE_SCOPES = [SCOPE_READ, SCOPE_WRITE]


class TokenError(Exception):
    """Carries an RFC 6749 error code. `description` is safe to return."""

    def __init__(self, error: str, description: str):
        super().__init__(description)
        self.error = error
        self.description = description


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pkce_matches(code_verifier: str, code_challenge: str) -> bool:
    """S256: BASE64URL(SHA256(verifier)) without padding must equal challenge."""
    if not (43 <= len(code_verifier) <= 128):
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(computed, code_challenge)


def parse_scope(scope: Optional[str]) -> list[str]:
    """Split, dedupe, validate. Empty → [read]. Unknown → TokenError."""
    parts = [s for s in (scope or "").split() if s]
    if not parts:
        return [SCOPE_READ]
    seen: list[str] = []
    for s in parts:
        if s not in KNOWN_SCOPES:
            raise TokenError("invalid_scope", f"Unknown scope: {s}")
        if s not in seen:
            seen.append(s)
    # Writing without reading makes no sense; grant read implicitly.
    if SCOPE_WRITE in seen and SCOPE_READ not in seen:
        seen.insert(0, SCOPE_READ)
    return seen


def scope_string(scopes: list[str]) -> str:
    return " ".join(scopes)


def _seconds(n: int) -> datetime.timedelta:
    return datetime.timedelta(seconds=n)


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------

def upsert_grant(user_id: int, client_id: str, scopes: list[str]) -> OAuthGrant:
    """One grant per (user, client). Re-consent widens scope, un-revokes."""
    grant = db.session.query(OAuthGrant).filter_by(user_id=user_id, client_id=client_id).first()
    if grant is None:
        grant = OAuthGrant(user_id=user_id, client_id=client_id, scope=scope_string(scopes))
        db.session.add(grant)
    else:
        merged = parse_scope(grant.scope) if grant.revoked_at is None else []
        for s in scopes:
            if s not in merged:
                merged.append(s)
        grant.scope = scope_string(merged)
        grant.revoked_at = None
    db.session.flush()
    return grant


def revoke_grant(grant: OAuthGrant) -> int:
    """Revoke a grant and every live token under it. Returns tokens revoked."""
    now = utcnow()
    grant.revoked_at = now
    count = (
        db.session.query(OAuthToken)
        .filter(OAuthToken.grant_id == grant.id, OAuthToken.revoked_at.is_(None))
        .update({"revoked_at": now}, synchronize_session=False)
    )
    db.session.flush()
    return count


# ---------------------------------------------------------------------------
# Authorization codes
# ---------------------------------------------------------------------------

def issue_code(grant: OAuthGrant, client_id: str, redirect_uri: str, scopes: list[str],
               code_challenge: str, resource: str) -> str:
    code = new_secret()
    db.session.add(OAuthAuthorizationCode(
        code_hash=hash_secret(code),
        grant_id=grant.id,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope_string(scopes),
        code_challenge=code_challenge,
        resource=resource,
        expires_at=utcnow() + _seconds(MCP_AUTH_CODE_TTL),
    ))
    db.session.flush()
    return code


@dataclass
class IssuedTokens:
    access_token: str
    refresh_token: Optional[str]
    expires_in: int
    scope: str


def _issue_pair(grant: OAuthGrant, scopes: list[str], resource: str, *,
                family_id: Optional[str], family_expires_at: Optional[datetime.datetime],
                with_refresh: bool) -> IssuedTokens:
    now = utcnow()
    stored_scopes = [s for s in scopes if s in RESOURCE_SCOPES]
    scope_str = scope_string(stored_scopes)

    access = new_secret()
    db.session.add(OAuthToken(
        token_hash=hash_secret(access),
        grant_id=grant.id,
        kind="access",
        family_id=family_id or new_secret(),
        scope=scope_str,
        resource=resource,
        issued_at=now,
        expires_at=now + _seconds(MCP_ACCESS_TOKEN_TTL),
    ))

    refresh = None
    if with_refresh:
        fam = family_id or new_secret()
        fam_exp = family_expires_at or (now + _seconds(MCP_REFRESH_TOKEN_MAX_TTL))
        refresh = new_secret()
        db.session.add(OAuthToken(
            token_hash=hash_secret(refresh),
            grant_id=grant.id,
            kind="refresh",
            family_id=fam,
            scope=scope_str,
            resource=resource,
            issued_at=now,
            expires_at=min(now + _seconds(MCP_REFRESH_TOKEN_IDLE_TTL), fam_exp),
            family_expires_at=fam_exp,
        ))

    grant.last_used_at = now
    db.session.flush()
    return IssuedTokens(access_token=access, refresh_token=refresh,
                        expires_in=MCP_ACCESS_TOKEN_TTL, scope=scope_str)


def exchange_code(code: str, client_id: str, redirect_uri: Optional[str],
                  code_verifier: Optional[str], resource: Optional[str]) -> IssuedTokens:
    """authorization_code grant. Every check that fails is `invalid_grant`
    except a malformed request, per RFC 6749 §5.2."""
    if not code_verifier:
        raise TokenError("invalid_request", "code_verifier is required.")

    row = db.session.query(OAuthAuthorizationCode).filter_by(code_hash=hash_secret(code)).first()
    if row is None:
        raise TokenError("invalid_grant", "Unknown authorization code.")

    now = utcnow()
    if row.used_at is not None:
        # Replay. Burn the whole grant's tokens: someone else has this code.
        revoke_grant(row.grant)
        db.session.commit()
        raise TokenError("invalid_grant", "Authorization code already used.")
    if row.expires_at < now:
        raise TokenError("invalid_grant", "Authorization code expired.")
    if row.client_id != client_id:
        raise TokenError("invalid_grant", "Authorization code was issued to a different client.")
    if redirect_uri is not None and redirect_uri != row.redirect_uri:
        raise TokenError("invalid_grant", "redirect_uri does not match the authorization request.")
    if resource is not None and resource.rstrip("/") != row.resource.rstrip("/"):
        raise TokenError("invalid_target", "resource does not match the authorization request.")
    if not pkce_matches(code_verifier, row.code_challenge):
        raise TokenError("invalid_grant", "PKCE verification failed.")
    if row.grant.revoked_at is not None:
        raise TokenError("invalid_grant", "Access has been revoked.")

    row.used_at = now
    scopes = parse_scope(row.scope)
    tokens = _issue_pair(row.grant, scopes, row.resource, family_id=None,
                         family_expires_at=None, with_refresh=SCOPE_OFFLINE in scopes)
    db.session.commit()
    return tokens


def refresh_tokens(refresh_token: str, client_id: str, requested_scope: Optional[str],
                   resource: Optional[str]) -> IssuedTokens:
    """refresh_token grant with rotation. Scope may only narrow."""
    row = db.session.query(OAuthToken).filter_by(token_hash=hash_secret(refresh_token)).first()
    if row is None or row.kind != "refresh":
        raise TokenError("invalid_grant", "Unknown refresh token.")

    now = utcnow()
    if row.revoked_at is not None:
        if row.replaced_by is not None:
            # A rotated-out refresh token came back: the family is compromised.
            db.session.query(OAuthToken).filter(
                OAuthToken.family_id == row.family_id, OAuthToken.revoked_at.is_(None)
            ).update({"revoked_at": now}, synchronize_session=False)
            db.session.commit()
        raise TokenError("invalid_grant", "Refresh token has been revoked.")
    if row.expires_at < now or (row.family_expires_at and row.family_expires_at < now):
        raise TokenError("invalid_grant", "Refresh token expired.")
    if row.grant.client_id != client_id:
        raise TokenError("invalid_grant", "Refresh token was issued to a different client.")
    if row.grant.revoked_at is not None:
        raise TokenError("invalid_grant", "Access has been revoked.")
    if resource is not None and resource.rstrip("/") != row.resource.rstrip("/"):
        raise TokenError("invalid_target", "resource does not match the original grant.")

    granted = parse_scope(row.scope)
    if requested_scope:
        wanted = [s for s in parse_scope(requested_scope) if s in RESOURCE_SCOPES]
        if any(s not in granted for s in wanted):
            raise TokenError("invalid_scope", "Refresh cannot widen the granted scope.")
        scopes = wanted or granted
    else:
        scopes = granted

    tokens = _issue_pair(row.grant, scopes + [SCOPE_OFFLINE], row.resource,
                         family_id=row.family_id, family_expires_at=row.family_expires_at,
                         with_refresh=True)
    row.revoked_at = now
    row.replaced_by = hash_secret(tokens.refresh_token)
    db.session.commit()
    return tokens


def revoke_token(token: str, client_id: Optional[str]) -> None:
    """RFC 7009: revoking either kind revokes its whole family. Unknown tokens
    succeed silently, as the RFC requires."""
    row = db.session.query(OAuthToken).filter_by(token_hash=hash_secret(token)).first()
    if row is None:
        return
    if client_id and row.grant.client_id != client_id:
        return
    db.session.query(OAuthToken).filter(
        OAuthToken.family_id == row.family_id, OAuthToken.revoked_at.is_(None)
    ).update({"revoked_at": utcnow()}, synchronize_session=False)
    db.session.commit()


@dataclass
class VerifiedToken:
    user_id: int
    client_id: str
    scopes: list[str]
    resource: str
    expires_at: int
    grant_id: int


def verify_access_token(token: str) -> Optional[VerifiedToken]:
    """Resource-server side: resolve a bearer token or return None."""
    row = db.session.query(OAuthToken).filter_by(token_hash=hash_secret(token)).first()
    if row is None or row.kind != "access" or row.revoked_at is not None:
        return None
    now = utcnow()
    if row.expires_at < now:
        return None
    grant = row.grant
    if grant.revoked_at is not None:
        return None
    # Cheap liveness signal for the Connected apps screen; at most once a minute.
    if grant.last_used_at is None or (now - grant.last_used_at).total_seconds() > 60:
        grant.last_used_at = now
        db.session.commit()
    return VerifiedToken(
        user_id=grant.user_id,
        client_id=grant.client_id,
        scopes=parse_scope(row.scope),
        resource=row.resource,
        expires_at=int(row.expires_at.timestamp()),
        grant_id=grant.id,
    )


def purge_expired(older_than_days: int = 7) -> dict:
    """Housekeeping for a periodic task: drop codes/tokens that expired a
    while ago. Revoked-but-unexpired rows are kept so replay detection works."""
    cutoff = utcnow() - datetime.timedelta(days=older_than_days)
    from oauth.models import OAuthAuthorizationRequest
    n_req = db.session.query(OAuthAuthorizationRequest).filter(
        OAuthAuthorizationRequest.expires_at < cutoff).delete(synchronize_session=False)
    n_code = db.session.query(OAuthAuthorizationCode).filter(
        OAuthAuthorizationCode.expires_at < cutoff).delete(synchronize_session=False)
    n_tok = db.session.query(OAuthToken).filter(
        OAuthToken.expires_at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    return {"requests": n_req, "codes": n_code, "tokens": n_tok}
