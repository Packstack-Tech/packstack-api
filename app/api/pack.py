from io import BytesIO
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import List
from sqlalchemy.orm import joinedload
from PIL import Image as PILImage, ImageOps

from models.base import User, Pack, PackItem, Image, PackGeography, PackCondition
from utils.auth import authenticate
from utils.digital_ocean import s3_file_upload

route = APIRouter()


class PackType(BaseModel):
    region: str
    trail_name: str = None
    start_date: str = None
    end_date: str = None
    temp_min: float = None
    temp_max: float = None
    distance: float = None
    notes: str = None
    public: bool = None
    removed: bool = None

    condition_ids: List[int] = None
    geography_ids: List[int] = None


@route.post("")
def create(payload: PackType, user: User = Depends(authenticate)):
    pack = payload.dict(exclude_none=True)
    condition_ids = pack.pop('condition_ids', None)
    geography_ids = pack.pop('geography_ids', None)
    new_pack = Pack(user_id=user.id, **pack)

    try:
        db.session.add(new_pack)
        db.session.commit()
        db.session.refresh(new_pack)
    except:
        raise HTTPException(400, "Unable to create pack.")

    if condition_ids:
        try:
            conditions = [dict(pack_id=new_pack.id, condition_id=id)
                          for id in condition_ids]
            db.session.bulk_insert_mappings(PackCondition, conditions)
            db.session.commit()
        except:
            pass

    if geography_ids:
        try:
            geographies = [dict(pack_id=new_pack.id, geography_id=id)
                           for id in geography_ids]
            db.session.bulk_insert_mappings(PackGeography, geographies)
            db.session.commit()
        except:
            pass

    db.session.refresh(new_pack)

    return new_pack


class PackUpdate(PackType):
    id: int


@route.put("")
def update(payload: PackUpdate, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(
        id=payload.id, user_id=user.id).first()

    if not pack:
        raise HTTPException(400, "Pack not found.")

    fields = payload.dict(exclude_none=True)
    condition_ids = fields.pop('condition_ids', None)
    geography_ids = fields.pop('geography_ids', None)

    try:
        for key, value in fields.items():
            setattr(pack, key, value)

        db.session.commit()
        db.session.refresh(pack)
    except Exception as e:
        raise HTTPException(400, "An error occurred while updating pack.")

    if condition_ids is not None:
        for pack_condition in pack.conditions:
            db.session.delete(pack_condition)
            db.session.commit()

        try:
            conditions = [dict(pack_id=pack.id, condition_id=id)
                          for id in condition_ids]
            db.session.bulk_insert_mappings(PackCondition, conditions)
        except:
            pass

    if geography_ids is not None:
        for pack_geography in pack.geographies:
            db.session.delete(pack_geography)
            db.session.commit()

        try:
            geographies = [dict(pack_id=pack.id, geography_id=id)
                           for id in geography_ids]
            db.session.bulk_insert_mappings(PackGeography, geographies)
        except:
            pass

    try:
        db.session.commit()
        db.session.refresh(pack)
    except:
        pass

    return pack


@route.post("/{pack_id}/upload-image")
def upload_image(pack_id, file: UploadFile = File(...), user: User = Depends(authenticate)):
    pack_image = Image(user_id=user.id, pack_id=pack_id)

    # save thumbnail & resized version
    temp_original = BytesIO()
    temp_thumb = BytesIO()

    img = PILImage.open(file.file)
    img = ImageOps.exif_transpose(img)
    img_format = 'PNG'
    content_type = PILImage.MIME[img_format]

    thumb = img.copy()

    img.thumbnail([1200, 1200], PILImage.ANTIALIAS)
    thumb.thumbnail([300, 300], PILImage.ANTIALIAS)

    img.save(temp_original, format=img_format, quality=60)
    thumb.save(temp_thumb, format=img_format, quality=75)
    temp_original.seek(0)
    temp_thumb.seek(0)

    try:
        db.session.add(pack_image)
        db.session.commit()
        pack_image.s3 = {'extension': '.png', 'entity': 'pack'}
        db.session.commit()
        db.session.refresh(pack_image)
    except Exception as e:
        print(e)
        raise HTTPException(
            400, "An error occurred while creating image metadata.")

    upload_success = s3_file_upload(
        temp_original, content_type=content_type, key=pack_image.s3_key)

    upload_thumb_success = s3_file_upload(
        temp_thumb, content_type=content_type, key=pack_image.s3_key_thumb)

    if not upload_success or not upload_thumb_success:
        db.session.delete(pack_image)
        db.session.commit()
        raise HTTPException(400, "An error occurred while saving image.")

    return pack_image


@route.get("/{pack_id}")
def fetch_one(pack_id):
    pack = db.session.query(Pack).options(
        joinedload(Pack.items), joinedload(Pack.user)).filter_by(id=pack_id).first()
    pack.categories = pack.items_by_category
    return pack


@route.get("s")
def fetch_all(user: User = Depends(authenticate)):
    packs = db.session.query(Pack).filter_by(user_id=user.id).all()
    return packs


class PackItemType(BaseModel):
    item_id: int
    quantity: int = None
    worn: bool = None


class AssocItems(BaseModel):
    items: List[PackItemType]


@route.post("/{pack_id}/items")
def add_items(pack_id, payload: AssocItems, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(
        id=pack_id, user_id=user.id).first()

    if not pack:
        raise HTTPException(
            400, "An error occurred while retrieving the pack.")

    # Remove existing pack items
    pack_items = db.session.query(PackItem).filter_by(pack_id=pack.id).all()
    for item in pack_items:
        db.session.delete(item)

    try:
        db.session.commit()
    except Exception:
        raise HTTPException(
            400, "An error occurred while saving pack associations.")

    # Create items in payload
    for item in payload.items:
        assoc_item = PackItem(pack_id=pack.id, **item.dict(exclude_none=True))
        db.session.add(assoc_item)

    try:
        db.session.commit()
        db.session.refresh(pack)
    except Exception as e:
        raise HTTPException(
            400, "An error occurred while adding pack associations.")

    return pack


@route.delete("/{pack_id}")
def remove(pack_id, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(
        id=pack_id, user_id=user.id).first()
    pack.removed = True

    db.session.commit()
    db.session.refresh(pack)

    return pack
