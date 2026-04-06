import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import Optional

from models.base import User, HikerProfile
from utils.auth import authenticate

logger = logging.getLogger(__name__)

route = APIRouter(dependencies=[Depends(authenticate)])


class HikerProfileType(BaseModel):
    name: str
    weight: Optional[float] = None
    height: Optional[float] = None
    year_of_birth: Optional[int] = None
    sex: Optional[str] = None
    body_type: Optional[str] = None
    is_default: bool = False


def _clear_other_defaults(user_id: int, exclude_id: int = None):
    query = db.session.query(HikerProfile).filter_by(user_id=user_id, is_default=True)
    if exclude_id:
        query = query.filter(HikerProfile.id != exclude_id)
    for profile in query.all():
        profile.is_default = False


@route.get("")
def list_profiles(user: User = Depends(authenticate)):
    return db.session.query(HikerProfile).filter_by(
        user_id=user.id
    ).order_by(HikerProfile.created_at).all()


@route.get("/{profile_id}")
def get_profile(profile_id: int, user: User = Depends(authenticate)):
    profile = db.session.query(HikerProfile).filter_by(
        id=profile_id, user_id=user.id).first()
    if not profile:
        raise HTTPException(404, "Hiker profile does not exist.")
    return profile


@route.post("", status_code=201)
def create_profile(payload: HikerProfileType, user: User = Depends(authenticate)):
    existing_count = db.session.query(HikerProfile).filter_by(user_id=user.id).count()
    is_default = True if existing_count == 0 else payload.is_default

    if is_default:
        _clear_other_defaults(user.id)

    profile = HikerProfile(
        user_id=user.id,
        name=payload.name,
        weight=payload.weight,
        height=payload.height,
        year_of_birth=payload.year_of_birth,
        sex=payload.sex,
        body_type=payload.body_type,
        is_default=is_default,
    )

    try:
        db.session.add(profile)
        db.session.commit()
        db.session.refresh(profile)
    except Exception:
        logger.exception("Failed to create hiker profile")
        raise HTTPException(400, "An error occurred while creating hiker profile.")

    return profile


class HikerProfileUpdateType(BaseModel):
    name: Optional[str] = None
    weight: Optional[float] = None
    height: Optional[float] = None
    year_of_birth: Optional[int] = None
    sex: Optional[str] = None
    body_type: Optional[str] = None
    is_default: Optional[bool] = None


@route.put("/{profile_id}")
def update_profile(profile_id: int, payload: HikerProfileUpdateType, user: User = Depends(authenticate)):
    profile = db.session.query(HikerProfile).filter_by(
        id=profile_id, user_id=user.id).first()
    if not profile:
        raise HTTPException(404, "Hiker profile does not exist.")

    fields = payload.dict(exclude_none=True)

    if fields.get("is_default"):
        _clear_other_defaults(user.id, exclude_id=profile.id)

    for key, value in fields.items():
        setattr(profile, key, value)

    try:
        db.session.commit()
        db.session.refresh(profile)
    except Exception:
        logger.exception("Failed to update hiker profile")
        raise HTTPException(400, "An error occurred while updating hiker profile.")

    return profile


@route.delete("/{profile_id}", status_code=204)
def delete_profile(profile_id: int, user: User = Depends(authenticate)):
    profile = db.session.query(HikerProfile).filter_by(
        id=profile_id, user_id=user.id).first()
    if not profile:
        raise HTTPException(404, "Hiker profile does not exist.")

    was_default = profile.is_default

    try:
        db.session.delete(profile)
        db.session.flush()
    except Exception:
        logger.exception("Failed to delete hiker profile")
        raise HTTPException(400, "An error occurred while deleting hiker profile.")

    if was_default:
        oldest = db.session.query(HikerProfile).filter_by(
            user_id=user.id
        ).order_by(HikerProfile.created_at).first()
        if oldest:
            oldest.is_default = True

    try:
        db.session.commit()
    except Exception:
        logger.exception("Failed to promote default hiker profile")
        raise HTTPException(400, "An error occurred while updating default profile.")
