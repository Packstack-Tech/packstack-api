"""Ask RevenueCat about a user's paid entitlement.

The webhook used to map event names straight to is_subscribed, which was fine
for one-time purchases only. With subscriptions on sale it gets two things
wrong: BILLING_ISSUE revoked access during the store's grace period, and an
EXPIRATION of one product revoked access even when another purchase (e.g. a
lifetime unlock bought after a monthly plan) still grants it. The customer's
current entitlement state is the only thing that answers both, so the webhook
reads it from here when a secret API key is configured.

The same read also gives the purchase details (plan, store, renewal date) the
webhook copies onto user.subscription_* -- see utils/user_sync.py.
"""
import logging
from typing import Optional

from utils.consts import REVENUECAT_ENTITLEMENT_ID, REVENUECAT_SECRET_API_KEY
from utils.user_sync import RevenueCatClient, entitlement_is_active  # noqa: F401 (re-export)

logger = logging.getLogger(__name__)

TIMEOUT_S = 5


def client() -> Optional[RevenueCatClient]:
    """A v1 client, or None when no secret key is configured."""
    if not REVENUECAT_SECRET_API_KEY:
        return None
    return RevenueCatClient(REVENUECAT_SECRET_API_KEY, timeout=TIMEOUT_S, max_rate_limit_retries=1)


def fetch_subscriber(app_user_id: str) -> Optional[dict]:
    """The v1 ``subscriber`` object, or None when it can't be read (no API key
    configured, network or API error) so the caller can fall back to the
    event type."""
    rc = client()
    if rc is None:
        return None
    try:
        return rc.get_subscriber(str(app_user_id))
    except Exception:
        logger.exception("RevenueCat subscriber lookup failed for %s", app_user_id)
        return None


def fetch_entitlement_active(app_user_id: str) -> Optional[bool]:
    """Whether the user's entitlement is active right now, or None when it
    can't be determined."""
    subscriber = fetch_subscriber(app_user_id)
    if subscriber is None:
        return None
    entitlements = subscriber.get("entitlements") or {}
    return entitlement_is_active(entitlements.get(REVENUECAT_ENTITLEMENT_ID))
