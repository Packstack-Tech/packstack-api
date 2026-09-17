"""End-to-end test of the MCP connector: OAuth 2.1 (CIMD client, PKCE,
resource indicator, consent, code exchange, refresh rotation, revocation)
and the MCP endpoint (initialize, tools/list, tools/call, prompts).

Runs against a real Postgres (see testenv.sh). Network is stubbed: the CIMD
fetch is monkeypatched so no outbound request happens.

    source testenv.sh && MCP_ENABLED=1 MCP_ISSUER=http://testserver \
      MCP_CONSENT_URL=http://localhost:5173/connect/authorize \
      python -m pytest tests/test_mcp_flow.py -q
"""

import base64
import datetime
import hashlib
import secrets
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import main
from oauth import clients as oauth_clients
from oauth import tokens as oauth_tokens
from utils.auth import generate_jwt

CIMD_URL = "https://claude.ai/oauth/test-client-metadata"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "http://testserver/mcp"

CIMD_DOC = {
    "client_id": CIMD_URL,
    "client_name": "Claude (test)",
    "client_uri": "https://claude.ai",
    "logo_uri": "https://claude.ai/logo.png",
    "redirect_uris": [CALLBACK, "http://localhost/callback", "http://127.0.0.1/callback"],
    "token_endpoint_auth_method": "none",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
}


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def stub_cimd(monkeypatch_module):
    monkeypatch_module.setattr(oauth_clients, "fetch_cimd", lambda client_id: dict(CIMD_DOC, client_id=client_id))


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def user(client):
    """A user with a trip, a pack and a few items, created straight in the DB."""
    from fastapi_sqlalchemy import db
    from models.base import Brand, Category, Item, ItemCategory, Pack, PackItem, Trip, User
    with db():
        suffix = secrets.token_hex(3)
        u = User(email=f"mcp-{suffix}@example.com", username=f"mcp{suffix}", unit_weight="IMPERIAL",
                 unit_distance="MI", unit_temperature="F", is_subscribed=False, email_verified=True)
        db.session.add(u); db.session.flush()
        shared = db.session.query(Category).filter_by(user_id=None, name="Shelter").first()
        if shared is None:
            shared = Category(name="Shelter", user_id=None); db.session.add(shared); db.session.flush()
        ic = ItemCategory(user_id=u.id, category_id=shared.id, sort_order=0); db.session.add(ic); db.session.flush()
        brand = db.session.query(Brand).filter_by(name="Durston").first() or Brand(name="Durston")
        db.session.add(brand); db.session.flush()
        tent = Item(user_id=u.id, name="Tent", weight=20.5, unit="oz", brand_id=brand.id, category_id=ic.id, notes="X-Mid 1")
        stove = Item(user_id=u.id, name="Stove", weight=85, unit="g")
        fuel = Item(user_id=u.id, name="Fuel", weight=7.4, unit="oz", consumable=True)
        db.session.add_all([tent, stove, fuel]); db.session.flush()
        trip = Trip(user_id=u.id, title="Test Loop", location="Tracy Ridge, PA",
                    start_date=datetime.date(2026, 10, 3), end_date=datetime.date(2026, 10, 5),
                    temp_min=0, temp_max=14, distance=54.2, daily_elevation_gain=274, terrain="rugged", pace="moderate",
                    uuid=uuid.uuid4())
        db.session.add(trip); db.session.flush()
        pack = Pack(user_id=u.id, trip_id=trip.id, title="Main"); db.session.add(pack); db.session.flush()
        db.session.add_all([
            PackItem(pack_id=pack.id, item_id=tent.id, quantity=1),
            PackItem(pack_id=pack.id, item_id=stove.id, quantity=1),
            PackItem(pack_id=pack.id, item_id=fuel.id, quantity=2),
        ])
        db.session.commit()
        return {"id": u.id, "trip_id": trip.id, "tent_id": tent.id, "cookie": generate_jwt(u)}


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def rpc(client, token, method, params=None, id_=1):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                             "Content-Type": "application/json"})
    return r


def authorize_and_consent(client, user, scope="packstack:read offline_access", approve=True):
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(8)
    r = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": CIMD_URL, "redirect_uri": CALLBACK, "scope": scope,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE,
    }, follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.startswith("http://localhost:5173/connect/authorize?request=")
    request_id = parse_qs(urlsplit(loc).query)["request"][0]

    cookies = {"access_token": user["cookie"]}
    d = client.get(f"/oauth/consent/{request_id}", cookies=cookies)
    assert d.status_code == 200, d.text
    details = d.json()
    assert details["client"]["name"] == "Claude (test)"
    assert details["redirect_host"] == "claude.ai"

    c = client.post("/oauth/consent", json={"request_id": request_id, "approve": approve}, cookies=cookies)
    assert c.status_code == 200, c.text
    redirect_to = c.json()["redirect_to"]
    q = parse_qs(urlsplit(redirect_to).query)
    assert q["state"] == [state]
    assert q["iss"] == ["http://testserver"]
    return q, verifier, details


def test_discovery(client):
    a = client.get("/.well-known/oauth-authorization-server").json()
    assert a["client_id_metadata_document_supported"] is True
    assert "none" in a["token_endpoint_auth_methods_supported"]
    assert a["code_challenge_methods_supported"] == ["S256"]
    p = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert p["resource"] == RESOURCE and p["authorization_servers"] == ["http://testserver"]


def test_unauthenticated_mcp_gets_challenge(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"' in www
    assert 'scope="packstack:read"' in www


def test_authorize_rejects_bad_redirect_without_redirecting(client):
    r = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": CIMD_URL, "redirect_uri": "https://evil.example/cb",
        "code_challenge": "x" * 43, "code_challenge_method": "S256", "resource": RESOURCE,
    }, follow_redirects=False)
    assert r.status_code == 400 and "redirect_uri" in r.text


def test_authorize_redirects_errors_for_missing_pkce_and_wrong_resource(client):
    r = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": CIMD_URL, "redirect_uri": CALLBACK, "state": "s1",
        "code_challenge": "x" * 43, "code_challenge_method": "plain", "resource": RESOURCE,
    }, follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["error"] == ["invalid_request"] and q["state"] == ["s1"] and q["iss"] == ["http://testserver"]

    r = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": CIMD_URL, "redirect_uri": CALLBACK,
        "code_challenge": "x" * 43, "code_challenge_method": "S256", "resource": "https://other.example/mcp",
    }, follow_redirects=False)
    assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["invalid_target"]


def test_loopback_redirect_ignores_port(client, user):
    verifier, challenge = pkce()
    r = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": CIMD_URL, "redirect_uri": "http://localhost:3118/callback",
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE,
    }, follow_redirects=False)
    assert r.status_code == 302 and "connect/authorize" in r.headers["location"]


def test_deny_returns_access_denied(client, user):
    q, _, _ = authorize_and_consent(client, user, approve=False)
    assert q["error"] == ["access_denied"]


def test_full_flow(client, user):
    q, verifier, details = authorize_and_consent(client, user)
    code = q["code"][0]

    # Wrong verifier fails and does not burn the code
    bad = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
        "client_id": CIMD_URL, "code_verifier": "b" * 50, "resource": RESOURCE})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"

    t = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
        "client_id": CIMD_URL, "code_verifier": verifier, "resource": RESOURCE})
    assert t.status_code == 200, t.text
    assert t.headers["cache-control"] == "no-store"
    tok = t.json()
    assert tok["token_type"] == "Bearer" and tok["scope"] == "packstack:read" and "refresh_token" in tok
    access, refresh = tok["access_token"], tok["refresh_token"]

    # Replaying the code is invalid_grant and revokes the grant's tokens
    again = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
        "client_id": CIMD_URL, "code_verifier": verifier, "resource": RESOURCE})
    assert again.json()["error"] == "invalid_grant"
    assert rpc(client, access, "initialize", {"protocolVersion": "2026-07-28", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}).status_code == 401

    # Fresh grant for the rest of the test
    q, verifier, _ = authorize_and_consent(client, user)
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": CALLBACK,
        "client_id": CIMD_URL, "code_verifier": verifier, "resource": RESOURCE}).json()
    access, refresh = tok["access_token"], tok["refresh_token"]

    # --- MCP
    r = rpc(client, access, "initialize", {"protocolVersion": "2026-07-28", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "1"}})
    assert r.status_code == 200, r.text
    init = r.json()["result"]
    assert init["serverInfo"]["name"] == "Packstack"
    assert "tools" in init["capabilities"] and "prompts" in init["capabilities"]

    r = rpc(client, access, "tools/list")
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert {"get_me", "list_trips", "get_trip", "search_gear", "get_item", "list_categories",
            "list_kits", "get_kit", "search_catalog", "list_hiker_profiles"} <= set(tools)
    assert tools["get_trip"]["annotations"]["readOnlyHint"] is True
    assert "trip_id" in tools["get_trip"]["inputSchema"]["properties"]

    r = rpc(client, access, "tools/call", {"name": "get_me", "arguments": {}})
    res = r.json()["result"]
    assert res.get("isError") is not True, res
    me = res["structuredContent"]
    assert me["units"]["item_weight_unit"] == "oz" and me["subscribed"] is False

    r = rpc(client, access, "tools/call", {"name": "list_trips", "arguments": {"when": "upcoming"}})
    trips = r.json()["result"]["structuredContent"]["trips"]
    assert any(t["trip_id"] == user["trip_id"] for t in trips)
    mine = next(t for t in trips if t["trip_id"] == user["trip_id"])
    # tent 20.5 oz + stove 85 g = 581.2 + 85 = 666.2 g base; fuel consumable 2×7.4 oz
    assert abs(mine["base_weight"]["grams"] - (20.5 * 28.3495 + 85)) < 0.5
    assert mine["pack_count"] == 1

    r = rpc(client, access, "tools/call", {"name": "get_trip", "arguments": {"trip_id": user["trip_id"]}})
    trip = r.json()["result"]["structuredContent"]
    assert trip["trip"]["temperature_low"]["fahrenheit"] == 32
    assert trip["packs"][0]["categories"][0]["category"] in ("Shelter", "Uncategorized")
    assert trip["totals_all_packs"]["consumable_weight"]["grams"] > 400

    r = rpc(client, access, "tools/call", {"name": "get_trip", "arguments": {"trip_id": user["trip_id"], "format": "markdown"}})
    assert "## Weight summary" in r.json()["result"]["structuredContent"]["markdown"]

    r = rpc(client, access, "tools/call", {"name": "get_trip", "arguments": {"trip_id": 999999999}})
    assert r.json()["result"]["isError"] is True

    r = rpc(client, access, "tools/call", {"name": "search_gear", "arguments": {"query": "durston", "sort": "weight_desc"}})
    gear = r.json()["result"]["structuredContent"]
    assert gear["count"] == 1 and gear["items"][0]["item_id"] == user["tent_id"]

    r = rpc(client, access, "tools/call", {"name": "get_item", "arguments": {"item_id": user["tent_id"]}})
    item = r.json()["result"]["structuredContent"]
    assert item["in_packs"][0]["trip_id"] == user["trip_id"]

    r = rpc(client, access, "prompts/list")
    assert {p["name"] for p in r.json()["result"]["prompts"]} == {"shakedown", "plan_pack", "lighten"}
    r = rpc(client, access, "prompts/get", {"name": "shakedown", "arguments": {"trip": "Test Loop"}})
    assert "get_trip" in r.json()["result"]["messages"][0]["content"]["text"]

    # --- Refresh rotates; old refresh token dies; replay of the old one kills the family
    rt = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                           "client_id": CIMD_URL, "resource": RESOURCE})
    assert rt.status_code == 200, rt.text
    new = rt.json()
    assert new["refresh_token"] != refresh and new["access_token"] != access
    assert rpc(client, new["access_token"], "tools/call", {"name": "get_me", "arguments": {}}).status_code == 200

    replay = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                               "client_id": CIMD_URL, "resource": RESOURCE})
    assert replay.json()["error"] == "invalid_grant"
    # family revoked → the new access token is dead too
    assert rpc(client, new["access_token"], "tools/call", {"name": "get_me", "arguments": {}}).status_code == 401

    # --- Connected apps: list and revoke
    cookies = {"access_token": user["cookie"]}
    q, verifier, _ = authorize_and_consent(client, user)
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": CALLBACK,
        "client_id": CIMD_URL, "code_verifier": verifier, "resource": RESOURCE}).json()
    assert rpc(client, tok["access_token"], "tools/call", {"name": "get_me", "arguments": {}}).status_code == 200

    grants = client.get("/oauth/grants", cookies=cookies).json()
    assert len(grants) == 1 and grants[0]["client"]["name"] == "Claude (test)"
    assert client.delete(f"/oauth/grants/{grants[0]['id']}", cookies=cookies).status_code == 204
    assert client.get("/oauth/grants", cookies=cookies).json() == []
    assert rpc(client, tok["access_token"], "tools/call", {"name": "get_me", "arguments": {}}).status_code == 401
    dead = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                             "client_id": CIMD_URL, "resource": RESOURCE})
    assert dead.json()["error"] == "invalid_grant"


def test_dcr_registration_and_flow(client, user):
    r = client.post("/oauth/register", json={"client_name": "Inspector", "redirect_uris": ["http://localhost:6274/oauth/callback"]})
    assert r.status_code == 201, r.text
    cid = r.json()["client_id"]
    assert cid.startswith("dcr_") and r.json()["token_endpoint_auth_method"] == "none"
    bad = client.post("/oauth/register", json={"redirect_uris": ["http://example.com/cb"]})
    assert bad.status_code == 400

    verifier, challenge = pkce()
    a = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": cid, "redirect_uri": "http://localhost:6274/oauth/callback",
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE,
    }, follow_redirects=False)
    assert a.status_code == 302
    request_id = parse_qs(urlsplit(a.headers["location"]).query)["request"][0]
    d = client.get(f"/oauth/consent/{request_id}", cookies={"access_token": user["cookie"]}).json()
    assert d["loopback_only"] is True and d["client"]["name"] == "Inspector"


def test_token_endpoint_error_codes(client):
    r = client.post("/oauth/token", data={"grant_type": "password", "client_id": CIMD_URL})
    assert r.json()["error"] == "unsupported_grant_type"
    r = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "nope", "client_id": CIMD_URL})
    assert r.json()["error"] == "invalid_grant"
    r = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": "x", "client_id": "unknown-client", "code_verifier": "v" * 50})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
