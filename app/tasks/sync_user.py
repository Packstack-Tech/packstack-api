"""Push a user's attributes to RevenueCat and Resend.

The mapping lives in utils/user_sync.py; this module is the Celery plumbing.
Callers use the enqueue_* helpers, which never raise: a broker outage must not
fail a registration, a profile save or a webhook, and the next sync (or a
re-run of the workshop backfill) heals anything that was dropped.
"""
import logging

import sentry_sdk

from celery_app import celery_app
from models.base import User
from tasks.enrich_product import get_session
from utils import user_sync
from utils.consts import (
    DEVELOPMENT, RESEND_API_KEY, RESEND_SEGMENT_IDS, REVENUECAT_SECRET_API_KEY,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 6


def _resend():
    return user_sync.ResendClient(RESEND_API_KEY) if RESEND_API_KEY else None


def _revenuecat():
    return user_sync.RevenueCatClient(REVENUECAT_SECRET_API_KEY) if REVENUECAT_SECRET_API_KEY else None


def _retry_or_report(task, exc, **context):
    if isinstance(exc, user_sync.SyncError) and exc.transient and task.request.retries < MAX_RETRIES:
        # 1, 2, 4 ... 32 minutes
        raise task.retry(exc=exc, countdown=60 * 2 ** task.request.retries)
    logger.error("User attribute sync failed (%s): %s", context, exc)
    sentry_sdk.capture_exception(exc)


@celery_app.task(bind=True, max_retries=MAX_RETRIES)
def sync_user_attributes(self, user_id: int, previous_email: str = None):
    with get_session() as session:
        user = session.get(User, user_id)
        if user is None:
            return {"skipped": "no such user"}
        try:
            result = user_sync.sync_user(
                user,
                resend=_resend(),
                revenuecat=_revenuecat(),
                previous_email=previous_email,
                segment_ids=RESEND_SEGMENT_IDS,
            )
        except user_sync.SyncError as exc:
            _retry_or_report(self, exc, user_id=user_id)
            return {"error": str(exc)}
    logger.info("Synced attributes for user %s: %s", user_id, result)
    return result


@celery_app.task(bind=True, max_retries=MAX_RETRIES)
def forget_revenuecat_attributes(self, user_id: int):
    """After account deletion: clear the personal attributes RevenueCat holds.
    The customer and its purchases stay -- they're revenue records."""
    rc = _revenuecat()
    if rc is None:
        return
    try:
        rc.set_attributes(str(user_id), {key: None for key in user_sync.REVENUECAT_PII_KEYS})
    except user_sync.SyncError as exc:
        _retry_or_report(self, exc, user_id=user_id, action="forget")


def _configured() -> bool:
    return not DEVELOPMENT and bool(RESEND_API_KEY or REVENUECAT_SECRET_API_KEY)


def enqueue_user_sync(user_id: int, previous_email: str = None) -> None:
    """Queue a full attribute push for this user. Pass previous_email when the
    email just changed so the Resend contact moves instead of duplicating."""
    if not _configured():
        return
    try:
        sync_user_attributes.delay(user_id, previous_email)
    except Exception:
        logger.warning("Could not enqueue attribute sync for user %s", user_id, exc_info=True)


def enqueue_revenuecat_forget(user_id: int) -> None:
    if not _configured() or not REVENUECAT_SECRET_API_KEY:
        return
    try:
        forget_revenuecat_attributes.delay(user_id)
    except Exception:
        logger.warning("Could not enqueue RevenueCat forget for user %s", user_id, exc_info=True)
