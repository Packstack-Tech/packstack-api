-- OAuth 2.1 authorization-server tables for the MCP connector.
-- Created automatically by Base.metadata.create_all() at API startup (the
-- models live in app/oauth/models.py on the shared Base); this file is the
-- reference DDL, matching the repo's convention of keeping schema changes as
-- SQL alongside the code.

CREATE TABLE IF NOT EXISTS oauth_client (
    client_id VARCHAR(1000) PRIMARY KEY,
    kind VARCHAR(20) NOT NULL,
    client_name VARCHAR(200),
    client_uri VARCHAR(1000),
    logo_uri VARCHAR(1000),
    redirect_uris JSON NOT NULL,
    token_endpoint_auth_method VARCHAR(40) NOT NULL DEFAULT 'none',
    client_secret_hash VARCHAR(64),
    metadata_fetched_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_authorization_request (
    id VARCHAR(64) PRIMARY KEY,
    client_id VARCHAR(1000) NOT NULL REFERENCES oauth_client(client_id),
    redirect_uri VARCHAR(2000) NOT NULL,
    scope VARCHAR(500) NOT NULL,
    state VARCHAR(2000),
    code_challenge VARCHAR(128) NOT NULL,
    resource VARCHAR(1000) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_grant (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES "user"(id),
    client_id VARCHAR(1000) NOT NULL REFERENCES oauth_client(client_id),
    scope VARCHAR(500) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_oauth_grant_user_id ON oauth_grant(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_oauth_grant_user_client ON oauth_grant(user_id, client_id);

CREATE TABLE IF NOT EXISTS oauth_authorization_code (
    code_hash VARCHAR(64) PRIMARY KEY,
    grant_id INTEGER NOT NULL REFERENCES oauth_grant(id),
    client_id VARCHAR(1000) NOT NULL,
    redirect_uri VARCHAR(2000) NOT NULL,
    scope VARCHAR(500) NOT NULL,
    code_challenge VARCHAR(128) NOT NULL,
    resource VARCHAR(1000) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS oauth_token (
    token_hash VARCHAR(64) PRIMARY KEY,
    grant_id INTEGER NOT NULL REFERENCES oauth_grant(id),
    kind VARCHAR(10) NOT NULL,
    family_id VARCHAR(64) NOT NULL,
    scope VARCHAR(500) NOT NULL,
    resource VARCHAR(1000) NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    family_expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    replaced_by VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS ix_oauth_token_grant ON oauth_token(grant_id);
CREATE INDEX IF NOT EXISTS ix_oauth_token_family ON oauth_token(family_id);
