import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel, validator
from typing import Optional

from models.base import User, Item, ItemLog
from utils.auth import authenticate
from utils.gear_lifecycle import replacement_score, get_benchmark

logger = logging.getLogger(__name__)

route = APIRouter(dependencies=[Depends(authenticate)])

VALID_CONDITIONS = {"new", "good", "fair", "worn", "retired"}
VALID_STATUSES = {"active", "wishlist", "retired", "sold", "lost"}
VALID_ACQUISITION_TYPES = {"purchased", "gifted", "traded", "diy"}
VALID_RETIRED_REASONS = {"worn_out", "upgraded", "lost", "sold", "gifted"}


class LifecycleUpdate(BaseModel):
    acquired_date: Optional[str] = None
    acquisition_type: Optional[str] = None
    purchase_retailer: Optional[str] = None
    condition: Optional[str] = None
    status: Optional[str] = None
    retired_date: Optional[str] = None
    retired_reason: Optional[str] = None
    replaced_by_id: int = None

    @validator(
        "acquired_date", "acquisition_type", "purchase_retailer",
        "condition", "status", "retired_date", "retired_reason",
        pre=True, always=True,
    )
    def empty_str_to_none(cls, v):
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


@route.put("/{item_id}/lifecycle")
def update_lifecycle(item_id: int, payload: LifecycleUpdate, user: User = Depends(authenticate)):
    item = db.session.query(Item).filter_by(id=item_id, user_id=user.id).first()
    if not item:
        raise HTTPException(404, "Item not found.")

    if payload.condition and payload.condition not in VALID_CONDITIONS:
        raise HTTPException(400, f"Invalid condition. Must be one of: {', '.join(VALID_CONDITIONS)}")

    if payload.status and payload.status not in VALID_STATUSES:
        raise HTTPException(400, f"Invalid status. Must be one of: {', '.join(VALID_STATUSES)}")

    if payload.acquisition_type and payload.acquisition_type not in VALID_ACQUISITION_TYPES:
        raise HTTPException(400, f"Invalid acquisition_type. Must be one of: {', '.join(VALID_ACQUISITION_TYPES)}")

    if payload.retired_reason and payload.retired_reason not in VALID_RETIRED_REASONS:
        raise HTTPException(400, f"Invalid retired_reason. Must be one of: {', '.join(VALID_RETIRED_REASONS)}")

    if payload.replaced_by_id:
        replacement = db.session.query(Item).filter_by(
            id=payload.replaced_by_id, user_id=user.id
        ).first()
        if not replacement:
            raise HTTPException(400, "Replacement item not found.")

    old_condition = item.condition

    fields = payload.dict(exclude_none=True)
    for key, value in fields.items():
        setattr(item, key, value)

    if payload.condition and payload.condition != old_condition:
        log_entry = ItemLog(
            item_id=item.id,
            user_id=user.id,
            event_type="condition_change",
            event_date=datetime.date.today(),
            old_condition=old_condition,
            new_condition=payload.condition,
        )
        db.session.add(log_entry)

    try:
        db.session.commit()
        db.session.refresh(item)
    except Exception:
        logger.exception("Failed to update item lifecycle")
        raise HTTPException(400, "Unable to update item lifecycle.")

    return item


VALID_EVENT_TYPES = {
    "acquired", "condition_change", "repair", "maintenance",
    "weight_check", "retired", "sold", "note",
}


class ItemLogCreate(BaseModel):
    event_type: str
    event_date: str
    note: Optional[str] = None
    old_condition: Optional[str] = None
    new_condition: Optional[str] = None
    old_weight: Optional[float] = None
    new_weight: Optional[float] = None
    cost: Optional[float] = None


@route.post("/{item_id}/log", status_code=201)
def create_log(item_id: int, payload: ItemLogCreate, user: User = Depends(authenticate)):
    item = db.session.query(Item).filter_by(id=item_id, user_id=user.id).first()
    if not item:
        raise HTTPException(404, "Item not found.")

    if payload.event_type not in VALID_EVENT_TYPES:
        raise HTTPException(400, f"Invalid event_type. Must be one of: {', '.join(VALID_EVENT_TYPES)}")

    log_entry = ItemLog(
        item_id=item.id,
        user_id=user.id,
        **payload.dict(),
    )

    try:
        db.session.add(log_entry)
        db.session.commit()
        db.session.refresh(log_entry)
    except Exception:
        logger.exception("Failed to create item log")
        raise HTTPException(400, "Unable to create log entry.")

    return log_entry


@route.get("/{item_id}/log")
def fetch_logs(item_id: int, user: User = Depends(authenticate)):
    item = db.session.query(Item).filter_by(id=item_id, user_id=user.id).first()
    if not item:
        raise HTTPException(404, "Item not found.")

    logs = db.session.query(ItemLog).filter_by(
        item_id=item.id
    ).order_by(ItemLog.event_date.desc(), ItemLog.created_at.desc()).all()

    return logs


@route.get("/{item_id}/replacement-score")
def get_replacement_score(item_id: int, user: User = Depends(authenticate)):
    item = db.session.query(Item).filter_by(id=item_id, user_id=user.id).first()
    if not item:
        raise HTTPException(404, "Item not found.")

    category_name = (
        item.category.category.name
        if item.category and item.category.category
        else "Miscellaneous"
    )

    benchmark = get_benchmark(db.session, user.id, category_name)
    is_default_fallback = benchmark.pop("is_default_fallback", False)
    score = replacement_score(item.acquired_date, item.condition, benchmark)

    return {
        "item_id": item.id,
        "score": score,
        "category": category_name,
        "benchmark": benchmark,
        "is_default_fallback": is_default_fallback,
        "acquired_date": str(item.acquired_date) if item.acquired_date else None,
        "condition": item.condition,
    }
