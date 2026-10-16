"""The item-form pickers must hand out legacy ids that carry the catalog's
spelling, and display names prefer the catalog. Regression for an item
picked as "Triple Crown Button Down" showing "riple Crown Button Down": the
picker returned the legacy Product the catalog row was researched from.

    source testenv.sh && python -m pytest tests/test_catalog_picker_ids.py -q
"""

import secrets

import pytest
from fastapi.testclient import TestClient
from fastapi_sqlalchemy import db

import main
from api import item as item_api
from models.base import Brand, CatalogProduct, CatalogVariant, Item, Product, User
from models.keys import canonical_variant_key, product_keys
from utils.auth import generate_jwt


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def no_enrich(monkeypatch):
    monkeypatch.setattr(item_api.enrich_product, "delay", lambda *a, **k: None)


@pytest.fixture
def user(client):
    with db():
        sfx = secrets.token_hex(4)
        u = User(email=f"picker-{sfx}@example.com", username=f"pk{sfx}", unit_weight="METRIC",
                 email_verified=True)
        db.session.add(u)
        db.session.commit()
        return {"id": u.id, "headers": {"Authorization": f"Bearer {generate_jwt(u)}"}}


@pytest.fixture
def typo_catalog(client):
    """A catalog product researched from a misspelled legacy row, under a
    brand whose first legacy row was misspelled too."""
    sfx = secrets.token_hex(3)
    brand_name = f"Jolly Gear {sfx}"
    with db():
        typo_brand = Brand(name=f"Joly Gear {sfx}")
        db.session.add(typo_brand); db.session.flush()
        typo = Product(name="riple Crown Button Down", brand_id=typo_brand.id)
        db.session.add(typo); db.session.flush()
        bk, pk = product_keys(brand_name, "Triple Crown Button Down")
        cp = CatalogProduct(brand_name=brand_name, product_name="Triple Crown Button Down",
                            display_name=f"{brand_name} Triple Crown Button Down",
                            brand_key=bk, product_key=pk, status="approved",
                            brand_id=typo_brand.id, product_id=typo.id)
        db.session.add(cp); db.session.flush()
        cv = CatalogVariant(catalog_product_id=cp.id, name="M/L", name_key=canonical_variant_key("M/L"),
                            weight=241, weight_unit="g")
        db.session.add(cv); db.session.commit()
        return {"brand": brand_name, "sfx": sfx, "typo_brand_id": typo_brand.id,
                "typo_product_id": typo.id, "cp_id": cp.id, "cv_id": cv.id}


def products(client, brand, **params):
    r = client.get("/resources/catalog/search", params={"brand": brand, **params})
    assert r.status_code == 200, r.text
    return {p["product_name"]: p["product_id"] for p in r.json()}


def test_product_ids_carry_catalog_spelling(client, typo_catalog):
    ids = products(client, typo_catalog["brand"])
    pid = ids["Triple Crown Button Down"]
    assert pid != typo_catalog["typo_product_id"]
    with db():
        p = db.session.get(Product, pid)
        assert p.name == "Triple Crown Button Down"
        assert db.session.get(Brand, p.brand_id).name == typo_catalog["brand"]
    # Stable: the row is created once and found after.
    assert products(client, typo_catalog["brand"])["Triple Crown Button Down"] == pid


def test_partial_search_still_filters(client, typo_catalog):
    assert "Triple Crown Button Down" in products(client, typo_catalog["brand"], q="crown")
    assert products(client, typo_catalog["brand"], q="nope") == {}


def test_brand_ids_carry_catalog_spelling(client, typo_catalog):
    r = client.get("/resources/catalog/search", params={"q": typo_catalog["sfx"]})
    assert r.status_code == 200, r.text
    ids = {b["brand_name"]: b["brand_id"] for b in r.json()}
    bid = ids[typo_catalog["brand"]]
    assert bid != typo_catalog["typo_brand_id"]
    with db():
        assert db.session.get(Brand, bid).name == typo_catalog["brand"]


def test_unknown_brand_creates_nothing(client):
    name = f"Nobody {secrets.token_hex(4)}"
    assert products(client, name) == {}
    with db():
        assert db.session.query(Brand).filter_by(name=name).count() == 0


def test_picked_item_stores_catalog_spelling(client, user, typo_catalog):
    brand_id = client.get("/resources/catalog/search", params={"q": typo_catalog["sfx"]}).json()[0]["brand_id"]
    pid = products(client, typo_catalog["brand"])["Triple Crown Button Down"]
    r = client.post("/item", json={"name": "Sun Hoodie", "brand_id": brand_id, "product_id": pid},
                    headers=user["headers"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["product"]["name"] == "Triple Crown Button Down"
    assert body["catalog_product_id"] == typo_catalog["cp_id"]


def test_product_edit_drops_explicit_pick(client, user, typo_catalog):
    """A locked explicit pick was about the old product; changing the product
    re-derives the link instead of leaving the stale one (whose name the
    clients would now show over the user's edit)."""
    r = client.post("/item", json={"name": "Shirt", "brand_new": typo_catalog["brand"],
                                   "product_new": "Triple Crown Button Down",
                                   "catalog_product_id": typo_catalog["cp_id"],
                                   "catalog_variant_id": typo_catalog["cv_id"]},
                    headers=user["headers"])
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["catalog_locked"] is True and item["catalog_product_id"] == typo_catalog["cp_id"]

    r = client.put("/item", json={"id": item["id"], "name": "Shirt", "brand_id": item["brand_id"],
                                  "product_new": f"Something Else {typo_catalog['sfx']}"},
                   headers=user["headers"])
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["catalog_product_id"] is None and item["catalog_locked"] is False


def test_detached_item_stays_detached(client, user, typo_catalog):
    r = client.post("/item", json={"name": "Shirt", "brand_new": typo_catalog["brand"],
                                   "product_new": "Other"}, headers=user["headers"])
    item = r.json()
    assert client.delete(f"/item/{item['id']}/catalog", headers=user["headers"]).status_code == 200
    r = client.put("/item", json={"id": item["id"], "name": "Shirt", "brand_id": item["brand_id"],
                                  "product_new": "Triple Crown Button Down"}, headers=user["headers"])
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["catalog_product_id"] is None and item["catalog_locked"] is True


def test_public_pack_item_shows_catalog_name():
    from catalog.resolver import display_product_name, display_variant_name
    from utils.pack_summary import _serialize_item_public

    cp = CatalogProduct(product_name="Tensor Insulated", status="approved")
    item = Item(id=1, name="Pad", product=Product(id=7, name="Tensor Insulated Regular"),
                catalog_product=cp, catalog_variant=CatalogVariant(name="Regular"))
    assert display_product_name(item) == "Tensor Insulated"
    assert display_variant_name(item) == "Regular"
    out = _serialize_item_public(item)
    assert out["product"]["name"] == "Tensor Insulated"
    assert out["product_variant"]["name"] == "Regular"

    cp.status = "merged"   # a dead catalog row never wins
    assert display_product_name(item) == "Tensor Insulated Regular"
    assert display_variant_name(item) is None
