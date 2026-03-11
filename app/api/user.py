import datetime
import logging
import random
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi_sqlalchemy import db
from sqlalchemy import func
from pydantic import BaseModel
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from typing import Optional

from models.base import User, EmailVerification, AuthOtp
from utils.auth import authenticate, generate_jwt, set_auth_cookie
from utils.consts import DEVELOPMENT, GOOGLE_CLIENT_IDS
from utils.resend_email import send_verification_email, send_otp_email, create_contact

logger = logging.getLogger(__name__)

route = APIRouter()

OTP_EXPIRY_MINUTES = 10


def _generate_otp() -> str:
    return f"{random.randint(0, 999999):06d}"


class SendOtpPayload(BaseModel):
    email: str
    username: Optional[str] = None
    is_registration: bool


@route.post("/send-otp")
def send_otp(payload: SendOtpPayload):
    email = payload.email.strip().lower()

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

    jwt_token = generate_jwt(user)
    set_auth_cookie(response, jwt_token)

    return {
        "user": user.to_dict(),
        "token": jwt_token,
    }


class GoogleAuthPayload(BaseModel):
    credential: str


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

    jwt_token = generate_jwt(user)
    set_auth_cookie(response, jwt_token)

    return {
        "user": user.to_dict(),
        "token": jwt_token
    }


@route.post("/logout")
def logout(response: Response):
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

    return user.to_dict()


@route.get("")
def fetch(user: User = Depends(authenticate)):
    return user.to_dict()


@route.get("/id/{id}")
def get_profile_by_id(id: int):
    user = db.session.query(User).filter_by(id=id).first()

    if not user:
        raise HTTPException(404, "User does not exist.")

    return user.to_dict()


@route.get("/profile/{username}")
def get_profile_by_username(username: str):
    user = db.session.query(User).filter(func.lower(
        User.username) == username.strip().lower()).first()

    if not user:
        raise HTTPException(404, "User does not exist.")

    return user.to_dict()


class VerifyEmailData(BaseModel):
    callback_id: str


@route.post("/verify-email")
def verify_email(payload: VerifyEmailData):
    callback_id = payload.callback_id.strip()

    verification = db.session.query(EmailVerification).filter_by(
        callback_id=callback_id).first()

    if not verification:
        raise HTTPException(400, "Invalid or expired verification link.")

    user = db.session.query(User).filter_by(id=verification.user_id).first()

    if not user:
        raise HTTPException(404, "User does not exist.")

    user.email_verified = True

    try:
        db.session.query(EmailVerification).filter_by(user_id=user.id).delete()
        db.session.commit()
    except Exception:
        logger.exception("Failed to verify email")
        raise HTTPException(400, "An error occurred.")

    return {"email_verified": True}


@route.post("/resend-verification")
def resend_verification(user: User = Depends(authenticate)):
    if user.email_verified:
        raise HTTPException(400, "Email is already verified.")

    db.session.query(EmailVerification).filter_by(user_id=user.id).delete()

    verification = EmailVerification(user_id=user.id)
    try:
        db.session.add(verification)
        db.session.commit()
        db.session.refresh(verification)
    except Exception:
        logger.exception("Failed to create email verification")
        raise HTTPException(400, "An error occurred.")

    if not DEVELOPMENT:
        send_verification_email(user.email, verification.callback_id)

    return {"sent": True}
