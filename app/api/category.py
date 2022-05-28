from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from sqlalchemy import or_

from models.base import User, Category, Item, ItemCategory
from utils.auth import authenticate

route = APIRouter()


class CategoryType(BaseModel):
    name: str
    consumable: bool = False


@route.post("")
def create(payload: CategoryType, user: User = Depends(authenticate)):
    new_category = Category(user_id=user.id, name=payload.name)

    try:
        db.session.add(new_category)
        db.session.commit()
        db.session.refresh(new_category)
    except:
        raise HTTPException(400, "Unable to create category.")

    position = db.session.query(
        ItemCategory).filter_by(user_id=user.id).count()
    item_category = ItemCategory(
        category_id=new_category.id, user_id=user.id, sort_order=position)

    try:
        db.session.add(item_category)
        db.session.commit()
        db.session.refresh(item_category)
    except:
        raise HTTPException(400, "Unable to create item category.")

    return item_category


@route.get("")
def fetch(user: User = Depends(authenticate)):
    return db.session.query(Category).filter(or_(Category.user_id == user.id, Category.user_id == None)).all()


class CategoryUpdateType(BaseModel):
    name: str = None


@route.put("/{category_id}")
def update(category_id, payload: CategoryUpdateType, user: User = Depends(authenticate)):
    category = db.session.query(Category).filter_by(
        id=category_id, user_id=user.id).first()
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
    category = db.session.query(Category).filter_by(
        id=category_id, user_id=user.id).first()
    if not category:
        raise HTTPException(400, "Category does not exist.")

    # Uncategorize associated items
    item_category = db.session.query(ItemCategory).filter_by(
        category_id=category.id, user_id=user.id).first()

    if item_category:
        items = db.session.query(Item).filter_by(category_id=item_category, user_id=user.id).all()
        for item in items:
            item.category_id = None

        try:
            db.session.delete(item_category)
            db.session.delete(category)
            db.session.commit()
        except Exception:
            raise HTTPException(400, "Unable to delete category.")

    return True
