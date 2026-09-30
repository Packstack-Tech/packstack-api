"""Worn quantity on pack items + the one base/worn/consumable weight rule.

The contract that matters most: PUT /pack rebuilds every row from the payload,
and mobile builds that predate `worn_quantity` resend the whole pack (with
only `worn`) on every checklist tick. That must not wipe a count set on web.

    source testenv.sh && python -m pytest tests/test_worn_quantity.py -q
"""

import secrets
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fastapi_sqlalchemy import db

import main
from models.base import Pack, User
from utils.auth import generate_jwt
from utils.pack_weight import effective_worn, normalize_worn, split_weight


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def user(client):
    with db():
        suffix = secrets.token_hex(4)
        u = User(email=f"worn-{suffix}@example.com", username=f"worn{suffix}", unit_weight="METRIC",
                 email_verified=True, is_subscribed=True)
        db.session.add(u)
        db.session.commit()
        return {"id": u.id, "headers": {"Authorization": f"Bearer {generate_jwt(u)}"}}


def add_item(client, user, **fields):
    body = {"name": "Thing", "weight": 100, "unit": "g", **fields}
    r = client.post("/item", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


def make_pack(client, user, items):
    r = client.post("/trip", json={"title": "Trip"}, headers=user["headers"])
    assert r.status_code == 201, r.text
    trip_id = r.json()["id"]
    r = client.post("/pack", json={"title": "Pack", "trip_id": trip_id, "items": items},
                    headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


def put_pack(client, user, pack, items):
    r = client.put(f"/pack/{pack['id']}", json={"title": pack["title"], "trip_id": pack["trip_id"],
                                               "items": items}, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def row(pack, item_id):
    return next(pi for pi in pack["items"] if pi["item_id"] == item_id)


# --- the compatibility contract ----------------------------------------------

def test_legacy_client_save_does_not_clobber_worn_quantity(client, user):
    shirt = add_item(client, user, name="Shirt")
    pack = make_pack(client, user, [{"item_id": shirt["id"], "quantity": 5,
                                     "worn": True, "worn_quantity": 3}])
    assert float(row(pack, shirt["id"])["worn_quantity"]) == 3

    # Old mobile build ticks a checklist box: whole pack, `worn` only.
    pack = put_pack(client, user, pack, [{"item_id": shirt["id"], "quantity": 5,
                                          "worn": True, "checked": True}])
    r = row(pack, shirt["id"])
    assert float(r["worn_quantity"]) == 3
    assert r["worn"] is True
    assert r["checked"] is True


def test_legacy_worn_true_with_no_stored_count_is_one_unit(client, user):
    shirt = add_item(client, user, name="Shirt")
    pack = make_pack(client, user, [{"item_id": shirt["id"], "quantity": 5, "worn": True}])
    assert float(row(pack, shirt["id"])["worn_quantity"]) == 1


def test_legacy_worn_false_clears(client, user):
    shirt = add_item(client, user, name="Shirt")
    pack = make_pack(client, user, [{"item_id": shirt["id"], "quantity": 5,
                                     "worn": True, "worn_quantity": 2}])
    pack = put_pack(client, user, pack, [{"item_id": shirt["id"], "quantity": 5, "worn": False}])
    r = row(pack, shirt["id"])
    assert float(r["worn_quantity"]) == 0
    assert r["worn"] is False


# --- clamping and consistency ---------------------------------------------------

def test_worn_quantity_clamped_and_worn_recomputed(client, user):
    shirt = add_item(client, user, name="Shirt")
    pack = make_pack(client, user, [{"item_id": shirt["id"], "quantity": 3,
                                     "worn": False, "worn_quantity": 9}])
    r = row(pack, shirt["id"])
    assert float(r["worn_quantity"]) == 3
    assert r["worn"] is True   # worn_quantity wins over a contradictory flag

    pack = put_pack(client, user, pack, [{"item_id": shirt["id"], "quantity": 3,
                                          "worn": False, "worn_quantity": -2}])
    r = row(pack, shirt["id"])
    assert float(r["worn_quantity"]) == 0
    assert r["worn"] is False


def test_new_client_echoing_legacy_row_keeps_it_worn(client, user):
    """Before the backfill, legacy rows load as worn=true, worn_quantity=0. A
    new client that saves without touching that row sends both back; that
    must not un-wear it."""
    shirt = add_item(client, user, name="Shirt")
    pack = make_pack(client, user, [{"item_id": shirt["id"], "quantity": 5,
                                     "worn": True, "worn_quantity": 2}])
    pack = put_pack(client, user, pack, [{"item_id": shirt["id"], "quantity": 5,
                                          "worn": True, "worn_quantity": 0}])
    r = row(pack, shirt["id"])
    assert float(r["worn_quantity"]) == 2
    assert r["worn"] is True


def test_lowering_quantity_clamps_stored_count(client, user):
    shirt = add_item(client, user, name="Shirt")
    pack = make_pack(client, user, [{"item_id": shirt["id"], "quantity": 5,
                                     "worn": True, "worn_quantity": 4}])
    # Old client lowers quantity to 2.
    pack = put_pack(client, user, pack, [{"item_id": shirt["id"], "quantity": 2, "worn": True}])
    assert float(row(pack, shirt["id"])["worn_quantity"]) == 2


def test_fractional_quantity_worn(client, user):
    thing = add_item(client, user, name="Half")
    pack = make_pack(client, user, [{"item_id": thing["id"], "quantity": 0.5, "worn": True}])
    assert float(row(pack, thing["id"])["worn_quantity"]) == 0.5


# --- the weight rule ------------------------------------------------------------

def test_partial_worn_breakdown(client, user):
    shirt = add_item(client, user, name="Shirt")   # 100 g each
    pack = make_pack(client, user, [{"item_id": shirt["id"], "quantity": 5,
                                     "worn": True, "worn_quantity": 1}])
    wb = pack["weight_breakdown"]
    assert wb["worn_g"] == 100
    assert wb["base_g"] == 400
    assert wb["total_g"] == 500


def test_worn_consumable_not_subtracted_twice(client, user):
    """Regression: the old app rule put one unit in worn AND every unit in
    consumable, so base went negative."""
    food = add_item(client, user, name="Snack", consumable=True)
    pack = make_pack(client, user, [{"item_id": food["id"], "quantity": 3,
                                     "worn": True, "worn_quantity": 1}])
    wb = pack["weight_breakdown"]
    assert wb["worn_g"] == 100
    assert wb["consumable_g"] == 200
    assert wb["base_g"] == 0
    assert wb["total_g"] == 300


def test_every_server_surface_agrees(client, user):
    """compute_pack_summary, the AI export and MCP totals use one rule."""
    from mcp_server.tools import _pack_totals
    from utils.ai_review import _summarize
    from utils.pack_summary import compute_pack_summary

    jacket = add_item(client, user, name="Jacket")
    shirt = add_item(client, user, name="Shirt")
    poles = add_item(client, user, name="Poles")
    snack = add_item(client, user, name="Snack", consumable=True)
    pack = make_pack(client, user, [
        {"item_id": jacket["id"], "quantity": 1, "worn": True},
        {"item_id": shirt["id"], "quantity": 5, "worn": True, "worn_quantity": 1},
        {"item_id": poles["id"], "quantity": 2, "worn": True, "worn_quantity": 2},
        {"item_id": snack["id"], "quantity": 3, "worn": True, "worn_quantity": 1},
    ])

    with db():
        p = db.session.query(Pack).get(pack["id"])
        summary = compute_pack_summary(p)["weight_breakdown"]
        ai = _summarize(p.items)
        caller = SimpleNamespace(unit="g", big_unit="kg")
        mcp = _pack_totals(list(p.items), caller)

    # jacket 100 worn; shirts 100 worn + 400 base; poles 200 worn; snack 100 worn + 200 consumable
    assert summary["worn_g"] == 500 and summary["base_g"] == 400 and summary["consumable_g"] == 200
    assert round(ai["worn"]) == 500 and round(ai["base"]) == 400 and round(ai["consumable"]) == 200
    assert mcp["worn_weight"]["grams"] == 500
    assert mcp["base_weight"]["grams"] == 400
    assert mcp["consumable_weight"]["grams"] == 200


def test_legacy_row_reads_as_one_worn():
    """A row written by the old API between the column and the backfill."""
    item = SimpleNamespace(weight=100, unit="g", consumable=False)
    pi = SimpleNamespace(item=item, quantity=5, worn=True, worn_quantity=0)
    assert effective_worn(pi) == 1
    assert split_weight(pi) == {"worn": 100, "consumable": 0.0, "base": 400, "total": 500}


def test_normalize_worn_table():
    assert normalize_worn(5, True, 3) == (3, True)
    assert normalize_worn(5, True, None, 3) == (3, True)      # old client keeps stored
    assert normalize_worn(5, True, None, 0) == (1, True)      # old client, nothing stored
    assert normalize_worn(5, False, None, 3) == (0, False)
    assert normalize_worn(2, True, None, 4) == (2, True)      # clamp to quantity
    assert normalize_worn(3, False, 9) == (3, True)           # count wins over flag
    assert normalize_worn(3, False, -1) == (0, False)
    assert normalize_worn(5, True, 0, 2) == (2, True)         # legacy echo keeps stored
    assert normalize_worn(5, True, 0, None) == (1, True)      # legacy echo, nothing stored


# --- copies ------------------------------------------------------------------------

def test_trip_clone_keeps_worn_quantity(client, user):
    shirt = add_item(client, user, name="Shirt")
    pack = make_pack(client, user, [{"item_id": shirt["id"], "quantity": 5,
                                     "worn": True, "worn_quantity": 2}])
    r = client.post(f"/trip/{pack['trip_id']}/clone", headers=user["headers"])
    assert r.status_code in (200, 201), r.text
    new_trip_id = r.json()["id"]
    packs = client.get(f"/pack/trip/{new_trip_id}", headers=user["headers"]).json()
    assert float(row(packs[0], shirt["id"])["worn_quantity"]) == 2


def test_public_serializer_carries_effective_worn(client, user):
    shirt = add_item(client, user, name="Shirt")
    pack = make_pack(client, user, [{"item_id": shirt["id"], "quantity": 5,
                                     "worn": True, "worn_quantity": 2}])
    from utils.pack_summary import serialize_pack_public
    with db():
        p = db.session.query(Pack).get(pack["id"])
        out = serialize_pack_public(p)
    assert out["items"][0]["worn_quantity"] == 2
