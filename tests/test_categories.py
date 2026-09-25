"""Category management API: the user's list, rename, merge, delete, and the
duplicate guards behind them.

Runs against a real Postgres, like test_mcp_flow.py (the unique indexes are
partial/functional and don't exist on SQLite):

    source testenv.sh && python -m pytest tests/test_categories.py -q
"""

import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from fastapi_sqlalchemy import db

import main
from models.base import Category, CategoryBenchmark, Item, ItemCategory, User
from utils.auth import generate_jwt


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def shared(name):
    with db():
        c = db.session.query(Category).filter_by(user_id=None, name=name).first()
        if c is None:
            c = Category(name=name, user_id=None)
            db.session.add(c)
            db.session.commit()
        return c.id


@pytest.fixture
def user(client):
    with db():
        suffix = secrets.token_hex(4)
        u = User(email=f"cat-{suffix}@example.com", username=f"cat{suffix}", unit_weight="IMPERIAL",
                 email_verified=True)
        db.session.add(u)
        db.session.commit()
        return {"id": u.id, "headers": {"Authorization": f"Bearer {generate_jwt(u)}"}}


def add_item(client, user, **fields):
    body = {"name": "Thing", "weight": 1, "unit": "oz", **fields}
    r = client.post("/item", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


def mine(client, user):
    r = client.get("/category/mine", headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def by_name(listing, name):
    return [c for c in listing["categories"] if c["name"] == name]


def test_mine_lists_empty_categories_and_suggestions(client, user):
    shelter = shared("Shelter")
    shared("Camera")
    add_item(client, user, category_id=shelter)
    r = client.post("/category", json={"name": "Poop Kit"}, headers=user["headers"])
    assert r.status_code == 201

    listing = mine(client, user)
    names = [c["name"] for c in listing["categories"]]
    assert names == ["Shelter", "Poop Kit"]
    assert by_name(listing, "Poop Kit")[0]["item_count"] == 0
    assert by_name(listing, "Shelter")[0]["item_count"] == 1
    assert "Camera" in [s["name"] for s in listing["suggested"]]
    assert "Shelter" not in [s["name"] for s in listing["suggested"]]


def test_create_is_idempotent_by_name(client, user):
    first = client.post("/category", json={"name": "  Bear   Kit "}, headers=user["headers"])
    assert first.status_code == 201
    again = client.post("/category", json={"name": "bear kit"}, headers=user["headers"])
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]

    # A shared name adopts the shared category instead of shadowing it.
    shelter = shared("Shelter")
    r = client.post("/category", json={"name": "shelter"}, headers=user["headers"])
    assert r.json()["category_id"] == shelter
    assert len(by_name(mine(client, user), "Bear Kit")) == 1


def test_new_category_by_name_on_item_does_not_duplicate(client, user):
    a = add_item(client, user, category_new="Fishing")
    b = add_item(client, user, category_new=" fishing ")
    assert a["category_id"] == b["category_id"]
    assert len(by_name(mine(client, user), "Fishing")) == 1


def test_concurrent_item_saves_share_one_itemcategory(client, user):
    cam = shared("Camera")
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: add_item(client, user, category_id=cam), range(8)))
    assert len({r["category_id"] for r in results}) == 1
    with db():
        assert db.session.query(ItemCategory).filter_by(user_id=user["id"], category_id=cam).count() == 1


def test_rename_shared_forks_and_hides_original(client, user):
    pack = shared("Pack")
    add_item(client, user, category_id=pack)
    r = client.put(f"/category/{pack}", json={"name": "Packs"}, headers=user["headers"])
    assert r.status_code == 200
    fork = r.json()
    assert fork["user_id"] == user["id"] and fork["forked_from_id"] == pack

    listing = mine(client, user)
    assert [c["name"] for c in listing["categories"]] == ["Packs"]
    assert listing["categories"][0]["item_count"] == 1
    assert "Pack" not in [s["name"] for s in listing["suggested"]]
    legacy = client.get("/category", headers=user["headers"]).json()
    assert "Pack" not in [c["name"] for c in legacy]
    assert "Packs" in [c["name"] for c in legacy]

    # Typing the shared name again brings it back as a real, separate category.
    add_item(client, user, category_new="Pack")
    legacy = client.get("/category", headers=user["headers"]).json()
    assert {"Pack", "Packs"} <= {c["name"] for c in legacy}


def test_rename_collision_returns_409_with_merge_target(client, user):
    clothing = shared("Clothing")
    add_item(client, user, category_id=clothing)
    own = client.post("/category", json={"name": "Clothes"}, headers=user["headers"]).json()

    r = client.put(f"/category/{own['category_id']}", json={"name": "CLOTHING"}, headers=user["headers"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "category_exists"
    assert detail["category_id"] == clothing

    # Case-only rename of your own category is fine.
    r = client.put(f"/category/{own['category_id']}", json={"name": "clothes"}, headers=user["headers"])
    assert r.status_code == 200 and r.json()["name"] == "clothes"


def test_rename_carries_benchmark(client, user):
    own = client.post("/category", json={"name": "Boots"}, headers=user["headers"]).json()
    with db():
        db.session.add(CategoryBenchmark(user_id=user["id"], category_name="Boots", lifespan_years=2))
        db.session.commit()
    client.put(f"/category/{own['category_id']}", json={"name": "Hiking Boots"}, headers=user["headers"])
    with db():
        names = [b.category_name for b in db.session.query(CategoryBenchmark).filter_by(user_id=user["id"])]
    assert names == ["Hiking Boots"]


def test_merge_moves_items_and_removes_source(client, user):
    toiletries = shared("Toiletries")
    add_item(client, user, category_id=toiletries)
    own = client.post("/category", json={"name": "Hygiene"}, headers=user["headers"]).json()
    add_item(client, user, category_id=own["category_id"])
    add_item(client, user, category_id=own["category_id"])

    r = client.post(f"/category/{own['category_id']}/merge", json={"into_category_id": toiletries},
                    headers=user["headers"])
    assert r.status_code == 200, r.text
    listing = mine(client, user)
    assert [c["name"] for c in listing["categories"]] == ["Toiletries"]
    assert listing["categories"][0]["item_count"] == 3
    with db():
        assert db.session.get(Category, own["category_id"]) is None


def test_merge_shared_into_own_stops_suggesting_it(client, user):
    first_aid = shared("First Aid")
    add_item(client, user, category_id=first_aid)
    own = client.post("/category", json={"name": "Med Kit"}, headers=user["headers"]).json()
    r = client.post(f"/category/{first_aid}/merge", json={"into_category_id": own["category_id"]},
                    headers=user["headers"])
    assert r.status_code == 200
    listing = mine(client, user)
    assert [c["name"] for c in listing["categories"]] == ["Med Kit"]
    assert listing["categories"][0]["item_count"] == 1
    assert "First Aid" not in [s["name"] for s in listing["suggested"]]


def test_delete_own_empty_and_shared(client, user):
    empty = client.post("/category", json={"name": "New cat2"}, headers=user["headers"]).json()
    assert client.delete(f"/category/{empty['category_id']}", headers=user["headers"]).status_code == 204
    with db():
        assert db.session.get(Category, empty["category_id"]) is None

    safety = shared("Safety")
    item = add_item(client, user, category_id=safety)
    assert client.delete(f"/category/{safety}", headers=user["headers"]).status_code == 204
    listing = mine(client, user)
    assert listing["categories"] == []
    assert "Safety" in [s["name"] for s in listing["suggested"]]
    with db():
        assert db.session.get(Item, item["id"]).category_id is None
        assert db.session.get(Category, safety) is not None

    # Not in the list any more.
    assert client.delete(f"/category/{safety}", headers=user["headers"]).status_code == 404


def test_cannot_touch_another_users_category(client, user):
    with db():
        suffix = secrets.token_hex(4)
        u = User(email=f"cat-{suffix}@example.com", username=f"cat{suffix}", email_verified=True)
        db.session.add(u)
        db.session.commit()
        other = {"id": u.id, "headers": {"Authorization": f"Bearer {generate_jwt(u)}"}}
    theirs = client.post("/category", json={"name": "Secret"}, headers=other["headers"]).json()
    cid = theirs["category_id"]
    assert client.put(f"/category/{cid}", json={"name": "Mine"}, headers=user["headers"]).status_code == 404
    assert client.delete(f"/category/{cid}", headers=user["headers"]).status_code == 404
    mine_cat = client.post("/category", json={"name": "Mine"}, headers=user["headers"]).json()
    r = client.post(f"/category/{mine_cat['category_id']}/merge", json={"into_category_id": cid},
                    headers=user["headers"])
    assert r.status_code == 404


def test_sort_endpoints_ignore_other_users_rows(client, user):
    with db():
        suffix = secrets.token_hex(4)
        u = User(email=f"cat-{suffix}@example.com", username=f"cat{suffix}", email_verified=True)
        db.session.add(u)
        db.session.commit()
        victim = {"id": u.id, "headers": {"Authorization": f"Bearer {generate_jwt(u)}"}}
    their_item = add_item(client, victim, category_new="Theirs")

    r = client.put("/item/sort", json=[{"id": their_item["id"], "sort_order": 5}], headers=user["headers"])
    assert r.status_code == 200
    r = client.put("/item/category/sort", json=[{"id": their_item["category_id"], "sort_order": 5}],
                   headers=user["headers"])
    assert r.status_code == 200
    with db():
        assert db.session.get(Item, their_item["id"]).user_id == victim["id"]
        assert db.session.get(ItemCategory, their_item["category_id"]).user_id == victim["id"]


def test_bad_names_rejected(client, user):
    assert client.post("/category", json={"name": "   "}, headers=user["headers"]).status_code == 400
    assert client.post("/category", json={"name": "x" * 51}, headers=user["headers"]).status_code == 400
