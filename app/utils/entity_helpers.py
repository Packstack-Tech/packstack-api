import logging
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models.base import Brand, Product, ProductVariant, Category, ItemCategory

logger = logging.getLogger(__name__)


def resolve_brand(session: Session, brand_name: str) -> int:
    name = brand_name.strip()
    existing = session.query(Brand).filter(
        func.lower(Brand.name) == name.lower()).first()
    if existing:
        return existing.id

    brand = Brand(name=name)
    session.add(brand)
    session.flush()
    return brand.id


def resolve_product(session: Session, product_name: str, brand_id: int) -> int:
    name = product_name.strip()
    existing = session.query(Product).filter(
        func.lower(Product.name) == name.lower(),
        Product.brand_id == brand_id).first()
    if existing:
        return existing.id

    product = Product(name=name, brand_id=brand_id)
    session.add(product)
    session.flush()
    return product.id


def resolve_product_variant(session: Session, variant_name: str, product_id: int) -> int:
    name = variant_name.strip()
    existing = session.query(ProductVariant).filter(
        func.lower(ProductVariant.name) == name.lower(),
        ProductVariant.product_id == product_id).first()
    if existing:
        return existing.id

    variant = ProductVariant(name=name, product_id=product_id)
    session.add(variant)
    session.flush()
    return variant.id


def resolve_category(session: Session, category_name: str, user_id: int) -> int:
    name = category_name.strip()
    existing = session.query(Category).filter(
        func.lower(Category.name) == name.lower()).first()
    if existing:
        return existing.id

    category = Category(name=name, user_id=user_id)
    session.add(category)
    session.flush()
    return category.id


def resolve_item_fields(session: Session, payload, user_id: int):
    """Resolve brand/product/variant/category *_new fields into *_id fields."""
    if payload.brand_new:
        payload.brand_id = resolve_brand(session, payload.brand_new)

    if payload.product_new and payload.brand_id:
        payload.product_id = resolve_product(session, payload.product_new, payload.brand_id)

    if payload.product_variant_new and payload.product_id:
        payload.product_variant_id = resolve_product_variant(
            session, payload.product_variant_new, payload.product_id)

    if payload.category_new:
        payload.category_id = resolve_category(session, payload.category_new, user_id)


def resolve_import_category(
    session: Session,
    category_name: str,
    user_id: int,
    category_cache: dict
) -> Optional[int]:
    """Resolve a category name to an ItemCategory ID for CSV imports.

    Uses category_cache to avoid repeated queries. The cache stores
    both the loaded categories and previously resolved results.
    """
    cache_key = category_name.lower()
    if cache_key in category_cache:
        return category_cache[cache_key]

    if '_all_categories' not in category_cache:
        all_cats = session.query(Category).filter(
            or_(Category.user_id == user_id, Category.user_id == None)).all()
        category_cache['_all_categories'] = {
            cat.name.lower(): cat.id for cat in all_cats
        }

    cat_map = category_cache['_all_categories']
    base_category_id = cat_map.get(cache_key)
    result = None

    if base_category_id:
        item_cat = session.query(ItemCategory.id).filter(
            ItemCategory.category_id == base_category_id,
            ItemCategory.user_id == user_id).first()

        if item_cat:
            result = item_cat[0]
        else:
            try:
                new_ic = ItemCategory(user_id=user_id, category_id=base_category_id)
                session.add(new_ic)
                session.flush()
                result = new_ic.id
            except Exception:
                session.rollback()
    else:
        try:
            new_cat = Category(name=category_name, user_id=user_id)
            session.add(new_cat)
            session.flush()
            cat_map[cache_key] = new_cat.id

            new_ic = ItemCategory(user_id=user_id, category_id=new_cat.id)
            session.add(new_ic)
            session.flush()
            result = new_ic.id
        except Exception:
            logger.exception("Failed to create category during import")
            session.rollback()

    category_cache[cache_key] = result
    return result
