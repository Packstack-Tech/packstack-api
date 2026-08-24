import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import List, Optional

from models.base import User, Pack, PackItem, Trip
from utils.auth import authenticate
from utils.consts import FREE_PACKS_PER_TRIP
from utils.pack_summary import serialize_pack, serialize_pack_public

logger = logging.getLogger(__name__)

route = APIRouter()

# FREE_PACKS_PER_TRIP is read from the environment (see utils/consts.py) and is
# unlimited unless set. Note this is NOT the "3 packs" limit users see on the
# Packs tab — that one counts Trips (see FREE_TRIP_LIMIT in trip.py). This
# limits the pack variants inside one trip.


def _enforce_pack_limit(user: User, trip_id: int | None):
    """Block non-subscribed users from exceeding the free pack allowance.

    Only packs attached to a trip are counted; a pack with no trip_id is a
    legacy record and is left alone. Packs that already exist over the limit
    are untouched — this blocks creating new ones, nothing else.
    """
    if user.is_subscribed or trip_id is None:
        return

    pack_count = db.session.query(Pack).filter_by(
        user_id=user.id, trip_id=trip_id).count()

    if pack_count >= FREE_PACKS_PER_TRIP:
        raise HTTPException(
            402, "Upgrade to add more packs to this trip.")


def _require_own_trip(user: User, trip_id: int | None):
    """Reject attaching a pack to a trip the caller does not own.

    This is authorization, not quota, and it is separate from the pack limit
    for a reason: the limit counts *the caller's* packs in the target trip, so
    for someone else's trip the count is 0 and the quota check passes happily.
    Without this, a pack could be attached to a stranger's trip, where it then
    renders both on their trip page (get_trip_packs returns every pack with the
    trip_id, not just the owner's) and on the public view.

    404 rather than 403, matching the rest of this module and not confirming
    whether the id exists.
    """
    if trip_id is None:
        return

    owns = db.session.query(Trip.id).filter_by(
        id=trip_id, user_id=user.id).first()

    if not owns:
        raise HTTPException(404, "Trip not found.")


def _enforce_pack_move(user: User, pack: Pack, new_trip_id: int | None):
    """Enforce the pack limit when a pack is attached to a *different* trip.

    Only a change of trip is gated, and this is the reason the `trip_id is
    None` short-circuit above is safe to keep: an unattached pack is invisible
    until something attaches it, and every path that attaches one comes through
    here. Creating a pack with a null trip_id and then assigning it -- the
    obvious way around the create-time gate -- is therefore blocked at the
    assign, not at the create.

    Gating an *unchanged* trip_id would be a serious bug rather than a stricter
    gate: the mobile sync loop PUTs every pack on every save with the trip_id
    it already has, so a grandfathered second pack would 402 on every routine
    save. Clients treat a failed save as retryable and show nothing, so the
    user would silently lose edits.
    """
    if new_trip_id is None or new_trip_id == pack.trip_id:
        return

    _require_own_trip(user, new_trip_id)
    _enforce_pack_limit(user, new_trip_id)


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
    _require_own_trip(user, pack.trip_id)
    _enforce_pack_limit(user, pack.trip_id)

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

    # An omitted trip_id means "leave it alone", not "detach". Pydantic v1
    # defaults it to None, so without this an update that simply doesn't
    # mention trip_id would silently orphan the pack -- and with the gate on,
    # re-attaching it could then be refused, stranding it for good. An
    # explicit null still detaches.
    trip_id_given = 'trip_id' in payload.__fields_set__
    new_trip_id = payload.trip_id if trip_id_given else pack.trip_id

    # Before any mutation, while pack.trip_id is still the stored value.
    _enforce_pack_move(user, pack, new_trip_id)

    try:
        pack.title = payload.title
        pack.trip_id = new_trip_id
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

    _enforce_pack_move(user, pack, payload.trip_id)

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
