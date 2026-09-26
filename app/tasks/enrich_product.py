"""Celery wrapper around catalog.enrich — one item per job.

All logic lives in app/catalog/enrich.py so the workshop batch driver runs
the same code. Names re-exported below keep older imports working.
"""

import logging
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from celery_app import celery_app
from tasks.catalog_image import find_product_image
from utils.consts import WORKER_DATABASE_URL
from catalog.enrich import (  # noqa: F401 — re-exports
    enrich_item, ensure_product, ensure_variant, link_items,
    ai_complete, compute_confidence, DEFAULT_MODEL,
    CATEGORIES, SUBCATEGORIES,
)

logger = logging.getLogger(__name__)

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            WORKER_DATABASE_URL,
            pool_size=2,
            max_overflow=3,
            pool_pre_ping=True,
            pool_recycle=300,
        )
    return _engine


@contextmanager
def get_session():
    engine = _get_engine()
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _queue_image(catalog_product_id: int) -> None:
    find_product_image.delay(catalog_product_id)


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def enrich_product(self, item_id: int):
    with get_session() as session:
        enrich_item(session, item_id, on_product_created=_queue_image)
