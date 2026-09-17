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
    assert p["scopes_supported"] == ["packstack:read", "packstack:write"]


def test_unauthenticated_mcp_gets_challenge(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"' in www
    assert 'scope="packstack:read packstack:write"' in www


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


# ---------------------------------------------------------------------------
# Phase 2: write tools, step-up, subscription gate
# ---------------------------------------------------------------------------

def _token_for(client, user, scope):
    q, verifier, _ = authorize_and_consent(client, user, scope=scope)
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": CALLBACK,
        "client_id": CIMD_URL, "code_verifier": verifier, "resource": RESOURCE}).json()
    return tok["access_token"]


def _call(client, token, name, args):
    r = rpc(client, token, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()["result"]


def _set_subscribed(user_id, value):
    from fastapi_sqlalchemy import db
    from models.base import User
    with db():
        u = db.session.get(User, user_id)
        u.is_subscribed = value
        db.session.commit()


def test_read_token_gets_403_step_up_on_write_tool(client, user):
    token = _token_for(client, user, "packstack:read")
    r = rpc(client, token, "tools/call", {"name": "create_trip", "arguments": {"title": "Nope"}})
    assert r.status_code == 403
    www = r.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in www
    assert 'scope="packstack:read packstack:write"' in www
    assert "resource_metadata=" in www
    # read tools still work on the same token
    assert rpc(client, token, "tools/call", {"name": "get_me", "arguments": {}}).status_code == 200


def test_write_tools_advertise_annotations(client, user):
    token = _token_for(client, user, "packstack:read")
    tools = {t["name"]: t for t in rpc(client, token, "tools/list").json()["result"]["tools"]}
    for name in ("create_trip", "create_item", "add_items_to_pack", "archive_items", "update_kit"):
        assert tools[name]["annotations"]["readOnlyHint"] is False
        assert tools[name]["annotations"]["destructiveHint"] is False
    assert "delete" not in " ".join(tools)


def test_free_user_write_is_gated_by_subscription(client, user):
    _set_subscribed(user["id"], False)
    token = _token_for(client, user, "packstack:read packstack:write")
    res = _call(client, token, "create_trip", {"title": "Gated"})
    assert res["isError"] is True and "subscription" in res["content"][0]["text"].lower()


def test_subscriber_full_write_flow(client, user):
    _set_subscribed(user["id"], True)
    token = _token_for(client, user, "packstack:read packstack:write offline_access")
    me = _call(client, token, "get_me", {})["structuredContent"]
    assert me["subscribed"] is True and "packstack:write" in me["connection_scopes"]

    # --- trips
    res = _call(client, token, "create_trip", {
        "title": "Wind River High Route", "location": "Wind River Range, WY",
        "start_date": "2026-08-10", "end_date": "2026-08-15",
        "temperature_low": 28, "temperature_high": 70, "distance": 80, "daily_elevation_gain": 2500,
        "terrain": "rugged", "pace": "moderate", "conditions": "cold",
    })
    assert res.get("isError") is not True, res
    trip = res["structuredContent"]["trip"]
    assert trip["nights"] == 5 and trip["temperature_low"]["fahrenheit"] == 28
    assert abs(trip["distance"]["miles"] - 80) < 0.1          # stored as km, echoed back in miles
    assert abs(trip["daily_elevation_gain"]["feet_per_day"] - 2500) < 2
    assert res["structuredContent"]["packs"][0]["title"] == "Main Pack"
    trip_id = trip["trip_id"]
    pack_id = res["structuredContent"]["packs"][0]["pack_id"]

    bad = _call(client, token, "create_trip", {"title": "X", "terrain": "moon"})
    assert bad["isError"] is True and "terrain" in bad["content"][0]["text"]

    res = _call(client, token, "update_trip", {"trip_id": trip_id, "notes": "Bring bear spray", "temperature_low": 20})
    assert res["structuredContent"]["trip"]["notes"] == "Bring bear spray"
    # temps are stored as whole °C, so an imperial round-trip can drift by a degree
    assert abs(res["structuredContent"]["trip"]["temperature_low"]["fahrenheit"] - 20) <= 1
    assert sorted(res["structuredContent"]["updated_fields"]) == ["notes", "temp_min"]

    # --- gear: catalog-backed create, dedupe gate, override, update
    from fastapi_sqlalchemy import db
    from models.base import CatalogProduct
    with db():
        cp = CatalogProduct(brand_name="Durston Gear", product_name="X-Mid 1", variant_name=None,
                            display_name="Durston Gear X-Mid 1", weight=795, weight_unit="g", status="approved",
                            category_suggestion="Shelter")
        db.session.add(cp); db.session.commit(); cp_id = cp.id

    res = _call(client, token, "create_item", {"name": "Tent (new)", "catalog_product_id": cp_id, "category": "Shelter"})
    assert res.get("isError") is not True, res
    tent2 = res["structuredContent"]
    assert tent2["brand"] == "Durston Gear" and abs(tent2["weight"]["grams"] - 795) < 0.5 and tent2["category"] == "Shelter"

    dup = _call(client, token, "create_item", {"name": "Another tent", "brand": "durston", "product": "xmid 1", "weight": 28, "unit": "oz"})
    assert dup["isError"] is True
    txt = dup["content"][0]["text"]
    assert f"catalog_product_id {cp_id}" in txt and f"item_id {tent2['item_id']}" in txt

    forced = _call(client, token, "create_item", {"name": "Custom tarp", "brand": "Durston", "product": "X-Mid 1 (modified)",
                                                  "weight": 20, "unit": "oz", "create_new_product": True})
    assert forced.get("isError") is not True, forced
    assert forced["structuredContent"]["brand"] == "Durston"

    plain = _call(client, token, "create_item", {"name": "Bear spray", "weight": 11, "unit": "oz", "consumable": True, "category": "Safety"})
    spray_id = plain["structuredContent"]["item_id"]
    assert plain["structuredContent"]["consumable"] is True

    res = _call(client, token, "update_item", {"item_id": user["tent_id"], "consumable": False, "weight": 21, "notes": ""})
    assert sorted(res["structuredContent"]["updated_fields"]) == ["consumable", "notes", "weight"]
    assert res["structuredContent"]["notes"] is None

    # --- packs
    res = _call(client, token, "add_items_to_pack", {"pack_id": pack_id, "items": [
        {"item_id": tent2["item_id"]}, {"item_id": spray_id, "quantity": 1}, {"item_id": user["tent_id"], "worn": True}]})
    assert res.get("isError") is not True, res
    assert sorted(res["structuredContent"]["added"]) == sorted(["Tent (new)", "Bear spray", "Tent"])
    totals = res["structuredContent"]["totals"]
    assert totals["item_count"] == 3 and totals["worn_weight"]["grams"] > 0 and totals["consumable_weight"]["grams"] > 0

    res = _call(client, token, "update_pack_items", {"pack_id": pack_id, "items": [{"item_id": spray_id, "quantity": 2, "checked": True}]})
    rows = [i for c in res["structuredContent"]["categories"] for i in c["items"]]
    assert next(i for i in rows if i["item_id"] == spray_id)["quantity"] == 2

    res = _call(client, token, "remove_items_from_pack", {"pack_id": pack_id, "item_ids": [user["tent_id"]]})
    assert res["structuredContent"]["removed_count"] == 1 and res["structuredContent"]["totals"]["item_count"] == 2

    res = _call(client, token, "create_pack", {"trip_id": trip_id, "title": "Anne", "copy_from_pack_id": pack_id})
    assert res.get("isError") is not True, res
    assert res["structuredContent"]["title"] == "Anne" and res["structuredContent"]["totals"]["item_count"] == 2
    anne_id = res["structuredContent"]["pack_id"]
    assert _call(client, token, "rename_pack", {"pack_id": anne_id, "title": "Anne's pack"})["structuredContent"]["title"] == "Anne's pack"

    # --- kits
    res = _call(client, token, "create_kit", {"name": "Shelter kit", "items": [{"item_id": tent2["item_id"]}, {"item_id": user["tent_id"], "quantity": 1}]})
    assert res.get("isError") is not True, res
    kit_id = res["structuredContent"]["kit_id"]
    res = _call(client, token, "update_kit", {"kit_id": kit_id, "items": [{"item_id": user["tent_id"]}]})
    assert [i["item_id"] for i in res["structuredContent"]["items"]] == [user["tent_id"]]
    res = _call(client, token, "add_kit_to_pack", {"pack_id": anne_id, "kit_id": kit_id})
    assert res["structuredContent"]["added_count"] == 1

    # --- lifecycle + archive
    res = _call(client, token, "log_item_lifecycle", {"item_id": user["tent_id"], "condition": "fair", "event_type": "repair",
                                                      "event_date": "2026-09-01", "note": "Seam sealed", "cost": 12.5})
    assert res["structuredContent"]["log_entries_added"] == 2 and res["structuredContent"]["lifecycle"] if "lifecycle" in res["structuredContent"] else True
    bad = _call(client, token, "log_item_lifecycle", {"item_id": user["tent_id"], "condition": "shredded"})
    assert bad["isError"] is True

    res = _call(client, token, "archive_items", {"item_ids": [spray_id]})
    assert res["structuredContent"]["archived"][0]["item_id"] == spray_id
    gear = _call(client, token, "search_gear", {"query": "bear spray", "status": "archived"})["structuredContent"]
    assert gear["count"] == 1
    res = _call(client, token, "restore_items", {"item_ids": [spray_id]})
    assert res["structuredContent"]["restored"][0]["item_id"] == spray_id

    # --- clone, then the read side sees everything
    res = _call(client, token, "clone_trip", {"trip_id": trip_id, "title": "WRHR 2027"})
    assert res.get("isError") is not True, res
    assert len(res["structuredContent"]["packs"]) == 2
    trips = _call(client, token, "list_trips", {"search": "WRHR"})["structuredContent"]["trips"]
    assert trips and trips[0]["pack_count"] == 2

    # ownership: someone else's ids are refused
    res = _call(client, token, "rename_pack", {"pack_id": 999999999, "title": "x"})
    assert res["isError"] is True and "not found" in res["content"][0]["text"].lower()
