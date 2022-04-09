from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import List
from sqlalchemy.orm import joinedload

from models.base import User, Trip, Pack, PackItem
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


@route.get("/{pack_id}")
def get_trip_packs(pack_id):
    pack = db.session.query(Pack).filter_by(id=pack_id).first()
    return pack


class PackItemType(BaseModel):
    item_id: int
    quantity: float = None
    worn: bool = False


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
        db.session.refresh()
    except Exception as e:
        print(e)
        raise HTTPException(400, "An error occurred while creating pack.")

    for item in pack.items:
        new_item = PackItem(pack_id=new_pack.id,
                            item_id=item.item_id,
                            quantity=item.quantity,
                            worn=item.worn)

        db.session.add(new_item)

    try:
        db.session.commit()
        db.session.refresh(new_pack)
    except Exception as e:
        print(e)
        raise HTTPException(400, "An error occurred while adding pack items.")

    return new_pack


@route.put("/{pack_id}")
def update_pack(pack_id, payload: PackType, user: User = Depends(authenticate)):
    pack = db.session.query(Pack).filter_by(
        user_id=user.id, pack_id=pack_id).first()
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
        new_item = PackItem(pack_id=pack_id,
                            item_id=item.item_id,
                            quantity=item.quantity,
                            worn=item.worn)

        db.session.add(new_item)

    try:
        db.session.commit()
        db.session.refresh(pack)
    except Exception as e:
        print(e)
        raise HTTPException(400, "An error occurred while adding pack items.")

    return pack
