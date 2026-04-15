import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi_sqlalchemy import db

from models.base import User
from utils.consts import REVENUECAT_WEBHOOK_SECRET

logger = logging.getLogger(__name__)

route = APIRouter()

SUBSCRIBE_EVENTS = {
    "INITIAL_PURCHASE",
    "RENEWAL",
    "UNCANCELLATION",
    "PRODUCT_CHANGE",
    "NON_RENEWING_PURCHASE",
}

UNSUBSCRIBE_EVENTS = {
    "EXPIRATION",
    "BILLING_ISSUE",
}


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
    app_user_id = event.get("app_user_id")

    if not event_type or not app_user_id:
        return {"ok": True}

    try:
        user_id = int(app_user_id)
    except (ValueError, TypeError):
        logger.warning("RevenueCat webhook with non-integer app_user_id: %s", app_user_id)
        return {"ok": True}

    if event_type in SUBSCRIBE_EVENTS:
        is_subscribed = True
    elif event_type in UNSUBSCRIBE_EVENTS:
        is_subscribed = False
    else:
        return {"ok": True}

    user = db.session.query(User).filter_by(id=user_id).first()
    if not user:
        logger.warning("RevenueCat webhook for unknown user_id: %s", user_id)
        return {"ok": True}

    user.is_subscribed = is_subscribed
    db.session.commit()

    logger.info("RevenueCat %s: user %s is_subscribed=%s", event_type, user_id, is_subscribed)
    return {"ok": True}
