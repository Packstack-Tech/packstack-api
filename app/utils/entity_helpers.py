import logging
import re
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models.base import Brand, Product, ProductVariant
# Re-exported: callers import these from here.
from utils.item_category import resolve_category, resolve_import_category  # noqa: F401

logger = logging.getLogger(__name__)


_NAME_SYMBOLS = re.compile(r"[™®©]")


def clean_name(value: str | None) -> str:
    """Normalize user-entered brand / product / variant text before it is
    stored: strip, collapse internal whitespace, drop ™ ® ©. These are the
    variations the catalog dedupe pass had to clean up after the fact."""
    if not value:
        return ""
    v = _NAME_SYMBOLS.sub("", value)
    return re.sub(r"\s+", " ", v).strip()


def resolve_brand(session: Session, brand_name: str) -> int:
    name = clean_name(brand_name)
    existing = session.query(Brand).filter(
        func.lower(Brand.name) == name.lower()).first()
    if existing:
        return existing.id

    brand = Brand(name=name)
    session.add(brand)
    session.flush()
    return brand.id


def resolve_product(session: Session, product_name: str, brand_id: int) -> int:
    name = clean_name(product_name)
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
    name = clean_name(variant_name)
    existing = session.query(ProductVariant).filter(
        func.lower(ProductVariant.name) == name.lower(),
        ProductVariant.product_id == product_id).first()
    if existing:
        return existing.id

    variant = ProductVariant(name=name, product_id=product_id)
    session.add(variant)
    session.flush()
    return variant.id


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
