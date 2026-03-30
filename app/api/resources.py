import csv
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from models.base import Brand, CatalogProduct, Product, User, Category, ProductVariant
from utils.auth import authenticate
from utils.consts import DEVELOPMENT
from seed.categories import default_categories

logger = logging.getLogger(__name__)

route = APIRouter()


class CreateBrand(BaseModel):
    name: str


@route.post("/brand", status_code=201)
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
def fetch_brand_detail(brand_id: int):
    brand = db.session.query(Brand).options(joinedload(
        Brand.products)).filter_by(id=brand_id).first()
    return brand


@route.get("/product/search/{brand_id}/{search_str}")
def search_products(brand_id: int, search_str: str, user: User = Depends(authenticate)):
    search = "%{}%".format(search_str.strip())
    products = db.session.query(Product).filter(
        Product.brand_id == brand_id, Product.name.ilike(search)).all()
    return products


@route.get("/product/variants/{product_id}")
def get_product_variants(product_id: int, user: User = Depends(authenticate)):
    variants = db.session.query(ProductVariant).filter_by(
        product_id=product_id).all()

    return variants


class CreateProduct(BaseModel):
    name: str
    brand_id: int = None


@route.post("/product", status_code=201)
def create_product(payload: CreateProduct, user: User = Depends(authenticate)):
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


@route.get("/catalog/search")
def catalog_search(
    q: str = "",
    brand: Optional[str] = Query(None),
    product: Optional[str] = Query(None),
):
    base = db.session.query(CatalogProduct).filter(
        CatalogProduct.status == "approved")

    if brand is not None and product is not None:
        entries = base.filter(
            CatalogProduct.brand_name == brand,
            CatalogProduct.product_name == product,
        ).order_by(CatalogProduct.variant_name).all()

        return [{
            "id": e.id,
            "brand_id": e.brand_id,
            "product_id": e.product_id,
            "product_variant_id": e.product_variant_id,
            "variant_name": e.variant_name,
            "weight": float(e.weight) if e.weight else None,
            "weight_unit": e.weight_unit,
            "product_url": e.product_url,
            "category_suggestion": e.category_suggestion,
        } for e in entries]

    if brand is not None:
        query = base.filter(CatalogProduct.brand_name == brand)
        if q:
            query = query.filter(CatalogProduct.product_name.ilike(f"%{q}%"))

        rows = (
            query
            .with_entities(
                func.min(CatalogProduct.product_id).label("product_id"),
                CatalogProduct.product_name,
            )
            .group_by(CatalogProduct.product_name)
            .order_by(CatalogProduct.product_name)
            .limit(50)
            .all()
        )
        return [{
            "product_id": r.product_id,
            "product_name": r.product_name,
        } for r in rows]

    query = base
    if q:
        query = query.filter(CatalogProduct.brand_name.ilike(f"%{q}%"))

    rows = (
        query
        .with_entities(
            func.min(CatalogProduct.brand_id).label("brand_id"),
            CatalogProduct.brand_name,
        )
        .group_by(CatalogProduct.brand_name)
        .order_by(CatalogProduct.brand_name)
        .limit(20)
        .all()
    )
    return [{
        "brand_id": r.brand_id,
        "brand_name": r.brand_name,
    } for r in rows]


@route.get("/brand/search/{query}")
def search_brands(query: str, user: User = Depends(authenticate)):
    search = "%{}%".format(query.strip())
    brands = db.session.query(Brand).filter(Brand.name.ilike(
        search), Brand.removed.is_(False)).limit(10).all()

    return brands


@route.get("/seed")
def seed_data():
    if not DEVELOPMENT:
        raise HTTPException(403, "Seed endpoint is only available in development.")

    with open('app/seed/brands.csv', newline='') as csvfile:
        reader = csv.reader(csvfile)
        for row in reader:
            brand = Brand(name=row[0])
            try:
                db.session.add(brand)
                db.session.commit()
            except IntegrityError:
                db.session.rollback()

    for category in default_categories():
        cat = db.session.query(Category).filter_by(name=category).first()
        if not cat:
            seed_category = Category(name=category)
            try:
                db.session.add(seed_category)
                db.session.commit()
            except Exception:
                logger.exception("Failed to seed category: %s", category)
                db.session.rollback()

    return
