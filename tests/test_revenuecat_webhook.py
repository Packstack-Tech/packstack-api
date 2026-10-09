"""RevenueCat webhook -> user.is_subscribed.

    source testenv.sh && python -m pytest tests/test_revenuecat_webhook.py -q

RevenueCat's API is stubbed (monkeypatch revenuecat.fetch_entitlement_active);
nothing here touches the network.
"""

import datetime
import secrets

import pytest
from fastapi.testclient import TestClient
from fastapi_sqlalchemy import db

import main
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


def send(client, event):
    r = client.post("/webhook/revenuecat", json={"event": event}, headers=HEADERS)
    assert r.status_code == 200, r.text


def ev(type_, user_id, **extra):
    return {"type": type_, "app_user_id": str(user_id),
            "entitlement_ids": [REVENUECAT_ENTITLEMENT_ID], **extra}


@pytest.fixture
def no_api(monkeypatch):
    """No secret key configured / lookup failed: event-type fallback."""
    monkeypatch.setattr(revenuecat, "fetch_entitlement_active", lambda _id: None)


def api_says(monkeypatch, answers):
    monkeypatch.setattr(revenuecat, "fetch_entitlement_active", lambda app_user_id: answers[str(app_user_id)])


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
