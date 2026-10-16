"""The "acquired" activity-log entry follows item.acquired_date.

    source testenv.sh && python -m pytest tests/test_acquired_log.py -q
"""

import secrets

import pytest
from fastapi.testclient import TestClient
from fastapi_sqlalchemy import db

import main
from models.base import User
from utils.auth import generate_jwt


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def user(client):
    with db():
        suffix = secrets.token_hex(4)
        u = User(email=f"acq-{suffix}@example.com", username=f"aq{suffix}", unit_weight="METRIC",
                 email_verified=True)
        db.session.add(u)
        db.session.commit()
        return {"id": u.id, "headers": {"Authorization": f"Bearer {generate_jwt(u)}"}}


def add_item(client, user, **fields):
    body = {"name": "Pack", "weight": 900, "unit": "g", "consumable": False, **fields}
    r = client.post("/item", json=body, headers=user["headers"])
    assert r.status_code == 201, r.text
    return r.json()


def acquired_dates(client, user, item_id):
    r = client.get(f"/item/{item_id}/log", headers=user["headers"])
    assert r.status_code == 200, r.text
    return [e["event_date"] for e in r.json() if e["event_type"] == "acquired"]


def put_item(client, user, item, **fields):
    r = client.put("/item", json={**item, **fields}, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


def test_create_with_date_logs_acquired(client, user):
    item = add_item(client, user, acquired_date="2019-06-04")
    assert acquired_dates(client, user, item["id"]) == ["2019-06-04"]


def test_create_without_date_logs_nothing(client, user):
    item = add_item(client, user)
    assert acquired_dates(client, user, item["id"]) == []


def test_edit_redates_the_existing_entry(client, user):
    item = add_item(client, user)
    item = put_item(client, user, item, acquired_date="2026-01-07")
    item = put_item(client, user, item, acquired_date="2019-06-04")
    assert acquired_dates(client, user, item["id"]) == ["2019-06-04"]


def test_clearing_the_date_keeps_the_entry(client, user):
    item = add_item(client, user, acquired_date="2019-06-04")
    put_item(client, user, item, acquired_date="")
    assert acquired_dates(client, user, item["id"]) == ["2019-06-04"]


def test_lifecycle_endpoint_syncs(client, user):
    item = add_item(client, user)
    for date in ("2026-01-07", "2019-06-04"):
        r = client.put(f"/item/{item['id']}/lifecycle", json={"acquired_date": date},
                       headers=user["headers"])
        assert r.status_code == 200, r.text
    assert acquired_dates(client, user, item["id"]) == ["2019-06-04"]
