from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Response
from fastapi_sqlalchemy import db
from sqlalchemy import func
from pydantic import BaseModel
from PIL import Image as PILImage, ImageOps

from models.base import User, Image, PasswordReset
from utils.auth import authenticate
from utils.digital_ocean import s3_file_upload
from utils.mailchimp import add_contact, send_reset_password_email

route = APIRouter()


class UserRegister(BaseModel):
    email: str
    username: str
    password: str


@route.post("")
def register(payload: UserRegister):
    email = payload.email.strip().lower()
    username = payload.username.strip()
    password = payload.password.strip()

    # Check if email or username is already in use
    existing_account = db.session.query(User).filter((User.email == email) | (
        func.lower(User.username) == username.lower())).first()

    if existing_account:
        raise HTTPException(
            status_code=400, detail="Email or username is already registered.")

    if len(username) > 15:
        raise HTTPException(
            status_code=400, detail="Username cannot exceed 15 characters.")

    if len(password) < 6:
        raise HTTPException(
            status_code=400, detail="Password must be at least 6 characters long.")

    # Hash password and save user
    hashed_password = User.generate_hash(password)
    new_user = User(email=email, username=username, password=hashed_password)

    try:
        db.session.add(new_user)
        db.session.commit()
        db.session.refresh(new_user)
    except:
        raise HTTPException(status_code=400, detail="An error occurred.")

    jwt_token = User.generate_jwt(new_user)

    # Mailchimp
    add_contact(email)

    return {
        "user": new_user.to_dict(),
        "token": jwt_token
    }


class UserLogin(BaseModel):
    emailOrUsername: str
    password: str


@route.post("/login")
def login(payload: UserLogin):
    emailOrUsername = payload.emailOrUsername.strip().lower()

    user = db.session.query(User).filter((User.email == emailOrUsername) | (
        func.lower(User.username) == emailOrUsername)).first()

    if not user:
        raise HTTPException(
            status_code=400, detail="Invalid username or password.")

    valid_password = User.verify_hash(payload.password, user.password)
    if not valid_password:
        raise HTTPException(
            status_code=400, detail="Invalid username or password.")

    jwt_token = User.generate_jwt(user)

    return {
        "user": user.to_dict(),
        "token": jwt_token
    }


class UserUpdate(BaseModel):
    display_name: str = None
    email: str = None
    bio: str = None
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
    except Exception as e:
        print(e)

    return user.to_dict()


@route.post("/avatar")
def upload_avatar(file: UploadFile = File(...), user: User = Depends(authenticate)):
    avatar = Image(user_id=user.id, avatar=True)

    # save thumbnail
    temp = BytesIO()
    img = PILImage.open(file.file)
    img = ImageOps.exif_transpose(img)
    img_format = 'PNG'
    content_type = PILImage.MIME[img_format]
    img = img.resize([400, 400], PILImage.ANTIALIAS)
    img.save(temp, format=img_format, optimize=True)
    temp.seek(0)

    try:
        db.session.add(avatar)
        db.session.commit()
        avatar.s3 = {'extension': '.png', 'entity': 'avatar'}
        db.session.commit()
        db.session.refresh(avatar)
    except Exception as e:
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
def get_profile(id):
    user = db.session.query(User).filter_by(id=id).first()

    if not user:
        raise HTTPException(400, "User does not exist.")

    return user.to_dict()


@route.get("/profile/{username}")
def get_profile(username):
    user = db.session.query(User).filter(func.lower(
        User.username) == username.strip().lower()).first()

    if not user:
        raise HTTPException(400, "User does not exist.")

    return user.to_dict()


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
    except Exception as e:
        print(e)
        raise HTTPException(400, "An error occurred.")

    # Send reset password email
    send_reset_password_email(email, reset_request.callback_id)

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
        raise HTTPException(400, "User does not exist.")

    hashed_password = User.generate_hash(password)
    user.password = hashed_password

    try:
        db.session.delete(reset_request)
        db.session.commit()
    except Exception as e:
        print(e)
        raise HTTPException(400, "An error occurred.")

    return Response(status_code=200)
