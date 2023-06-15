import csv

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import List
from io import StringIO
from sqlalchemy import or_, func

from models.base import User, Item, ItemCategory, Category, Brand, Product
from utils.auth import authenticate
from utils.item_category import get_or_create_item_category

route = APIRouter()


class ItemType(BaseModel):
    name: str
    brand_id: int = None
    brand_new: str = None
    product_id: int = None
    product_new: str = None
    category_id: int = None
    category_new: str = None
    weight: float = None
    unit: str = None
    price: float = None
    consumable: bool = False
    product_url: str = None
    wishlist: bool = None
    notes: str = None

# TODO functionize brand/product/category creation


@route.post("")
def create(payload: ItemType, user: User = Depends(authenticate)):
    # If brand_new is provided, create a new brand
    if payload.brand_new:
        new_brand = payload.brand_new.strip()
        existing_brand = db.session.query(Brand).filter(
            func.lower(payload.brand_new) == new_brand.lower()).first()

        if existing_brand:
            payload.brand_id = existing_brand.id
        else:
            brand = Brand(name=new_brand)
            db.session.add(brand)
            db.session.commit()
            db.session.refresh(brand)
            payload.brand_id = brand.id

    # If product_new is provided, create a new product and assign brand_id
    if payload.product_new and payload.brand_id:
        new_product = payload.product_new.strip()
        existing_product = db.session.query(Product).filter(
            func.lower(payload.product_new) == new_product.lower()).first()

        if existing_product:
            payload.product_id = existing_product.id
        else:
            product = Product(name=new_product, brand_id=payload.brand_id)
            db.session.add(product)
            db.session.commit()
            db.session.refresh(product)
            payload.product_id = product.id

    # If category_new is provided, create a new category
    if payload.category_new:
        new_category = payload.category_new.strip()
        existing_category = db.session.query(Category).filter(
            func.lower(payload.category_new) == new_category.lower()).first()

        if existing_category:
            payload.category_id = existing_category.id
        else:
            category = Category(name=new_category, user_id=user.id)
            db.session.add(category)
            db.session.commit()
            db.session.refresh(category)
            payload.category_id = category.id

    # Creates an item-specific category for the user
    if payload.category_id:
        payload.category_id = get_or_create_item_category(
            db.session, payload.category_id, user.id)

    # Remove data-creation fields from dict
    item_data = payload.dict()
    item_data.pop("product_new")
    item_data.pop("brand_new")
    item_data.pop("category_new")

    new_item = Item(user_id=user.id, **item_data)

    try:
        db.session.add(new_item)
        db.session.commit()
    except Exception as e:
        print(e)
        raise HTTPException(400, "Unable to create item.")

    return new_item


class ItemUpdate(ItemType):
    id: int
    name: str = None


@route.put("")
def update(payload: ItemUpdate, user: User = Depends(authenticate)):
    # If brand_new is provided, create a new brand
    if payload.brand_new:
        new_brand = payload.brand_new.strip()
        existing_brand = db.session.query(Brand).filter(
            func.lower(payload.brand_new) == new_brand.lower()).first()

        if existing_brand:
            payload.brand_id = existing_brand.id
        else:
            brand = Brand(name=new_brand)
            db.session.add(brand)
            db.session.commit()
            db.session.refresh(brand)
            payload.brand_id = brand.id

    # If product_new is provided, create a new product and assign brand_id
    if payload.product_new and payload.brand_id:
        new_product = payload.product_new.strip()
        existing_product = db.session.query(Product).filter(
            func.lower(payload.product_new) == new_product.lower()).first()

        if existing_product:
            payload.product_id = existing_product.id
        else:
            product = Product(name=new_product, brand_id=payload.brand_id)
            db.session.add(product)
            db.session.commit()
            db.session.refresh(product)
            payload.product_id = product.id

    # If category_new is provided, create a new category
    if payload.category_new:
        new_category = payload.category_new.strip()
        existing_category = db.session.query(Category).filter(
            func.lower(payload.category_new) == new_category.lower()).first()

        if existing_category:
            payload.category_id = existing_category.id
        else:
            category = Category(name=new_category, user_id=user.id)
            db.session.add(category)
            db.session.commit()
            db.session.refresh(category)
            payload.category_id = category.id

    if payload.category_id:
        payload.category_id = get_or_create_item_category(
            db.session, payload.category_id, user.id)

    # Remove data-creation fields from dict
    fields = payload.dict()
    fields.pop("product_new")
    fields.pop("brand_new")
    fields.pop("category_new")

    item = db.session.query(Item).filter_by(
        id=payload.id, user_id=user.id).first()

    if not item:
        raise HTTPException(400, "Item not found.")

    for key, value in fields.items():
        setattr(item, key, value)

    try:
        db.session.commit()
        db.session.refresh(item)
    except Exception as e:
        print(e)
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
    except Exception as e:
        print(e)
        raise HTTPException(
            400, "An error occurred while updating item order.")

    return True


@route.put("/category/sort")
def sort_items(categories: SortItems, user: User = Depends(authenticate)):
    item_category_mappings = [dict(id=category.id, user_id=user.id, sort_order=category.sort_order)
                              for category in categories]

    try:
        db.session.bulk_update_mappings(ItemCategory, item_category_mappings)
        db.session.commit()
    except Exception as e:
        print(e)
        raise HTTPException(
            400, "An error occurred while updating category order.")

    return True


@route.get("s")
def fetch(user: User = Depends(authenticate)):
    items = db.session.query(Item).filter_by(
        user_id=user.id, removed=False).all()
    return items


@route.delete("/{item_id}")
def remove(item_id, user: User = Depends(authenticate)):
    item = db.session.query(Item).filter_by(
        id=item_id, user_id=user.id).first()
    item.removed = True

    db.session.commit()
    db.session.refresh(item)

    return item


@route.post("/import")
async def import_items(file: UploadFile = File(...), user: User = Depends(authenticate)):
    categories = db.session.query(Category).filter(
        or_(Category.user_id == user.id, Category.user_id == None)).all()
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
    for i, row in enumerate(rows):
        name = row.get("name")
        brand = row.get("manufacturer")
        product = row.get("product")
        category = row.get("category")
        weight = row.get("weight")
        unit = row.get("unit")
        product_url = row.get("url")
        price = row.get("price", None)
        notes = row.get("notes", None)

        if not name:
            continue

        if unit:
            unit = unit.lower().strip()
            if unit == 'gram' or unit == 'grams':
                unit = 'g'

            if unit == 'kilogram' or unit == 'kilograms':
                unit = 'kg'

            if unit == 'ounce' or unit == 'ounces':
                unit = 'oz'

            if unit == 'lbs' or unit == 'pound' or unit == 'pounds':
                unit = 'lb'

            if unit not in ['g', 'kg', 'oz', 'lb']:
                errors.append(generate_error(
                    i, 'Invalid unit. Must be one of: g, kg, oz, lb'))
                continue

        if weight:
            weight = float(weight)
        else:
            weight = None

        if price:
            price = float(price)
        else:
            price = None

        brand_id = None
        if brand:
            brand = brand.strip()
            brand_entity = db.session.query(
                Brand.id).filter(Brand.name == brand).first()
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
            product = product.strip()
            product_entity = db.session.query(Product.id).filter(
                Product.name == product, Product.brand_id == brand_id).first()
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
            category_id = next(
                (cat.id for cat in categories if cat.name == category), None)

            if category_id:
                item_category_entity = db.session.query(ItemCategory.id).filter(
                    ItemCategory.category_id == category_id, ItemCategory.user_id == user.id).first()

                if item_category_entity:
                    category_id = item_category_entity[0]

                else:
                    new_item_category = ItemCategory(
                        user_id=user.id, category_id=category_id)
                    try:
                        db.session.add(new_item_category)
                        db.session.commit()
                        db.session.refresh(new_item_category)
                        category_id = new_item_category.id
                    except Exception as e:
                        category_id = None
                        db.session.rollback()

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
                            consumable=False))

    if errors:
        return {'success': False, 'errors': errors, 'count': len(errors)}

    try:
        db.session.bulk_insert_mappings(Item, entries)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        raise HTTPException(
            400, 'An unexpected error occurred while importing items.')

    return {'success': True, 'errors': [], 'count': len(entries)}
