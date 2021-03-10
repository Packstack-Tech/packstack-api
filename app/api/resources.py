from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel

from models.base import Brand, Condition, Geography, Product, User
from models.enums import WeightUnit, Currency, Plan, UnitSystem, Month
from utils.utils import enum_to_dict
from utils.auth import authenticate

route = APIRouter()


@route.get("")
def fetch():
    conditions = db.session.query(Condition).all()
    geographies = db.session.query(Geography).all()

    return {
        "conditions": conditions,
        "currencies": enum_to_dict(Currency),
        "geographies": geographies,
        "plans": enum_to_dict(Plan),
        "months": enum_to_dict(Month),
        "unitSystem": enum_to_dict(UnitSystem),
        "weightUnits": enum_to_dict(WeightUnit)
    }


class CreateBrand(BaseModel):
    name: str


@route.post("/brand")
def create_brand(payload: CreateBrand, user: User = Depends(authenticate)):
    if len(payload.name) <= 1:
        raise HTTPException(400, 'Brand name must be longer')

    try:
        new_brand = Brand(name=payload.name)
        db.session.add(new_brand)
        db.session.commit()
        db.session.refresh(new_brand)
    except Exception:
        raise HTTPException(400, 'An error occurred while creating brand.')

    return new_brand


@route.get("/brands")
def fetch_brands():
    brands = db.session.query(Brand).filter_by(removed=False).all()
    return brands


# get brand
@route.get("/brand/{brand_id}")
def fetch_brands(brand_id):
    brand = db.session.query(Brand).filter_by(id=brand_id).first()
    return brand


class CreateProduct(BaseModel):
    name: str
    brand_id: int = None


@route.post("/product")
def create_brand(payload: CreateProduct, user: User = Depends(authenticate)):
    if len(payload.name) <= 1:
        raise HTTPException(400, 'Product name must be longer')

    try:
        new_product = Product(name=payload.name, brand_id=payload.brand_id)
        db.session.add(new_product)
        db.session.commit()
        db.session.refresh(new_product)
    except Exception:
        raise HTTPException(400, 'An error occurred while creating product.')

    return new_product
