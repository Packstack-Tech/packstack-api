import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import Optional

from models.base import User, HikerProfile
from utils.auth import authenticate

logger = logging.getLogger(__name__)

route = APIRouter(dependencies=[Depends(authenticate)])

# One profile per user. Multi-hiker profiles were removed from the product in
# Sept 2026 (a handful of users ever assigned a pack to a second profile). The
# table, the is_default column and pack.hiker_profile_id were left in place
# rather than migrated; clients treat the is_default row (else the oldest) as
# THE profile. The list/get/update/delete routes below stay for mobile builds
# still in the wild; nothing new should be built on them.


class HikerProfileType(BaseModel):
    name: str
    weight: Optional[float] = None
    height: Optional[float] = None
    year_of_birth: Optional[int] = None
    sex: Optional[str] = None
    body_type: Optional[str] = None


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

    # Hard limit of one for everyone. Old mobile builds that still offer an
    # "Add" button surface this as their generic create-failed alert.
    if existing_count > 0:
        raise HTTPException(409, "You already have a hiker profile.")

    profile = HikerProfile(
        user_id=user.id,
        name=payload.name,
        weight=payload.weight,
        height=payload.height,
        year_of_birth=payload.year_of_birth,
        sex=payload.sex,
        body_type=payload.body_type,
        is_default=True,
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


@route.put("/{profile_id}")
def update_profile(profile_id: int, payload: HikerProfileUpdateType, user: User = Depends(authenticate)):
    profile = db.session.query(HikerProfile).filter_by(
        id=profile_id, user_id=user.id).first()
    if not profile:
        raise HTTPException(404, "Hiker profile does not exist.")

    fields = payload.dict(exclude_none=True)

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
