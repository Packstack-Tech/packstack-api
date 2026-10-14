"""Keep user attributes in sync between Packstack, RevenueCat and Resend.

One writer per field:

* The Packstack DB owns identity: email, username, display name, signup date.
* RevenueCat owns billing. The webhook copies it onto ``user.subscription_*``
  (``billing_from_subscriber``) so everything downstream reads one place.
* Resend only receives. The exception is a contact's ``unsubscribed`` flag:
  Resend owns it and nothing here ever sets it to false.

Every push is a full snapshot, never a diff, so a sync is safe to repeat or
retry and a missed one is healed by the next.

We write to RevenueCat only for users it has reported a purchase for
(``in_revenuecat``). Writing attributes for a never-seen app user id creates
the customer, which would put API-made customers into RevenueCat's charts and
could race the SDK's own logIn during mobile onboarding.

Dependency-light on purpose (stdlib + requests): the workshop backfill imports
this module, so the one-time sync and the live sync can't drift apart.
"""
import datetime
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Set, Tuple
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

USER_AGENT = "packstack-user-sync/1.0"

# --- billing -----------------------------------------------------------------

STATUS_ACTIVE = "active"
STATUS_CANCELLED = "cancelled"          # auto-renew off, access until period end
STATUS_BILLING_ISSUE = "billing_issue"  # store retrying the charge, access kept
STATUS_EXPIRED = "expired"              # had access, doesn't now (incl. refunds)
ACCESS_STATUSES = {STATUS_ACTIVE, STATUS_CANCELLED, STATUS_BILLING_ISSUE}

# Segment keys. Each user is in exactly one; see billing_view().
SEGMENT_FREE = "free"
SEGMENT_PRO = "pro"
SEGMENT_LAPSED = "lapsed"
SEGMENTS = (SEGMENT_FREE, SEGMENT_PRO, SEGMENT_LAPSED)

# Product-id keywords, checked in order (annual before monthly so "12month"
# isn't read as monthly). Override exceptions with REVENUECAT_PRODUCT_PLANS.
_PLAN_KEYWORDS = (
    ("annual", ("annual", "year", "p1y", "12m", "12_month", "12month")),
    ("monthly", ("month", "p1m")),
    ("weekly", ("week", "p1w")),
)


def parse_ts(value: Optional[str]) -> Optional[datetime.datetime]:
    """RevenueCat ISO timestamp -> aware UTC datetime."""
    if not value:
        return None
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _naive_utc(dt: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """DB timestamps are naive UTC (see User.created_at)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def _iso_date(dt: Optional[datetime.datetime]) -> Optional[str]:
    return dt.date().isoformat() if dt else None


def entitlement_is_active(entitlement: Optional[dict],
                          now: Optional[datetime.datetime] = None) -> bool:
    """True if a v1 ``subscriber.entitlements[<id>]`` object grants access now.

    v1 returns expired entitlements too, so the dates decide: no expiry means
    lifetime; otherwise active until the later of the expiry and any billing
    grace period.
    """
    if not entitlement:
        return False
    now = now or _utcnow()
    expires = parse_ts(entitlement.get("expires_date"))
    if expires is None:
        return True
    grace = parse_ts(entitlement.get("grace_period_expires_date"))
    return max(expires, grace or expires) > now


def plan_for(product_id: Optional[str], store: Optional[str],
             expires_at: Optional[datetime.datetime],
             period_start: Optional[datetime.datetime] = None,
             period_type: Optional[str] = None,
             overrides: Optional[Mapping[str, str]] = None) -> str:
    """lifetime | annual | monthly | weekly | promotional | subscription."""
    if overrides and product_id in overrides:
        return overrides[product_id]
    if (store or "").lower() == "promotional":
        return "promotional"
    if expires_at is None:
        return "lifetime"
    pid = (product_id or "").lower()
    for plan, words in _PLAN_KEYWORDS:
        if any(w in pid for w in words):
            return plan
    # No telling name: infer from the current period's length. Trials and
    # intro offers have their own lengths, so only trust a normal period.
    if period_start and (period_type or "normal").lower() == "normal":
        days = (expires_at - period_start).days
        if 5 <= days <= 9:
            return "weekly"
        if 26 <= days <= 33:
            return "monthly"
        if 360 <= days <= 370:
            return "annual"
    return "subscription"


@dataclass(frozen=True)
class Billing:
    """What RevenueCat says about a customer's paid entitlement."""
    active: bool
    status: Optional[str] = None        # None: never had the entitlement
    plan: Optional[str] = None
    product_id: Optional[str] = None
    store: Optional[str] = None         # RevenueCat store, lowercased
    expires_at: Optional[datetime.datetime] = None   # None: lifetime / none
    started_at: Optional[datetime.datetime] = None   # first purchase ("Pro since")

    def columns(self, now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        """``user.subscription_*`` values. Deliberately excludes is_subscribed:
        the webhook owns that flag and decides it with its own fallbacks."""
        return {
            "subscription_status": self.status,
            "subscription_plan": self.plan,
            "subscription_product_id": self.product_id[:200] if self.product_id else None,
            "subscription_store": self.store[:20] if self.store else None,
            "subscription_expires_at": _naive_utc(self.expires_at),
            "subscription_started_at": _naive_utc(self.started_at),
            "subscription_synced_at": _naive_utc(now or _utcnow()),
        }


def _earliest_purchase(subscriptions: dict, non_subscriptions: dict) -> Optional[datetime.datetime]:
    dates = []
    for sub in subscriptions.values():
        if not sub.get("is_sandbox"):
            dates.append(parse_ts(sub.get("original_purchase_date") or sub.get("purchase_date")))
    for purchases in non_subscriptions.values():
        for p in purchases or []:
            if not p.get("is_sandbox"):
                dates.append(parse_ts(p.get("purchase_date")))
    dates = [d for d in dates if d]
    return min(dates) if dates else None


def billing_from_subscriber(subscriber: dict, entitlement_id: str,
                            plan_overrides: Optional[Mapping[str, str]] = None,
                            now: Optional[datetime.datetime] = None) -> Billing:
    """Billing state from a v1 ``GET /subscribers/{id}`` ``subscriber`` object."""
    now = now or _utcnow()
    entitlement = (subscriber.get("entitlements") or {}).get(entitlement_id)
    if not entitlement:
        return Billing(active=False)

    subscriptions = subscriber.get("subscriptions") or {}
    non_subscriptions = subscriber.get("non_subscriptions") or {}
    product_id = entitlement.get("product_identifier")
    sub = subscriptions.get(product_id)
    one_time = non_subscriptions.get(product_id) or []
    latest_one_time = max(one_time, key=lambda p: p.get("purchase_date") or "") if one_time else None
    store = ((sub or latest_one_time or {}).get("store") or "").lower() or None

    active = entitlement_is_active(entitlement, now)
    expires_at = parse_ts(entitlement.get("expires_date"))
    if not active:
        status = STATUS_EXPIRED
    elif sub and sub.get("billing_issues_detected_at"):
        status = STATUS_BILLING_ISSUE
    elif expires_at is None:
        status = STATUS_ACTIVE
    elif sub and sub.get("unsubscribe_detected_at"):
        status = STATUS_CANCELLED
    else:
        status = STATUS_ACTIVE

    period_start = parse_ts(entitlement.get("purchase_date") or (sub or {}).get("purchase_date"))
    plan = plan_for(product_id, store, expires_at, period_start,
                    (sub or {}).get("period_type"), plan_overrides)
    started_at = _earliest_purchase(subscriptions, non_subscriptions) or period_start
    return Billing(active=active, status=status, plan=plan, product_id=product_id,
                   store=store, expires_at=expires_at, started_at=started_at)


def store_label(store: Optional[str]) -> Optional[str]:
    """Where the customer manages the purchase: app_store | play_store | web | …"""
    s = (store or "").lower()
    if not s:
        return None
    if s in ("app_store", "mac_app_store"):
        return "app_store"
    if s in ("rc_billing", "stripe", "paddle"):
        return "web"
    return s


@dataclass(frozen=True)
class BillingView:
    """A user's billing as Resend sees it, read from the DB columns."""
    status: str                      # free | active | cancelled | billing_issue | expired
    plan: str                        # none | manual | lifetime | annual | monthly | …
    store: Optional[str]
    started_at: Optional[datetime.datetime]
    period_ends_at: Optional[datetime.datetime]
    segment: str                     # free | pro | lapsed


def billing_view(user) -> BillingView:
    """is_subscribed decides access; subscription_* describes the purchase.

    is_subscribed without a RevenueCat purchase behind it (legacy Stripe era or
    a manual grant) is reported as plan "manual".
    """
    status = user.subscription_status
    store = store_label(user.subscription_store)
    started = user.subscription_started_at
    ends = user.subscription_expires_at
    if user.is_subscribed:
        if status in ACCESS_STATUSES:
            return BillingView(status, user.subscription_plan or "subscription",
                               store, started, ends, SEGMENT_PRO)
        return BillingView(STATUS_ACTIVE, "manual", None, None, None, SEGMENT_PRO)
    if status:
        return BillingView(STATUS_EXPIRED, user.subscription_plan or "none",
                           store, started, ends, SEGMENT_LAPSED)
    return BillingView("free", "none", None, None, None, SEGMENT_FREE)


def in_revenuecat(user) -> bool:
    """RevenueCat has reported a purchase for this user (the webhook or the
    backfill wrote subscription_status), so writing attributes can't create
    a customer. is_subscribed alone isn't enough: legacy Stripe and manual
    grants set it without RevenueCat ever seeing the user."""
    return user.subscription_status is not None


# --- attribute snapshots -------------------------------------------------------

# Cleared in RevenueCat when an account is deleted. Purchases stay (they're
# revenue records); the personal data goes.
REVENUECAT_PII_KEYS = ("$email", "$displayName", "username")


def revenuecat_attributes(user) -> Dict[str, Optional[str]]:
    """Subscriber attributes. $email makes customers searchable by email in the
    dashboard; $displayName replaces the numeric id on the customer page."""
    return {
        "$email": user.email,
        "$displayName": user.display_name or user.username,
        "username": user.username,
    }


# (key, type, fallback) -- the Resend contact properties this sync writes.
# Resend rejects a property that doesn't exist yet, so these are created by
# `python -m user_attribute_sync.run setup` before the API deploy.
RESEND_PROPERTY_SCHEMA: Tuple[Tuple[str, str, Optional[Any]], ...] = (
    ("user_id", "number", None),
    ("username", "string", None),
    ("display_name", "string", None),
    ("signed_up_at", "string", None),      # YYYY-MM-DD
    ("plan_status", "string", "free"),     # free|active|cancelled|billing_issue|expired
    ("plan", "string", "none"),            # none|manual|lifetime|annual|monthly|weekly|promotional|subscription
    ("store", "string", None),             # app_store|play_store|web|promotional
    ("pro_since", "string", None),         # YYYY-MM-DD of first purchase
    ("period_ends_at", "string", None),    # YYYY-MM-DD renewal/end; empty for lifetime
)


def resend_properties(user) -> Dict[str, Any]:
    """Contact properties. None clears a value. first_name/last_name and
    unsubscribed are never sent: the DB doesn't store names (Google/Apple
    names are set once at contact creation) and Resend owns opt-outs."""
    view = billing_view(user)
    return {
        "user_id": user.id,
        "username": user.username,
        "display_name": user.display_name or None,
        "signed_up_at": _iso_date(user.created_at),
        "plan_status": view.status,
        "plan": view.plan,
        "store": view.store,
        "pro_since": _iso_date(view.started_at),
        "period_ends_at": _iso_date(view.period_ends_at),
    }


# --- HTTP ----------------------------------------------------------------------

class SyncError(Exception):
    """A failed call. ``transient`` (network, 429, 5xx) means retry later;
    otherwise the request itself is wrong and retrying won't help."""

    def __init__(self, message: str, status: Optional[int] = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient


class _Http:
    def __init__(self, base_url: str, token: str, session: Optional[requests.Session] = None,
                 min_interval: float = 0.0, max_rate_limit_retries: int = 4, timeout: float = 10):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        self.min_interval = min_interval
        self.max_rate_limit_retries = max_rate_limit_retries
        self.timeout = timeout
        self._last = 0.0
        self.calls = 0

    def _throttle(self):
        if self.min_interval:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self._last = time.monotonic()

    def request(self, method: str, path: str, json: Optional[dict] = None,
                allow: Iterable[int] = ()) -> Tuple[int, Any]:
        """(status, parsed body). 2xx and ``allow`` statuses return; anything
        else raises SyncError."""
        allow = set(allow)
        for attempt in range(self.max_rate_limit_retries + 1):
            self._throttle()
            self.calls += 1
            try:
                res = self.session.request(method, self.base_url + path, json=json,
                                           headers=self.headers, timeout=self.timeout)
            except requests.RequestException as exc:
                raise SyncError(f"{method} {path}: {exc}", transient=True) from exc
            if res.status_code == 429 and attempt < self.max_rate_limit_retries:
                try:
                    delay = float(res.headers.get("retry-after") or 1)
                except ValueError:
                    delay = 1.0
                time.sleep(min(max(delay, 0.5), 30))
                continue
            break
        status = res.status_code
        try:
            body = res.json() if res.content else None
        except ValueError:
            body = res.text
        if 200 <= status < 300 or status in allow:
            return status, body
        transient = status == 429 or status >= 500
        raise SyncError(f"{method} {path} -> {status}: {str(body)[:300]}",
                        status=status, transient=transient)


def _seg(value: str) -> str:
    return quote(str(value), safe="@")


class RevenueCatClient(_Http):
    """RevenueCat REST v1 with a secret (sk_) key."""

    def __init__(self, secret_key: str, **kwargs):
        super().__init__("https://api.revenuecat.com/v1", secret_key, **kwargs)

    def get_subscriber(self, app_user_id: str) -> dict:
        """Note: creates the customer if RevenueCat has never seen this id."""
        _, body = self.request("GET", f"/subscribers/{_seg(app_user_id)}")
        return (body or {}).get("subscriber") or {}

    def set_attributes(self, app_user_id: str, attributes: Mapping[str, Optional[str]]) -> None:
        """None or "" deletes the attribute."""
        now_ms = int(time.time() * 1000)
        payload = {"attributes": {
            key: {"value": value if value is not None else "", "updated_at_ms": now_ms}
            for key, value in attributes.items()
        }}
        self.request("POST", f"/subscribers/{_seg(app_user_id)}/attributes", json=payload)


class ResendClient(_Http):
    """Resend global contacts, contact properties and segments."""

    def __init__(self, api_key: str, **kwargs):
        super().__init__("https://api.resend.com", api_key, **kwargs)

    # contacts
    def get_contact(self, email: str) -> Optional[dict]:
        status, body = self.request("GET", f"/contacts/{_seg(email)}", allow=(404,))
        return None if status == 404 else body

    def update_contact(self, email: str, properties: Mapping[str, Any]) -> bool:
        """False if there's no such contact."""
        status, _ = self.request("PATCH", f"/contacts/{_seg(email)}",
                                 json={"properties": dict(properties)}, allow=(404,))
        return status != 404

    def create_contact(self, email: str, properties: Mapping[str, Any],
                       unsubscribed: Optional[bool] = None,
                       first_name: Optional[str] = None,
                       last_name: Optional[str] = None) -> bool:
        """False if a contact with this email already exists."""
        body: Dict[str, Any] = {
            "email": email,
            "properties": {k: v for k, v in properties.items() if v is not None},
        }
        if unsubscribed:
            body["unsubscribed"] = True
        if first_name:
            body["first_name"] = first_name
        if last_name:
            body["last_name"] = last_name
        status, _ = self.request("POST", "/contacts", json=body, allow=(409, 422))
        return status not in (409, 422)

    def remove_contact(self, email: str) -> bool:
        status, _ = self.request("DELETE", f"/contacts/{_seg(email)}", allow=(404,))
        return status != 404

    def upsert_contact(self, email: str, properties: Mapping[str, Any], create: bool = True) -> str:
        """'updated' | 'created' | 'missing' (absent and create=False)."""
        if self.update_contact(email, properties):
            return "updated"
        if not create:
            return "missing"
        if self.create_contact(email, properties):
            return "created"
        # Created by someone else between our PATCH and POST.
        if self.update_contact(email, properties):
            return "updated"
        raise SyncError(f"contact {email} neither updatable nor creatable")

    def rename_contact(self, old_email: str, new_email: str,
                       properties: Mapping[str, Any], create: bool = True) -> str:
        """Move a contact to a new address. Resend can't change a contact's
        email, so: create the new one carrying the old one's opt-out and
        names, then delete the old one. Safe to repeat."""
        old = self.get_contact(old_email)
        if old is None:
            return self.upsert_contact(new_email, properties, create=create)
        unsubscribed = bool(old.get("unsubscribed"))
        if self.update_contact(new_email, properties):
            if unsubscribed:
                # Honour the opt-out. We only ever set this to true.
                self.request("PATCH", f"/contacts/{_seg(new_email)}", json={"unsubscribed": True})
        elif not self.create_contact(new_email, properties, unsubscribed=unsubscribed,
                                     first_name=old.get("first_name"),
                                     last_name=old.get("last_name")):
            self.update_contact(new_email, properties)
        self.remove_contact(old_email)
        return "renamed"

    # segments
    def contact_segment_ids(self, email: str) -> Set[str]:
        status, body = self.request("GET", f"/contacts/{_seg(email)}/segments?limit=100", allow=(404,))
        if status == 404:
            return set()
        return {s["id"] for s in (body or {}).get("data", [])}

    def add_to_segment(self, email: str, segment_id: str) -> None:
        self.request("POST", f"/contacts/{_seg(email)}/segments/{_seg(segment_id)}", allow=(409, 422))

    def remove_from_segment(self, email: str, segment_id: str) -> None:
        self.request("DELETE", f"/contacts/{_seg(email)}/segments/{_seg(segment_id)}", allow=(404,))

    # setup
    def _list(self, path: str) -> list:
        items, after = [], None
        while True:
            q = f"{path}?limit=100" + (f"&after={after}" if after else "")
            _, body = self.request("GET", q)
            data = (body or {}).get("data", [])
            items.extend(data)
            if not (body or {}).get("has_more") or not data:
                return items
            after = data[-1]["id"]

    def list_contact_properties(self) -> list:
        return self._list("/contact-properties")

    def create_contact_property(self, key: str, type_: str, fallback: Optional[Any] = None) -> dict:
        body: Dict[str, Any] = {"key": key, "type": type_}
        if fallback is not None:
            body["fallback_value"] = fallback
        _, res = self.request("POST", "/contact-properties", json=body)
        return res

    def list_segments(self) -> list:
        return self._list("/segments")

    def create_segment(self, name: str) -> dict:
        _, res = self.request("POST", "/segments", json={"name": name})
        return res


# --- orchestration -------------------------------------------------------------

def sync_segments(client: ResendClient, email: str, target: str,
                  segment_ids: Mapping[str, str]) -> None:
    """Put the contact in exactly one of the managed plan segments. Segments
    we don't manage are left alone."""
    managed = {sid for sid in segment_ids.values() if sid}
    if not managed:
        return
    want = segment_ids.get(target)
    have = client.contact_segment_ids(email) & managed
    for sid in have - {want}:
        client.remove_from_segment(email, sid)
    if want and want not in have:
        client.add_to_segment(email, want)


def push_resend(client: ResendClient, user, previous_email: Optional[str] = None,
                segment_ids: Optional[Mapping[str, str]] = None,
                create_missing: bool = True) -> str:
    props = resend_properties(user)
    if previous_email and previous_email.strip().lower() != (user.email or "").strip().lower():
        outcome = client.rename_contact(previous_email, user.email, props, create=create_missing)
    else:
        outcome = client.upsert_contact(user.email, props, create=create_missing)
    if outcome != "missing" and segment_ids:
        sync_segments(client, user.email, billing_view(user).segment, segment_ids)
    return outcome


def push_revenuecat(client: RevenueCatClient, user) -> bool:
    if not in_revenuecat(user):
        return False
    client.set_attributes(str(user.id), revenuecat_attributes(user))
    return True


def sync_user(user, resend: Optional[ResendClient] = None,
              revenuecat: Optional[RevenueCatClient] = None,
              previous_email: Optional[str] = None,
              segment_ids: Optional[Mapping[str, str]] = None,
              create_missing: bool = True) -> Dict[str, Any]:
    """Push one user's snapshot to whichever services are configured. Raises
    the first SyncError; everything before it has already been applied, and
    re-running is harmless."""
    result: Dict[str, Any] = {}
    if revenuecat is not None:
        result["revenuecat"] = push_revenuecat(revenuecat, user)
    if resend is not None:
        result["resend"] = push_resend(resend, user, previous_email=previous_email,
                                       segment_ids=segment_ids, create_missing=create_missing)
    return result
