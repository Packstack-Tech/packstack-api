"""Owned quantity on closet items + the over-pack check.

The contract that matters most: a client that predates `quantity` (the mobile
build in the wild) omits it on every PUT /item, and that must NOT reset the
stored value. Runs against a real Postgres like the other API tests:

    source testenv.sh && python -m pytest tests/test_gear_quantity.py -q
"""

import secrets

import pytest
from fastapi.testclient import TestClient
from fastapi_sqlalchemy import db

import main
from models.base import User
from utils.auth import generate_jwt
from utils.overpack import overpacked_items


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def user(client):
    with db():
        suffix = secrets.token_hex(4)
        u = User(email=f"qty-{suffix}@example.com", username=f"qty{suffix}", unit_weight="IMPERIAL",
                 email_verified=True, is_subscribed=True)
        db.session.add(u)
        db.session.commit()
        return {"id": u.id, "headers": {"Authorization": f"Bearer {generate_jwt(u)}"}}


def add_item(client, user, **fields):
    body = {"name": "Thing", "weight": 1, "unit": "oz", **fields}
    r = client.post("/item", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


def legacy_edit_payload(item):
    """What the pre-quantity mobile build sends: the whole item echoed back,
    minus anything it doesn't know about."""
    return {
        "id": item["id"], "name": item["name"], "weight": item["weight"], "unit": item["unit"],
        "price": item["price"] or 0, "calories": item["calories"] or 0,
        "consumable": item["consumable"], "product_url": item["product_url"] or "",
        "notes": item["notes"] or "",
    }


# --- item create / update ---------------------------------------------------

def test_create_defaults_to_one(client, user):
    assert add_item(client, user)["quantity"] == 1


def test_create_with_explicit_null_defaults_to_one(client, user):
    assert add_item(client, user, quantity=None)["quantity"] == 1


def test_create_with_quantity(client, user):
    assert add_item(client, user, quantity=3)["quantity"] == 3


@pytest.mark.parametrize("bad", [0, -1, 2.5, "two", True])
def test_rejects_non_positive_or_fractional(client, user, bad):
    r = client.post("/item", json={"name": "T", "weight": 1, "unit": "oz", "quantity": bad},
                    headers=user["headers"])
    assert r.status_code == 422, r.text


def test_legacy_client_update_does_not_clobber_quantity(client, user):
    """THE compatibility test. Old mobile edits an item without sending
    quantity; the stored count must survive."""
    item = add_item(client, user, quantity=3)
    payload = legacy_edit_payload(item)
    payload["name"] = "Renamed by old build"
    r = client.put("/item", json=payload, headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Renamed by old build"
    assert r.json()["quantity"] == 3


def test_update_with_explicit_null_leaves_quantity(client, user):
    item = add_item(client, user, quantity=2)
    payload = legacy_edit_payload(item)
    payload["quantity"] = None
    r = client.put("/item", json=payload, headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["quantity"] == 2


def test_update_changes_quantity(client, user):
    item = add_item(client, user)
    payload = legacy_edit_payload(item)
    payload["quantity"] = 4
    r = client.put("/item", json=payload, headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["quantity"] == 4


# --- user settings ------------------------------------------------------------

def test_user_settings_default_and_update(client, user):
    me = client.get("/user", headers=user["headers"]).json()
    assert me["overpack_mode"] == "warn"
    assert me["overpack_include_consumables"] is True

    r = client.put("/user", json={"overpack_mode": "block", "overpack_include_consumables": False},
                   headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["overpack_mode"] == "block"
    assert r.json()["overpack_include_consumables"] is False

    # An old client updating something else leaves both alone.
    r = client.put("/user", json={"bio": "hi"}, headers=user["headers"])
    assert r.json()["overpack_mode"] == "block"
    assert r.json()["overpack_include_consumables"] is False

    r = client.put("/user", json={"overpack_mode": "loud"}, headers=user["headers"])
    assert r.status_code == 422


# --- over-pack detection ------------------------------------------------------

def make_trip_with_packs(client, user, item_id, quantities, consumable_item_id=None):
    r = client.post("/trip", json={"title": "Trip"}, headers=user["headers"])
    assert r.status_code == 201, r.text
    trip_id = r.json()["id"]
    for i, q in enumerate(quantities):
        items = [{"item_id": item_id, "quantity": q}]
        if consumable_item_id:
            items.append({"item_id": consumable_item_id, "quantity": q})
        r = client.post("/pack", json={"title": f"Pack {i}", "trip_id": trip_id, "items": items},
                        headers=user["headers"])
        assert r.status_code == 201, r.text
    return trip_id


def test_overpacked_sums_across_packs(client, user):
    tent = add_item(client, user, name="Tent", quantity=1)
    trip_id = make_trip_with_packs(client, user, tent["id"], [1, 1])
    r = client.get(f"/pack/trip/{trip_id}/overpacked", headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "warn"
    assert [(e["item_id"], e["owned"], e["packed"]) for e in body["items"]] == [(tent["id"], 1, 2.0)]


def test_not_overpacked_when_owned_enough(client, user):
    tent = add_item(client, user, name="Tent", quantity=2)
    trip_id = make_trip_with_packs(client, user, tent["id"], [1, 1])
    r = client.get(f"/pack/trip/{trip_id}/overpacked", headers=user["headers"])
    assert r.json()["items"] == []


def test_consumables_setting(client, user):
    tent = add_item(client, user, name="Tent", quantity=5)
    fuel = add_item(client, user, name="Fuel", quantity=1, consumable=True)
    trip_id = make_trip_with_packs(client, user, tent["id"], [1, 1], consumable_item_id=fuel["id"])

    r = client.get(f"/pack/trip/{trip_id}/overpacked", headers=user["headers"])
    assert [e["item_id"] for e in r.json()["items"]] == [fuel["id"]]

    client.put("/user", json={"overpack_include_consumables": False}, headers=user["headers"])
    r = client.get(f"/pack/trip/{trip_id}/overpacked", headers=user["headers"])
    assert r.json()["items"] == []
    assert r.json()["include_consumables"] is False


def test_pack_write_never_rejected_for_overpacking(client, user):
    """Block mode is client-side only; the API must keep accepting."""
    client.put("/user", json={"overpack_mode": "block"}, headers=user["headers"])
    tent = add_item(client, user, name="Tent", quantity=1)
    trip_id = make_trip_with_packs(client, user, tent["id"], [3])
    r = client.get(f"/pack/trip/{trip_id}", headers=user["headers"])
    assert r.status_code == 200
    assert float(r.json()[0]["items"][0]["quantity"]) == 3.0


def test_helper_handles_missing_quantity_gracefully():
    class Item:  # rows that somehow predate the backfill
        def __init__(self, id, q, consumable=False):
            self.id, self.quantity, self.consumable, self.name = id, q, consumable, f"i{id}"

    class PI:
        def __init__(self, item, q):
            self.item, self.item_id, self.quantity = item, item.id, q

    class Pack:
        def __init__(self, items):
            self.items = items

    legacy = Item(1, None)
    packs = [Pack([PI(legacy, 1)]), Pack([PI(legacy, 1)])]
    assert overpacked_items(packs) == {1: {"owned": 1, "packed": 2.0, "name": "i1"}}
