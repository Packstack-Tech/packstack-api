from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from sqlalchemy import or_

from models.base import User, Category, Item
from utils.auth import authenticate

route = APIRouter()


class CategoryType(BaseModel):
    name: str
    consumable: bool = None


@route.post("")
def create(payload: CategoryType, user: User = Depends(authenticate)):
    new_category = Category(user_id=user.id, **payload.dict())

    try:
        db.session.add(new_category)
        db.session.commit()
        db.session.refresh(new_category)
    except:
        raise HTTPException(400, "Unable to create category.")

    return new_category


@route.get("")
def fetch(user: User = Depends(authenticate)):
    return db.session.query(Category).filter(or_(Category.user_id == user.id, Category.user_id == None)).all()


class CategoryUpdateType(BaseModel):
    name: str = None
    consumable: bool = None


@route.put("/{category_id}")
def update(category_id, payload: CategoryUpdateType, user: User = Depends(authenticate)):
    category = db.session.query(Category).filter_by(id=category_id, user_id=user.id).first()
    if not category:
        raise HTTPException(400, "Category does not exist.")

    fields = payload.dict(exclude_none=True)
    for key, value in fields.items():
        setattr(category, key, value)

    try:
        db.session.commit()
        db.session.refresh(category)
    except Exception:
        raise HTTPException(400, "Unable to update category.")

    return category


@route.delete("/{category_id}")
def delete(category_id, user: User = Depends(authenticate)):
    category = db.session.query(Category).filter_by(id=category_id, user_id=user.id).first()
    if not category:
        raise HTTPException(400, "Category does not exist.")

    # Reassign associated items
    category_items = db.session.query(Item).filter_by(category_id=category.id, user_id=user.id).all()
    for item in category_items:
        item.category_id = None

    try:
        db.session.commit()
    except Exception:
        raise HTTPException(400, "Unable to reassign existing items.")

    try:
        db.session.delete(category)
        db.session.commit()
    except Exception:
        raise HTTPException(400, "Unable to delete category.")

    return True
