import csv
import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from fastapi_sqlalchemy import db
from pydantic import BaseModel, RootModel, field_validator
from typing import List, Optional
from io import StringIO
from sqlalchemy import or_, func

from models.base import User, Item, ItemLog, ItemCategory, Category, Brand, Product, ProductVariant, CatalogProduct, CatalogVariant
from utils.auth import authenticate
from utils.weight import standardize_weight_unit
from utils.item_category import get_or_create_item_category
from utils.entity_helpers import (
    resolve_item_fields, resolve_import_category, resolve_brand, resolve_product, clean_name,
)
from tasks.enrich_product import enrich_product
from catalog.resolver import resolve_product, resolve_variant

logger = logging.getLogger(__name__)

route = APIRouter(dependencies=[Depends(authenticate)])


class ItemType(BaseModel):
    name: str
    brand_id: Optional[int] = None
    brand_new: Optional[str] = None
    product_id: Optional[int] = None
    product_new: Optional[str] = None
    product_variant_id: Optional[int] = None
    product_variant_new: Optional[str] = None
    # Explicit catalog picks from the quick-add / item-form pickers. Both are
    # optional: when absent the link is derived from brand/product/variant.
    catalog_product_id: Optional[int] = None
    catalog_variant_id: Optional[int] = None
    category_id: Optional[int] = None
    category_new: Optional[str] = None
    weight: Optional[float] = None
    unit: Optional[str] = None
    price: Optional[float] = None
    # Owned quantity. None means "not sent" — create falls back to the model
    # default (1) and update leaves the stored value alone. Clients that
    # predate the field omit it on every PUT, so None must never reach the
    # row or every edit from an old build would wipe the user's count.
    quantity: Optional[int] = None
    calories: Optional[float] = None
    consumable: bool = False
    product_url: Optional[str] = None
    notes: Optional[str] = None

    acquired_date: Optional[str] = None
    acquisition_type: Optional[str] = None
    purchase_retailer: Optional[str] = None
    condition: Optional[str] = None
    status: Optional[str] = None
    retired_date: Optional[str] = None
    retired_reason: Optional[str] = None
    replaced_by_id: Optional[int] = None

    @field_validator(
        "acquired_date", "acquisition_type", "purchase_retailer",
        "condition", "status", "retired_date", "retired_reason",
        "product_url", "notes",
        mode="before",
    )
    @classmethod
    def empty_str_to_none(cls, v):
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("quantity", mode="before")
    @classmethod
    def quantity_positive_int(cls, v):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        if isinstance(v, bool):
            raise ValueError("quantity must be a whole number of at least 1")
        if isinstance(v, float) and not v.is_integer():
            raise ValueError("quantity must be a whole number of at least 1")
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError("quantity must be a whole number of at least 1")
        if n < 1:
            raise ValueError("quantity must be a whole number of at least 1")
        return n


def _derive_catalog_link(session, item) -> tuple[int | None, int | None]:
    """(catalog_product_id, catalog_variant_id) for an item from its
    brand/product/variant text. Product first; a variant that doesn't
    match keeps the product link (variant None) — never a different named
    variant. Locked items are never touched by callers of this."""
    brand = item.brand.name if item.brand else None
    product = item.product.name if item.product else None
    if not brand or not product:
        return None, None
    cp = resolve_product(session, brand, product)
    if cp is None:
        return None, None
    variant_text = item.product_variant.name if item.product_variant else None
    cv = resolve_variant(session, cp, variant_text)
    return cp.id, (cv.id if cv else None)


def _apply_catalog_pick(session, item, catalog_product_id, catalog_variant_id) -> bool:
    """Honor an explicit pick from a catalog picker. Returns True if applied.
    The pair must be consistent (variant belongs to product, product live)."""
    if not catalog_product_id:
        return False
    cp = session.query(CatalogProduct).filter_by(id=catalog_product_id, status="approved").first()
    if cp is None:
        return False
    cv = None
    if catalog_variant_id:
        cv = session.query(CatalogVariant).filter_by(id=catalog_variant_id, catalog_product_id=cp.id).first()
    item.catalog_product_id = cp.id
    item.catalog_variant_id = cv.id if cv else None
    return True


@route.post("", status_code=201)
def create(payload: ItemType, user: User = Depends(authenticate)):
    resolve_item_fields(db.session, payload, user.id)

    if payload.category_id:
        payload.category_id = get_or_create_item_category(
            db.session, payload.category_id, user.id)

    item_data = payload.model_dump()
    item_data.pop("product_new")
    item_data.pop("product_variant_new")
    item_data.pop("brand_new")
    item_data.pop("category_new")
    pick_product = item_data.pop("catalog_product_id")
    pick_variant = item_data.pop("catalog_variant_id")
    if item_data.get("quantity") is None:
        item_data.pop("quantity", None)   # model default -> 1

    new_item = Item(user_id=user.id, **item_data)
    db.session.add(new_item)
    db.session.flush()

    if _apply_catalog_pick(db.session, new_item, pick_product, pick_variant):
        new_item.catalog_locked = True   # an explicit pick is the user's choice
    elif new_item.brand_id and new_item.product_id:
        new_item.catalog_product_id, new_item.catalog_variant_id = _derive_catalog_link(db.session, new_item)

    try:
        db.session.commit()
        db.session.refresh(new_item)
    except Exception:
        logger.exception("Failed to create item")
        raise HTTPException(400, "Unable to create item.")

    if new_item.brand_id and new_item.product_id and not new_item.catalog_locked:
        enrich_product.delay(new_item.id)

    return new_item


class ItemUpdate(ItemType):
    id: int
    name: Optional[str] = None


@route.put("")
def update(payload: ItemUpdate, user: User = Depends(authenticate)):
    resolve_item_fields(db.session, payload, user.id)

    if payload.category_id:
        payload.category_id = get_or_create_item_category(
            db.session, payload.category_id, user.id)

    fields = payload.model_dump()
    fields.pop("product_new")
    fields.pop("product_variant_new")
    fields.pop("brand_new")
    fields.pop("category_new")
    pick_product = fields.pop("catalog_product_id")
    pick_variant = fields.pop("catalog_variant_id")
    if fields.get("quantity") is None:
        fields.pop("quantity", None)   # omitted == unchanged, never null

    item = db.session.query(Item).filter_by(
        id=payload.id, user_id=user.id).first()

    if not item:
        raise HTTPException(404, "Item not found.")

    old_condition = item.condition
    old_acquired_date = item.acquired_date
    old_identity = (item.brand_id, item.product_id, item.product_variant_id)
    old_catalog_product_id, old_catalog_variant_id = item.catalog_product_id, item.catalog_variant_id

    for key, value in fields.items():
        setattr(item, key, value)

    # The catalog link is derived from brand/product/variant. If any of
    # those changed, re-derive it so a stale pairing can't survive an edit
    # (e.g. user switches "Regular" -> "Large" and keeps seeing Regular).
    # A locked item (user detached or explicitly chose) is left alone.
    new_identity = (item.brand_id, item.product_id, item.product_variant_id)
    identity_changed = new_identity != old_identity
    # Clients often echo the item back whole; a catalog pair equal to what
    # the item already has is not a pick. Only a *changed* pair is explicit.
    explicit_pick = (pick_product is not None
                     and (pick_product, pick_variant) != (old_catalog_product_id, old_catalog_variant_id))
    if explicit_pick and _apply_catalog_pick(db.session, item, pick_product, pick_variant):
        item.catalog_locked = True
    elif identity_changed and not item.catalog_locked:
        db.session.flush()
        db.session.refresh(item)
        item.catalog_product_id, item.catalog_variant_id = _derive_catalog_link(db.session, item)

    try:
        if payload.condition and payload.condition != old_condition:
            log = ItemLog(
                item_id=item.id,
                user_id=user.id,
                event_type="condition_change",
                note=f"Condition changed from {old_condition or 'unset'} to {payload.condition}",
                old_condition=old_condition,
                new_condition=payload.condition,
                event_date=datetime.date.today(),
            )
            db.session.add(log)

        if payload.acquired_date and not old_acquired_date:
            log = ItemLog(
                item_id=item.id,
                user_id=user.id,
                event_type="acquired",
                event_date=payload.acquired_date,
            )
            db.session.add(log)

        db.session.commit()
        db.session.refresh(item)
    except Exception:
        logger.exception("Failed to update item")
        raise HTTPException(400, "Unable to update item.")

    if (identity_changed and not item.catalog_locked
            and item.brand_id and item.product_id):
        # Enrich even when a product matched: the variant may be new.
        enrich_product.delay(item.id)

    return item


@route.delete("/{item_id}/catalog")
def detach_catalog(item_id: int, user: User = Depends(authenticate)):
    """Detach the auto-assigned catalog product from an item and lock it so
    neither enrichment nor future edits re-attach one. The user's own
    name / weight / url are untouched — only the manufacturer-spec link goes."""
    item = db.session.query(Item).filter_by(
        id=item_id, user_id=user.id).first()
    if not item:
        raise HTTPException(404, "Item not found.")

    item.catalog_product_id = None
    item.catalog_variant_id = None
    item.catalog_locked = True
    try:
        db.session.commit()
        db.session.refresh(item)
    except Exception:
        logger.exception("Failed to detach catalog product")
        raise HTTPException(400, "Unable to detach catalog product.")
    return item


class CatalogAttach(BaseModel):
    catalog_product_id: int
    catalog_variant_id: Optional[int] = None


@route.put("/{item_id}/catalog")
def attach_catalog(item_id: int, payload: CatalogAttach,
                   user: User = Depends(authenticate)):
    """Explicitly pair an item with a catalog product. Locks the item so the
    user's choice is not overridden by auto-matching."""
    item = db.session.query(Item).filter_by(
        id=item_id, user_id=user.id).first()
    if not item:
        raise HTTPException(404, "Item not found.")
    if not _apply_catalog_pick(db.session, item, payload.catalog_product_id, payload.catalog_variant_id):
        raise HTTPException(404, "Catalog product not found.")
    item.catalog_locked = True
    try:
        db.session.commit()
        db.session.refresh(item)
    except Exception:
        logger.exception("Failed to attach catalog product")
        raise HTTPException(400, "Unable to attach catalog product.")
    return item


class ItemOrder(BaseModel):
    id: int
    sort_order: int


class SortItems(RootModel[List[ItemOrder]]):
    """Request body is a bare JSON array of {id, sort_order}."""

    def __iter__(self):
        return iter(self.root)


@route.put("/sort")
def sort_items(items: SortItems, user: User = Depends(authenticate)):
    # Only the caller's rows, and never write user_id: bulk_update_mappings
    # updates by primary key alone, so the old mapping (which set
    # user_id=user.id on whatever ids were sent) handed other users' rows
    # to the caller.
    requested = {item.id: item.sort_order for item in items}
    owned = {r[0] for r in db.session.query(Item.id).filter(
        Item.id.in_(list(requested)), Item.user_id == user.id)}
    item_mappings = [dict(id=i, sort_order=requested[i]) for i in owned]

    try:
        db.session.bulk_update_mappings(Item, item_mappings)
        db.session.commit()
    except Exception:
        logger.exception("Failed to sort items")
        raise HTTPException(
            400, "An error occurred while updating item order.")

    return True


@route.put("/category/sort")
def sort_categories(categories: SortItems, user: User = Depends(authenticate)):
    # See sort_items: only the caller's rows, and never write user_id.
    requested = {category.id: category.sort_order for category in categories}
    owned = {r[0] for r in db.session.query(ItemCategory.id).filter(
        ItemCategory.id.in_(list(requested)), ItemCategory.user_id == user.id)}
    item_category_mappings = [dict(id=i, sort_order=requested[i]) for i in owned]

    try:
        db.session.bulk_update_mappings(ItemCategory, item_category_mappings)
        db.session.commit()
    except Exception:
        logger.exception("Failed to sort categories")
        raise HTTPException(
            400, "An error occurred while updating category order.")

    return True


@route.get("s")
def fetch(user: User = Depends(authenticate)):
    items = db.session.query(Item).filter_by(
        user_id=user.id, deleted=False).all()
    return items


@route.get("s/grouped")
def fetch_grouped(user: User = Depends(authenticate)):
    items = db.session.query(Item).filter_by(user_id=user.id, deleted=False).all()

    groups = {}
    for item in items:
        cat_key = item.category_id or "uncategorized"
        if cat_key not in groups:
            groups[cat_key] = {"category": item.category, "items": []}
        groups[cat_key]["items"].append(item)

    for group in groups.values():
        group["items"].sort(key=lambda i: (i.sort_order or 0, i.created_at))

    return sorted(
        groups.values(),
        key=lambda g: g["category"].sort_order if g["category"] else float("inf"),
    )


@route.delete("/{item_id}", status_code=204)
def remove(item_id: int, user: User = Depends(authenticate)):
    item = db.session.query(Item).filter_by(
        id=item_id, user_id=user.id).first()

    if not item:
        raise HTTPException(404, "Item not found.")

    item.removed = True
    db.session.commit()


@route.post("/{item_id}/delete", status_code=204)
def soft_delete(item_id: int, user: User = Depends(authenticate)):
    item = db.session.query(Item).filter_by(
        id=item_id, user_id=user.id).first()

    if not item:
        raise HTTPException(404, "Item not found.")

    item.deleted = True
    db.session.commit()


class BulkItemIds(RootModel[List[int]]):
    """Request body is a bare JSON array of item ids."""

    def __iter__(self):
        return iter(self.root)


@route.put("/bulk-archive")
def bulk_archive(item_ids: BulkItemIds, user: User = Depends(authenticate)):
    ids = list(item_ids)
    db.session.query(Item).filter(
        Item.id.in_(ids), Item.user_id == user.id
    ).update({"removed": True}, synchronize_session="fetch")
    db.session.commit()
    return True


@route.put("/bulk-restore")
def bulk_restore(item_ids: BulkItemIds, user: User = Depends(authenticate)):
    ids = list(item_ids)
    db.session.query(Item).filter(
        Item.id.in_(ids), Item.user_id == user.id
    ).update({"removed": False}, synchronize_session="fetch")
    db.session.commit()
    return True


@route.post("/bulk-delete", status_code=204)
def bulk_delete(item_ids: BulkItemIds, user: User = Depends(authenticate)):
    ids = list(item_ids)
    db.session.query(Item).filter(
        Item.id.in_(ids), Item.user_id == user.id
    ).update({"deleted": True}, synchronize_session="fetch")
    db.session.commit()


@route.post("/import/lighterpack", status_code=201)
async def import_lighterpack_items(file: UploadFile = File(...), user: User = Depends(authenticate)):
    contents = await file.read()
    decoded = contents.decode("utf-8", errors="replace")
    buffer = StringIO(decoded)
    csvReader = csv.DictReader(buffer)

    rows = [dict((k.lower().strip(), v.strip())
                 for k, v in row.items() if k) for row in csvReader]
    buffer.close()

    def generate_error(line, message):
        return dict({'line': line + 2, 'error': message})

    entries = []
    errors = []
    category_cache = {}

    for i, row in enumerate(rows):
        name = row.get("item name")
        category = row.get("category")
        description = row.get("desc")
        weight = row.get("weight")
        unit = row.get("unit")
        product_url = row.get("url")
        price = row.get("price", None)
        consumable = row.get("consumable", None)

        if not name:
            continue

        if unit:
            try:
                unit = standardize_weight_unit(unit)
            except Exception as e:
                errors.append(generate_error(i, str(e)))
                continue

        if weight:
            try:
                weight = float(weight)
            except (ValueError, TypeError):
                errors.append(generate_error(i, "Invalid weight value."))
                continue
        else:
            weight = None

        if price:
            try:
                price = float(price)
            except (ValueError, TypeError):
                errors.append(generate_error(i, "Invalid price value."))
                continue
        else:
            price = None

        category_id = None
        if category:
            category_id = resolve_import_category(
                db.session, category, user.id, category_cache)

        entries.append(dict(user_id=user.id,
                            category_id=category_id,
                            name=name,
                            weight=weight,
                            unit=unit,
                            price=price,
                            product_url=product_url,
                            notes=description,
                            consumable=bool(consumable)))

    if errors:
        return {'success': False, 'errors': errors, 'count': len(errors)}

    try:
        db.session.bulk_insert_mappings(Item, entries)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise HTTPException(
            400, 'An unexpected error occurred while importing items.')

    return {'success': True, 'errors': [], 'count': len(entries)}


@route.post("/import/csv", status_code=201)
async def import_items(file: UploadFile = File(...), user: User = Depends(authenticate)):
    contents = await file.read()
    decoded = contents.decode("utf-8", errors="replace")
    buffer = StringIO(decoded)
    csvReader = csv.DictReader(buffer)

    rows = [dict((k.lower().strip(), v.strip())
                 for k, v in row.items() if k) for row in csvReader]
    buffer.close()

    def generate_error(line, message):
        return dict({'line': line + 2, 'error': message})

    entries = []
    errors = []
    category_cache = {}

    for i, row in enumerate(rows):
        name = row.get("name")
        brand = row.get("manufacturer")
        product = row.get("product")
        category = row.get("category")
        weight = row.get("weight")
        unit = row.get("unit")
        product_url = row.get("product_url")
        price = row.get("price", None)
        consumable = row.get("consumable", None)
        notes = row.get("notes", None)
        # Owned quantity. Optional column; blank -> 1. This is the Packstack
        # CSV (round-trips our own export); the separate LighterPack import
        # deliberately leaves `qty` alone because there it is a pack quantity.
        quantity_raw = row.get("quantity") or row.get("owned_quantity") or row.get("qty")

        if not name:
            continue

        quantity = 1
        if quantity_raw:
            try:
                quantity = int(float(quantity_raw))
            except (ValueError, TypeError):
                errors.append(generate_error(i, "Invalid quantity value."))
                continue
            if quantity < 1:
                errors.append(generate_error(i, "Quantity must be at least 1."))
                continue

        if unit:
            try:
                unit = standardize_weight_unit(unit)
            except Exception as e:
                errors.append(generate_error(i, str(e)))
                continue

        if weight:
            try:
                weight = float(weight)
            except (ValueError, TypeError):
                errors.append(generate_error(i, "Invalid weight value."))
                continue
        else:
            weight = None

        if price:
            try:
                price = float(price)
            except (ValueError, TypeError):
                errors.append(generate_error(i, "Invalid price value."))
                continue
        else:
            price = None

        # Same resolvers as the item form / MCP: cleaned names, case-insensitive
        # match. The import used to build Brand/Product rows directly, which
        # let trailing spaces and ™/® through and created duplicates.
        brand_id = None
        if brand and clean_name(brand):
            try:
                brand_id = resolve_brand(db.session, brand)
                db.session.commit()
            except Exception:
                logger.exception("CSV import: could not resolve brand %r", brand)
                brand_id = None
                db.session.rollback()

        product_id = None
        if brand_id and product and clean_name(product):
            try:
                product_id = resolve_product(db.session, product, brand_id)
                db.session.commit()
            except Exception:
                logger.exception("CSV import: could not resolve product %r", product)
                product_id = None
                db.session.rollback()

        category_id = None
        if category:
            category_id = resolve_import_category(
                db.session, category, user.id, category_cache)

        entries.append(dict(user_id=user.id,
                            brand_id=brand_id,
                            product_id=product_id,
                            category_id=category_id,
                            name=name,
                            quantity=quantity,
                            weight=weight,
                            unit=unit,
                            price=price,
                            product_url=product_url,
                            notes=notes,
                            consumable=bool(consumable)))

    if errors:
        return {'success': False, 'errors': errors, 'count': len(errors)}

    try:
        db.session.bulk_insert_mappings(Item, entries)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise HTTPException(
            400, 'An unexpected error occurred while importing items.')

    return {'success': True, 'errors': [], 'count': len(entries)}
