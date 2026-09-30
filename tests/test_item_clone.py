"""POST /item/{id}/clone -- copies a gear item's core characteristics.

    source testenv.sh && python -m pytest tests/test_item_clone.py -q
"""

import secrets

import pytest
from fastapi.testclient import TestClient
from fastapi_sqlalchemy import db

import main
from models.base import Item, ItemLog, User
from utils.auth import generate_jwt


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def make_user():
    with db():
        suffix = secrets.token_hex(4)
        u = User(email=f"clone-{suffix}@example.com", username=f"cl{suffix}", unit_weight="METRIC",
                 email_verified=True)
        db.session.add(u)
        db.session.commit()
        return {"id": u.id, "headers": {"Authorization": f"Bearer {generate_jwt(u)}"}}


@pytest.fixture
def user(client):
    return make_user()


def add_item(client, user, **fields):
    body = {"name": "Tee", "weight": 120, "unit": "g", "price": 35, "calories": 0,
            "consumable": False, "product_url": "https://example.com/tee",
            "notes": "the blue one, has a hole", "quantity": 3,
            "acquired_date": "2025-06-01", "acquisition_type": "purchased",
            "purchase_retailer": "REI", "condition": "good", **fields}
    r = client.post("/item", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


def clone(client, user, item_id):
    r = client.post(f"/item/{item_id}/clone", headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


def test_copies_core_and_resets_the_rest(client, user):
    src = add_item(client, user)
    cp = clone(client, user, src["id"])

    assert cp["id"] != src["id"]
    assert cp["name"] == "Tee (Copy)"
    for f in ("weight", "unit", "price", "calories", "consumable", "product_url", "category_id"):
        assert cp[f] == src[f], f

    assert cp["quantity"] == 1
    assert cp["notes"] is None
    for f in ("acquired_date", "acquisition_type", "purchase_retailer", "condition",
              "retired_date", "retired_reason", "replaced_by_id"):
        assert cp[f] is None, f
    assert cp["status"] == "active"
    assert cp["removed"] is False


def test_activity_log_not_copied(client, user):
    src = add_item(client, user)   # acquired_date on create
    # A condition change writes an ItemLog row on the source.
    r = client.put("/item", json={"id": src["id"], "name": src["name"], "condition": "worn",
                                  "weight": 120, "unit": "g"}, headers=user["headers"])
    assert r.status_code == 200, r.text
    cp = clone(client, user, src["id"])
    with db():
        assert db.session.query(ItemLog).filter_by(item_id=src["id"]).count() >= 1
        assert db.session.query(ItemLog).filter_by(item_id=cp["id"]).count() == 0


def test_catalog_lock_preserved(client, user):
    src = add_item(client, user)
    r = client.delete(f"/item/{src['id']}/catalog", headers=user["headers"])
    assert r.status_code == 200, r.text
    cp = clone(client, user, src["id"])
    assert cp["catalog_locked"] is True
    assert cp["catalog_product_id"] is None


def test_wishlist_stays_wishlist(client, user):
    src = add_item(client, user, status="wishlist")
    assert clone(client, user, src["id"])["status"] == "wishlist"


def test_retired_becomes_active(client, user):
    src = add_item(client, user, status="retired", retired_reason="worn_out")
    cp = clone(client, user, src["id"])
    assert cp["status"] == "active"
    assert cp["retired_reason"] is None


def test_archived_source_gives_active_copy(client, user):
    src = add_item(client, user)
    client.delete(f"/item/{src['id']}", headers=user["headers"])   # archive
    cp = clone(client, user, src["id"])
    assert cp["removed"] is False


def test_long_name_truncated_to_fit(client, user):
    long = "x" * 100
    src = add_item(client, user, name=long)
    cp = clone(client, user, src["id"])
    assert len(cp["name"]) == 100
    assert cp["name"].endswith(" (Copy)")


def test_source_untouched(client, user):
    src = add_item(client, user)
    clone(client, user, src["id"])
    with db():
        again = db.session.query(Item).get(src["id"])
        assert again.notes == "the blue one, has a hole"
        assert again.quantity == 3
        assert again.name == "Tee"


def test_other_users_item_is_404(client, user):
    src = add_item(client, user)
    other = make_user()
    r = client.post(f"/item/{src['id']}/clone", headers=other["headers"])
    assert r.status_code == 404
