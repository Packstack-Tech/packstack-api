"""Pack versions: PUT /pack/{id} refuses a save made against a stale copy.

PUT rebuilds every row from the payload, so before versions a client holding
an old copy (a tab left open, a phone showing its cached pack) silently
deleted whatever had been added since. These pin the contract the web and
mobile clients rely on.

    source testenv.sh && python -m pytest tests/test_pack_version.py -q
"""

import secrets

import pytest
from fastapi.testclient import TestClient
from fastapi_sqlalchemy import db

import main
from models.base import PackSnapshot, User
from utils.auth import generate_jwt


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def user(client):
    with db():
        suffix = secrets.token_hex(4)
        u = User(email=f"ver-{suffix}@example.com", username=f"ver{suffix}", unit_weight="METRIC",
                 email_verified=True, is_subscribed=True)
        db.session.add(u)
        db.session.commit()
        return {"id": u.id, "headers": {"Authorization": f"Bearer {generate_jwt(u)}"}}


def add_item(client, user, name):
    r = client.post("/item", json={"name": name, "weight": 100, "unit": "g"}, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_pack(client, user, item_ids):
    r = client.post("/trip", json={"title": "Trip"}, headers=user["headers"])
    assert r.status_code == 201, r.text
    r = client.post("/pack", json={"title": "Pack", "trip_id": r.json()["id"],
                                   "items": [{"item_id": i, "quantity": 1} for i in item_ids]},
                    headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


def put(client, user, pack, item_ids, version="from-pack", **extra):
    body = {"title": pack["title"], "trip_id": pack["trip_id"],
            "items": [{"item_id": i, "quantity": 1} for i in item_ids], **extra}
    if version == "from-pack":
        body["version"] = pack["version"]
    elif version is not None:
        body["version"] = version
    return client.put(f"/pack/{pack['id']}", json=body, headers=user["headers"])


def ids(pack):
    return sorted(pi["item_id"] for pi in pack["items"])


def test_save_against_current_version_succeeds_and_bumps(client, user):
    a, b = add_item(client, user, "A"), add_item(client, user, "B")
    pack = make_pack(client, user, [a])
    r = put(client, user, pack, [a, b])
    assert r.status_code == 200, r.text
    assert ids(r.json()) == sorted([a, b])
    assert r.json()["version"] > pack["version"]


def test_stale_save_is_refused_with_current_pack(client, user):
    a, b, c = (add_item(client, user, n) for n in "ABC")
    stale = make_pack(client, user, [a])

    # Another device adds B.
    fresh = put(client, user, stale, [a, b])
    assert fresh.status_code == 200, fresh.text

    # The stale copy ticks something / adds C: must not drop B.
    r = put(client, user, stale, [a, c])
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "pack_conflict"
    assert ids(body["pack"]) == sorted([a, b])
    assert body["pack"]["version"] == fresh.json()["version"]

    current = client.get(f"/pack/{stale['id']}", headers=user["headers"]).json()
    assert ids(current) == sorted([a, b])


def test_retry_with_returned_version_succeeds(client, user):
    a, b = add_item(client, user, "A"), add_item(client, user, "B")
    stale = make_pack(client, user, [a])
    put(client, user, stale, [a, b])
    conflict = put(client, user, stale, [a]).json()["pack"]
    r = put(client, user, conflict, [a, b], checked=True)
    assert r.status_code == 200, r.text


def test_rename_bumps_version(client, user):
    a = add_item(client, user, "A")
    pack = make_pack(client, user, [a])
    renamed = client.put(f"/pack/{pack['id']}", json={"title": "Renamed", "items": [{"item_id": a}],
                                                      "version": pack["version"]},
                         headers=user["headers"]).json()
    assert renamed["version"] > pack["version"]
    # The old copy would rename it back.
    assert put(client, user, pack, [a]).status_code == 409


def test_unversioned_client_still_saves(client, user):
    a, b = add_item(client, user, "A"), add_item(client, user, "B")
    pack = make_pack(client, user, [a])
    put(client, user, pack, [a, b])
    r = put(client, user, pack, [a], version=None)
    assert r.status_code == 200, r.text
    assert ids(r.json()) == [a]


def test_removal_is_snapshotted(client, user):
    a, b = add_item(client, user, "A"), add_item(client, user, "B")
    pack = make_pack(client, user, [a, b])
    assert put(client, user, pack, [a], version=None).status_code == 200
    with db():
        snaps = db.session.query(PackSnapshot).filter_by(pack_id=pack["id"]).all()
        assert len(snaps) == 1
        assert snaps[0].removed_item_ids == [b]
        assert sorted(i["item_id"] for i in snaps[0].items) == sorted([a, b])
        assert snaps[0].versioned is False


def test_save_without_removal_writes_no_snapshot(client, user):
    a = add_item(client, user, "A")
    pack = make_pack(client, user, [a])
    assert put(client, user, pack, [a], checked=True).status_code == 200
    with db():
        assert db.session.query(PackSnapshot).filter_by(pack_id=pack["id"]).count() == 0


def test_item_toggle_endpoint_bumps_version(client, user):
    # Writes that bypass PUT (the checklist endpoint, MCP tools) must also
    # invalidate older copies -- the triggers cover them.
    a = add_item(client, user, "A")
    pack = make_pack(client, user, [a])
    r = client.put(f"/pack/{pack['id']}/item/{a}", json={"checked": True}, headers=user["headers"])
    assert r.status_code == 200, r.text
    assert put(client, user, pack, [a]).status_code == 409


def test_item_toggle_endpoint_requires_ownership(client, user):
    a = add_item(client, user, "A")
    pack = make_pack(client, user, [a])
    with db():
        suffix = secrets.token_hex(4)
        other = User(email=f"ver-{suffix}@example.com", username=f"ver{suffix}", unit_weight="METRIC",
                     email_verified=True, is_subscribed=True)
        db.session.add(other)
        db.session.commit()
        headers = {"Authorization": f"Bearer {generate_jwt(other)}"}
    r = client.put(f"/pack/{pack['id']}/item/{a}", json={"checked": True}, headers=headers)
    assert r.status_code == 404, r.text


def test_duplicate_item_in_payload_keeps_first_copy(client, user):
    a, b = add_item(client, user, "A"), add_item(client, user, "B")
    pack = make_pack(client, user, [a])
    body = {"title": pack["title"], "trip_id": pack["trip_id"], "version": pack["version"],
            "items": [{"item_id": a, "quantity": 2, "checked": True},
                      {"item_id": b, "quantity": 1},
                      {"item_id": a, "quantity": 1}]}
    r = client.put(f"/pack/{pack['id']}", json=body, headers=user["headers"])
    assert r.status_code == 200, r.text
    assert ids(r.json()) == sorted([a, b])
    row = next(pi for pi in r.json()["items"] if pi["item_id"] == a)
    assert row["quantity"] == 2 and row["checked"] is True


def test_duplicate_item_on_create_is_collapsed(client, user):
    a = add_item(client, user, "A")
    pack = make_pack(client, user, [a, a])
    assert ids(pack) == [a]
