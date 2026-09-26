"""Catalog identity resolution and the one product serializer.

Every write path (item create/update, MCP create_item, the Celery
enrichment task, workshop batch scripts) resolves brand/product/variant
text through here BEFORE creating anything, so a product can only exist
once per normalized identity. Every read path serializes a product through
`serialize_product` so web, mobile, MCP and the public site see one shape.

Identity keys come from models/keys.py and are enforced by unique indexes
(catalogproduct.brand_key+product_key, catalogvariant.catalog_product_id+
name_key). See claude/catalog-variants-pivot.md.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from models.base import CatalogProduct, CatalogVariant, Item
from models.keys import product_keys, canonical_variant_key
from utils.weight import convert_weight

LIVE_STATUSES = ("approved",)


# ── Lookup ──────────────────────────────────────────────────────────────────

def resolve_product(session: Session, brand_text: str, product_text: str,
                    statuses=LIVE_STATUSES) -> CatalogProduct | None:
    """Existing product for this brand/product text, matched by normalized
    key. Never creates."""
    if not brand_text or not product_text:
        return None
    bk, pk = product_keys(brand_text, product_text)
    if not bk or not pk:
        return None
    q = session.query(CatalogProduct).filter(
        CatalogProduct.brand_key == bk,
        CatalogProduct.product_key == pk,
    )
    if statuses:
        q = q.filter(CatalogProduct.status.in_(statuses))
    return q.first()


def resolve_variant(session: Session, product: CatalogProduct, variant_text: str | None,
                    include_hidden: bool = False) -> CatalogVariant | None:
    """Existing variant of `product` for this text, by canonical key, then
    by recorded aliases. Never creates."""
    if product is None or not variant_text:
        return None
    key = canonical_variant_key(variant_text)
    if not key:
        return None
    q = session.query(CatalogVariant).filter(CatalogVariant.catalog_product_id == product.id)
    if not include_hidden:
        q = q.filter(CatalogVariant.hidden.is_(False))
    variants = q.all()
    for v in variants:
        if v.name_key == key:
            return v
    for v in variants:
        for alias in v.aliases or []:
            if canonical_variant_key(alias) == key:
                return v
    return None


def resolve(session: Session, brand_text: str, product_text: str, variant_text: str | None):
    """(product, variant) — product may be None; variant is None when the
    product is None or the text doesn't match a known variant."""
    product = resolve_product(session, brand_text, product_text)
    return product, resolve_variant(session, product, variant_text)


def record_alias(variant: CatalogVariant, spelling: str | None) -> None:
    """Remember a user spelling that resolved to this variant so the next
    user who types it matches without an AI call."""
    if not spelling:
        return
    spelling = spelling.strip()
    if not spelling or spelling == variant.name:
        return
    aliases = list(variant.aliases or [])
    if spelling not in aliases:
        aliases.append(spelling)
        variant.aliases = aliases


# ── Weight ──────────────────────────────────────────────────────────────────

def to_grams(weight, unit) -> float | None:
    if weight is None or not unit:
        return None
    try:
        return float(convert_weight(weight, unit, "g"))
    except Exception:
        return None


def effective_weight(item: Item) -> tuple[float | None, str | None, str]:
    """(weight, unit, source) with source in {'item', 'variant', 'product', 'none'}.

    item.weight ?? variant.weight ?? product.weight — the user's own
    measurement always wins; the catalog only supplies a default."""
    if item.weight is not None:
        return float(item.weight), item.unit, "item"
    v = getattr(item, "catalog_variant", None)
    if v is not None and v.weight is not None:
        return float(v.weight), v.weight_unit, "variant"
    p = getattr(item, "catalog_product", None)
    if p is not None and p.weight is not None:
        return float(p.weight), p.weight_unit, "product"
    return None, None, "none"


def effective_weight_grams(item: Item) -> float | None:
    w, u, _ = effective_weight(item)
    return to_grams(w, u)


# ── Serialization ───────────────────────────────────────────────────────────

def serialize_variant(v: CatalogVariant, product: CatalogProduct) -> dict:
    """A variant as the clients see it. `has_weight` is the whole point:
    weight-bearing variants are shown in pickers; aesthetic ones are not."""
    return {
        "id": v.id,
        "name": v.name,
        "weight": float(v.weight) if v.weight is not None else None,
        "weight_unit": v.weight_unit,
        "has_weight": v.weight is not None,
        "kcal": v.kcal if v.kcal is not None else product.kcal,
        "image_url": v.image_url or product.image_url,
        "kind": v.kind,
        "sort_order": v.sort_order,
    }


def weight_range_g(product: CatalogProduct, variants) -> tuple[float | None, float | None]:
    ws = [to_grams(product.weight, product.weight_unit)]
    ws += [to_grams(v.weight, v.weight_unit) for v in variants]
    ws = [w for w in ws if w is not None]
    if not ws:
        return None, None
    return round(min(ws), 2), round(max(ws), 2)


def serialize_product(product: CatalogProduct, compact: bool = False, variants=None) -> dict:
    """The one product shape. `compact` trims prose for typeaheads.

    variants: pre-fetched list to avoid N+1; otherwise the relationship is
    used. Hidden variants are never serialized."""
    if variants is None:
        variants = [v for v in (product.variants or []) if not v.hidden]
    else:
        variants = [v for v in variants if not v.hidden]
    variants = sorted(variants, key=lambda v: ((v.weight is None), v.sort_order or 0, v.name.lower()))
    lo, hi = weight_range_g(product, variants)
    out = {
        "id": product.id,
        "brand_name": product.brand_name,
        "product_name": product.product_name,
        "display_name": product.display_name,
        "brand_id": product.brand_id,
        "product_id": product.product_id,
        "weight": float(product.weight) if product.weight is not None else None,
        "weight_unit": product.weight_unit,
        "kcal": product.kcal,
        "weight_range_g": {"min": lo, "max": hi} if lo is not None else None,
        "lightest_weight_g": lo,   # compat alias for browse sorting / public site
        "product_url": product.product_url,
        "image_url": product.image_url,
        "category": product.category_suggestion,
        "subcategory": product.subcategory,
        "catalog_url_slug": product.catalog_url_slug,
        "variants": [serialize_variant(v, product) for v in variants],
        "weight_variant_count": sum(1 for v in variants if v.weight is not None),
    }
    if not compact:
        out.update({
            "description": product.description,
            "additional_specs": product.additional_specs,
            "status": product.status,
        })
    return out
