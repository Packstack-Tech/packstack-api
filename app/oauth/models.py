"""OAuth 2.1 authorization-server tables for the MCP connector.

Defined here rather than in models/base.py because only the API reads them.
They share the same declarative Base, so `Base.metadata.create_all()` in
main.py creates them on startup — this module just has to be imported first
(main.py imports it via the oauth router).

Tokens and codes are stored as SHA-256 hashes; the plaintext exists only in
the response that hands it to the client.
"""

import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text,
)
from sqlalchemy.orm import relationship

from models.base import Base


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class OAuthClient(Base):
    """An MCP client that may request tokens.

    kind:
      cimd          — client_id is an https URL to a Client ID Metadata Document
                      we fetched and cached (Claude, ChatGPT, Claude Code).
      dcr           — registered via POST /oauth/register; opaque client_id.
      preregistered — inserted by hand (e.g. Anthropic-held credentials).
    """
    __tablename__ = "oauth_client"

    client_id = Column(String(1000), primary_key=True)
    kind = Column(String(20), nullable=False)
    client_name = Column(String(200))
    client_uri = Column(String(1000))
    logo_uri = Column(String(1000))
    redirect_uris = Column(JSON, nullable=False, default=list)
    token_endpoint_auth_method = Column(String(40), nullable=False, default="none")
    client_secret_hash = Column(String(64))
    # For CIMD: when the metadata document was last fetched; refetched after 24 h
    # or on a redirect-URI mismatch.
    metadata_fetched_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class OAuthAuthorizationRequest(Base):
    """A validated /oauth/authorize request waiting for the user's consent.

    The authorize endpoint validates everything it can without a user, stores
    the request here, and redirects the browser to the web app's consent page
    with this id. Consent POSTs the id back and we mint the code from the
    stored, already-validated parameters — the browser never carries them.
    """
    __tablename__ = "oauth_authorization_request"

    id = Column(String(64), primary_key=True)
    client_id = Column(String(1000), ForeignKey("oauth_client.client_id"), nullable=False)
    redirect_uri = Column(String(2000), nullable=False)
    scope = Column(String(500), nullable=False)
    state = Column(String(2000))
    code_challenge = Column(String(128), nullable=False)
    resource = Column(String(1000), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    client = relationship("OAuthClient", lazy="joined")


class OAuthGrant(Base):
    """One row per (user, client): the thing "Connected apps" lists and revokes.

    Revoking a grant revokes every token that hangs off it. A repeat consent
    for the same client updates the row (scope may widen) instead of adding one.
    """
    __tablename__ = "oauth_grant"
    __table_args__ = (
        Index("ix_oauth_grant_user_client", "user_id", "client_id", unique=True),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    client_id = Column(String(1000), ForeignKey("oauth_client.client_id"), nullable=False)
    scope = Column(String(500), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_used_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))

    client = relationship("OAuthClient", lazy="joined")


class OAuthAuthorizationCode(Base):
    """Single-use code exchanged at /oauth/token. Bound to everything the
    authorize request carried so the exchange can verify client, redirect
    URI, PKCE and resource."""
    __tablename__ = "oauth_authorization_code"

    code_hash = Column(String(64), primary_key=True)
    grant_id = Column(Integer, ForeignKey("oauth_grant.id"), nullable=False)
    client_id = Column(String(1000), nullable=False)
    redirect_uri = Column(String(2000), nullable=False)
    scope = Column(String(500), nullable=False)
    code_challenge = Column(String(128), nullable=False)
    resource = Column(String(1000), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True))

    grant = relationship("OAuthGrant", lazy="joined")


class OAuthToken(Base):
    """Access and refresh tokens.

    Refresh tokens rotate: using one marks it revoked with `replaced_by` set
    and issues a new pair in the same family. Presenting an already-rotated
    refresh token is treated as theft and revokes the whole family.
    """
    __tablename__ = "oauth_token"
    __table_args__ = (
        Index("ix_oauth_token_grant", "grant_id"),
        Index("ix_oauth_token_family", "family_id"),
    )

    token_hash = Column(String(64), primary_key=True)
    grant_id = Column(Integer, ForeignKey("oauth_grant.id"), nullable=False)
    kind = Column(String(10), nullable=False)          # access | refresh
    family_id = Column(String(64), nullable=False)
    scope = Column(String(500), nullable=False)
    resource = Column(String(1000), nullable=False)
    issued_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    # Refresh tokens only: hard ceiling regardless of how often they rotate.
    family_expires_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    replaced_by = Column(String(64))

    grant = relationship("OAuthGrant", lazy="joined")
