"""Assembles the Packstack MCP server and mounts it into the FastAPI app.

We use the official `mcp` SDK for the protocol (JSON-RPC over Streamable
HTTP, tool schema generation, auth context) but assemble the ASGI chain
ourselves instead of calling `MCPServer.streamable_http_app()`, because:

* the SDK's app wants to own `/.well-known/oauth-protected-resource`, which
  our OAuth router already serves with richer content;
* we want our own 401 challenge (with a `scope` hint, which the SDK omits)
  and, in Phase 2, a per-tool scope check that can answer 403 at the HTTP
  layer for step-up authorization;
* mounting a sub-application at `/mcp` would make `/mcp` (no slash) redirect.

The server runs stateless with JSON responses: every tool call is one HTTP
request/response, which is what lets fastapi_sqlalchemy's per-request
`db.session` work inside tools unchanged.
"""

import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, BearerAuthBackend
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from mcp_server.context import run_sync
from mcp_server.prompts import register_prompts
from mcp_server.tools import register_read_tools
from mcp_server.writes import WRITE_TOOL_NAMES, register_write_tools
from oauth import tokens as oauth_tokens
from utils.consts import MCP_DOCS_URL, MCP_ISSUER, MCP_RESOURCE_URL

logger = logging.getLogger(__name__)

RESOURCE_METADATA_URL = f"{MCP_ISSUER}/.well-known/oauth-protected-resource/mcp"

SERVER_INSTRUCTIONS = """Packstack is a gear inventory and packing-list app for backpackers.
A user has one gear closet (their owned items), trips (each with dates, location and conditions),
and one or more packs per trip (the list of closet items actually carried, with quantity and a
worn flag). Kits are reusable bundles of closet items. Weights in results always include grams
and the user's preferred display unit — use the display unit when talking to the user and grams
when doing arithmetic. Call get_me first to learn the user's units and subscription status.
Editing requires a Packstack subscription; read access is always available."""


# ---------------------------------------------------------------------------
# Token verification (resource-server side)
# ---------------------------------------------------------------------------

class PackstackTokenVerifier(TokenVerifier):
    """Looks the bearer token up in oauth_token. Runs the query in a worker
    thread; the SDK calls this once per HTTP request."""

    async def verify_token(self, token: str) -> AccessToken | None:
        verified = await run_sync(oauth_tokens.verify_access_token, token)
        if verified is None:
            return None
        return AccessToken(
            token=token,
            client_id=verified.client_id,
            scopes=verified.scopes,
            expires_at=verified.expires_at,
            resource=verified.resource,
            subject=str(verified.user_id),
            claims={"grant_id": verified.grant_id},
        )


# ---------------------------------------------------------------------------
# HTTP-layer authorization
# ---------------------------------------------------------------------------

class RequireAuth:
    """401 with an RFC 9728 challenge when there is no valid token; 403
    insufficient_scope when the token lacks a required scope.

    `write_tools` is consulted for tools/call requests so a read-only token
    calling a write tool gets an HTTP 403 with the scope it needs — the signal
    MCP clients use to run step-up authorization (spec §5).
    """

    def __init__(self, app, required_scopes: list[str], write_tools: frozenset[str] = frozenset()):
        self.app = app
        self.required_scopes = required_scopes
        self.write_tools = write_tools

    # Body inspection needs the JSON-RPC request; batched arrays are handled too.

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        user = scope.get("user")
        if not isinstance(user, AuthenticatedUser):
            # Ask for read AND write up front. Claude.ai does not run step-up
            # authorization on a 403 insufficient_scope from a tool call (it
            # relays the error to the model instead — observed Sept 17, 2026),
            # so a read-only first consent would leave hosted-Claude users
            # unable to edit without manually reconnecting. Writes are still
            # gated by subscription at call time, so granting the scope is
            # harmless for free accounts. The 403 path below stays for clients
            # that do implement step-up.
            await self._challenge(send, 401, "invalid_token", "Authentication required.",
                                  f"{oauth_tokens.SCOPE_READ} {oauth_tokens.SCOPE_WRITE}")
            return
        have = set(user.scopes)
        missing = [s for s in self.required_scopes if s not in have]
        if missing:
            await self._challenge(send, 403, "insufficient_scope", f"Required scope: {' '.join(missing)}", " ".join(self.required_scopes))
            return

        if self.write_tools and oauth_tokens.SCOPE_WRITE not in have and scope.get("method") == "POST":
            # Peek at the JSON-RPC body for a write tool call. We must replay
            # the body to the downstream app afterwards.
            body = b""
            more = True
            while more:
                message = await receive()
                body += message.get("body", b"")
                more = message.get("more_body", False)
            if self._calls_write_tool(body):
                await self._challenge(send, 403, "insufficient_scope",
                                      "This action needs permission to make changes.",
                                      f"{oauth_tokens.SCOPE_READ} {oauth_tokens.SCOPE_WRITE}")
                return

            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay, send)
            return

        await self.app(scope, receive, send)

    def _calls_write_tool(self, body: bytes) -> bool:
        try:
            msg = json.loads(body)
        except ValueError:
            return False
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            if isinstance(m, dict) and m.get("method") == "tools/call":
                name = (m.get("params") or {}).get("name")
                if name in self.write_tools:
                    return True
        return False

    async def _challenge(self, send: Send, status: int, error: str, description: str, scope_hint: str) -> None:
        www = (f'Bearer error="{error}", error_description="{description}", '
               f'scope="{scope_hint}", resource_metadata="{RESOURCE_METADATA_URL}"')
        body = json.dumps({"error": error, "error_description": description}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"www-authenticate", www.encode()),
            (b"cache-control", b"no-store"),
        ]})
        await send({"type": "http.response.body", "body": body})


class ToolCallLogger:
    """Logs each tools/call (name, user, client, ms, status) without arguments."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        tool = None
        try:
            msg = json.loads(body)
            if isinstance(msg, dict) and msg.get("method") == "tools/call":
                tool = (msg.get("params") or {}).get("name")
        except ValueError:
            pass

        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        status_holder = {}

        async def capture(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        started = time.monotonic()
        try:
            await self.app(scope, replay, capture)
        finally:
            if tool:
                user = scope.get("user")
                token = getattr(user, "access_token", None)
                logger.info("mcp tool=%s user=%s client=%s status=%s ms=%d",
                            tool, getattr(token, "subject", None), getattr(token, "client_id", None),
                            status_holder.get("status"), int((time.monotonic() - started) * 1000))


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_mcp_server() -> MCPServer:
    mcp = MCPServer(
        name="Packstack",
        title="Packstack",
        instructions=SERVER_INSTRUCTIONS,
        website_url=MCP_DOCS_URL,
        version="1.0.0",
        token_verifier=PackstackTokenVerifier(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(MCP_ISSUER),
            resource_server_url=AnyHttpUrl(MCP_RESOURCE_URL),
            required_scopes=[oauth_tokens.SCOPE_READ],
            validate_token_resource=True,
        ),
    )
    register_read_tools(mcp)
    register_write_tools(mcp)
    register_prompts(mcp)
    return mcp


def mount_mcp(app: FastAPI) -> None:
    """Add the /mcp route to `app` and wire the session manager's lifespan.

    Call once at import time from main.py when MCP_ENABLED.
    """
    mcp = build_mcp_server()

    session_manager = StreamableHTTPSessionManager(
        app=mcp._lowlevel_server,
        json_response=True,
        stateless=True,
        # We sit behind Cloudflare and DO's load balancer; Host is whatever the
        # proxy forwards. DNS-rebinding protection is for localhost servers.
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    asgi = StreamableHTTPASGIApp(session_manager)
    asgi = ToolCallLogger(asgi)
    asgi = RequireAuth(asgi, required_scopes=[oauth_tokens.SCOPE_READ], write_tools=frozenset(WRITE_TOOL_NAMES))
    asgi = AuthContextMiddleware(asgi)
    asgi = AuthenticationMiddleware(
        asgi,
        backend=BearerAuthBackend(PackstackTokenVerifier(), resource_server_url=AnyHttpUrl(MCP_RESOURCE_URL)),
    )

    app.router.routes.append(Route("/mcp", endpoint=asgi, methods=["GET", "POST", "DELETE"]))

    # The session manager needs a running task group for the app's lifetime.
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(_app):
        async with previous_lifespan(_app):
            async with session_manager.run():
                logger.info("MCP server mounted at %s", MCP_RESOURCE_URL)
                yield

    app.router.lifespan_context = lifespan
    app.state.mcp = mcp
