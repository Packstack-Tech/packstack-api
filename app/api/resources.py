import csv
import logging
import re
from collections import defaultdict
from itertools import groupby
from operator import attrgetter
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from sqlalchemy import case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from models.base import Brand, CatalogProduct, Product, User, Category, ProductVariant
from utils.auth import authenticate
from utils.consts import DEVELOPMENT
from utils.weight import convert_weight
from seed.categories import default_categories

logger = logging.getLogger(__name__)

route = APIRouter()


class CreateBrand(BaseModel):
    name: str


@route.post("/brand", status_code=201)
def create_brand(payload: CreateBrand, user: User = Depends(authenticate)):
    if len(payload.name) <= 1:
        raise HTTPException(400, 'Brand name must be longer')

    try:
        new_brand = Brand(name=payload.name)
        db.session.add(new_brand)
        db.session.commit()
        db.session.refresh(new_brand)
    except Exception:
        raise HTTPException(400, 'An error occurred while creating brand.')

    return new_brand


@route.get("/brands")
def fetch_brands():
    brands = db.session.query(Brand).filter_by(removed=False).all()
    return brands


@route.get("/brand/{brand_id}")
def fetch_brand_detail(brand_id: int):
    brand = db.session.query(Brand).options(joinedload(
        Brand.products)).filter_by(id=brand_id).first()
    return brand


@route.get("/product/search/{brand_id}/{search_str}")
def search_products(brand_id: int, search_str: str, user: User = Depends(authenticate)):
    search = "%{}%".format(search_str.strip())
    products = db.session.query(Product).filter(
        Product.brand_id == brand_id, Product.name.ilike(search)).all()
    return products


@route.get("/product/variants/{product_id}")
def get_product_variants(product_id: int, user: User = Depends(authenticate)):
    variants = db.session.query(ProductVariant).filter_by(
        product_id=product_id).all()

    return variants


class CreateProduct(BaseModel):
    name: str
    brand_id: int = None


@route.post("/product", status_code=201)
def create_product(payload: CreateProduct, user: User = Depends(authenticate)):
    if len(payload.name) <= 1:
        raise HTTPException(400, 'Product name must be longer')

    try:
        new_product = Product(name=payload.name, brand_id=payload.brand_id)
        db.session.add(new_product)
        db.session.commit()
        db.session.refresh(new_product)
    except Exception:
        raise HTTPException(400, 'An error occurred while creating product.')

    return new_product


@route.get("/catalog/search")
def catalog_search(
    q: str = "",
    brand: Optional[str] = Query(None),
    product: Optional[str] = Query(None),
):
    base = db.session.query(CatalogProduct).filter(
        CatalogProduct.status == "approved")

    if brand is not None and product is not None:
        entries = base.filter(
            CatalogProduct.brand_name == brand,
            CatalogProduct.product_name == product,
        ).order_by(CatalogProduct.variant_name).all()

        return [{
            "id": e.id,
            "brand_id": e.brand_id,
            "product_id": e.product_id,
            "product_variant_id": e.product_variant_id,
            "variant_name": e.variant_name,
            "weight": float(e.weight) if e.weight else None,
            "weight_unit": e.weight_unit,
            "product_url": e.product_url,
            "category_suggestion": e.category_suggestion,
        } for e in entries]

    if brand is not None:
        query = base.filter(CatalogProduct.brand_name == brand)
        if q:
            query = query.filter(CatalogProduct.product_name.ilike(f"%{q}%"))

        rows = (
            query
            .with_entities(
                func.min(CatalogProduct.product_id).label("product_id"),
                CatalogProduct.product_name,
            )
            .group_by(CatalogProduct.product_name)
            .order_by(CatalogProduct.product_name)
            .limit(50)
            .all()
        )
        return [{
            "product_id": r.product_id,
            "product_name": r.product_name,
        } for r in rows]

    query = base
    if q:
        query = query.filter(CatalogProduct.brand_name.ilike(f"%{q}%"))

    rows = (
        query
        .with_entities(
            func.min(CatalogProduct.brand_id).label("brand_id"),
            CatalogProduct.brand_name,
        )
        .group_by(CatalogProduct.brand_name)
        .order_by(CatalogProduct.brand_name)
        .limit(20)
        .all()
    )
    return [{
        "brand_id": r.brand_id,
        "brand_name": r.brand_name,
    } for r in rows]


def _slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r'[&/]', '', s)
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')


def _weight_to_grams(weight, weight_unit) -> float | None:
    if weight is None or weight_unit is None:
        return None
    try:
        return convert_weight(weight, weight_unit, "g")
    except Exception:
        return None


@route.get("/catalog/categories")
def catalog_categories():
    rows = (
        db.session.query(
            CatalogProduct.category_suggestion,
            CatalogProduct.subcategory,
            func.count(CatalogProduct.id).label("cnt"),
        )
        .filter(
            CatalogProduct.status == "approved",
            CatalogProduct.subcategory.isnot(None),
            CatalogProduct.category_suggestion.isnot(None),
        )
        .group_by(CatalogProduct.category_suggestion, CatalogProduct.subcategory)
        .order_by(CatalogProduct.category_suggestion, CatalogProduct.subcategory)
        .all()
    )

    grouped: dict[str, list] = defaultdict(list)
    for cat, sub, cnt in rows:
        grouped[cat].append({
            "name": sub,
            "slug": _slugify(sub),
            "product_count": cnt,
        })

    return sorted(
        [{"category": cat, "subcategories": subs} for cat, subs in grouped.items()],
        key=lambda g: sum(s["product_count"] for s in g["subcategories"]),
        reverse=True,
    )


def _serialize_product_groups(entries, compact: bool = False):
    """Group CatalogProduct rows (sorted by brand, product) into products
    with nested variants. Shared by catalog browse and product search.

    ``compact`` trims the prose-heavy fields (description, additional_specs,
    display_name, per-variant images) for the quick-add typeahead, which only
    needs identity, weight and calories to build an item."""
    products = []
    key_fn = attrgetter("brand_name", "product_name")
    for (brand, product), group_iter in groupby(entries, key=key_fn):
        variants_raw = list(group_iter)
        variants = []
        lightest_g: float | None = None
        product_url: str | None = None
        catalog_url_slug: str | None = None
        image_url: str | None = None

        for v in variants_raw:
            w_g = _weight_to_grams(v.weight, v.weight_unit)
            if w_g is not None and (lightest_g is None or w_g < lightest_g):
                lightest_g = w_g
            if not product_url and v.product_url:
                product_url = v.product_url
            if not catalog_url_slug and v.catalog_url_slug:
                catalog_url_slug = v.catalog_url_slug
            if not image_url and v.image_url:
                image_url = v.image_url

            variant = {
                "id": v.id,
                "brand_id": v.brand_id,
                "product_id": v.product_id,
                "product_variant_id": v.product_variant_id,
                "variant_name": v.variant_name,
                "weight": float(v.weight) if v.weight is not None else None,
                "weight_unit": v.weight_unit,
                "kcal": v.kcal,
            }
            if not compact:
                variant.update({
                    "display_name": v.display_name,
                    "image_url": v.image_url,
                    "description": v.description,
                    "additional_specs": v.additional_specs,
                })
            variants.append(variant)

        first = variants_raw[0]
        products.append({
            "brand_name": brand,
            "product_name": product,
            "product_url": product_url,
            "catalog_url_slug": catalog_url_slug,
            "image_url": image_url,
            "category": first.category_suggestion,
            "subcategory": first.subcategory,
            "subcategory_slug": _slugify(first.subcategory) if first.subcategory else None,
            "lightest_weight_g": round(lightest_g, 2) if lightest_g is not None else None,
            "variants": variants,
        })

    return products


@route.get("/catalog/browse/{slug}")
def catalog_browse(slug: str):
    # Build slug -> subcategory name lookup from live data
    distinct = (
        db.session.query(CatalogProduct.subcategory)
        .filter(
            CatalogProduct.status == "approved",
            CatalogProduct.subcategory.isnot(None),
        )
        .distinct()
        .all()
    )
    slug_map = {_slugify(r[0]): r[0] for r in distinct}
    subcategory_name = slug_map.get(slug)
    if not subcategory_name:
        raise HTTPException(404, "Subcategory not found")

    entries = (
        db.session.query(CatalogProduct)
        .filter(
            CatalogProduct.status == "approved",
            CatalogProduct.subcategory == subcategory_name,
        )
        .order_by(CatalogProduct.brand_name, CatalogProduct.product_name)
        .all()
    )

    category_name = entries[0].category_suggestion if entries else None

    products = _serialize_product_groups(entries)
    products.sort(key=lambda p: (
        p["lightest_weight_g"] is None,
        p["lightest_weight_g"] or 0,
    ))

    return {
        "subcategory": subcategory_name,
        "category": category_name,
        "slug": slug,
        "product_count": len(products),
        "products": products,
    }


MAX_GEAR_SEARCH_PRODUCTS = 50


@route.get("/catalog/products/search")
def catalog_product_search(q: str = "", compact: bool = False):
    """Freeform gear search across brand, product and subcategory names.

    Subcategory is searched so generic queries ("sleeping pad", "quilt") return
    results, which is how people search in the quick-add flow.

    Returns grouped products in the same shape as catalog browse; pass
    ``compact=1`` for the trimmed typeahead payload."""
    q = q.strip()
    if len(q) < 2:
        return []

    search = f"%{q}%"

    name_match = or_(
        CatalogProduct.brand_name.ilike(search),
        CatalogProduct.product_name.ilike(search),
        (CatalogProduct.brand_name + " " +
         CatalogProduct.product_name).ilike(search),
    )

    # Name matches outrank subcategory-only matches, so a generic query like
    # "tent" surfaces products actually named "tent" before the whole Tent
    # subcategory, and the row cap truncates the least relevant rather than
    # everything after brand "C". Ordering stays grouped by brand+product so
    # a product's variants remain adjacent for _serialize_product_groups.
    relevance = case((name_match, 0), else_=1)

    entries = (
        db.session.query(CatalogProduct)
        .filter(
            CatalogProduct.status == "approved",
            or_(name_match, CatalogProduct.subcategory.ilike(search)),
        )
        .order_by(relevance, CatalogProduct.brand_name,
                  CatalogProduct.product_name)
        .limit(500)
        .all()
    )

    return _serialize_product_groups(
        entries, compact=compact)[:MAX_GEAR_SEARCH_PRODUCTS]


@route.get("/brand/search/{query}")
def search_brands(query: str, user: User = Depends(authenticate)):
    search = "%{}%".format(query.strip())
    brands = db.session.query(Brand).filter(Brand.name.ilike(
        search), Brand.removed.is_(False)).limit(10).all()

    return brands


@route.get("/seed")
def seed_data():
    if not DEVELOPMENT:
        raise HTTPException(403, "Seed endpoint is only available in development.")

    with open('app/seed/brands.csv', newline='') as csvfile:
        reader = csv.reader(csvfile)
        for row in reader:
            brand = Brand(name=row[0])
            try:
                db.session.add(brand)
                db.session.commit()
            except IntegrityError:
                db.session.rollback()

    for category in default_categories():
        cat = db.session.query(Category).filter_by(name=category).first()
        if not cat:
            seed_category = Category(name=category)
            try:
                db.session.add(seed_category)
                db.session.commit()
            except Exception:
                logger.exception("Failed to seed category: %s", category)
                db.session.rollback()

    return
