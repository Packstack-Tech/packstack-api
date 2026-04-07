import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import List, Optional

from models.base import User, Pack, PackItem, Trip
from utils.auth import authenticate
from utils.pack_summary import serialize_pack, serialize_pack_public

logger = logging.getLogger(__name__)

route = APIRouter()


@route.get("s")
def get_user_packs(user: User = Depends(authenticate), limit: int = 100, offset: int = 0):
    user_packs = db.session.query(Pack).filter_by(
        user_id=user.id).offset(offset).limit(limit).all()
    return user_packs


@route.get("/trip/{trip_id}/public")
def get_trip_packs_public(trip_id: int):
    trip_packs = db.session.query(Pack).filter_by(trip_id=trip_id).all()
    return [serialize_pack_public(p) for p in trip_packs]


@route.get("/trip/{trip_id}")
def get_trip_packs(trip_id: int, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(id=trip_id, user_id=user.id).first()
    if not trip:
        raise HTTPException(404, "Trip not found.")

    trip_packs = db.session.query(Pack).filter_by(trip_id=trip_id).all()
    return [serialize_pack(p) for p in trip_packs]


@route.get("/{id}")
def get_pack_by_id(id: int, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(id=id, user_id=user.id).first()
    if not pack:
        raise HTTPException(404, "Pack does not exist.")
    return serialize_pack(pack)


class PackItemType(BaseModel):
    item_id: int
    quantity: float = None
    worn: bool = False
    checked: bool = False
    sort_order: int = 0


class PackType(BaseModel):
    title: str
    trip_id: int = None
    hiker_profile_id: Optional[int] = None
    items: List[PackItemType] = None


@route.post("", status_code=201)
def create_pack(pack: PackType, user: User = Depends(authenticate)):
    new_pack = Pack(title=pack.title, trip_id=pack.trip_id, hiker_profile_id=pack.hiker_profile_id, user_id=user.id)

    try:
        db.session.add(new_pack)
        db.session.flush()
    except Exception:
        logger.exception("Failed to create pack")
        raise HTTPException(400, "An error occurred while creating pack.")

    for item in pack.items:
        new_item = PackItem(pack_id=new_pack.id,
                            item_id=item.item_id,
                            quantity=item.quantity,
                            worn=item.worn,
                            checked=item.checked,
                            sort_order=item.sort_order)

        db.session.add(new_item)

    try:
        db.session.commit()
        db.session.refresh(new_pack)
    except Exception:
        logger.exception("Failed to add pack items")
        raise HTTPException(400, "An error occurred while adding pack items.")

    return serialize_pack(new_pack)


@route.put("/{id}")
def update_pack(id: int, payload: PackType, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(
        user_id=user.id, id=id).first()
    if not pack:
        raise HTTPException(404, "Pack does not exist.")

    try:
        pack.title = payload.title
        pack.trip_id = payload.trip_id
        pack.hiker_profile_id = payload.hiker_profile_id
        pack.items = [
            PackItem(pack_id=pack.id,
                     item_id=item.item_id,
                     quantity=item.quantity,
                     worn=item.worn,
                     checked=item.checked,
                     sort_order=item.sort_order)
            for item in payload.items
        ]
        db.session.commit()
        db.session.refresh(pack)
    except Exception:
        logger.exception("Failed to update pack")
        raise HTTPException(
            400, "An error occurred while updating pack.")

    return serialize_pack(pack)


class PackItemToggle(BaseModel):
    checked: bool


@route.put("/{pack_id}/item/{item_id}")
def update_pack_item(pack_id: int, item_id: int, payload: PackItemToggle, user: User = Depends(authenticate)):
    item = db.session.query(PackItem).filter_by(
        pack_id=pack_id, item_id=item_id).first()

    if not item:
        raise HTTPException(404, "Pack item does not exist.")

    try:
        item.checked = payload.checked
        db.session.commit()
    except Exception:
        raise HTTPException(400, "An error occurred while updating pack item.")

    return True


@route.get("/legacy/unassigned")
def get_unassigned_packs(user: User = Depends(authenticate)):
    unassigned_packs = db.session.query(Pack).filter_by(
        user_id=user.id, trip_id=None).all()

    return unassigned_packs


@route.post("/{pack_id}/generate", status_code=201)
def generate_pack(pack_id: int, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(
        id=pack_id, user_id=user.id).first()

    if not pack:
        raise HTTPException(404, "Pack does not exist.")

    trip = Trip(user_id=user.id, title=pack.title, location=pack.title)
    try:
        db.session.add(trip)
        db.session.flush()
    except Exception:
        raise HTTPException(400, "An error occurred while generating pack.")

    pack.trip_id = trip.id
    try:
        db.session.commit()
        db.session.refresh(trip)
    except Exception:
        raise HTTPException(400, "An error occurred while associating pack.")

    return trip


class AssignPack(BaseModel):
    trip_id: int = None


@route.put("/{pack_id}/assign")
def assign_pack(pack_id: int, payload: AssignPack, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(
        id=pack_id, user_id=user.id).first()

    if not pack:
        raise HTTPException(404, "Pack does not exist.")

    pack.trip_id = payload.trip_id
    try:
        db.session.commit()
        db.session.refresh(pack)
    except Exception:
        raise HTTPException(400, "An error occurred while assigning pack.")

    return pack


@route.delete("/{pack_id}", status_code=204)
def delete_pack(pack_id: int, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(
        id=pack_id, user_id=user.id).first()

    if not pack:
        raise HTTPException(404, "Pack does not exist.")

    try:
        db.session.delete(pack)
        db.session.commit()
    except Exception:
        raise HTTPException(400, "An error occurred while deleting pack.")
