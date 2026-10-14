"""The live attribute sync end to end: profile edits and account deletion
enqueue the right work, and the Celery task pushes what utils/user_sync.py
builds. Real Postgres like the other API tests; RevenueCat and Resend are a
fake HTTP session.

    source testenv.sh && python -m pytest tests/test_user_sync_flow.py -q
"""
import datetime
import secrets

import pytest
from fastapi.testclient import TestClient
from fastapi_sqlalchemy import db

import main
from api import user as user_api
from models.base import User
from tasks import sync_user as task_mod
from utils.auth import generate_jwt
from utils.user_sync import ResendClient, RevenueCatClient

from test_user_sync import FakeResponse, FakeSession


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def make_user(**kw):
    with db():
        suffix = secrets.token_hex(4)
        u = User(email=f"sync-{suffix}@example.com", username=f"sync{suffix}", email_verified=True,
                 display_name="Ada", **kw)
        db.session.add(u)
        db.session.commit()
        return {"id": u.id, "email": u.email,
                "headers": {"Authorization": f"Bearer {generate_jwt(u)}"}}


@pytest.fixture
def enqueued(monkeypatch):
    calls = {"sync": [], "forget": []}
    monkeypatch.setattr(user_api, "enqueue_user_sync",
                        lambda user_id, previous_email=None: calls["sync"].append((user_id, previous_email)))
    monkeypatch.setattr(user_api, "enqueue_revenuecat_forget",
                        lambda user_id: calls["forget"].append(user_id))
    return calls


# --- endpoints --------------------------------------------------------------------

def test_email_change_enqueues_with_previous_email(client, enqueued):
    u = make_user()
    r = client.put("/user", json={"email": "moved-" + u["email"]}, headers=u["headers"])
    assert r.status_code == 200, r.text
    assert enqueued["sync"] == [(u["id"], u["email"])]


def test_display_name_change_enqueues_without_previous_email(client, enqueued):
    u = make_user()
    client.put("/user", json={"display_name": "Grace"}, headers=u["headers"])
    assert enqueued["sync"] == [(u["id"], None)]


def test_unsynced_field_change_enqueues_nothing(client, enqueued):
    u = make_user()
    client.put("/user", json={"bio": "ultralight-ish", "display_name": "Ada"}, headers=u["headers"])
    assert enqueued["sync"] == []


def test_delete_forgets_revenuecat_only_for_purchasers(client, enqueued, monkeypatch):
    monkeypatch.setattr(user_api, "delete_contact", lambda email: None)
    free = make_user()
    paid = make_user(is_subscribed=True, subscription_status="active")
    assert client.delete("/user", headers=free["headers"]).status_code == 200
    assert client.delete("/user", headers=paid["headers"]).status_code == 200
    assert enqueued["forget"] == [paid["id"]]


# --- task ---------------------------------------------------------------------------

def test_task_pushes_resend_and_revenuecat(monkeypatch):
    u = make_user(is_subscribed=True, subscription_status="active", subscription_plan="annual",
                  subscription_store="app_store",
                  subscription_expires_at=datetime.datetime(2027, 3, 1),
                  subscription_started_at=datetime.datetime(2026, 3, 1))
    email = u["email"]
    resend_session = FakeSession({
        ("PATCH", f"/contacts/{email}"): FakeResponse(200, {}),
        ("GET", f"/contacts/{email}/segments?limit=100"): FakeResponse(200, {"data": []}),
        ("POST", f"/contacts/{email}/segments/seg_pro"): FakeResponse(201, {}),
    })
    rc_session = FakeSession({("POST", f"/v1/subscribers/{u['id']}/attributes"): FakeResponse(200, {})})
    monkeypatch.setattr(task_mod, "_resend", lambda: ResendClient("re_x", session=resend_session))
    monkeypatch.setattr(task_mod, "_revenuecat", lambda: RevenueCatClient("sk_x", session=rc_session))
    monkeypatch.setattr(task_mod, "RESEND_SEGMENT_IDS", {"pro": "seg_pro", "free": "seg_free"})

    result = task_mod.sync_user_attributes(u["id"])

    assert result == {"revenuecat": True, "resend": "updated"}
    props = resend_session.calls[0][2]["properties"]
    assert props["plan_status"] == "active" and props["plan"] == "annual"
    assert props["store"] == "app_store" and props["period_ends_at"] == "2027-03-01"
    assert rc_session.calls[0][2]["attributes"]["$email"]["value"] == email


def test_task_skips_missing_user():
    assert task_mod.sync_user_attributes(10 ** 9) == {"skipped": "no such user"}


def test_enqueue_never_raises(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("broker down")
    monkeypatch.setattr(task_mod, "_configured", lambda: True)
    monkeypatch.setattr(task_mod, "REVENUECAT_SECRET_API_KEY", "sk_x")
    monkeypatch.setattr(task_mod.sync_user_attributes, "delay", boom)
    monkeypatch.setattr(task_mod.forget_revenuecat_attributes, "delay", boom)
    task_mod.enqueue_user_sync(1)
    task_mod.enqueue_revenuecat_forget(1)
