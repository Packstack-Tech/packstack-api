"""RevenueCat webhook -> user.is_subscribed and user.subscription_*.

    source testenv.sh && python -m pytest tests/test_revenuecat_webhook.py -q

RevenueCat's API is stubbed (monkeypatch revenuecat.fetch_subscriber) and the
attribute-sync enqueue is captured; nothing here touches the network.
"""

import datetime
import secrets

import pytest
from fastapi.testclient import TestClient
from fastapi_sqlalchemy import db

import main
from api import webhook
from models.base import User
from utils import revenuecat
from utils.consts import REVENUECAT_ENTITLEMENT_ID, REVENUECAT_WEBHOOK_SECRET
from utils.revenuecat import entitlement_is_active

HEADERS = {"Authorization": f"Bearer {REVENUECAT_WEBHOOK_SECRET}"} if REVENUECAT_WEBHOOK_SECRET else {}


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def make_user(subscribed=False):
    with db():
        suffix = secrets.token_hex(4)
        u = User(email=f"rc-{suffix}@example.com", username=f"rc{suffix}", email_verified=True,
                 is_subscribed=subscribed)
        db.session.add(u)
        db.session.commit()
        return u.id


def subscribed(user_id):
    with db():
        return db.session.query(User).get(user_id).is_subscribed


def billing(user_id):
    with db():
        u = db.session.query(User).get(user_id)
        return (u.subscription_status, u.subscription_plan, u.subscription_store)


def send(client, event):
    r = client.post("/webhook/revenuecat", json={"event": event}, headers=HEADERS)
    assert r.status_code == 200, r.text


def ev(type_, user_id, **extra):
    return {"type": type_, "app_user_id": str(user_id),
            "entitlement_ids": [REVENUECAT_ENTITLEMENT_ID], **extra}


@pytest.fixture(autouse=True)
def enqueued(monkeypatch):
    """User ids the webhook asked to push to RevenueCat/Resend."""
    calls = []
    monkeypatch.setattr(webhook, "enqueue_user_sync", lambda user_id, *a, **k: calls.append(user_id))
    return calls


@pytest.fixture
def no_api(monkeypatch):
    """No secret key configured / lookup failed: event-type fallback."""
    monkeypatch.setattr(revenuecat, "fetch_subscriber", lambda _id: None)


def _subscriber(active):
    """A v1 subscriber whose entitlement is (in)active: an annual App Store
    plan for True, an expired one for False, no entitlement for None."""
    if active is None:
        return {"entitlements": {}}
    expires = "2099-01-01T00:00:00Z" if active else "2020-01-01T00:00:00Z"
    return {
        "entitlements": {REVENUECAT_ENTITLEMENT_ID: {
            "expires_date": expires, "product_identifier": "pro_annual",
            "purchase_date": "2019-01-01T00:00:00Z"}},
        "subscriptions": {"pro_annual": {
            "expires_date": expires, "purchase_date": "2019-01-01T00:00:00Z",
            "original_purchase_date": "2019-01-01T00:00:00Z", "store": "app_store",
            "period_type": "normal", "is_sandbox": False}},
    }


def api_says(monkeypatch, answers):
    monkeypatch.setattr(revenuecat, "fetch_subscriber",
                        lambda app_user_id: _subscriber(answers[str(app_user_id)]))


# --- fallback (no API key) ------------------------------------------------------

def test_fallback_purchase_grants(client, no_api):
    uid = make_user()
    send(client, ev("INITIAL_PURCHASE", uid))
    assert subscribed(uid) is True


def test_fallback_billing_issue_keeps_access(client, no_api):
    """Grace period: the store is still retrying the charge."""
    uid = make_user(subscribed=True)
    send(client, ev("BILLING_ISSUE", uid))
    assert subscribed(uid) is True


def test_fallback_cancellation_keeps_access(client, no_api):
    """Auto-renew off: access continues until the period ends."""
    uid = make_user(subscribed=True)
    send(client, ev("CANCELLATION", uid))
    assert subscribed(uid) is True


def test_fallback_expiration_revokes(client, no_api):
    uid = make_user(subscribed=True)
    send(client, ev("EXPIRATION", uid))
    assert subscribed(uid) is False


def test_fallback_ignores_other_entitlements(client, no_api):
    uid = make_user(subscribed=True)
    send(client, ev("EXPIRATION", uid, entitlement_ids=["something_else"]))
    assert subscribed(uid) is True


def test_transfer_without_api_changes_nothing(client, no_api):
    a, b = make_user(subscribed=True), make_user()
    send(client, {"type": "TRANSFER", "transferred_from": [str(a)], "transferred_to": [str(b)]})
    assert subscribed(a) is True and subscribed(b) is False


# --- with RevenueCat's API ------------------------------------------------------

def test_api_expiration_but_lifetime_still_active(client, monkeypatch):
    """Monthly plan expired, but a lifetime purchase still grants access."""
    uid = make_user(subscribed=True)
    api_says(monkeypatch, {str(uid): True})
    send(client, ev("EXPIRATION", uid))
    assert subscribed(uid) is True


def test_api_purchase_writes_billing_and_enqueues_sync(client, monkeypatch, enqueued):
    uid = make_user()
    api_says(monkeypatch, {str(uid): True})
    send(client, ev("INITIAL_PURCHASE", uid))
    assert subscribed(uid) is True
    assert billing(uid) == ("active", "annual", "app_store")
    assert enqueued == [uid]


def test_api_cancellation_marks_cancelled_but_keeps_access(client, monkeypatch):
    uid = make_user(subscribed=True)
    sub = _subscriber(True)
    sub["subscriptions"]["pro_annual"]["unsubscribe_detected_at"] = "2026-10-01T00:00:00Z"
    monkeypatch.setattr(revenuecat, "fetch_subscriber", lambda _id: sub)
    send(client, ev("CANCELLATION", uid))
    assert subscribed(uid) is True
    assert billing(uid)[0] == "cancelled"


def test_api_expiry_marks_expired(client, monkeypatch):
    uid = make_user(subscribed=True)
    api_says(monkeypatch, {str(uid): False})
    send(client, ev("EXPIRATION", uid))
    assert subscribed(uid) is False
    assert billing(uid) == ("expired", "annual", "app_store")


def test_fallback_leaves_billing_columns_alone(client, no_api, enqueued):
    uid = make_user()
    send(client, ev("INITIAL_PURCHASE", uid))
    assert subscribed(uid) is True
    assert billing(uid) == (None, None, None)
    assert enqueued == [uid]   # Resend still learns plan_status from is_subscribed


def test_no_change_event_enqueues_nothing(client, no_api, enqueued):
    uid = make_user(subscribed=True)
    send(client, ev("BILLING_ISSUE", uid))
    assert enqueued == []


def test_api_cancellation_refund_revokes(client, monkeypatch):
    """A refund arrives as CANCELLATION and removes the entitlement at once."""
    uid = make_user(subscribed=True)
    api_says(monkeypatch, {str(uid): False})
    send(client, ev("CANCELLATION", uid, cancel_reason="CUSTOMER_SUPPORT"))
    assert subscribed(uid) is False


def test_api_transfer_syncs_both_users(client, monkeypatch):
    a, b = make_user(subscribed=True), make_user()
    api_says(monkeypatch, {str(a): False, str(b): True})
    send(client, {"type": "TRANSFER", "transferred_from": [str(a)], "transferred_to": [str(b)]})
    assert subscribed(a) is False and subscribed(b) is True


def test_test_and_anonymous_events_ignored(client, no_api):
    send(client, {"type": "TEST", "app_user_id": "abc"})
    send(client, {"type": "INITIAL_PURCHASE", "app_user_id": "$RCAnonymousID:123"})


def test_bad_secret_rejected(client):
    if not REVENUECAT_WEBHOOK_SECRET:
        pytest.skip("no webhook secret configured")
    r = client.post("/webhook/revenuecat", json={"event": {}}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


# --- entitlement date logic -------------------------------------------------------

NOW = datetime.datetime(2026, 10, 9, tzinfo=datetime.timezone.utc)


@pytest.mark.parametrize("ent,expected", [
    (None, False),
    ({"expires_date": None}, True),                                   # lifetime
    ({"expires_date": "2026-11-09T00:00:00Z"}, True),                 # in period
    ({"expires_date": "2026-10-01T00:00:00Z"}, False),                # lapsed
    ({"expires_date": "2026-10-01T00:00:00Z",
      "grace_period_expires_date": "2026-10-20T00:00:00Z"}, True),    # in grace
    ({"expires_date": "2026-10-01T00:00:00Z",
      "grace_period_expires_date": "2026-10-05T00:00:00Z"}, False),   # grace over
])
def test_entitlement_is_active(ent, expected):
    assert entitlement_is_active(ent, now=NOW) is expected
