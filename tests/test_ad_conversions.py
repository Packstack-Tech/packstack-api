"""Ad attribution at registration and Google Ads conversion uploads
(utils/ad_conversions.py). Real Postgres like the other API tests; Google is a
fake HTTP session.

    source testenv.sh && python -m pytest tests/test_ad_conversions.py -q
"""
import datetime
import json
import secrets

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from fastapi_sqlalchemy import db

import main
from api import user as user_api
from api import webhook
from models.base import AdConversion, AuthOtp, User, UserAttribution
from utils import ad_conversions as ac
from utils import revenuecat
from utils.auth import generate_jwt
from utils.consts import REVENUECAT_WEBHOOK_SECRET

HEADERS = {"Authorization": f"Bearer {REVENUECAT_WEBHOOK_SECRET}"} if REVENUECAT_WEBHOOK_SECRET else {}
NOW = datetime.datetime(2026, 10, 14, 12, 0)


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """No broker, no RevenueCat, no attribute sync in these tests."""
    queued = []
    monkeypatch.setattr(user_api, "enqueue_ad_conversion", lambda c: queued.append(c.id) if c else None)
    monkeypatch.setattr(webhook, "enqueue_ad_conversion", lambda c: queued.append(c.id) if c else None)
    monkeypatch.setattr(user_api, "enqueue_user_sync", lambda *a, **k: None)
    monkeypatch.setattr(webhook, "enqueue_user_sync", lambda *a, **k: None)
    monkeypatch.setattr(revenuecat, "fetch_subscriber", lambda _id: None)
    return queued


def register(client, attribution=None):
    email = f"ads-{secrets.token_hex(4)}@example.com"
    with db():
        db.session.add(AuthOtp(email=email, otp_code="123456", is_registration=True,
                               expires_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=5)))
        db.session.commit()
    body = {"email": email, "otp": "123456", "is_registration": True}
    if attribution is not None:
        body["attribution"] = attribution
    r = client.post("/user/verify-otp", json=body)
    assert r.status_code == 200, r.text
    return r.json()["user"]["id"]


def rows(user_id):
    with db():
        attr = db.session.get(UserAttribution, user_id)
        convs = db.session.query(AdConversion).filter_by(user_id=user_id).order_by(AdConversion.id).all()
        return (attr and {"gclid": attr.gclid, "utm_source": attr.utm_source},
                [(c.kind, c.transaction_id, float(c.value) if c.value is not None else None, c.status)
                 for c in convs])


def now_ms():
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)


# --- registration --------------------------------------------------------------------

def test_ad_click_registration_records_signup(client, quiet):
    uid = register(client, {"gclid": "CjwKCAabc", "utm_source": "google", "utm_medium": "cpc",
                            "landing": "/", "ts": now_ms() - 60_000})
    attr, convs = rows(uid)
    assert attr == {"gclid": "CjwKCAabc", "utm_source": "google"}
    assert convs == [("sign_up", f"signup-{uid}", None, "pending")]
    assert len(quiet) == 1


def test_utm_only_registration_saves_attribution_without_conversion(client, quiet):
    uid = register(client, {"utm_source": "reddit", "utm_campaign": "ul-fall"})
    attr, convs = rows(uid)
    assert attr == {"gclid": None, "utm_source": "reddit"}
    assert convs == [] and quiet == []


def test_registration_without_attribution_is_unchanged(client, quiet):
    uid = register(client)
    assert rows(uid) == (None, [])


def test_garbage_attribution_never_blocks_registration(client):
    uid = register(client, {"gclid": "x" * 5000, "ts": -5})
    attr, convs = rows(uid)
    assert len(attr["gclid"]) == 255 and len(convs) == 1


# --- RevenueCat webhook --------------------------------------------------------------

def purchase(client, uid, **extra):
    event = {"type": "INITIAL_PURCHASE", "app_user_id": str(uid), "id": secrets.token_hex(8),
             "transaction_id": "txn-" + str(uid), "price": 29.0, "environment": "PRODUCTION",
             "purchased_at_ms": now_ms(), "entitlement_ids": ["Full Access"], **extra}
    r = client.post("/webhook/revenuecat", json={"event": event}, headers=HEADERS)
    assert r.status_code == 200, r.text


def test_first_purchase_records_value_once(client):
    uid = register(client, {"gclid": "CjwKCApurchase", "ts": now_ms()})
    purchase(client, uid)
    purchase(client, uid)     # RevenueCat retry of the same transaction
    _, convs = rows(uid)
    assert convs == [("sign_up", f"signup-{uid}", None, "pending"),
                     ("purchase", f"txn-{uid}", 29.0, "pending")]


@pytest.mark.parametrize("extra", [
    {"environment": "SANDBOX"},
    {"price": 0},
    {"type": "RENEWAL"},
])
def test_non_conversions_are_ignored(client, extra):
    uid = register(client, {"gclid": "CjwKCAskip", "ts": now_ms()})
    purchase(client, uid, **extra)
    assert [c[0] for c in rows(uid)[1]] == ["sign_up"]


def test_purchase_without_ad_click_is_ignored(client):
    uid = register(client, {"utm_source": "newsletter"})
    purchase(client, uid)
    assert rows(uid)[1] == []


def test_purchase_outside_click_window_is_ignored(client):
    old = now_ms() - 89 * 24 * 3600 * 1000
    uid = register(client, {"gclid": "CjwKCAold", "ts": old})
    purchase(client, uid, purchased_at_ms=old + 91 * 24 * 3600 * 1000)
    assert [c[0] for c in rows(uid)[1]] == ["sign_up"]


def test_account_deletion_removes_attribution(client):
    uid = register(client, {"gclid": "CjwKCAdel", "ts": now_ms()})
    with db():
        u = db.session.get(User, uid)
        headers = {"Authorization": f"Bearer {generate_jwt(u)}"}
    assert client.delete("/user", headers=headers).status_code == 200
    assert rows(uid) == (None, [])


# --- pure helpers -------------------------------------------------------------------

def test_clean_attribution_clamps_timestamps():
    ms = lambda dt: dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000
    assert ac.clean_attribution({"gclid": "a", "ts": ms(NOW - datetime.timedelta(days=2))},
                                now=NOW)["clicked_at"] == NOW - datetime.timedelta(days=2)
    assert ac.clean_attribution({"gclid": "a", "ts": ms(NOW + datetime.timedelta(days=2))},
                                now=NOW)["clicked_at"] == NOW
    assert ac.clean_attribution({"gclid": "a", "ts": ms(NOW - datetime.timedelta(days=200))},
                                now=NOW)["clicked_at"] == NOW
    assert ac.clean_attribution({"landing": "/"}, now=NOW) is None


@pytest.mark.parametrize("raw,normalized", [
    (" Cloudy.SanFrancisco+shopping@Gmail.com ", "cloudysanfrancisco@gmail.com"),
    ("user.name+NYC@Example.com", "user.name+nyc@example.com"),
])
def test_email_normalization(raw, normalized):
    assert ac.normalize_email(raw) == normalized


# --- upload against a fake Google -----------------------------------------------------

def _service_account():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {"client_email": "ads@proj.iam.gserviceaccount.com", "private_key": pem,
            "private_key_id": "k1", "token_uri": "https://oauth2.googleapis.com/token"}


class FakeGoogle:
    def __init__(self, ingest_status=200, ingest_body=None):
        self.ingest_status, self.ingest_body = ingest_status, ingest_body or {}
        self.calls = []

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append((url, json, data, headers))
        resp = type("R", (), {})()
        if "oauth2" in url:
            resp.status_code, body = 200, {"access_token": "tok", "expires_in": 3600}
        else:
            resp.status_code, body = self.ingest_status, self.ingest_body
        resp.json = lambda: body
        resp.text = str(body)
        resp.content = b"x"
        return resp


def client_with(fake, validate_only=False):
    cfg = ac.GoogleAdsConfig(customer_id="1234567890", service_account=_service_account(),
                             conversion_actions={"sign_up": "111", "purchase": "222"},
                             validate_only=validate_only)
    return ac.DataManagerClient(cfg, session=fake)


def _conversion(client):
    uid = register(client, {"gclid": "CjwKCAsend", "ts": now_ms()})
    purchase(client, uid)
    with db():
        return uid, [c.id for c in db.session.query(AdConversion).filter_by(user_id=uid).order_by(AdConversion.id)]


def test_send_marks_sent_with_expected_payload(client):
    uid, (signup_id, purchase_id) = _conversion(client)
    fake = FakeGoogle()
    with db():
        assert ac.send_conversion(db.session, purchase_id, client_with(fake)) == "sent"
        email = db.session.get(User, uid).email
    url, body, _, headers = fake.calls[-1]
    assert url == ac.INGEST_URL and headers["Authorization"] == "Bearer tok"
    assert body["destinations"] == [{"operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": "1234567890"},
                                     "productDestinationId": "222"}]
    event = body["events"][0]
    assert event["adIdentifiers"] == {"gclid": "CjwKCAsend"}
    assert event["transactionId"] == f"txn-{uid}" and event["conversionValue"] == 29.0
    assert event["userData"]["userIdentifiers"][0]["emailAddress"] == ac.hash_email(email)
    assert body["encoding"] == "HEX" and body["validateOnly"] is False
    assert rows(uid)[1][1][3] == "sent"


def test_consent_from_the_banner_is_sent_with_the_conversion(client):
    uid = register(client, {"gclid": "CjwKCAeu", "ts": now_ms(), "consent": "granted"})
    with db():
        signup_id = db.session.query(AdConversion.id).filter_by(user_id=uid).scalar()
        fake = FakeGoogle()
        assert ac.send_conversion(db.session, signup_id, client_with(fake)) == "sent"
    assert fake.calls[-1][1]["events"][0]["consent"] == {
        "adUserData": "CONSENT_GRANTED", "adPersonalization": "CONSENT_GRANTED"}


def test_no_consent_field_outside_the_banner_regions(client):
    uid, (signup_id, _) = _conversion(client)
    fake = FakeGoogle()
    with db():
        ac.send_conversion(db.session, signup_id, client_with(fake))
        assert db.session.get(UserAttribution, uid).ad_consent is None
    assert "consent" not in fake.calls[-1][1]["events"][0]


def test_only_granted_counts_as_consent():
    assert ac.clean_attribution({"gclid": "a", "consent": "yes please"}, now=NOW)["ad_consent"] is None
    assert ac.clean_attribution({"gclid": "a", "consent": "granted"}, now=NOW)["ad_consent"] == "granted"


def test_validate_only_leaves_row_pending(client):
    uid, (signup_id, _) = _conversion(client)
    with db():
        assert ac.send_conversion(db.session, signup_id, client_with(FakeGoogle(), validate_only=True)) == "pending"
        assert db.session.get(AdConversion, signup_id).last_error == "validated"


def test_rejection_marks_failed_and_outage_raises(client):
    uid, (signup_id, purchase_id) = _conversion(client)
    with db():
        assert ac.send_conversion(db.session, signup_id,
                                  client_with(FakeGoogle(400, {"error": "bad action"}))) == "failed"
        with pytest.raises(ac.ConversionError) as e:
            ac.send_conversion(db.session, purchase_id, client_with(FakeGoogle(503)))
        assert e.value.transient
        row = db.session.get(AdConversion, purchase_id)
        assert row.status == "pending" and row.attempts == 1


def test_config_from_env():
    env = {"GOOGLE_ADS_CUSTOMER_ID": "123-456-7890", "GOOGLE_ADS_SIGNUP_CONVERSION_ACTION_ID": "111",
           "GOOGLE_ADS_SERVICE_ACCOUNT_JSON": json.dumps({"client_email": "a", "private_key": "b"})}
    cfg = ac.GoogleAdsConfig.from_env(env.get)
    assert cfg.customer_id == "1234567890" and cfg.conversion_actions == {"sign_up": "111"}
    assert ac.GoogleAdsConfig.from_env({}.get) is None


def test_auth_response_says_whether_the_account_is_new(client):
    email = f"new-{secrets.token_hex(4)}@example.com"
    for is_registration in (True, False):
        with db():
            db.session.add(AuthOtp(email=email, otp_code="123456", is_registration=is_registration,
                                   expires_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=5)))
            db.session.commit()
        r = client.post("/user/verify-otp", json={"email": email, "otp": "123456",
                                                 "is_registration": is_registration})
        assert r.status_code == 200, r.text
        assert r.json()["is_new_user"] is is_registration
