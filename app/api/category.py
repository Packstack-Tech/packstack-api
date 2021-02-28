from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from sqlalchemy import or_

from models.base import User, Category
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
def categories(user: User = Depends(authenticate)):
    return db.session.query(Category).filter(or_(Category.user_id == user.id, Category.user_id == None)).all()
