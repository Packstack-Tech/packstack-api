import csv
import logging

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import List
from io import StringIO
from sqlalchemy import or_, func

from models.base import User, Item, ItemCategory, Category, Brand, Product, ProductVariant
from utils.auth import authenticate
from utils.weight import standardize_weight_unit
from utils.item_category import get_or_create_item_category
from utils.entity_helpers import resolve_item_fields, resolve_import_category
from tasks.enrich_product import enrich_product

logger = logging.getLogger(__name__)

route = APIRouter(dependencies=[Depends(authenticate)])


class ItemType(BaseModel):
    name: str
    brand_id: int = None
    brand_new: str = None
    product_id: int = None
    product_new: str = None
    product_variant_id: int = None
    product_variant_new: str = None
    category_id: int = None
    category_new: str = None
    weight: float = None
    unit: str = None
    price: float = None
    consumable: bool = False
    product_url: str = None
    wishlist: bool = None
    notes: str = None


@route.post("", status_code=201)
def create(payload: ItemType, user: User = Depends(authenticate)):
    resolve_item_fields(db.session, payload, user.id)

    if payload.category_id:
        payload.category_id = get_or_create_item_category(
            db.session, payload.category_id, user.id)

    item_data = payload.dict()
    item_data.pop("product_new")
    item_data.pop("product_variant_new")
    item_data.pop("brand_new")
    item_data.pop("category_new")

    new_item = Item(user_id=user.id, **item_data)

    try:
        db.session.add(new_item)
        db.session.commit()
    except Exception:
        logger.exception("Failed to create item")
        raise HTTPException(400, "Unable to create item.")

    if new_item.brand_id and new_item.product_id:
        enrich_product.delay(
            new_item.brand_id,
            new_item.product_id,
            new_item.product_variant_id,
        )

    return new_item


class ItemUpdate(ItemType):
    id: int
    name: str = None


@route.put("")
def update(payload: ItemUpdate, user: User = Depends(authenticate)):
    resolve_item_fields(db.session, payload, user.id)

    if payload.category_id:
        payload.category_id = get_or_create_item_category(
            db.session, payload.category_id, user.id)

    fields = payload.dict()
    fields.pop("product_new")
    fields.pop("product_variant_new")
    fields.pop("brand_new")
    fields.pop("category_new")

    item = db.session.query(Item).filter_by(
        id=payload.id, user_id=user.id).first()

    if not item:
        raise HTTPException(404, "Item not found.")

    for key, value in fields.items():
        setattr(item, key, value)

    try:
        db.session.commit()
        db.session.refresh(item)
    except Exception:
        logger.exception("Failed to update item")
        raise HTTPException(400, "Unable to update item.")

    return item


class ItemOrder(BaseModel):
    id: int
    sort_order: int


class SortItems(BaseModel):
    __root__: List[ItemOrder]

    def __iter__(self):
        return iter(self.__root__)


@route.put("/sort")
def sort_items(items: SortItems, user: User = Depends(authenticate)):
    item_mappings = [dict(id=item.id, user_id=user.id, sort_order=item.sort_order)
                     for item in items]

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
    item_category_mappings = [dict(id=category.id, user_id=user.id, sort_order=category.sort_order)
                              for category in categories]

    try:
        db.session.bulk_update_mappings(ItemCategory, item_category_mappings)
        db.session.commit()
    except Exception:
        logger.exception("Failed to sort categories")
        raise HTTPException(
            400, "An error occurred while updating category order.")

    return True


@route.get("s")
def fetch(user: User = Depends(authenticate), limit: int = 100, offset: int = 0):
    items = db.session.query(Item).filter_by(
        user_id=user.id).offset(offset).limit(limit).all()
    return items


@route.get("s/grouped")
def fetch_grouped(user: User = Depends(authenticate)):
    items = db.session.query(Item).filter_by(user_id=user.id).all()

    groups = {}
    for item in items:
        cat_key = item.category_id or "uncategorized"
        if cat_key not in groups:
            groups[cat_key] = {"category": item.category, "items": []}
        groups[cat_key]["items"].append(item)

    for group in groups.values():
        group["items"].sort(key=lambda i: i.sort_order or 0)

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


class BulkItemIds(BaseModel):
    __root__: List[int]

    def __iter__(self):
        return iter(self.__root__)


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


@route.post("/import/lighterpack", status_code=201)
async def import_lighterpack_items(file: UploadFile = File(...), user: User = Depends(authenticate)):
    contents = await file.read()
    decoded = contents.decode()
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
    decoded = contents.decode()
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

        brand_id = None
        if brand:
            brand_entity = db.session.query(
                Brand.id).filter(func.lower(Brand.name) == brand.lower()).first()

            if brand_entity:
                brand_id = brand_entity[0]
            else:
                new_brand = Brand(name=brand)
                try:
                    db.session.add(new_brand)
                    db.session.commit()
                    db.session.refresh(new_brand)
                    brand_id = new_brand.id
                except Exception:
                    brand_id = None
                    db.session.rollback()

        product_id = None
        if brand_id and product:
            product_entity = db.session.query(Product.id).filter(
                func.lower(Product.name) == product.lower(), Product.brand_id == brand_id).first()

            if product_entity:
                product_id = product_entity[0]
            else:
                new_product = Product(brand_id=brand_id, name=product)
                try:
                    db.session.add(new_product)
                    db.session.commit()
                    db.session.refresh(new_product)
                    product_id = new_product.id
                except Exception:
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
