"""utils/user_sync.py: billing derivation, attribute snapshots, and the
Resend/RevenueCat clients against a fake HTTP session.

    source testenv.sh && python -m pytest tests/test_user_sync.py -q

No database, no network.
"""
import datetime
from types import SimpleNamespace

import pytest

from utils import user_sync
from utils.user_sync import (
    ResendClient, RevenueCatClient, SyncError, billing_from_subscriber, billing_view,
    in_revenuecat, plan_for, resend_properties, revenuecat_attributes,
)

ENT = "Full Access"
NOW = datetime.datetime(2026, 10, 13, tzinfo=datetime.timezone.utc)


def ts(days_from_now):
    return (NOW + datetime.timedelta(days=days_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_user(**kw):
    base = dict(
        id=42, email="hiker@example.com", username="trailfox", display_name="Ada",
        created_at=datetime.datetime(2025, 5, 1, 12, 0), is_subscribed=False,
        subscription_status=None, subscription_plan=None, subscription_product_id=None,
        subscription_store=None, subscription_expires_at=None, subscription_started_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- billing_from_subscriber ------------------------------------------------------

def test_no_entitlement_is_never_purchased():
    b = billing_from_subscriber({"entitlements": {}}, ENT, now=NOW)
    assert b.active is False and b.status is None and b.columns()["subscription_status"] is None


def test_lifetime_web_purchase():
    sub = {
        "entitlements": {ENT: {"expires_date": None, "product_identifier": "pro_lifetime",
                               "purchase_date": ts(-30)}},
        "non_subscriptions": {"pro_lifetime": [{"purchase_date": ts(-30), "store": "rc_billing",
                                                "is_sandbox": False}]},
    }
    b = billing_from_subscriber(sub, ENT, now=NOW)
    assert (b.active, b.status, b.plan, b.store) == (True, "active", "lifetime", "rc_billing")
    assert b.expires_at is None
    assert b.started_at.date() == (NOW - datetime.timedelta(days=30)).date()


def monthly(**sub_extra):
    sub = {"expires_date": ts(10), "purchase_date": ts(-20), "original_purchase_date": ts(-200),
           "period_type": "normal", "store": "app_store", "is_sandbox": False}
    sub.update(sub_extra)
    return {
        "entitlements": {ENT: {"expires_date": sub["expires_date"], "product_identifier": "pro_monthly",
                               "purchase_date": sub["purchase_date"]}},
        "subscriptions": {"pro_monthly": sub},
    }


def test_active_monthly():
    b = billing_from_subscriber(monthly(), ENT, now=NOW)
    assert (b.status, b.plan, b.store) == ("active", "monthly", "app_store")
    assert b.started_at.date() == (NOW - datetime.timedelta(days=200)).date()   # original purchase


def test_cancelled_keeps_access_until_period_end():
    b = billing_from_subscriber(monthly(unsubscribe_detected_at=ts(-1)), ENT, now=NOW)
    assert b.active is True and b.status == "cancelled"


def test_billing_issue_in_grace():
    data = monthly(expires_date=ts(-1), billing_issues_detected_at=ts(-1))
    data["entitlements"][ENT]["grace_period_expires_date"] = ts(5)
    b = billing_from_subscriber(data, ENT, now=NOW)
    assert b.active is True and b.status == "billing_issue"


def test_expired():
    b = billing_from_subscriber(monthly(expires_date=ts(-3)), ENT, now=NOW)
    assert b.active is False and b.status == "expired" and b.plan == "monthly"


def test_promotional_grant():
    sub = {
        "entitlements": {ENT: {"expires_date": None, "product_identifier": "rc_promo_Full Access_lifetime",
                               "purchase_date": ts(-2)}},
        "subscriptions": {"rc_promo_Full Access_lifetime": {"store": "promotional", "expires_date": None,
                                                             "purchase_date": ts(-2)}},
    }
    b = billing_from_subscriber(sub, ENT, now=NOW)
    assert (b.status, b.plan, b.store) == ("active", "promotional", "promotional")


@pytest.mark.parametrize("product_id,days,expected", [
    ("io.packstack.pro.annual", 365, "annual"),
    ("pro_yearly", 365, "annual"),
    ("pro:p1y", 365, "annual"),
    ("pro_12month", 365, "annual"),
    ("pro_monthly", 30, "monthly"),
    ("pro_p1m", 30, "monthly"),
    ("plan_a", 31, "monthly"),        # no keyword: period length decides
    ("plan_b", 365, "annual"),
    ("plan_c", 7, "weekly"),
    ("plan_d", 90, "subscription"),
])
def test_plan_for(product_id, days, expected):
    start = NOW
    assert plan_for(product_id, "play_store", start + datetime.timedelta(days=days), start, "normal") == expected


def test_plan_for_trial_period_does_not_guess_from_length():
    assert plan_for("plan_a", "app_store", NOW + datetime.timedelta(days=7), NOW, "trial") == "subscription"


def test_plan_override_wins():
    assert plan_for("weird_sku", "app_store", NOW, NOW, overrides={"weird_sku": "annual"}) == "annual"


def test_columns_are_naive_utc():
    cols = billing_from_subscriber(monthly(), ENT, now=NOW).columns(now=NOW)
    assert cols["subscription_expires_at"].tzinfo is None
    assert cols["subscription_synced_at"] == NOW.replace(tzinfo=None)
    assert "is_subscribed" not in cols


# --- billing_view / snapshots -------------------------------------------------------

def test_view_free():
    v = billing_view(make_user())
    assert (v.status, v.plan, v.segment) == ("free", "none", "free")


def test_view_pro_from_revenuecat():
    u = make_user(is_subscribed=True, subscription_status="cancelled", subscription_plan="annual",
                  subscription_store="play_store", subscription_expires_at=datetime.datetime(2027, 1, 1))
    v = billing_view(u)
    assert (v.status, v.plan, v.store, v.segment) == ("cancelled", "annual", "play_store", "pro")


def test_view_manual_grant_without_revenuecat():
    v = billing_view(make_user(is_subscribed=True))
    assert (v.status, v.plan, v.segment) == ("active", "manual", "pro")


def test_view_manual_grant_over_expired_purchase():
    v = billing_view(make_user(is_subscribed=True, subscription_status="expired", subscription_plan="monthly"))
    assert (v.status, v.plan) == ("active", "manual")


def test_view_lapsed():
    v = billing_view(make_user(subscription_status="expired", subscription_plan="monthly",
                               subscription_store="rc_billing"))
    assert (v.status, v.plan, v.store, v.segment) == ("expired", "monthly", "web", "lapsed")


def test_resend_properties_match_schema():
    props = resend_properties(make_user(is_subscribed=True, subscription_status="active",
                                        subscription_plan="lifetime", subscription_store="app_store",
                                        subscription_started_at=datetime.datetime(2026, 2, 3)))
    assert set(props) == {key for key, _, _ in user_sync.RESEND_PROPERTY_SCHEMA}
    assert props == {
        "user_id": 42, "username": "trailfox", "display_name": "Ada", "signed_up_at": "2025-05-01",
        "plan_status": "active", "plan": "lifetime", "store": "app_store", "pro_since": "2026-02-03",
        "period_ends_at": None,
    }


def test_revenuecat_attributes_fall_back_to_username():
    attrs = revenuecat_attributes(make_user(display_name=None))
    assert attrs == {"$email": "hiker@example.com", "$displayName": "trailfox", "username": "trailfox"}


def test_in_revenuecat_only_once_revenuecat_reported_a_purchase():
    assert not in_revenuecat(make_user())
    assert not in_revenuecat(make_user(is_subscribed=True))      # manual / legacy grant
    assert in_revenuecat(make_user(is_subscribed=True, subscription_status="active"))
    assert in_revenuecat(make_user(subscription_status="expired"))


# --- clients against a fake HTTP session ----------------------------------------------

class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = b"x" if body is not None else b""
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    """Answers (method, path) from a script; records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        path = url.split(".com", 1)[1]
        self.calls.append((method, path, json))
        answer = self.routes.get((method, path))
        if isinstance(answer, list):
            answer = answer.pop(0)
        if answer is None:
            raise AssertionError(f"unexpected {method} {path}")
        return answer


def resend(routes):
    session = FakeSession(routes)
    return ResendClient("re_test", session=session), session


def test_upsert_updates_existing_contact():
    client, s = resend({("PATCH", "/contacts/hiker@example.com"): FakeResponse(200, {"id": "c1"})})
    assert client.upsert_contact("hiker@example.com", {"plan": "none"}) == "updated"
    assert s.calls == [("PATCH", "/contacts/hiker@example.com", {"properties": {"plan": "none"}})]


def test_upsert_creates_missing_contact_without_nulls():
    client, s = resend({
        ("PATCH", "/contacts/hiker@example.com"): FakeResponse(404, {"message": "not found"}),
        ("POST", "/contacts"): FakeResponse(201, {"id": "c1"}),
    })
    assert client.upsert_contact("hiker@example.com", {"plan": "none", "store": None}) == "created"
    assert s.calls[-1] == ("POST", "/contacts", {"email": "hiker@example.com", "properties": {"plan": "none"}})


def test_upsert_can_skip_creation():
    client, _ = resend({("PATCH", "/contacts/a@b.co"): FakeResponse(404, {})})
    assert client.upsert_contact("a@b.co", {}, create=False) == "missing"


def test_plus_addresses_are_escaped():
    client, s = resend({("PATCH", "/contacts/a%2Bb@c.co"): FakeResponse(200, {})})
    client.update_contact("a+b@c.co", {})
    assert s.calls[0][1] == "/contacts/a%2Bb@c.co"


def test_rename_carries_opt_out_and_names_then_deletes_old():
    client, s = resend({
        ("GET", "/contacts/old@x.co"): FakeResponse(200, {"id": "c1", "email": "old@x.co", "unsubscribed": True,
                                                          "first_name": "Ada", "last_name": "L"}),
        ("PATCH", "/contacts/new@x.co"): FakeResponse(404, {}),
        ("POST", "/contacts"): FakeResponse(201, {"id": "c2"}),
        ("DELETE", "/contacts/old@x.co"): FakeResponse(200, {"deleted": True}),
    })
    assert client.rename_contact("old@x.co", "new@x.co", {"plan": "none"}) == "renamed"
    post = [c for c in s.calls if c[0] == "POST"][0][2]
    assert post == {"email": "new@x.co", "properties": {"plan": "none"}, "unsubscribed": True,
                    "first_name": "Ada", "last_name": "L"}
    assert s.calls[-1][:2] == ("DELETE", "/contacts/old@x.co")


def test_rename_when_old_contact_is_gone_just_upserts():
    client, s = resend({
        ("GET", "/contacts/old@x.co"): FakeResponse(404, {}),
        ("PATCH", "/contacts/new@x.co"): FakeResponse(200, {}),
    })
    assert client.rename_contact("old@x.co", "new@x.co", {}) == "updated"


def test_sync_segments_moves_contact_and_leaves_unmanaged_alone():
    ids = {"free": "seg_free", "pro": "seg_pro", "lapsed": "seg_lapsed"}
    client, s = resend({
        ("GET", "/contacts/a@b.co/segments?limit=100"): FakeResponse(200, {"data": [
            {"id": "seg_free"}, {"id": "seg_newsletter"}]}),
        ("DELETE", "/contacts/a@b.co/segments/seg_free"): FakeResponse(200, {}),
        ("POST", "/contacts/a@b.co/segments/seg_pro"): FakeResponse(201, {}),
    })
    user_sync.sync_segments(client, "a@b.co", "pro", ids)
    assert [(m, p) for m, p, _ in s.calls[1:]] == [
        ("DELETE", "/contacts/a@b.co/segments/seg_free"),
        ("POST", "/contacts/a@b.co/segments/seg_pro"),
    ]


def test_sync_segments_noop_when_already_right():
    client, s = resend({("GET", "/contacts/a@b.co/segments?limit=100"):
                        FakeResponse(200, {"data": [{"id": "seg_pro"}]})})
    user_sync.sync_segments(client, "a@b.co", "pro", {"pro": "seg_pro", "free": "seg_free"})
    assert len(s.calls) == 1


def test_rate_limit_is_retried(monkeypatch):
    monkeypatch.setattr(user_sync.time, "sleep", lambda _s: None)
    client, s = resend({("PATCH", "/contacts/a@b.co"): [
        FakeResponse(429, {"message": "slow down"}, {"retry-after": "1"}),
        FakeResponse(200, {}),
    ]})
    assert client.update_contact("a@b.co", {}) is True
    assert len(s.calls) == 2


def test_errors_are_classified():
    client, _ = resend({("PATCH", "/contacts/a@b.co"): FakeResponse(422, {"message": "bad property"})})
    with pytest.raises(SyncError) as e:
        client.update_contact("a@b.co", {"nope": 1})
    assert e.value.transient is False and e.value.status == 422

    client, _ = resend({("PATCH", "/contacts/a@b.co"): FakeResponse(503, {})})
    with pytest.raises(SyncError) as e:
        client.update_contact("a@b.co", {})
    assert e.value.transient is True


def test_push_skips_revenuecat_for_free_users():
    session = FakeSession({})
    rc = RevenueCatClient("sk_test", session=session)
    assert user_sync.push_revenuecat(rc, make_user()) is False
    assert session.calls == []


def test_revenuecat_set_attributes_payload():
    session = FakeSession({("POST", "/v1/subscribers/42/attributes"): FakeResponse(200, {})})
    rc = RevenueCatClient("sk_test", session=session)
    assert user_sync.push_revenuecat(rc, make_user(is_subscribed=True, subscription_status="active")) is True
    attrs = session.calls[0][2]["attributes"]
    assert {k: v["value"] for k, v in attrs.items()} == {
        "$email": "hiker@example.com", "$displayName": "Ada", "username": "trailfox"}
    assert all(isinstance(v["updated_at_ms"], int) for v in attrs.values())


def test_sync_user_with_email_change_renames_then_segments():
    ids = {"free": "seg_free"}
    client, s = resend({
        ("GET", "/contacts/old@x.co"): FakeResponse(200, {"id": "c1", "unsubscribed": False}),
        ("PATCH", "/contacts/hiker@example.com"): FakeResponse(200, {}),
        ("DELETE", "/contacts/old@x.co"): FakeResponse(200, {}),
        ("GET", "/contacts/hiker@example.com/segments?limit=100"): FakeResponse(200, {"data": []}),
        ("POST", "/contacts/hiker@example.com/segments/seg_free"): FakeResponse(201, {}),
    })
    result = user_sync.sync_user(make_user(), resend=client, previous_email="old@x.co", segment_ids=ids)
    assert result == {"resend": "renamed"}
    assert s.calls[-1][:2] == ("POST", "/contacts/hiker@example.com/segments/seg_free")
