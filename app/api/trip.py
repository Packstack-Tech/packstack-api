import datetime
import logging
import uuid as uuid_module

from io import BytesIO
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import List
from sqlalchemy.orm import joinedload
from PIL import Image as PILImage, ImageOps

from models.base import User, Trip, Image, TripGeography, TripCondition, Pack, PackItem
from utils.auth import authenticate
from utils.digital_ocean import s3_file_upload, s3_file_delete
from utils.utils import clone_model

logger = logging.getLogger(__name__)

route = APIRouter()


@route.get("")
def fetch():
    now = datetime.datetime.utcnow()
    trips = db.session.query(Trip).filter(
        Trip.end_date != None,
        Trip.end_date <= now,
        Trip.removed == False,
        Trip.published == True
    ).order_by(Trip.end_date.desc()).limit(35).all()

    return trips


@route.get("/info/{trip_id}")
def fetch_info(trip_id: str):
    try:
        uuid_val = uuid_module.UUID(trip_id)
        trip = db.session.query(Trip).filter_by(uuid=uuid_val).first()
    except ValueError:
        try:
            trip = db.session.query(Trip).filter_by(id=int(trip_id)).first()
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid trip identifier.")

    if not trip:
        raise HTTPException(404, "Trip not found.")

    user = db.session.query(User.username,
                            User.unit_distance,
                            User.unit_temperature,
                            User.unit_weight).filter_by(id=trip.user_id).first()

    if not user:
        raise HTTPException(404, "Trip owner not found.")

    packs = db.session.query(Pack).filter_by(trip_id=trip.id).all()
    return {
        "trip": trip,
        "packs": packs,
        "user": user._asdict()
    }


@route.get("/sitemap")
def get_sitemap():
    trips = db.session.query(Trip.id, Trip.title, Trip.updated_at).filter_by(
        removed=False, published=True).all()

    data = [{
        'id': trip.id,
        'title': trip.title,
        'updated_at': trip.updated_at
    } for trip in trips]

    return data


class TripType(BaseModel):
    title: str
    location: str = None
    start_date: str = None
    end_date: str = None
    temp_min: float = None
    temp_max: float = None
    distance: float = None
    notes: str = None
    published: bool = None
    removed: bool = None

    condition_ids: List[int] = None
    geography_ids: List[int] = None


@route.post("", status_code=201)
def create(payload: TripType, user: User = Depends(authenticate)):
    trip = payload.dict(exclude_none=True)
    condition_ids = trip.pop('condition_ids', None)
    geography_ids = trip.pop('geography_ids', None)
    new_trip = Trip(user_id=user.id, **trip)

    try:
        db.session.add(new_trip)
        db.session.commit()
        db.session.refresh(new_trip)
    except Exception:
        raise HTTPException(400, "Unable to create trip.")

    if condition_ids:
        try:
            conditions = [dict(trip_id=new_trip.id, condition_id=id)
                          for id in condition_ids]
            db.session.bulk_insert_mappings(TripCondition, conditions)
            db.session.commit()
        except Exception:
            logger.exception("Failed to insert trip conditions")

    if geography_ids:
        try:
            geographies = [dict(trip_id=new_trip.id, geography_id=id)
                           for id in geography_ids]
            db.session.bulk_insert_mappings(TripGeography, geographies)
            db.session.commit()
        except Exception:
            logger.exception("Failed to insert trip geographies")

    db.session.refresh(new_trip)

    return new_trip


class TripUpdate(TripType):
    id: int


@route.put("")
def update(payload: TripUpdate, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=payload.id, user_id=user.id).first()

    if not trip:
        raise HTTPException(404, "Trip not found.")

    fields = payload.dict(exclude_none=True)
    condition_ids = fields.pop('condition_ids', None)
    geography_ids = fields.pop('geography_ids', None)

    try:
        for key, value in fields.items():
            setattr(trip, key, value)

        db.session.commit()
        db.session.refresh(trip)
    except Exception:
        raise HTTPException(400, "An error occurred while updating trip.")

    if condition_ids is not None:
        db.session.query(TripCondition).filter_by(trip_id=trip.id).delete()
        try:
            conditions = [dict(trip_id=trip.id, condition_id=id)
                          for id in condition_ids]
            db.session.bulk_insert_mappings(TripCondition, conditions)
        except Exception:
            logger.exception("Failed to update trip conditions")

    if geography_ids is not None:
        db.session.query(TripGeography).filter_by(trip_id=trip.id).delete()
        try:
            geographies = [dict(trip_id=trip.id, geography_id=id)
                           for id in geography_ids]
            db.session.bulk_insert_mappings(TripGeography, geographies)
        except Exception:
            logger.exception("Failed to update trip geographies")

    try:
        db.session.commit()
        db.session.refresh(trip)
    except Exception:
        logger.exception("Failed to commit trip update")

    return trip


@route.post("/{trip_id}/clone", status_code=201)
def clone(trip_id: int, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=trip_id, user_id=user.id).first()

    if not trip:
        raise HTTPException(404, "Trip not found.")

    cloned_trip_data = clone_model(trip, ['title', 'location', 'created_at'])
    cloned_trip = Trip(
        **cloned_trip_data,
        title=f"{trip.title} (Copy)",
        location=f"{trip.location} (Copy)",
        created_at=datetime.datetime.utcnow()
    )

    try:
        db.session.add(cloned_trip)
        db.session.flush()

        packs = db.session.query(Pack).filter_by(trip_id=trip.id).all()
        for pack in packs:
            cloned_pack_data = clone_model(pack, ['trip_id'])
            cloned_pack = Pack(**cloned_pack_data, trip_id=cloned_trip.id)
            db.session.add(cloned_pack)
            db.session.flush()

            for item in pack.items:
                cloned_item_data = clone_model(item)
                cloned_item = PackItem(
                    **cloned_item_data,
                    pack_id=cloned_pack.id,
                    item_id=item.item_id
                )
                db.session.add(cloned_item)

        db.session.commit()
        db.session.refresh(cloned_trip)
    except Exception:
        db.session.rollback()
        raise HTTPException(400, "An error occurred while cloning trip.")

    return cloned_trip


@route.post("/{trip_id}/upload-image", status_code=201)
def upload_image(trip_id: int, file: UploadFile = File(...), user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(id=trip_id).first()
    if not trip:
        raise HTTPException(404, "Trip not found.")

    sort_order = len(trip.images)
    trip_image = Image(user_id=user.id,
                       trip_id=trip_id,
                       sort_order=sort_order)

    temp_original = BytesIO()
    temp_thumb = BytesIO()

    img = PILImage.open(file.file)
    img = ImageOps.exif_transpose(img)
    img_format = 'PNG'
    content_type = PILImage.MIME[img_format]

    thumb = img.copy()

    img.thumbnail([1000, 1000], PILImage.LANCZOS)
    thumb.thumbnail([250, 250], PILImage.LANCZOS)

    img.save(temp_original, format=img_format, quality=65, optimize=True)
    thumb.save(temp_thumb, format=img_format, quality=85, optimize=True)
    temp_original.seek(0)
    temp_thumb.seek(0)

    try:
        db.session.add(trip_image)
        db.session.commit()
        trip_image.s3 = {'extension': '.png', 'entity': 'trip'}
        db.session.commit()
        db.session.refresh(trip_image)
    except Exception:
        logger.exception("Failed to create trip image metadata")
        raise HTTPException(
            400, "An error occurred while creating image metadata.")

    upload_success = s3_file_upload(
        temp_original, content_type=content_type, key=trip_image.s3_key)

    upload_thumb_success = s3_file_upload(
        temp_thumb, content_type=content_type, key=trip_image.s3_key_thumb)

    if not upload_success or not upload_thumb_success:
        db.session.delete(trip_image)
        db.session.commit()
        raise HTTPException(400, "An error occurred while saving image.")

    return trip_image


class PhotoOrder(BaseModel):
    id: int
    sort_order: int


class SortTripPhotos(BaseModel):
    __root__: List[PhotoOrder]

    def __iter__(self):
        return iter(self.__root__)


@route.post("/{trip_id}/sort-photos")
def sort_images(trip_id: int, photos: SortTripPhotos, user: User = Depends(authenticate)):
    photo_mappings = [dict(id=photo.id, sort_order=photo.sort_order)
                      for photo in photos]

    try:
        db.session.bulk_update_mappings(Image, photo_mappings)
        trip = db.session.query(Trip).filter_by(
            id=trip_id, user_id=user.id).first()
        db.session.commit()
        db.session.refresh(trip)
    except Exception:
        raise HTTPException(
            400, "An error occurred while updating photo order.")

    return trip.images


@route.delete("/{trip_id}", status_code=204)
def remove_trip(trip_id: int, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=trip_id, user_id=user.id).first()

    if not trip:
        raise HTTPException(403, "Permission denied.")

    for image in trip.images:
        s3_file_delete(image.s3_key)
        s3_file_delete(image.s3_key_thumb)
        db.session.delete(image)

    linked_packs = db.session.query(Pack).filter_by(
        user_id=user.id, trip_id=trip.id).all()
    for pack in linked_packs:
        pack.trip_id = None

    try:
        trip.removed = True
        db.session.commit()
    except Exception:
        raise HTTPException(400, "An error occurred while deleting trip.")


class UpdateImage(BaseModel):
    caption: str = None


@route.put("/{trip_id}/image/{id}")
def update_image(trip_id: int, id: int, payload: UpdateImage, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=trip_id, user_id=user.id).first()

    if not trip:
        raise HTTPException(403, "Permission denied.")

    image = db.session.query(Image).filter_by(id=id).first()

    if not image:
        raise HTTPException(404, "Image not found.")

    try:
        image.caption = payload.caption
        db.session.commit()
        db.session.refresh(image)
    except Exception:
        raise HTTPException(400, "An error occurred saving image.")

    return image


@route.put("/{trip_id}/publish")
def toggle_publish(trip_id: int, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=trip_id, user_id=user.id).first()

    if not trip:
        raise HTTPException(403, "Permission denied.")

    trip.published = not trip.published
    try:
        db.session.commit()
        db.session.refresh(trip)
    except Exception:
        raise HTTPException(400, "An error occurred.")

    return trip


@route.delete("/{trip_id}/image/{id}", status_code=204)
def remove_image(trip_id: int, id: int, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=trip_id, user_id=user.id).first()

    if not trip:
        raise HTTPException(403, "Permission denied.")

    image = db.session.query(Image).filter_by(id=id).first()
    if not image:
        raise HTTPException(404, "Image not found.")

    s3_file_delete(image.s3_key)
    s3_file_delete(image.s3_key_thumb)

    db.session.delete(image)
    db.session.commit()


@route.get("/{trip_id}")
def fetch_one(trip_id: int):
    trip = db.session.query(Trip).options(
        joinedload(Trip.user)).filter_by(id=trip_id).first()
    return trip


@route.get("s")
def fetch_all(user: User = Depends(authenticate), limit: int = 100, offset: int = 0):
    trips = db.session.query(Trip).filter_by(
        user_id=user.id, removed=False
    ).order_by(Trip.end_date.desc()).offset(offset).limit(limit).all()

    return trips
