"""Unit test for app/catalog/resolver.py against in-memory SQLite.

    cd api && python -m pytest tests/test_catalog_resolver.py -q
    (or: python tests/test_catalog_resolver.py)
"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))          # models
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))  # utils, catalog

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models.base import Base, CatalogProduct, CatalogVariant, Item, User
from models.keys import product_keys, canonical_variant_key
from catalog.resolver import (resolve_product, resolve_variant, resolve, record_alias,
                              effective_weight, serialize_product)


def make_session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return Session(eng)


def make_product(s, brand, name, weight=None, unit="g", status="approved", **kw):
    bk, pk = product_keys(brand, name)
    p = CatalogProduct(brand_name=brand, product_name=name, display_name=f"{brand} {name}",
                       brand_key=bk, product_key=pk, weight=weight, weight_unit=unit,
                       status=status, created_at=datetime.datetime.utcnow(), **kw)
    s.add(p); s.flush()
    return p


def make_variant(s, p, name, weight=None, unit="g", **kw):
    v = CatalogVariant(catalog_product_id=p.id, name=name, name_key=canonical_variant_key(name),
                       weight=weight, weight_unit=unit if weight is not None else None,
                       created_at=datetime.datetime.utcnow(), **kw)
    s.add(v); s.flush()
    return v


def test_resolver():
    s = make_session()
    tensor = make_product(s, "NEMO", "Tensor", weight=400, description="pad", image_url="t.jpg")
    reg = make_variant(s, tensor, "Regular", weight=None)                       # aesthetic
    long_ = make_variant(s, tensor, "Long", weight=450)
    blue = make_variant(s, tensor, "Blue", weight=None, image_url="blue.jpg")
    hidden = make_variant(s, tensor, "Bogus", weight=999, hidden=True)
    make_product(s, "Durston Gear", "X-Mid Pro 2", weight=509)
    make_product(s, "Durston Gear", "X-Mid Pro 2+", weight=545)
    make_product(s, "Petzl", "Old Thing", weight=1, status="migrated")

    # product: spelling / suffix / symbol tolerant, + preserved, status-aware
    assert resolve_product(s, "nemo equipment", "TENSOR").id == tensor.id
    assert resolve_product(s, "Nemo™", " tensor ").id == tensor.id
    assert resolve_product(s, "Durston", "X-Mid Pro 2").product_name == "X-Mid Pro 2"
    assert resolve_product(s, "Durston", "X-Mid Pro 2+").product_name == "X-Mid Pro 2+"
    assert resolve_product(s, "Petzl", "Old Thing") is None
    assert resolve_product(s, "Petzl", "Old Thing", statuses=None).status == "migrated"
    assert resolve_product(s, "", "Tensor") is None

    # variant: key, alias map, recorded aliases, hidden excluded
    assert resolve_variant(s, tensor, "reg").id == reg.id            # built-in alias map
    assert resolve_variant(s, tensor, "LONG ").id == long_.id
    assert resolve_variant(s, tensor, "Wide") is None
    record_alias(long_, "Lng"); record_alias(long_, "Lng"); record_alias(long_, "Long")
    assert long_.aliases == ["Lng"]
    assert resolve_variant(s, tensor, "lng").id == long_.id
    assert resolve_variant(s, tensor, "Bogus") is None
    assert resolve_variant(s, tensor, "Bogus", include_hidden=True).id == hidden.id
    assert resolve(s, "nemo", "tensor", "long")[1].id == long_.id
    assert resolve(s, "nope", "tensor", "long") == (None, None)

    # effective weight chain
    u = User(email="t@t", username="t"); s.add(u); s.flush()
    i1 = Item(user_id=u.id, name="a", weight=410, unit="g", catalog_product_id=tensor.id, catalog_variant_id=long_.id)
    i2 = Item(user_id=u.id, name="b", weight=None, catalog_product_id=tensor.id, catalog_variant_id=long_.id)
    i3 = Item(user_id=u.id, name="c", weight=None, catalog_product_id=tensor.id, catalog_variant_id=blue.id)
    i4 = Item(user_id=u.id, name="d", weight=None)
    s.add_all([i1, i2, i3, i4]); s.flush(); s.expire_all()
    assert effective_weight(i1) == (410.0, "g", "item")
    assert effective_weight(i2) == (450.0, "g", "variant")
    assert effective_weight(i3) == (400.0, "g", "product")     # aesthetic → product default
    assert effective_weight(i4) == (None, None, "none")

    # serializer
    s.expire_all()
    out = serialize_product(s.get(CatalogProduct, tensor.id))
    names = [v["name"] for v in out["variants"]]
    assert names == ["Long", "Blue", "Regular"], names            # weight-bearing first, hidden gone
    assert out["variants"][0]["has_weight"] and not out["variants"][1]["has_weight"]
    assert out["variants"][1]["image_url"] == "blue.jpg" and out["variants"][2]["image_url"] == "t.jpg"
    assert out["weight_range_g"] == {"min": 400.0, "max": 450.0}
    assert out["weight_variant_count"] == 1 and out["description"] == "pad"
    assert "description" not in serialize_product(tensor, compact=True)
    print("RESOLVER OK")


if __name__ == "__main__":
    test_resolver()
