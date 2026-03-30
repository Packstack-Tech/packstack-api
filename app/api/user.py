import datetime
import json
import logging
import random
import time
import uuid

import jwt as pyjwt
import requests as http_requests
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi_sqlalchemy import db
from sqlalchemy import func, or_
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from typing import Optional

from models.base import (
    User, EmailVerification, AuthOtp,
    Item, Category, ItemCategory, Kit, KitItem, Pack, PackItem,
    Post, Trip, Image, TripCondition, TripGeography,
    Comment, Follow, LikePost, LikeTrip, LikeComment, LikeImage, Reported,
)
from utils.auth import authenticate, generate_jwt, set_auth_cookie
from cryptography.hazmat.primitives import serialization
from utils.consts import (
    DEVELOPMENT, GOOGLE_CLIENT_IDS, APPLE_CLIENT_IDS, REVIEW_EMAIL, REVIEW_OTP,
    APPLE_KEY_ID, APPLE_TEAM_ID, APPLE_PRIVATE_KEY, GOOGLE_CLIENT_SECRET,
)
from utils.resend_email import send_otp_email, create_contact, delete_contact

logger = logging.getLogger(__name__)

route = APIRouter()

PROFILE_LOAD_OPTIONS = (
    selectinload(User.trips),
    selectinload(User.avatar),
)


def user_with_profile(user_id):
    """Load a User with the relationships needed by to_dict()."""
    return db.session.query(User).options(
        *PROFILE_LOAD_OPTIONS
    ).filter_by(id=user_id).first()


OTP_EXPIRY_MINUTES = 10
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_JWKS_TTL = 3600

_apple_jwks_cache = {"keys": None, "fetched_at": 0}


def _get_apple_public_key(kid):
    now = time.time()
    if _apple_jwks_cache["keys"] is None or now - _apple_jwks_cache["fetched_at"] > APPLE_JWKS_TTL:
        resp = http_requests.get(APPLE_JWKS_URL, timeout=10)
        resp.raise_for_status()
        _apple_jwks_cache["keys"] = resp.json()["keys"]
        _apple_jwks_cache["fetched_at"] = now

    for key in _apple_jwks_cache["keys"]:
        if key["kid"] == kid:
            return pyjwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))

    _apple_jwks_cache["keys"] = None
    resp = http_requests.get(APPLE_JWKS_URL, timeout=10)
    resp.raise_for_status()
    _apple_jwks_cache["keys"] = resp.json()["keys"]
    _apple_jwks_cache["fetched_at"] = time.time()

    for key in _apple_jwks_cache["keys"]:
        if key["kid"] == kid:
            return pyjwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))

    return None


def _generate_otp() -> str:
    return f"{random.randint(0, 999999):06d}"


def _generate_apple_client_secret():
    private_key = serialization.load_pem_private_key(
        APPLE_PRIVATE_KEY.encode("utf-8"), password=None
    )
    now = int(time.time())
    payload = {
        "iss": APPLE_TEAM_ID,
        "iat": now,
        "exp": now + 86400 * 180,
        "aud": "https://appleid.apple.com",
        "sub": APPLE_CLIENT_IDS[0],
    }
    token = pyjwt.encode(payload, private_key, algorithm="ES256", headers={"kid": APPLE_KEY_ID})
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token


def _exchange_apple_code(authorization_code):
    """Exchange an Apple authorization code for a refresh token."""
    try:
        client_secret = _generate_apple_client_secret()
        resp = http_requests.post(
            "https://appleid.apple.com/auth/token",
            data={
                "client_id": APPLE_CLIENT_IDS[0],
                "client_secret": client_secret,
                "code": authorization_code,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("refresh_token")
    except Exception:
        logger.warning("Failed to exchange Apple authorization code", exc_info=True)
        return None


def _revoke_apple_token(refresh_token):
    """Revoke an Apple refresh token. Best-effort, never raises."""
    try:
        client_secret = _generate_apple_client_secret()
        http_requests.post(
            "https://appleid.apple.com/auth/revoke",
            data={
                "client_id": APPLE_CLIENT_IDS[0],
                "client_secret": client_secret,
                "token": refresh_token,
                "token_type_hint": "refresh_token",
            },
            timeout=10,
        )
    except Exception:
        logger.warning("Failed to revoke Apple token", exc_info=True)


def _exchange_google_code(server_auth_code):
    """Exchange a Google server auth code for a refresh token."""
    try:
        resp = http_requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_IDS[0],
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": server_auth_code,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("refresh_token")
    except Exception:
        logger.warning("Failed to exchange Google server auth code", exc_info=True)
        return None


def _revoke_google_token(refresh_token):
    """Revoke a Google refresh token. Best-effort, never raises."""
    try:
        http_requests.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": refresh_token},
            timeout=10,
        )
    except Exception:
        logger.warning("Failed to revoke Google token", exc_info=True)


class SendOtpPayload(BaseModel):
    email: str
    username: Optional[str] = None
    is_registration: bool


@route.post("/send-otp")
def send_otp(payload: SendOtpPayload):
    email = payload.email.strip().lower()

    if REVIEW_EMAIL and email == REVIEW_EMAIL.strip().lower():
        return {"sent": True}

    if payload.is_registration:
        if not payload.username:
            raise HTTPException(400, "Username is required for registration.")

        username = payload.username.strip()

        if len(username) > 15:
            raise HTTPException(400, "Username cannot exceed 15 characters.")

        existing = db.session.query(User).filter(
            (User.email == email) | (func.lower(User.username) == username.lower())
        ).first()

        if existing:
            raise HTTPException(409, "Email or username is already registered.")
    else:
        user = db.session.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(404, "No account found with that email.")

    db.session.query(AuthOtp).filter(
        AuthOtp.email == email,
        AuthOtp.is_registration == payload.is_registration,
    ).delete()

    otp_code = _generate_otp()
    otp = AuthOtp(
        email=email,
        otp_code=otp_code,
        username=payload.username.strip() if payload.username else None,
        is_registration=payload.is_registration,
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=OTP_EXPIRY_MINUTES),
    )

    try:
        db.session.add(otp)
        db.session.commit()
    except Exception:
        logger.exception("Failed to create OTP")
        raise HTTPException(400, "An error occurred.")

    if not DEVELOPMENT:
        send_otp_email(email, otp_code)

    return {"sent": True}


class VerifyOtpPayload(BaseModel):
    email: str
    otp: str
    is_registration: bool
    username: Optional[str] = None


@route.post("/verify-otp")
def verify_otp(payload: VerifyOtpPayload, response: Response):
    email = payload.email.strip().lower()
    otp = payload.otp.strip()

    if (
        REVIEW_EMAIL
        and REVIEW_OTP
        and email == REVIEW_EMAIL.strip().lower()
        and otp == REVIEW_OTP.strip()
    ):
        user = db.session.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(404, "Account not found.")
        user = user_with_profile(user.id)
        jwt_token = generate_jwt(user)
        set_auth_cookie(response, jwt_token)
        return {"user": user.to_dict(), "token": jwt_token}

    record = db.session.query(AuthOtp).filter(
        AuthOtp.email == email,
        AuthOtp.is_registration == payload.is_registration,
        AuthOtp.expires_at > datetime.datetime.utcnow(),
    ).order_by(AuthOtp.created_at.desc()).first()

    if not record or record.otp_code != otp:
        raise HTTPException(401, "Invalid or expired code.")

    if payload.is_registration:
        username = (payload.username or record.username or "").strip()
        if not username:
            raise HTTPException(400, "Username is required for registration.")

        existing = db.session.query(User).filter(
            (User.email == email) | (func.lower(User.username) == username.lower())
        ).first()
        if existing:
            raise HTTPException(409, "Email or username is already registered.")

        new_user = User(email=email, username=username, password=None, email_verified=True)
        try:
            db.session.add(new_user)
            db.session.commit()
            db.session.refresh(new_user)
        except Exception:
            logger.exception("Failed to create user")
            raise HTTPException(400, "An error occurred.")

        if not DEVELOPMENT:
            create_contact(email)

        user = new_user
    else:
        user = db.session.query(User).filter(User.email == email).first()
        if not user:
            raise HTTPException(404, "Account not found.")

    db.session.query(AuthOtp).filter(AuthOtp.email == email).delete()
    db.session.commit()

    user = user_with_profile(user.id)
    jwt_token = generate_jwt(user)
    set_auth_cookie(response, jwt_token)

    return {
        "user": user.to_dict(),
        "token": jwt_token,
    }


class GoogleAuthPayload(BaseModel):
    credential: str
    server_auth_code: Optional[str] = None


@route.post("/google-auth")
def google_auth(payload: GoogleAuthPayload, response: Response):
    if not GOOGLE_CLIENT_IDS:
        raise HTTPException(
            status_code=500, detail="Google authentication is not configured.")

    idinfo = None
    for client_id in GOOGLE_CLIENT_IDS:
        try:
            idinfo = google_id_token.verify_oauth2_token(
                payload.credential,
                google_requests.Request(),
                client_id,
            )
            break
        except ValueError:
            continue

    if not idinfo:
        raise HTTPException(status_code=401, detail="Invalid Google token.")

    google_sub = idinfo["sub"]
    email = idinfo["email"]
    name = idinfo.get("name", "")

    user = db.session.query(User).filter_by(google_id=google_sub).first()

    if not user:
        user = db.session.query(User).filter(
            func.lower(User.email) == email.lower()
        ).first()
        if user:
            user.google_id = google_sub
            user.email_verified = True
            db.session.commit()

    if not user:
        username = email.split("@")[0][:15]
        existing = db.session.query(User).filter(
            func.lower(User.username) == username.lower()
        ).first()
        if existing:
            username = (username[:7] + uuid.uuid4().hex[:8])[:15]

        user = User(
            email=email,
            username=username,
            password=None,
            google_id=google_sub,
            display_name=name[:50] if name else None,
            email_verified=True,
        )
        db.session.add(user)
        db.session.commit()
        db.session.refresh(user)

        if not DEVELOPMENT:
            first_name, _, last_name = name.partition(" ") if name else ("", "", "")
            create_contact(email, first_name, last_name)

    if payload.server_auth_code and GOOGLE_CLIENT_SECRET:
        refresh_token = _exchange_google_code(payload.server_auth_code)
        if refresh_token:
            user.google_refresh_token = refresh_token
            db.session.commit()

    user = user_with_profile(user.id)
    jwt_token = generate_jwt(user)
    set_auth_cookie(response, jwt_token)

    return {
        "user": user.to_dict(),
        "token": jwt_token
    }


class AppleAuthPayload(BaseModel):
    identity_token: str
    full_name: Optional[str] = None
    authorization_code: Optional[str] = None


@route.post("/apple-auth")
def apple_auth(payload: AppleAuthPayload, response: Response):
    if not APPLE_CLIENT_IDS:
        raise HTTPException(
            status_code=500, detail="Apple authentication is not configured.")

    try:
        header = pyjwt.get_unverified_header(payload.identity_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Apple token.")

    kid = header.get("kid")
    if not kid:
        raise HTTPException(status_code=401, detail="Invalid Apple token.")

    public_key = _get_apple_public_key(kid)
    if not public_key:
        raise HTTPException(status_code=401, detail="Unable to verify Apple token.")

    try:
        decoded = pyjwt.decode(
            payload.identity_token,
            public_key,
            algorithms=["RS256"],
            audience=APPLE_CLIENT_IDS,
            issuer="https://appleid.apple.com",
        )
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Apple token has expired.")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid Apple token.")

    apple_sub = decoded["sub"]
    email = decoded.get("email", "")
    name = payload.full_name or ""

    if not email:
        raise HTTPException(status_code=401, detail="Apple token missing email.")

    user = db.session.query(User).filter_by(apple_id=apple_sub).first()

    if not user:
        user = db.session.query(User).filter(
            func.lower(User.email) == email.lower()
        ).first()
        if user:
            user.apple_id = apple_sub
            user.email_verified = True
            db.session.commit()

    if not user:
        username = email.split("@")[0][:15]
        existing = db.session.query(User).filter(
            func.lower(User.username) == username.lower()
        ).first()
        if existing:
            username = (username[:7] + uuid.uuid4().hex[:8])[:15]

        user = User(
            email=email,
            username=username,
            password=None,
            apple_id=apple_sub,
            display_name=name[:50] if name else None,
            email_verified=True,
        )
        db.session.add(user)
        db.session.commit()
        db.session.refresh(user)

        if not DEVELOPMENT:
            first_name, _, last_name = name.partition(" ") if name else ("", "", "")
            create_contact(email, first_name, last_name)

    if payload.authorization_code and APPLE_PRIVATE_KEY:
        refresh_token = _exchange_apple_code(payload.authorization_code)
        if refresh_token:
            user.apple_refresh_token = refresh_token
            db.session.commit()

    user = user_with_profile(user.id)
    jwt_token = generate_jwt(user)
    set_auth_cookie(response, jwt_token)

    return {
        "user": user.to_dict(),
        "token": jwt_token,
    }


@route.post("/logout")
def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    return {"ok": True}


@route.delete("")
def delete_account(response: Response, user: User = Depends(authenticate)):
    user_id = user.id
    email = user.email

    if user.apple_refresh_token and APPLE_PRIVATE_KEY:
        _revoke_apple_token(user.apple_refresh_token)

    if user.google_refresh_token:
        _revoke_google_token(user.google_refresh_token)

    if not DEVELOPMENT:
        delete_contact(email)

    try:
        kit_ids = [r[0] for r in db.session.query(Kit.id).filter_by(user_id=user_id)]
        pack_ids = [r[0] for r in db.session.query(Pack.id).filter_by(user_id=user_id)]
        trip_ids = [r[0] for r in db.session.query(Trip.id).filter_by(user_id=user_id)]
        item_ids = [r[0] for r in db.session.query(Item.id).filter_by(user_id=user_id)]
        post_ids = [r[0] for r in db.session.query(Post.id).filter_by(user_id=user_id)]
        image_ids = [r[0] for r in db.session.query(Image.id).filter_by(user_id=user_id)]
        comment_ids = [r[0] for r in db.session.query(Comment.id).filter_by(user_id=user_id)]

        other_comment_filters = []
        if post_ids:
            other_comment_filters.append(Comment.post_id.in_(post_ids))
        if trip_ids:
            other_comment_filters.append(Comment.trip_id.in_(trip_ids))
        if other_comment_filters:
            other_comment_ids = [r[0] for r in db.session.query(Comment.id).filter(
                or_(*other_comment_filters),
                Comment.user_id != user_id,
            )]
        else:
            other_comment_ids = []
        all_comment_ids = list(set(comment_ids + other_comment_ids))

        # Likes on user's content + user's own likes on others' content
        if all_comment_ids:
            db.session.query(LikeComment).filter(LikeComment.comment_id.in_(all_comment_ids)).delete(synchronize_session=False)
        db.session.query(LikeComment).filter_by(user_id=user_id).delete(synchronize_session=False)

        if post_ids:
            db.session.query(LikePost).filter(LikePost.post_id.in_(post_ids)).delete(synchronize_session=False)
        db.session.query(LikePost).filter_by(user_id=user_id).delete(synchronize_session=False)

        if trip_ids:
            db.session.query(LikeTrip).filter(LikeTrip.trip_id.in_(trip_ids)).delete(synchronize_session=False)
        db.session.query(LikeTrip).filter_by(user_id=user_id).delete(synchronize_session=False)

        if image_ids:
            db.session.query(LikeImage).filter(LikeImage.image_id.in_(image_ids)).delete(synchronize_session=False)
        db.session.query(LikeImage).filter_by(user_id=user_id).delete(synchronize_session=False)

        # Follows (both directions) and reports
        db.session.query(Follow).filter(
            (Follow.user_id == user_id) | (Follow.following_id == user_id)
        ).delete(synchronize_session=False)

        if post_ids:
            db.session.query(Reported).filter(Reported.post_id.in_(post_ids)).delete(synchronize_session=False)
        if trip_ids:
            db.session.query(Reported).filter(Reported.trip_id.in_(trip_ids)).delete(synchronize_session=False)
        db.session.query(Reported).filter_by(user_id=user_id).delete(synchronize_session=False)

        # Comments (user's own + others' on user's content)
        if all_comment_ids:
            db.session.query(Comment).filter(Comment.id.in_(all_comment_ids)).delete(synchronize_session=False)

        # Kit items + kits
        if kit_ids:
            db.session.query(KitItem).filter(KitItem.kit_id.in_(kit_ids)).delete(synchronize_session=False)
        if item_ids:
            db.session.query(KitItem).filter(KitItem.item_id.in_(item_ids)).delete(synchronize_session=False)
        db.session.query(Kit).filter_by(user_id=user_id).delete(synchronize_session=False)

        # Pack items + packs
        if pack_ids:
            db.session.query(PackItem).filter(PackItem.pack_id.in_(pack_ids)).delete(synchronize_session=False)
        if item_ids:
            db.session.query(PackItem).filter(PackItem.item_id.in_(item_ids)).delete(synchronize_session=False)
        db.session.query(Pack).filter_by(user_id=user_id).delete(synchronize_session=False)

        # Trip associations
        if trip_ids:
            db.session.query(TripCondition).filter(TripCondition.trip_id.in_(trip_ids)).delete(synchronize_session=False)
            db.session.query(TripGeography).filter(TripGeography.trip_id.in_(trip_ids)).delete(synchronize_session=False)

        # Images
        db.session.query(Image).filter_by(user_id=user_id).delete(synchronize_session=False)

        # Posts
        db.session.query(Post).filter_by(user_id=user_id).delete(synchronize_session=False)

        # Trips
        db.session.query(Trip).filter_by(user_id=user_id).delete(synchronize_session=False)

        # Items, then item categories, then categories
        db.session.query(Item).filter_by(user_id=user_id).delete(synchronize_session=False)
        db.session.query(ItemCategory).filter_by(user_id=user_id).delete(synchronize_session=False)
        db.session.query(Category).filter_by(user_id=user_id).delete(synchronize_session=False)

        # Auth artifacts
        db.session.query(EmailVerification).filter_by(user_id=user_id).delete(synchronize_session=False)
        db.session.query(AuthOtp).filter(AuthOtp.email == email).delete(synchronize_session=False)

        # User
        db.session.query(User).filter_by(id=user_id).delete(synchronize_session=False)

        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("Failed to delete account")
        raise HTTPException(500, "Failed to delete account.")

    response.delete_cookie("access_token", path="/")
    return {"ok": True}


class UserUpdate(BaseModel):
    display_name: str = None
    email: str = None
    bio: str = None
    unit_weight: str = None
    unit_distance: str = None
    unit_temperature: str = None
    currency: str = None
    facebook_url: str = None
    instagram_url: str = None
    youtube_url: str = None
    twitter_url: str = None
    snap_url: str = None
    personal_url: str = None


@route.put("")
def update(payload: UserUpdate, user: User = Depends(authenticate)):
    fields = payload.dict(exclude_none=True)

    for key, value in fields.items():
        setattr(user, key, value)

    try:
        db.session.commit()
        db.session.refresh(user)
    except Exception:
        logger.exception("Failed to update user profile")
        raise HTTPException(400, "Unable to update profile.")

    return user_with_profile(user.id).to_dict()


@route.get("")
def fetch(user: User = Depends(authenticate)):
    return user_with_profile(user.id).to_dict()


@route.get("/id/{id}")
def get_profile_by_id(id: int):
    user = user_with_profile(id)

    if not user:
        raise HTTPException(404, "User does not exist.")

    return user.to_dict()


@route.get("/profile/{username}")
def get_profile_by_username(username: str):
    user = db.session.query(User).options(
        *PROFILE_LOAD_OPTIONS
    ).filter(func.lower(
        User.username) == username.strip().lower()).first()

    if not user:
        raise HTTPException(404, "User does not exist.")

    return user.to_dict()


