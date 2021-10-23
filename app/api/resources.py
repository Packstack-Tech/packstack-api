import csv

from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from models.base import Brand, Condition, Geography, Product, User
from utils.auth import authenticate
from utils.digital_ocean import s3_client

route = APIRouter()


@route.get("")
def fetch():
    conditions = db.session.query(Condition).all()
    geographies = db.session.query(Geography).all()

    return {
        "conditions": conditions,
        "geographies": geographies,
    }


@route.get("/buckets")
def fetch_buckets():
    # List all buckets on your account.
    response = s3_client.list_buckets()
    spaces = [space['Name'] for space in response['Buckets']]
    print("Spaces List: %s" % spaces)


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


@route.get("/brand/{brand_id}")
def fetch_brand(brand_id):
    brand = db.session.query(Brand).options(joinedload(
        Brand.products)).filter_by(id=brand_id).first()
    return brand


@route.get("/brand/search/{search_str}")
def search_brands(search_str, user: User = Depends(authenticate)):
    search = "%{}%".format(search_str.strip())
    brands = db.session.query(Brand).filter(Brand.name.ilike(search)).all()
    return brands


@route.get("/product/search/{brand_id}/{search_str}")
def search_products(brand_id, search_str, user: User = Depends(authenticate)):
    search = "%{}%".format(search_str.strip())
    products = db.session.query(Product).filter(
        Product.brand_id == brand_id, Product.name.ilike(search)).all()
    return products


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


# DEVELOPMENT :: SEED DATABASE
@route.get("/seed")
def seed_data():
    with open('seed/brands.csv', newline='') as csvfile:
        reader = csv.reader(csvfile)
        for row in reader:
            brand = Brand(name=row[0])
            try:
                db.session.add(brand)
                db.session.commit()
            except IntegrityError as e:
                db.session.rollback()

    return
