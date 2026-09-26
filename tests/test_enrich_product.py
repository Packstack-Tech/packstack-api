"""catalog.enrich: ensure_product / ensure_variant / link_items / enrich_item on SQLite with the model stubbed.

    cd api && python tests/test_enrich_product.py
"""
import datetime
import os
import sys
from pathlib import Path
from unittest import mock

os.environ.setdefault("WORKER_DATABASE_URL", "sqlite://")
os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models.base import Base, Brand, Product, ProductVariant, Item, CatalogProduct, CatalogVariant, User
from catalog import enrich as ep


def setup():
    eng = create_engine("sqlite://"); Base.metadata.create_all(eng); s = Session(eng)
    b = Brand(name="Nemo"); s.add(b); s.flush()
    p = Product(brand_id=b.id, name="Tensor Insulated Regular"); s.add(p); s.flush()
    pv = ProductVariant(product_id=p.id, name="reg"); s.add(pv); s.flush()
    u = User(email="t@t", username="t"); s.add(u); s.flush()
    items = [Item(user_id=u.id, name="pad", brand_id=b.id, product_id=p.id, product_variant_id=pv.id, weight=1, unit="g"),
             Item(user_id=u.id, name="pad2", brand_id=b.id, product_id=p.id, weight=1, unit="g"),
             Item(user_id=u.id, name="locked", brand_id=b.id, product_id=p.id, weight=1, unit="g", catalog_locked=True)]
    s.add_all(items); s.commit()
    return s, b, p, pv, items


def test_flow():
    s, b, p, pv, items = setup()
    product_ai = {"is_valid_product": True, "brand_name": "NEMO Equipment", "product_name": "Tensor Insulated",
                  "variant_name": "Regular", "weight_grams": 425, "product_url": "https://nemoequipment.com/tensor",
                  "description": "pad", "category": "Sleep System", "subcategory": "Sleeping Pad"}
    variant_ai = {"is_variant": True, "affects_weight": False, "canonical_name": "Regular", "kind": "size"}
    m_img = mock.Mock()
    with mock.patch.object(ep, "_call_ai_product", return_value=product_ai) as m_prod, \
         mock.patch.object(ep, "_call_ai_variant", return_value=variant_ai) as m_var, \
         mock.patch.object(ep, "_check_url", return_value=200):
        # 1. unknown product → researched, inserted with keys, image hook called
        cp = ep.ensure_product(s, b, p, "reg", on_product_created=m_img)
        assert cp is not None and cp.product_name == "Tensor Insulated" and cp.brand_key == "nemo" and cp.product_key == "tensorinsulated"
        assert m_prod.call_count == 1 and m_img.call_args[0] == (cp.id,)
        # 2. same product again → resolved by key, no AI
        assert ep.ensure_product(s, b, p, None).id == cp.id and m_prod.call_count == 1
        # spelling / suffix variant of the brand also resolves
        b2 = Brand(name="nemo equipment"); s.add(b2); s.flush()
        p2 = Product(brand_id=b2.id, name="tensor insulated"); s.add(p2); s.flush()
        assert ep.ensure_product(s, b2, p2, None).id == cp.id and m_prod.call_count == 1

        # 3. unknown variant → one narrow AI call, inserted, alias recorded (cosmetic → no weight)
        cv = ep.ensure_variant(s, cp, "reg")
        assert cv is not None and cv.name == "Regular" and cv.weight is None and cv.kind == "size"
        assert cv.aliases == ["reg"] and m_var.call_count == 1
        # 4. known variant via alias map / recorded alias → no AI
        assert ep.ensure_variant(s, cp, "Reg").id == cv.id and m_var.call_count == 1
        assert ep.ensure_variant(s, cp, "REGULAR").id == cv.id and m_var.call_count == 1
        # 5. spec-like text → ignored without AI
        assert ep.ensure_variant(s, cp, "690g") is None and m_var.call_count == 1
        # 6. weight-affecting variant
        m_var.return_value = {"is_variant": True, "affects_weight": True, "canonical_name": "Long", "weight_grams": 480, "kind": "length"}
        cv_long = ep.ensure_variant(s, cp, "long")
        assert float(cv_long.weight) == 480 and cv_long.weight_unit == "g"
        # 7. model says not a variant
        m_var.return_value = {"is_variant": False, "affects_weight": False, "canonical_name": None}
        assert ep.ensure_variant(s, cp, "bought 2019") is None

        # 8. linking honors lock and variant
        n = ep.link_items(s, cp, cv, p, pv.id)
        s.expire_all()
        i0, i1, i2 = [s.get(Item, i.id) for i in items]
        assert (i0.catalog_product_id, i0.catalog_variant_id) == (cp.id, cv.id)
        assert (i1.catalog_product_id, i1.catalog_variant_id) == (cp.id, None)
        assert (i2.catalog_product_id, i2.catalog_variant_id) == (None, None)   # locked
        assert n == 3

        # 9. rejected product short-circuits forever
        b3 = Brand(name="Misc"); s.add(b3); s.flush()
        p3 = Product(brand_id=b3.id, name="small bag"); s.add(p3); s.flush()
        m_prod.return_value = {"is_valid_product": False, "brand_name": "Misc", "product_name": "small bag"}
        assert ep.ensure_product(s, b3, p3, None) is None and m_prod.call_count == 2
        assert ep.ensure_product(s, b3, p3, None) is None and m_prod.call_count == 2
        assert s.query(CatalogProduct).filter_by(status="rejected").count() == 1

        # 10. enrich_item end to end on a fresh item: resolves by FK, variant by alias, links
        s.expire_all()
        fresh = Item(user_id=1, name="pad3", brand_id=b.id, product_id=p.id, product_variant_id=pv.id, weight=1, unit="g")
        s.add(fresh); s.commit()
        cp2, cv2 = ep.enrich_item(s, fresh.id)
        assert cp2.id == cp.id and cv2.id == cv.id and m_prod.call_count == 2 and m_var.call_count == 3
        s.expire_all()
        assert (s.get(Item, fresh.id).catalog_product_id, s.get(Item, fresh.id).catalog_variant_id) == (cp.id, cv.id)
        # locked item is untouched
        locked = s.get(Item, items[2].id)
        assert ep.enrich_item(s, locked.id) == (None, None)
    print("ENRICH OK")


if __name__ == "__main__":
    test_flow()
