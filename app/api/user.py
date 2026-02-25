import logging
import uuid

from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Response
from fastapi_sqlalchemy import db
from sqlalchemy import func
from pydantic import BaseModel
from PIL import Image as PILImage, ImageOps
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from models.base import User, Image, PasswordReset, EmailVerification
from utils.auth import authenticate, generate_jwt, set_auth_cookie
from utils.consts import DEVELOPMENT, GOOGLE_CLIENT_IDS
from utils.digital_ocean import s3_file_upload
from utils.resend_email import send_password_reset, send_verification_email, create_contact

logger = logging.getLogger(__name__)

route = APIRouter()


class UserRegister(BaseModel):
    email: str
    username: str
    password: str


@route.post("", status_code=201)
def register(payload: UserRegister, response: Response):
    email = payload.email.strip().lower()
    username = payload.username.strip()
    password = payload.password.strip()

    existing_account = db.session.query(User).filter((User.email == email) | (
        func.lower(User.username) == username.lower())).first()

    if existing_account:
        raise HTTPException(
            status_code=409, detail="Email or username is already registered.")

    if len(username) > 15:
        raise HTTPException(
            status_code=400, detail="Username cannot exceed 15 characters.")

    if len(password) < 6:
        raise HTTPException(
            status_code=400, detail="Password must be at least 6 characters long.")

    hashed_password = User.generate_hash(password)
    new_user = User(email=email, username=username, password=hashed_password)

    try:
        db.session.add(new_user)
        db.session.commit()
        db.session.refresh(new_user)
    except Exception:
        raise HTTPException(status_code=400, detail="An error occurred.")

    jwt_token = generate_jwt(new_user)
    set_auth_cookie(response, jwt_token)

    verification = EmailVerification(user_id=new_user.id)
    try:
        db.session.add(verification)
        db.session.commit()
        db.session.refresh(verification)
    except Exception:
        logger.exception("Failed to create email verification")

    if not DEVELOPMENT:
        send_verification_email(email, verification.callback_id)
        create_contact(email)

    return {
        "user": new_user.to_dict(),
        "token": jwt_token
    }


class UserLogin(BaseModel):
    emailOrUsername: str
    password: str


@route.post("/login")
def login(payload: UserLogin, response: Response):
    emailOrUsername = payload.emailOrUsername.strip().lower()

    user = db.session.query(User).filter((User.email == emailOrUsername) | (
        func.lower(User.username) == emailOrUsername)).first()

    if not user:
        raise HTTPException(
            status_code=401, detail="Invalid username or password.")

    if not user.password:
        raise HTTPException(
            status_code=401, detail="Invalid username or password.")

    valid_password = User.verify_hash(payload.password, user.password)
    if not valid_password:
        raise HTTPException(
            status_code=401, detail="Invalid username or password.")

    jwt_token = generate_jwt(user)
    set_auth_cookie(response, jwt_token)

    return {
        "user": user.to_dict(),
        "token": jwt_token
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
    except Exception:
        logger.exception("Failed to update user profile")

    return user.to_dict()


@route.post("/avatar", status_code=201)
def upload_avatar(file: UploadFile = File(...), user: User = Depends(authenticate)):
    avatar = Image(user_id=user.id, avatar=True)

    temp = BytesIO()
    img = PILImage.open(file.file)
    img = ImageOps.exif_transpose(img)
    img_format = 'PNG'
    content_type = PILImage.MIME[img_format]
    img = img.resize([400, 400], PILImage.LANCZOS)
    img.save(temp, format=img_format, optimize=True)
    temp.seek(0)

    try:
        db.session.add(avatar)
        db.session.commit()
        avatar.s3 = {'extension': '.png', 'entity': 'avatar'}
        db.session.commit()
        db.session.refresh(avatar)
    except Exception:
        raise HTTPException(
            400, "An error occurred while creating image metadata.")

    upload_success = s3_file_upload(
        temp, content_type=content_type, key=avatar.s3_key)
    if not upload_success:
        db.session.delete(avatar)
        db.session.commit()
        raise HTTPException(400, "An error occurred while saving avatar.")

    db.session.refresh(user)

    return user


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


class RequestReset(BaseModel):
    email: str


@route.post("/request-password-reset")
def request_password_reset(payload: RequestReset):
    email = payload.email.strip().lower()
    user = db.session.query(User).filter(
        func.lower(User.email) == email).first()

    if not user:
        return Response(status_code=200)

    reset_request = PasswordReset(user_id=user.id)
    try:
        db.session.add(reset_request)
        db.session.commit()
        db.session.refresh(reset_request)
    except Exception:
        logger.exception("Failed to create password reset request")
        raise HTTPException(400, "An error occurred.")

    if not DEVELOPMENT:
        send_password_reset(email, reset_request.callback_id)

    return Response(status_code=200)


class PasswordResetData(BaseModel):
    password: str
    callback_id: str


@route.post("/reset-password")
def password_reset(payload: PasswordResetData):
    password = payload.password.strip()
    callback_id = payload.callback_id.strip()

    reset_request = db.session.query(PasswordReset).filter_by(
        callback_id=callback_id).first()

    if not reset_request:
        raise HTTPException(400, "Invalid request.")

    user = db.session.query(User).filter_by(id=reset_request.user_id).first()

    if not user:
        raise HTTPException(404, "User does not exist.")

    hashed_password = User.generate_hash(password)
    user.password = hashed_password

    try:
        db.session.delete(reset_request)
        db.session.commit()
    except Exception:
        logger.exception("Failed to reset password")
        raise HTTPException(400, "An error occurred.")

    return Response(status_code=200)
