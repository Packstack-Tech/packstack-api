import datetime

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
def fetch_info(trip_id):
    trip = db.session.query(Trip).filter_by(uuid=trip_id).first()

    # fetch pack owner's info
    user = db.session.query(User.username,
                            User.unit_distance,
                            User.unit_temperature,
                            User.unit_weight).filter_by(id=trip.user_id).first()._asdict()

    if not trip:
        raise HTTPException(400, "Trip not found.")

    packs = db.session.query(Pack).filter_by(trip_id=trip.id).all()
    return {
        "trip": trip,
        "packs": packs,
        "user": user
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


@route.post("")
def create(payload: TripType, user: User = Depends(authenticate)):
    trip = payload.dict(exclude_none=True)
    condition_ids = trip.pop('condition_ids', None)
    geography_ids = trip.pop('geography_ids', None)
    new_trip = Trip(user_id=user.id, **trip)

    try:
        db.session.add(new_trip)
        db.session.commit()
        db.session.refresh(new_trip)
    except:
        raise HTTPException(400, "Unable to create trip.")

    if condition_ids:
        try:
            conditions = [dict(trip_id=new_trip.id, condition_id=id)
                          for id in condition_ids]
            db.session.bulk_insert_mappings(TripCondition, conditions)
            db.session.commit()
        except:
            pass

    if geography_ids:
        try:
            geographies = [dict(trip_id=new_trip.id, geography_id=id)
                           for id in geography_ids]
            db.session.bulk_insert_mappings(TripGeography, geographies)
            db.session.commit()
        except:
            pass

    db.session.refresh(new_trip)

    return new_trip


class TripUpdate(TripType):
    id: int


@route.put("")
def update(payload: TripUpdate, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=payload.id, user_id=user.id).first()

    if not trip:
        raise HTTPException(400, "trip not found.")

    fields = payload.dict(exclude_none=True)
    condition_ids = fields.pop('condition_ids', None)
    geography_ids = fields.pop('geography_ids', None)

    try:
        for key, value in fields.items():
            setattr(trip, key, value)

        db.session.commit()
        db.session.refresh(trip)
    except Exception as e:
        raise HTTPException(400, "An error occurred while updating trip.")

    if condition_ids is not None:
        for trip_condition in trip.conditions:
            db.session.delete(trip_condition)
            db.session.commit()

        try:
            conditions = [dict(trip_id=trip.id, condition_id=id)
                          for id in condition_ids]
            db.session.bulk_insert_mappings(TripCondition, conditions)
        except:
            pass

    if geography_ids is not None:
        for trip_geography in trip.geographies:
            db.session.delete(trip_geography)
            db.session.commit()

        try:
            geographies = [dict(trip_id=trip.id, geography_id=id)
                           for id in geography_ids]
            db.session.bulk_insert_mappings(TripGeography, geographies)
        except:
            pass

    try:
        db.session.commit()
        db.session.refresh(trip)
    except:
        pass

    return trip


@route.post("/{trip_id}/clone")
def clone(trip_id, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=trip_id, user_id=user.id).first()

    if not trip:
        raise HTTPException(400, "Trip not found.")

    cloned_trip_data = clone_model(trip, ['title', 'location', 'created_at'])
    cloned_trip = Trip(
        **cloned_trip_data,
        title=f"{trip.title} (Copy)",
        location=f"{trip.location} (Copy)",
        created_at=datetime.datetime.utcnow()
    )

    try:
        db.session.add(cloned_trip)
        db.session.commit()
        db.session.refresh(cloned_trip)
    except:
        raise HTTPException(400, "An error occurred while cloning trip.")

    packs = db.session.query(Pack).filter_by(trip_id=trip.id).all()
    for pack in packs:
        cloned_pack_data = clone_model(pack, ['trip_id'])
        cloned_pack = Pack(**cloned_pack_data, trip_id=cloned_trip.id)
        db.session.add(cloned_pack)
        db.session.commit()
        db.session.refresh(cloned_pack)

        for item in pack.items:
            cloned_item_data = clone_model(item)
            cloned_item = PackItem(
                **cloned_item_data,
                pack_id=cloned_pack.id,
                item_id=item.item_id
            )
            db.session.add(cloned_item)
            db.session.commit()
            db.session.refresh(cloned_item)

    db.session.refresh(cloned_trip)

    return cloned_trip


@route.post("/{trip_id}/upload-image")
def upload_image(trip_id, file: UploadFile = File(...), user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(id=trip_id).first()
    sort_order = len(trip.images)
    trip_image = Image(user_id=user.id,
                       trip_id=trip_id,
                       sort_order=sort_order)

    # save thumbnail & resized version
    temp_original = BytesIO()
    temp_thumb = BytesIO()

    img = PILImage.open(file.file)
    img = ImageOps.exif_transpose(img)
    img_format = 'PNG'
    content_type = PILImage.MIME[img_format]

    thumb = img.copy()

    img.thumbnail([1000, 1000], PILImage.ANTIALIAS)
    thumb.thumbnail([250, 250], PILImage.ANTIALIAS)

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
    except Exception as e:
        print(e)
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
def sort_images(trip_id, photos: SortTripPhotos, user: User = Depends(authenticate)):
    photo_mappings = [dict(id=photo.id, sort_order=photo.sort_order)
                      for photo in photos]

    try:
        db.session.bulk_update_mappings(Image, photo_mappings)
        trip = db.session.query(Trip).filter_by(
            id=trip_id, user_id=user.id).first()
        db.session.commit()
        db.session.refresh(trip)
    except:
        raise HTTPException(
            400, "An error occurred while updating photo order.")

    return trip.images


@route.delete("/{trip_id}")
def remove_trip(trip_id, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=trip_id, user_id=user.id).first()

    if not trip:
        raise HTTPException(400, "Permission denied.")

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
    except:
        raise HTTPException(400, "An error occurred while deleting trip.")

    return True


class UpdateImage(BaseModel):
    caption: str = None


@route.put("/{trip_id}/image/{id}")
def update_image(trip_id, id, payload: UpdateImage, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=trip_id, user_id=user.id).first()

    if not trip:
        raise HTTPException(400, "Permission denied.")

    image = db.session.query(Image).filter_by(id=id).first()

    if not image:
        raise HTTPException(400, "Image not found.")

    try:
        image.caption = payload.caption
        db.session.commit()
        db.session.refresh(image)
    except:
        raise HTTPException(400, "An error occurred saving image.")

    return image


@route.put("/{trip_id}/publish")
def toggle_publish(trip_id, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=trip_id, user_id=user.id).first()

    if not trip:
        raise HTTPException(400, "Permission denied.")

    trip.published = not trip.published
    try:
        db.session.commit()
        db.session.refresh(trip)
    except:
        raise HTTPException(400, "An error occurred.")

    return trip


@route.delete("/{trip_id}/image/{id}")
def remove_image(trip_id, id, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=trip_id, user_id=user.id).first()

    if not trip:
        raise HTTPException(400, "Permission denied.")

    image = db.session.query(Image).filter_by(id=id).first()
    s3_file_delete(image.s3_key)
    s3_file_delete(image.s3_key_thumb)

    db.session.delete(image)
    db.session.commit()

    return {
        "trip_id": trip_id,
        "image_id": id
    }


@route.get("/{trip_id}")
def fetch_one(trip_id):
    trip = db.session.query(Trip).options(
        joinedload(Trip.user)).filter_by(id=trip_id).first()
    return trip


@route.get("s")
def fetch_all(user: User = Depends(authenticate)):
    trips = db.session.query(Trip).filter_by(
        user_id=user.id, removed=False).order_by(Trip.end_date.desc()).all()

    return trips
