"""Upload AdConversion rows to Google Ads (utils/ad_conversions.py).

Rows are written by the registration endpoints and the RevenueCat webhook.
Until Google Ads is configured (GOOGLE_ADS_* env) they simply stay 'pending';
`python -m ad_conversions.run flush` in the workshop sends them later.
"""
import logging
import os

import sentry_sdk

from celery_app import celery_app
from tasks.enrich_product import get_session
from utils import ad_conversions
from utils.consts import DEVELOPMENT

logger = logging.getLogger(__name__)

MAX_RETRIES = 6
_config = ad_conversions.GoogleAdsConfig.from_env(os.getenv)


@celery_app.task(bind=True, max_retries=MAX_RETRIES)
def upload_ad_conversion(self, conversion_id: int):
    if _config is None:
        return "unconfigured"
    client = ad_conversions.DataManagerClient(_config)
    with get_session() as session:
        try:
            status = ad_conversions.send_conversion(session, conversion_id, client)
        except ad_conversions.ConversionError as exc:
            if self.request.retries < MAX_RETRIES:
                raise self.retry(exc=exc, countdown=60 * 2 ** self.request.retries)
            sentry_sdk.capture_exception(exc)
            return "failed"
    if status == "failed":
        logger.error("Ad conversion %s rejected by Google", conversion_id)
        sentry_sdk.capture_message(f"Ad conversion {conversion_id} rejected by Google")
    return status


def enqueue_ad_conversion(conversion) -> None:
    """Never raises; a dropped upload stays 'pending' for the workshop flush."""
    if conversion is None or DEVELOPMENT or _config is None:
        return
    try:
        upload_ad_conversion.delay(conversion.id)
    except Exception:
        logger.warning("Could not enqueue ad conversion %s", conversion.id, exc_info=True)
