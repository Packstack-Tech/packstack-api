import datetime
import logging
import uuid as uuid_module

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import joinedload, noload

from models.base import User, Trip, Pack, PackItem, Item
from tasks.enrich_trip import enrich_trip
from utils.ai_review import build_ai_review_markdown
from utils.pack_summary import serialize_pack_public
from utils.auth import authenticate
from utils.utils import clone_model

logger = logging.getLogger(__name__)

route = APIRouter()

# Number of active (non-removed) trips a non-subscribed user may have.
FREE_TRIP_LIMIT = 3


def _enforce_trip_limit(user: User):
    """Block non-subscribed users from exceeding the free trip allowance."""
    if user.is_subscribed:
        return

    active_trips = db.session.query(Trip).filter_by(
        user_id=user.id, removed=False).count()

    if active_trips >= FREE_TRIP_LIMIT:
        raise HTTPException(
            402, "Upgrade to create more than three packs.")


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


PUBLIC_TRIP_FIELDS = (
    "id", "uuid", "title", "location", "start_date", "end_date",
    "temp_min", "temp_max", "temp_category", "distance",
    "daily_elevation_gain", "terrain", "pace", "notes", "published",
)


def _public_packs_query(trip_id: int):
    """Packs for the public page, without the CatalogProduct join.

    Item eagerly joins CatalogProduct (description, JSON specs, image url) on
    every load. The public serializer never reads it, so skipping the join
    trims the row width and the payload for share pages.
    """
    return (
        db.session.query(Pack)
        .filter_by(trip_id=trip_id)
        .options(joinedload(Pack.items).joinedload(PackItem.item).noload(Item.catalog_product))
        .order_by(Pack.id)
    )


@route.get("/public/{trip_id}")
def fetch_public(trip_id: str, response: Response):
    """Everything the public pack page needs in one round trip: trip header,
    owner display units, and the packs in their public shape. Replaces the
    /meta + /pack/trip/{id}/public pair so the page can render server-side
    without a client-side fetch after hydration.
    """
    trip = _resolve_trip(trip_id)

    user = db.session.query(User.username,
                            User.unit_distance,
                            User.unit_temperature,
                            User.unit_weight).filter_by(id=trip.user_id).first()
    if not user:
        raise HTTPException(404, "Trip owner not found.")

    packs = _public_packs_query(trip.id).all()

    # Public, read-only data: let the CDN and browser hold it briefly so a
    # burst of visitors to a shared link doesn't each hit Postgres.
    response.headers["Cache-Control"] = "public, max-age=30, s-maxage=60, stale-while-revalidate=300"

    return {
        "trip": {k: getattr(trip, k) for k in PUBLIC_TRIP_FIELDS},
        "user": user._asdict(),
        "packs": [serialize_pack_public(p) for p in packs],
    }


@route.get("/meta/{trip_id}")
def fetch_meta(trip_id: str):
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

    return {
        "trip": trip,
        "user": user._asdict()
    }


def _resolve_trip(trip_id: str) -> Trip:
    """Look up a trip by public uuid or numeric id, as the share URLs do."""
    try:
        uuid_val = uuid_module.UUID(trip_id)
        trip = db.session.query(Trip).filter_by(uuid=uuid_val).first()
    except ValueError:
        try:
            trip = db.session.query(Trip).filter_by(id=int(trip_id)).first()
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid trip identifier.")

    if not trip or trip.removed:
        raise HTTPException(404, "Trip not found.")
    return trip


@route.get("/{trip_id}/ai-review")
def fetch_ai_review(trip_id: str):
    """Trip + packs + totals as one markdown document for pasting into an AI
    assistant. Same visibility as /info and /meta (anyone with the link).
    Backs the "Copy for AI" button on the public pack page.
    """
    trip = _resolve_trip(trip_id)

    user = db.session.query(User.unit_distance,
                            User.unit_temperature).filter_by(id=trip.user_id).first()
    if not user:
        raise HTTPException(404, "Trip owner not found.")

    packs = db.session.query(Pack).filter_by(trip_id=trip.id).order_by(Pack.id).all()
    public_url = f"https://packstack.io/pack/{trip.uuid or trip.id}"
    markdown = build_ai_review_markdown(trip, packs, user, public_url=public_url)

    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


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
    location: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    temp_min: Optional[float] = None
    temp_max: Optional[float] = None
    temp_category: Optional[str] = None
    distance: Optional[float] = None
    daily_elevation_gain: Optional[float] = None
    terrain: Optional[str] = None
    pace: Optional[str] = None
    notes: Optional[str] = None
    published: Optional[bool] = None
    removed: Optional[bool] = None


@route.post("", status_code=201)
def create(payload: TripType, user: User = Depends(authenticate)):
    _enforce_trip_limit(user)

    new_trip = Trip(user_id=user.id, **payload.model_dump(exclude_none=True))

    try:
        db.session.add(new_trip)
        db.session.commit()
        db.session.refresh(new_trip)
    except Exception:
        raise HTTPException(400, "Unable to create trip.")

    if payload.location and payload.location.strip():
        new_trip.enrich_status = "pending"
        db.session.commit()
        db.session.refresh(new_trip)
        enrich_trip.delay(new_trip.id)

    return new_trip


class TripUpdate(TripType):
    id: int


@route.put("")
def update(payload: TripUpdate, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=payload.id, user_id=user.id).first()

    if not trip:
        raise HTTPException(404, "Trip not found.")

    old_location = trip.location
    old_start_date = str(trip.start_date) if trip.start_date else None
    old_end_date = str(trip.end_date) if trip.end_date else None
    fields = payload.model_dump(exclude_none=True)

    try:
        for key, value in fields.items():
            setattr(trip, key, value)

        location_changed = payload.location and payload.location != old_location
        dates_changed = (
            (payload.start_date and payload.start_date != old_start_date) or
            (payload.end_date and payload.end_date != old_end_date)
        )
        if trip.location and (location_changed or dates_changed):
            trip.enrich_status = "pending"

        db.session.commit()
        db.session.refresh(trip)
    except Exception:
        raise HTTPException(400, "An error occurred while updating trip.")

    if trip.enrich_status == "pending":
        enrich_trip.delay(trip.id)

    return trip


@route.post("/{trip_id}/clone", status_code=201)
def clone(trip_id: int, user: User = Depends(authenticate)):
    _enforce_trip_limit(user)

    trip = db.session.query(Trip).filter_by(
        id=trip_id, user_id=user.id).first()

    if not trip:
        raise HTTPException(404, "Trip not found.")

    cloned_trip_data = clone_model(trip, ['title', 'location', 'created_at', 'uuid'])
    cloned_trip = Trip(
        **cloned_trip_data,
        title=f"{trip.title} (Copy)",
        location=f"{trip.location} (Copy)",
        created_at=datetime.datetime.utcnow()
    )

    try:
        db.session.add(cloned_trip)
        db.session.flush()

        # Deliberately not gated by FREE_PACKS_PER_TRIP: a clone is a copy of
        # content the user already has, and dropping packs from it would be a
        # silent, invisible loss. The clone is still bounded by the trip limit
        # enforced above, and an over-limit source trip can only exist because
        # it was grandfathered in.
        packs = db.session.query(Pack).filter_by(trip_id=trip.id).all()
        for pack in packs:
            # hiker_profile_id is a dead column (single-profile product); don't
            # carry stale assignments into the clone.
            cloned_pack_data = clone_model(pack, ['trip_id', 'hiker_profile_id'])
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


@route.delete("/{trip_id}", status_code=204)
def remove_trip(trip_id: int, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).filter_by(
        id=trip_id, user_id=user.id).first()

    if not trip:
        raise HTTPException(403, "Permission denied.")

    linked_packs = db.session.query(Pack).filter_by(
        user_id=user.id, trip_id=trip.id).all()
    for pack in linked_packs:
        pack.trip_id = None

    try:
        trip.removed = True
        db.session.commit()
    except Exception:
        raise HTTPException(400, "An error occurred while deleting trip.")


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


@route.get("/{trip_id}")
def fetch_one(trip_id: int, user: User = Depends(authenticate)):
    trip = db.session.query(Trip).options(
        joinedload(Trip.user)).filter_by(id=trip_id, user_id=user.id).first()
    if not trip:
        raise HTTPException(404, "Trip not found.")
    return trip


@route.get("s")
def fetch_all(user: User = Depends(authenticate), limit: int = 100, offset: int = 0):
    trips = db.session.query(Trip).filter_by(
        user_id=user.id, removed=False
    ).order_by(Trip.end_date.desc()).offset(offset).limit(limit).all()

    return trips
