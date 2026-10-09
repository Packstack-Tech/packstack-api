"""Ask RevenueCat whether a user's paid entitlement is active.

The webhook used to map event names straight to is_subscribed, which was fine
for one-time purchases only. With subscriptions on sale it gets two things
wrong: BILLING_ISSUE revoked access during the store's grace period, and an
EXPIRATION of one product revoked access even when another purchase (e.g. a
lifetime unlock bought after a monthly plan) still grants it. The customer's
current entitlement state is the only thing that answers both, so the webhook
reads it from here when a secret API key is configured.
"""
import datetime
import logging
from typing import Optional
from urllib.parse import quote

import requests

from utils.consts import REVENUECAT_ENTITLEMENT_ID, REVENUECAT_SECRET_API_KEY

logger = logging.getLogger(__name__)

API_URL = "https://api.revenuecat.com/v1/subscribers/{app_user_id}"
TIMEOUT_S = 5


def _parse(ts: Optional[str]) -> Optional[datetime.datetime]:
    if not ts:
        return None
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def entitlement_is_active(entitlement: Optional[dict], now: Optional[datetime.datetime] = None) -> bool:
    """True if a v1 `subscriber.entitlements[<id>]` object grants access now.

    v1 returns expired entitlements too, so the dates decide: no expiry means
    lifetime; otherwise active until the later of the expiry and any billing
    grace period.
    """
    if not entitlement:
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    expires = _parse(entitlement.get("expires_date"))
    if expires is None:
        return True
    grace = _parse(entitlement.get("grace_period_expires_date"))
    return max(expires, grace or expires) > now


def fetch_entitlement_active(app_user_id: str) -> Optional[bool]:
    """Whether the user's entitlement is active right now, or None when it
    can't be determined (no API key configured, network or API error) so the
    caller can fall back to the event type."""
    if not REVENUECAT_SECRET_API_KEY:
        return None
    try:
        res = requests.get(
            API_URL.format(app_user_id=quote(str(app_user_id), safe="")),
            headers={"Authorization": f"Bearer {REVENUECAT_SECRET_API_KEY}"},
            timeout=TIMEOUT_S,
        )
        res.raise_for_status()
        entitlements = res.json().get("subscriber", {}).get("entitlements", {})
        return entitlement_is_active(entitlements.get(REVENUECAT_ENTITLEMENT_ID))
    except Exception:
        logger.exception("RevenueCat subscriber lookup failed for %s", app_user_id)
        return None
