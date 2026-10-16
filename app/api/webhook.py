import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi_sqlalchemy import db
from starlette.concurrency import run_in_threadpool

from models.base import User
from tasks.ad_conversions import enqueue_ad_conversion
from tasks.sync_user import enqueue_user_sync
from utils import ad_conversions, revenuecat, user_sync
from utils.consts import (
    REVENUECAT_ENTITLEMENT_ID, REVENUECAT_PRODUCT_PLANS, REVENUECAT_WEBHOOK_SECRET,
)

logger = logging.getLogger(__name__)

route = APIRouter()

# Fallback only -- used when RevenueCat's API can't be asked (no secret key
# configured, or the lookup failed). The authoritative path is the customer
# read in _sync_user (revenuecat.fetch_subscriber).
GRANT_EVENTS = {
    "INITIAL_PURCHASE",
    "RENEWAL",
    "UNCANCELLATION",
    "PRODUCT_CHANGE",
    "NON_RENEWING_PURCHASE",
    "SUBSCRIPTION_EXTENDED",
    "TEMPORARY_ENTITLEMENT_GRANT",
}
REVOKE_EVENTS = {
    # Fires when access actually ends -- including after a billing grace
    # period runs out. BILLING_ISSUE (start of the grace period) and
    # CANCELLATION (auto-renew turned off; access continues to period end)
    # deliberately do NOT revoke.
    "EXPIRATION",
}

# Events that can change entitlement state; with an API key, any of these
# triggers a lookup. TEST and unknown types are ignored.
SYNC_EVENTS = GRANT_EVENTS | REVOKE_EVENTS | {
    "BILLING_ISSUE",
    "CANCELLATION",
    "SUBSCRIPTION_PAUSED",
    "REFUND_REVERSED",
}


def _int_id(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None   # anonymous ($RCAnonymousID:...) or malformed ids


def _fallback_state(event: dict):
    """is_subscribed implied by the event alone, or None for "no change"."""
    entitlement_ids = event.get("entitlement_ids")
    if entitlement_ids is not None and REVENUECAT_ENTITLEMENT_ID not in entitlement_ids:
        return None   # a product that doesn't grant our entitlement
    event_type = event.get("type")
    if event_type in GRANT_EVENTS:
        return True
    if event_type in REVOKE_EVENTS:
        return False
    return None


async def _sync_user(app_user_id, event: dict, use_event_fallback: bool = True):
    user_id = _int_id(app_user_id)
    if user_id is None:
        logger.warning("RevenueCat webhook with non-integer app_user_id: %s", app_user_id)
        return

    subscriber = await run_in_threadpool(revenuecat.fetch_subscriber, app_user_id)
    billing = None
    if subscriber is not None:
        billing = user_sync.billing_from_subscriber(
            subscriber, REVENUECAT_ENTITLEMENT_ID, REVENUECAT_PRODUCT_PLANS)
    is_subscribed = billing.active if billing else None
    source = "api"
    if is_subscribed is None and use_event_fallback:
        is_subscribed = _fallback_state(event)
        source = "event"
    if is_subscribed is None:
        return

    user = db.session.query(User).filter_by(id=user_id).first()
    if not user:
        logger.warning("RevenueCat webhook for unknown user_id: %s", user_id)
        return
    user.is_subscribed = is_subscribed
    if billing:
        # Plan, store, renewal date... -- what Resend gets. Without an API key
        # there's nothing trustworthy to write, so these keep their last value.
        user.update(billing.columns())
    db.session.commit()
    # Push the new billing state to Resend, and identity to RevenueCat now
    # that RevenueCat knows this customer.
    enqueue_user_sync(user_id)
    logger.info("RevenueCat %s: user %s is_subscribed=%s status=%s (%s)",
                event.get("type"), user_id, is_subscribed,
                billing.status if billing else None, source)


@route.post("/revenuecat")
async def revenuecat_webhook(request: Request):
    if REVENUECAT_WEBHOOK_SECRET:
        auth = request.headers.get("Authorization")
        expected = f"Bearer {REVENUECAT_WEBHOOK_SECRET}"
        if auth != expected:
            raise HTTPException(401, "Unauthorized")

    body = await request.json()
    event = body.get("event", {})
    event_type = event.get("type")

    if event_type == "TRANSFER":
        # Purchases moved between app user ids (e.g. a restore on a device
        # signed into another account). Only RevenueCat knows the result, so
        # without an API key there's nothing reliable to apply.
        for app_user_id in (event.get("transferred_from") or []) + (event.get("transferred_to") or []):
            await _sync_user(app_user_id, event, use_event_fallback=False)
        return {"ok": True}

    app_user_id = event.get("app_user_id")
    if event_type not in SYNC_EVENTS or not app_user_id:
        return {"ok": True}

    await _sync_user(app_user_id, event)
    _record_ad_conversion(app_user_id, event)
    return {"ok": True}


def _record_ad_conversion(app_user_id, event: dict):
    """First purchases by users who came from a Google ad -> Google Ads
    (utils/ad_conversions.py). Never fails the webhook."""
    user_id = _int_id(app_user_id)
    if user_id is None or event.get("type") not in ad_conversions.PURCHASE_EVENTS:
        return
    try:
        enqueue_ad_conversion(ad_conversions.record_purchase_from_event(db.session, user_id, event))
    except Exception:
        logger.exception("Ad conversion for RevenueCat event %s failed", event.get("id"))
