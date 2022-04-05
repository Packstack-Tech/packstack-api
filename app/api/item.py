from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import List

from models.base import User, Item, ItemCategory
from utils.auth import authenticate
from utils.item_category import get_or_create_item_category

route = APIRouter()


class ItemType(BaseModel):
    name: str
    brand_id: int = None
    product_id: int = None
    category_id: int = None
    weight: float = None
    unit: str = None
    price: float = None
    consumable: bool = False
    product_url: str = None
    wishlist: bool = None
    notes: str = None


@route.post("")
def create(payload: ItemType, user: User = Depends(authenticate)):
    item_category_id = get_or_create_item_category(
        db.session, payload.category_id, user.id)
    payload.category_id = item_category_id

    new_item = Item(user_id=user.id, **payload.dict())

    try:
        db.session.add(new_item)
        db.session.commit()
        db.session.refresh(new_item)
    except:
        raise HTTPException(400, "Unable to create item.")

    return new_item


class ItemUpdate(ItemType):
    id: int
    name: str = None


@route.put("")
def update(payload: ItemUpdate, user: User = Depends(authenticate)):
    item_category_id = get_or_create_item_category(
        db.session, payload.category_id, user.id)
    payload.category_id = item_category_id

    item = db.session.query(Item).filter_by(
        id=payload.id, user_id=user.id).first()

    if not item:
        raise HTTPException(400, "Item not found.")

    fields = payload.dict()
    for key, value in fields.items():
        setattr(item, key, value)

    try:
        db.session.commit()
        db.session.refresh(item)
    except Exception as e:
        print(e)

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
    items = db.session.query(Item).filter_by(user_id=user.id).all()
    return items


@route.delete("/{item_id}")
def remove(item_id, user: User = Depends(authenticate)):
    item = db.session.query(Item).filter_by(
        id=item_id, user_id=user.id).first()
    item.removed = True

    db.session.commit()
    db.session.refresh(item)

    return item
