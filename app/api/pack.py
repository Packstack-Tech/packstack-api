from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import List

from models.base import User, Pack, PackItem, Trip
from utils.auth import authenticate

route = APIRouter()


@route.get("s")
def get_user_packs(user: User = Depends(authenticate)):
    user_packs = db.session.query(Pack).filter_by(user_id=user.id).all()
    return user_packs


@route.get("/trip/{trip_id}")
def get_trip_packs(trip_id):
    trip_packs = db.session.query(Pack).filter_by(trip_id=trip_id).all()
    return trip_packs


@route.get("/{id}")
def get_trip_packs(id):
    pack = db.session.query(Pack).filter_by(id=id).first()
    return pack


class PackItemType(BaseModel):
    item_id: int
    quantity: float = None
    worn: bool = False
    checked: bool = False
    sort_order: int = 0


class PackType(BaseModel):
    title: str
    trip_id: int = None
    items: List[PackItemType] = None


@route.post("")
def create_pack(pack: PackType, user: User = Depends(authenticate)):
    new_pack = Pack(title=pack.title, trip_id=pack.trip_id, user_id=user.id)

    try:
        db.session.add(new_pack)
        db.session.commit()
        db.session.refresh(new_pack)
    except Exception as e:
        print(e)
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
    except Exception as e:
        print(e)
        raise HTTPException(400, "An error occurred while adding pack items.")

    return new_pack


@route.put("/{id}")
def update_pack(id, payload: PackType, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(
        user_id=user.id, id=id).first()
    if not pack:
        raise HTTPException(400, "Pack does not exist.")

    try:
        pack.title = payload.title
        pack.trip_id = payload.trip_id
        db.session.query(PackItem).filter_by(pack_id=pack.id).delete()
        db.session.commit()
    except Exception as e:
        print(e)
        raise HTTPException(
            400, "An error occurred while updating pack items.")

    for item in payload.items:
        new_item = PackItem(pack_id=pack.id,
                            item_id=item.item_id,
                            quantity=item.quantity,
                            worn=item.worn,
                            checked=item.checked,
                            sort_order=item.sort_order)

        db.session.add(new_item)

    try:
        db.session.commit()
        db.session.refresh(pack)
    except Exception as e:
        print(e)
        raise HTTPException(400, "An error occurred while adding pack items.")

    return pack


class PackItemToggle(BaseModel):
    checked: bool


@route.put("/{pack_id}/item/{item_id}")
def update_pack_item(pack_id, item_id, payload: PackItemToggle, user: User = Depends(authenticate)):
    item = db.session.query(PackItem).filter_by(
        pack_id=pack_id, item_id=item_id).first()

    if not item:
        raise HTTPException(400, "Pack item does not exist.")

    try:
        item.checked = payload.checked
        db.session.commit()
    except Exception as e:
        raise HTTPException(400, "An error occurred while updating pack item.")

    return True


@route.get("/legacy/unassigned")
def get_unassigned_packs(user: User = Depends(authenticate)):
    unassigned_packs = db.session.query(Pack).filter_by(
        user_id=user.id, trip_id=None).all()

    return unassigned_packs


@route.post("/{pack_id}/generate")
def generate_pack(pack_id, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(
        id=pack_id, user_id=user.id).first()

    if not pack:
        raise HTTPException(400, "Pack does not exist.")

    trip = Trip(user_id=user.id, title=pack.title, location=pack.title)
    try:
        db.session.add(trip)
        db.session.commit()
        db.session.refresh(trip)
    except Exception as e:
        raise HTTPException(400, "An error occurred while generating pack.")

    pack.trip_id = trip.id
    try:
        db.session.commit()
        db.session.refresh(pack)
    except Exception as e:
        raise HTTPException(400, "An error occurred while associating pack.")

    db.session.refresh(trip)
    return trip


class AssignPack(BaseModel):
    trip_id: int = None


@route.put("/{pack_id}/assign")
def assign_pack(pack_id, payload: AssignPack, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(
        id=pack_id, user_id=user.id).first()

    if not pack:
        raise HTTPException(400, "Pack does not exist.")

    pack.trip_id = payload.trip_id
    try:
        db.session.commit()
        db.session.refresh(pack)
    except Exception as e:
        raise HTTPException(400, "An error occurred while assigning pack.")

    return pack


@route.delete("/{pack_id}")
def delete_pack(pack_id, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(
        id=pack_id, user_id=user.id).first()

    if not pack:
        raise HTTPException(400, "Pack does not exist.")

    try:
        db.session.delete(pack)
        db.session.commit()
    except Exception as e:
        raise HTTPException(400, "An error occurred while deleting pack.")

    return True
