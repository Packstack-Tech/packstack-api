"""Report sign-ups and purchases to Google Ads from the server.

Flow:
  1. packstack.io / the web app capture the ad click id (gclid / gbraid /
     wbraid) and UTM tags and send them with registration
     (app/src/lib/attribution.ts).
  2. The registration endpoints save them as a UserAttribution row
     (save_attribution) and record a "sign_up" AdConversion.
  3. The RevenueCat webhook records a "purchase" AdConversion with the price
     for a first purchase (record_purchase_from_event).
  4. tasks/ad_conversions.py uploads each row through Google's Data Manager
     API (send_conversion). Google dedupes on transaction_id, so re-sending a
     row can't double count.

Only users who arrived from a Google ad click get conversions recorded:
without a click id there's nothing for Google to credit, and we don't send
anyone else's (hashed) email.

Dependency-light (stdlib + requests + PyJWT) so the workshop can import it
to report and re-send.
"""
import base64
import datetime
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

import jwt
import requests

logger = logging.getLogger(__name__)

SIGN_UP = "sign_up"
PURCHASE = "purchase"
KINDS = (SIGN_UP, PURCHASE)

CLICK_ID_KEYS = ("gclid", "gbraid", "wbraid")   # in order of preference
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")
# Google's longest click-through conversion window.
CLICK_WINDOW = datetime.timedelta(days=90)

# RevenueCat events that are a customer paying for the first time for a
# product: a new subscription or a one-time unlock. Renewals are left out.
PURCHASE_EVENTS = {"INITIAL_PURCHASE", "NON_RENEWING_PURCHASE"}

INGEST_URL = "https://datamanager.googleapis.com/v1/events:ingest"
SCOPE = "https://www.googleapis.com/auth/datamanager"
USER_AGENT = "packstack-ad-conversions/1.0"


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _clip(value: Any, n: int = 255) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value[:n] or None


# --- attribution ---------------------------------------------------------------

def clean_attribution(payload: Optional[Mapping[str, Any]],
                      now: Optional[datetime.datetime] = None) -> Optional[Dict[str, Any]]:
    """Column values for a UserAttribution from the client payload, or None
    when there's nothing worth keeping. The client's timestamp is trusted only
    within the click window (and never in the future)."""
    if not payload:
        return None
    now = now or _utcnow()
    values: Dict[str, Any] = {k: _clip(payload.get(k)) for k in CLICK_ID_KEYS + UTM_KEYS}
    if not any(values.values()):
        return None
    values["landing_path"] = _clip(payload.get("landing"))
    clicked_at = now
    ts = payload.get("ts")
    if isinstance(ts, (int, float)) and ts > 0:
        candidate = datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc).replace(tzinfo=None)
        if now - CLICK_WINDOW < candidate <= now:
            clicked_at = candidate
    values["clicked_at"] = clicked_at
    return values


def click_id(attribution) -> Optional[Dict[str, str]]:
    """{"gclid": "..."} -- one id, the best one present, as Google wants."""
    if attribution is None:
        return None
    for key in CLICK_ID_KEYS:
        value = getattr(attribution, key, None)
        if value:
            return {key: value}
    return None


def in_window(attribution, event_at: datetime.datetime) -> bool:
    clicked = getattr(attribution, "clicked_at", None)
    if clicked is None:
        return False
    # A little slack for clock differences between the browser and us.
    return clicked - datetime.timedelta(minutes=5) <= event_at <= clicked + CLICK_WINDOW


# --- event payload ---------------------------------------------------------------

def normalize_email(email: str) -> str:
    """Google's rules: lowercase, no whitespace; for gmail/googlemail also drop
    dots and any +suffix in the local part."""
    email = "".join((email or "").split()).lower()
    local, _, domain = email.partition("@")
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{domain}" if domain else email


def hash_email(email: str) -> str:
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


def _rfc3339(dt: datetime.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_event(conversion, attribution, email: Optional[str]) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "adIdentifiers": click_id(attribution) or {},
        "transactionId": conversion.transaction_id,
        "eventTimestamp": _rfc3339(conversion.event_at),
        "eventSource": "WEB",
    }
    if conversion.value is not None:
        event["conversionValue"] = float(conversion.value)
        event["currency"] = conversion.currency or "USD"
    if email:
        # Enhanced conversions: improves matching when the click id alone
        # can't be joined (cross-device, cookie loss).
        event["userData"] = {"userIdentifiers": [{"emailAddress": hash_email(email)}]}
    return event


# --- config + client -------------------------------------------------------------

@dataclass
class GoogleAdsConfig:
    customer_id: str                         # 10 digits, no dashes
    conversion_actions: Dict[str, str]       # kind -> conversion action id
    service_account: Dict[str, Any]          # the JSON key file's contents
    login_customer_id: Optional[str] = None  # manager account, if access is via one
    validate_only: bool = False

    @classmethod
    def from_env(cls, getenv: Callable[[str], Optional[str]]) -> Optional["GoogleAdsConfig"]:
        """None unless everything needed to upload is set."""
        customer = (getenv("GOOGLE_ADS_CUSTOMER_ID") or "").replace("-", "").strip()
        raw_key = (getenv("GOOGLE_ADS_SERVICE_ACCOUNT_JSON") or "").strip()
        actions = {
            SIGN_UP: (getenv("GOOGLE_ADS_SIGNUP_CONVERSION_ACTION_ID") or "").strip(),
            PURCHASE: (getenv("GOOGLE_ADS_PURCHASE_CONVERSION_ACTION_ID") or "").strip(),
        }
        actions = {k: v for k, v in actions.items() if v}
        if not (customer and raw_key and actions):
            return None
        try:
            key = json.loads(raw_key if raw_key.startswith("{") else base64.b64decode(raw_key))
        except Exception:
            logger.error("GOOGLE_ADS_SERVICE_ACCOUNT_JSON is neither JSON nor base64 JSON")
            return None
        login = (getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or "").replace("-", "").strip() or None
        validate = (getenv("GOOGLE_ADS_VALIDATE_ONLY") or "").strip().lower() in ("1", "true", "yes", "on")
        return cls(customer_id=customer, conversion_actions=actions, service_account=key,
                   login_customer_id=login, validate_only=validate)


class ConversionError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient


@dataclass
class _Token:
    value: str = ""
    expires_at: float = 0.0


class DataManagerClient:
    """Uploads events with a service-account key (no Google Ads developer
    token involved)."""

    def __init__(self, config: GoogleAdsConfig, session: Optional[requests.Session] = None,
                 timeout: float = 15):
        self.config = config
        self.session = session or requests.Session()
        self.timeout = timeout
        self._token = _Token()

    def _access_token(self) -> str:
        if self._token.value and time.time() < self._token.expires_at - 60:
            return self._token.value
        sa = self.config.service_account
        token_uri = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
        now = int(time.time())
        try:
            assertion = jwt.encode(
                {"iss": sa["client_email"], "scope": SCOPE, "aud": token_uri, "iat": now, "exp": now + 3600},
                sa["private_key"], algorithm="RS256",
                headers={"kid": sa["private_key_id"]} if sa.get("private_key_id") else None,
            )
        except Exception as exc:
            # A broken key won't fix itself; don't retry.
            raise ConversionError(f"service account key unusable: {exc}") from exc
        try:
            res = self.session.post(token_uri, timeout=self.timeout, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        except requests.RequestException as exc:
            raise ConversionError(f"token request failed: {exc}", transient=True) from exc
        if res.status_code != 200:
            raise ConversionError(f"token request -> {res.status_code}: {res.text[:300]}",
                                  status=res.status_code, transient=res.status_code >= 500)
        body = res.json()
        self._token = _Token(body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._token.value

    def destination(self, kind: str) -> Dict[str, Any]:
        dest: Dict[str, Any] = {
            "operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": self.config.customer_id},
            "productDestinationId": self.config.conversion_actions[kind],
        }
        if self.config.login_customer_id:
            dest["loginAccount"] = {"accountType": "GOOGLE_ADS", "accountId": self.config.login_customer_id}
        return dest

    def ingest(self, kind: str, event: Dict[str, Any]) -> Dict[str, Any]:
        body = {
            "destinations": [self.destination(kind)],
            "encoding": "HEX",
            "events": [event],
            "validateOnly": self.config.validate_only,
        }
        try:
            res = self.session.post(INGEST_URL, json=body, timeout=self.timeout, headers={
                "Authorization": f"Bearer {self._access_token()}", "User-Agent": USER_AGENT})
        except requests.RequestException as exc:
            raise ConversionError(f"ingest failed: {exc}", transient=True) from exc
        if res.status_code != 200:
            raise ConversionError(f"ingest -> {res.status_code}: {res.text[:500]}", status=res.status_code,
                                  transient=res.status_code == 429 or res.status_code >= 500)
        try:
            return res.json() if res.content else {}
        except ValueError:
            return {}


# --- recording + sending (DB session passed in) -------------------------------------

def save_attribution(session, user_id: int, payload: Optional[Mapping[str, Any]]):
    """Store a new user's attribution. Never raises: a malformed payload must
    not cost anyone their registration."""
    from models.base import UserAttribution
    try:
        values = clean_attribution(payload)
        if not values:
            return None
        row = session.get(UserAttribution, user_id)
        if row is None:
            row = UserAttribution(user_id=user_id, **values)
            session.add(row)
        session.commit()
        return row
    except Exception:
        logger.exception("Failed to save attribution for user %s", user_id)
        session.rollback()
        return None


def record_conversion(session, user_id: int, kind: str, transaction_id: str,
                      event_at: datetime.datetime, value: Optional[float] = None,
                      currency: Optional[str] = None):
    """Insert a pending AdConversion if this user came from an ad click within
    the window. Returns the row, or None (no click, out of window, already
    recorded). Never raises."""
    from models.base import AdConversion, UserAttribution
    from sqlalchemy.exc import IntegrityError
    try:
        attribution = session.get(UserAttribution, user_id)
        if not click_id(attribution) or not in_window(attribution, event_at):
            return None
        row = AdConversion(user_id=user_id, kind=kind, transaction_id=str(transaction_id)[:255],
                           value=value, currency=currency, event_at=event_at, status="pending")
        try:
            with session.begin_nested():
                session.add(row)
        except IntegrityError:
            return None     # webhook retry: already recorded
        session.commit()
        return row
    except Exception:
        logger.exception("Failed to record %s conversion for user %s", kind, user_id)
        session.rollback()
        return None


def record_signup(session, user_id: int, now: Optional[datetime.datetime] = None):
    return record_conversion(session, user_id, SIGN_UP, f"signup-{user_id}", now or _utcnow())


def record_purchase_from_event(session, user_id: int, event: Mapping[str, Any]):
    """A RevenueCat webhook event -> purchase conversion, when it is one."""
    if event.get("type") not in PURCHASE_EVENTS:
        return None
    if (event.get("environment") or "").upper() == "SANDBOX":
        return None
    price = event.get("price")
    if price is None or float(price) <= 0:
        return None     # free trials, refunds, unknown price
    ms = event.get("purchased_at_ms") or event.get("event_timestamp_ms")
    event_at = (datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).replace(tzinfo=None)
                if ms else _utcnow())
    txn = event.get("transaction_id") or event.get("original_transaction_id") or event.get("id")
    if not txn:
        return None
    return record_conversion(session, user_id, PURCHASE, txn, event_at,
                             value=round(float(price), 2), currency="USD")


def send_conversion(session, conversion_id: int, client: DataManagerClient) -> str:
    """Upload one AdConversion. Returns its new status. Raises
    ConversionError only for transient failures (the caller retries); a
    permanent failure is stored on the row as 'failed'."""
    from models.base import AdConversion, User, UserAttribution
    conv = session.get(AdConversion, conversion_id)
    if conv is None or conv.status in ("sent", "skipped"):
        return conv.status if conv else "missing"
    if conv.kind not in client.config.conversion_actions:
        conv.status, conv.last_error = "skipped", f"no conversion action configured for {conv.kind}"
        session.commit()
        return conv.status
    attribution = session.get(UserAttribution, conv.user_id)
    user = session.get(User, conv.user_id)
    event = build_event(conv, attribution, user.email if user else None)
    conv.attempts = (conv.attempts or 0) + 1
    try:
        response = client.ingest(conv.kind, event)
    except ConversionError as exc:
        conv.last_error = str(exc)[:2000]
        if not exc.transient:
            conv.status = "failed"
        session.commit()
        if exc.transient:
            raise
        return conv.status
    warnings = response.get("fieldWarnings") if isinstance(response, dict) else None
    if client.config.validate_only:
        # Dry run: Google accepted the shape; leave it pending for the real send.
        conv.last_error = f"validated{': ' + json.dumps(warnings)[:1500] if warnings else ''}"
    else:
        conv.status, conv.sent_at = "sent", _utcnow()
        conv.last_error = json.dumps(warnings)[:2000] if warnings else None
    session.commit()
    return conv.status
