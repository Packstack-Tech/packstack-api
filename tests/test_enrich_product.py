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
         mock.patch.object(ep, "_call_ai_recall", return_value=None) as m_recall, \
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

        # 8b. variant that affects weight but the model has no figure: users' median fills it, no search
        m_var.return_value = {"is_variant": True, "affects_weight": True, "canonical_name": "Wide", "weight_grams": None, "kind": "size"}
        pv_wide = ProductVariant(product_id=p.id, name="wide"); s.add(pv_wide); s.flush()
        s.add(Item(user_id=1, name="w", brand_id=b.id, product_id=p.id, product_variant_id=pv_wide.id, weight=16, unit="oz")); s.commit()
        calls_before = m_var.call_count
        cv_wide = ep.ensure_variant(s, cp, "wide", legacy_variant_id=pv_wide.id)
        assert round(float(cv_wide.weight)) == 454 and m_var.call_count == calls_before + 1   # 16 oz, one classify call, no search
        assert m_var.call_args.kwargs.get("user_median_g") and not m_var.call_args.kwargs.get("with_search")

        # 9. rejected product short-circuits forever
        b3 = Brand(name="Acme"); s.add(b3); s.flush()
        p3 = Product(brand_id=b3.id, name="thingamajig deluxe"); s.add(p3); s.flush()
        m_prod.return_value = {"is_valid_product": False, "brand_name": "Acme", "product_name": "thingamajig deluxe"}
        assert ep.ensure_product(s, b3, p3, None) is None and m_prod.call_count == 2
        assert ep.ensure_product(s, b3, p3, None) is None and m_prod.call_count == 2
        assert s.query(CatalogProduct).filter_by(status="rejected").count() == 1

        # 9b. junk brand/product: rejected with NO model call at all
        bj = Brand(name="Generic"); s.add(bj); s.flush()
        pj = Product(brand_id=bj.id, name="Stuff sack"); s.add(pj); s.flush()
        before = (m_prod.call_count, m_recall.call_count)
        assert ep.ensure_product(s, bj, pj, None) is None
        assert (m_prod.call_count, m_recall.call_count) == before
        assert s.query(CatalogProduct).filter_by(status="rejected").count() == 2
        assert ep.is_junk("Nemo", "Tensor") is None and ep.is_junk("MYOG", "Quilt") and ep.is_junk("Osprey", "Pack")

        # 9c. tier 1 accepted when users' weights corroborate; tier 2 never called
        bt = Brand(name="BRS"); s.add(bt); s.flush()
        pt = Product(brand_id=bt.id, name="BRS-3000T"); s.add(pt); s.flush()
        s.add(Item(user_id=1, name="stove", brand_id=bt.id, product_id=pt.id, weight=26, unit="g")); s.commit()
        m_recall.return_value = {"is_valid_product": True, "brand_name": "BRS", "product_name": "BRS-3000T",
                                 "weight_grams": 25, "confidence": 0.95, "product_url": "https://brs.example/3000t",
                                 "category": "Kitchen", "subcategory": "Stove"}
        before = m_prod.call_count
        cpt = ep.ensure_product(s, bt, pt, None)
        assert cpt is not None and float(cpt.weight) == 25 and m_prod.call_count == before   # no web research
        # 9d. tier 1 disagrees with users (says 60 g, users say 26 g) → falls through to web research
        bt2 = Brand(name="Soto"); s.add(bt2); s.flush()
        pt2 = Product(brand_id=bt2.id, name="Amicus"); s.add(pt2); s.flush()
        s.add(Item(user_id=1, name="stove", brand_id=bt2.id, product_id=pt2.id, weight=75, unit="g")); s.commit()
        m_recall.return_value = {"is_valid_product": True, "brand_name": "SOTO", "product_name": "Amicus", "weight_grams": 120, "confidence": 0.9, "product_url": "https://x"}
        m_prod.return_value = {"is_valid_product": True, "brand_name": "SOTO", "product_name": "Amicus", "weight_grams": 75, "product_url": "https://soto.example/amicus", "category": "Kitchen"}
        cpt2 = ep.ensure_product(s, bt2, pt2, None)
        assert float(cpt2.weight) == 75 and m_prod.call_count == before + 1
        m_recall.return_value = None
        m_prod.return_value = product_ai

        # 10. enrich_item end to end on a fresh item: resolves by FK, variant by alias, links
        s.expire_all()
        fresh = Item(user_id=1, name="pad3", brand_id=b.id, product_id=p.id, product_variant_id=pv.id, weight=1, unit="g")
        s.add(fresh); s.commit()
        prod_calls, var_calls = m_prod.call_count, m_var.call_count
        cp2, cv2 = ep.enrich_item(s, fresh.id)
        # FK hit + alias hit: no new product research, no new variant classification
        assert cp2.id == cp.id and cv2.id == cv.id
        assert (m_prod.call_count, m_var.call_count) == (prod_calls, var_calls)
        s.expire_all()
        assert (s.get(Item, fresh.id).catalog_product_id, s.get(Item, fresh.id).catalog_variant_id) == (cp.id, cv.id)
        # locked item is untouched
        locked = s.get(Item, items[2].id)
        assert ep.enrich_item(s, locked.id) == (None, None)
    print("ENRICH OK")


if __name__ == "__main__":
    test_flow()
