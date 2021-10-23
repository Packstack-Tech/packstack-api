from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel

from models.base import User, Item
from utils.auth import authenticate

route = APIRouter()


class ItemType(BaseModel):
    name: str
    brand_id: int = None
    product_id: int = None
    category_id: int = None
    weight: float = 0.0
    unit: str = None
    price: float = 0.0
    consumable: bool = False
    product_url: str = None
    wishlist: bool = None
    notes: str = None


@route.post("")
def create(payload: ItemType, user: User = Depends(authenticate)):
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
    item = db.session.query(Item).filter_by(id=payload.id, user_id=user.id).first()

    if not item:
        raise HTTPException(400, "Item not found.")

    fields = payload.dict(exclude_none=True)
    for key, value in fields.items():
        setattr(item, key, value)

    try:
        db.session.commit()
        db.session.refresh(item)
    except Exception as e:
        print(e)

    return item


@route.get("s")
def fetch(user: User = Depends(authenticate)):
    items = db.session.query(Item).filter_by(user_id=user.id).all()
    return items


@route.delete("/{item_id}")
def remove(item_id, user: User = Depends(authenticate)):
    item = db.session.query(Item).filter_by(id=item_id, user_id=user.id).first()
    item.removed = True

    db.session.commit()
    db.session.refresh(item)

    return item

