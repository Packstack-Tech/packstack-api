import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import List

from models.base import User, Kit, KitItem
from utils.auth import authenticate

logger = logging.getLogger(__name__)

route = APIRouter()

FREE_KIT_LIMIT = 1


def _enforce_kit_limit(user: User):
    """Block non-subscribed users from exceeding the free kit allowance."""
    if user.is_subscribed:
        return

    kit_count = db.session.query(Kit).filter_by(user_id=user.id).count()

    if kit_count >= FREE_KIT_LIMIT:
        raise HTTPException(
            402, "Upgrade to create more than one kit.")


class KitItemType(BaseModel):
    item_id: int
    quantity: float = 1


class KitType(BaseModel):
    name: str
    items: List[KitItemType] = []


@route.get("s")
def get_user_kits(user: User = Depends(authenticate)):
    kits = db.session.query(Kit).filter_by(user_id=user.id).all()
    return kits


@route.get("/{kit_id}")
def get_kit(kit_id: int, user: User = Depends(authenticate)):
    kit = db.session.query(Kit).filter_by(id=kit_id, user_id=user.id).first()
    if not kit:
        raise HTTPException(404, "Kit does not exist.")
    return kit


@route.post("", status_code=201)
def create_kit(payload: KitType, user: User = Depends(authenticate)):
    _enforce_kit_limit(user)

    kit = Kit(name=payload.name, user_id=user.id)

    try:
        db.session.add(kit)
        db.session.flush()
    except Exception:
        logger.exception("Failed to create kit")
        raise HTTPException(400, "An error occurred while creating kit.")

    for item in payload.items:
        kit_item = KitItem(
            kit_id=kit.id,
            item_id=item.item_id,
            quantity=item.quantity,
        )
        db.session.add(kit_item)

    try:
        db.session.commit()
        db.session.refresh(kit)
    except Exception:
        logger.exception("Failed to add kit items")
        raise HTTPException(400, "An error occurred while adding kit items.")

    return kit


@route.put("/{kit_id}")
def update_kit(kit_id: int, payload: KitType, user: User = Depends(authenticate)):
    kit = db.session.query(Kit).filter_by(id=kit_id, user_id=user.id).first()
    if not kit:
        raise HTTPException(404, "Kit does not exist.")

    try:
        kit.name = payload.name
        db.session.query(KitItem).filter_by(kit_id=kit.id).delete()
        db.session.flush()
    except Exception:
        logger.exception("Failed to update kit")
        raise HTTPException(400, "An error occurred while updating kit.")

    for item in payload.items:
        kit_item = KitItem(
            kit_id=kit.id,
            item_id=item.item_id,
            quantity=item.quantity,
        )
        db.session.add(kit_item)

    try:
        db.session.commit()
        db.session.refresh(kit)
    except Exception:
        logger.exception("Failed to update kit items")
        raise HTTPException(400, "An error occurred while updating kit items.")

    return kit


@route.delete("/{kit_id}", status_code=204)
def delete_kit(kit_id: int, user: User = Depends(authenticate)):
    kit = db.session.query(Kit).filter_by(id=kit_id, user_id=user.id).first()
    if not kit:
        raise HTTPException(404, "Kit does not exist.")

    try:
        db.session.delete(kit)
        db.session.commit()
    except Exception:
        logger.exception("Failed to delete kit")
        raise HTTPException(400, "An error occurred while deleting kit.")
