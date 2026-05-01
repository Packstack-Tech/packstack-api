import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel
from typing import Optional

from models.base import User, CategoryBenchmark
from utils.auth import authenticate
from utils.gear_lifecycle import get_all_benchmarks, DEFAULT_BENCHMARKS

logger = logging.getLogger(__name__)

route = APIRouter(dependencies=[Depends(authenticate)])


@route.get("")
def fetch_benchmarks(user: User = Depends(authenticate)):
    return get_all_benchmarks(db.session, user.id)


class BenchmarkUpdate(BaseModel):
    lifespan_years: Optional[float] = None
    expected_nights: Optional[float] = None
    expected_distance: Optional[float] = None
    distance_unit: Optional[str] = None


@route.put("/{category_name}")
def upsert_benchmark(category_name: str, payload: BenchmarkUpdate, user: User = Depends(authenticate)):
    if category_name not in DEFAULT_BENCHMARKS:
        raise HTTPException(400, f"Unknown category: {category_name}")

    override = db.session.query(CategoryBenchmark).filter_by(
        user_id=user.id, category_name=category_name
    ).first()

    if not override:
        override = CategoryBenchmark(user_id=user.id, category_name=category_name)
        db.session.add(override)

    fields = payload.dict(exclude_none=True)
    for key, value in fields.items():
        setattr(override, key, value)

    try:
        db.session.commit()
        db.session.refresh(override)
    except Exception:
        logger.exception("Failed to update benchmark")
        raise HTTPException(400, "Unable to update benchmark.")

    return get_all_benchmarks(db.session, user.id)


@route.delete("/{category_name}", status_code=204)
def reset_benchmark(category_name: str, user: User = Depends(authenticate)):
    override = db.session.query(CategoryBenchmark).filter_by(
        user_id=user.id, category_name=category_name
    ).first()

    if override:
        db.session.delete(override)
        db.session.commit()
