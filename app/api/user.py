from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from fastapi_sqlalchemy import db
from pydantic import BaseModel

from models.base import User, Image
from models.enums import WeightUnit, Currency
from utils.auth import authenticate
from utils.digital_ocean import s3_file_upload

route = APIRouter()


class UserAuth(BaseModel):
    email: str
    password: str


@route.post("")
def register(payload: UserAuth):
    email = payload.email.strip().lower()
    password = payload.password.strip()

    # Check if email is already in use
    existing_account = db.session.query(User).filter_by(email=email).first()

    if existing_account:
        raise HTTPException(status_code=400, detail="Email address is already registered.")

    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters long.")

    # Hash password and save user
    hashed_password = User.generate_hash(password)
    new_user = User(email=email, password=hashed_password)

    try:
        db.session.add(new_user)
        db.session.commit()
        db.session.refresh(new_user)
    except:
        raise HTTPException(status_code=400, detail="An error occurred.")

    jwt_token = User.generate_jwt(new_user)

    return {
        "user": new_user.to_dict(),
        "token": jwt_token
    }


@route.post("/login")
def login(payload: UserAuth):
    email = payload.email.strip().lower()
    user = db.session.query(User).filter_by(email=email).first()

    if not user:
        raise HTTPException(status_code=400, detail="Account does not exist.")

    valid_password = User.verify_hash(payload.password, user.password)
    if not valid_password:
        raise HTTPException(status_code=400, detail="Password is incorrect.")

    jwt_token = User.generate_jwt(user)

    return {
        "user": user.to_dict(),
        "token": jwt_token
    }


class UserUpdate(BaseModel):
    display_name: str = None
    unit: WeightUnit = None
    currency: Currency = None
    deactivated: bool = None
    instagram_url: str = None
    youtube_url: str = None
    twitter_url: str = None
    reddit_url: str = None


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
    avatar = Image(user_id=user.id)

    try:
        db.session.add(avatar)
        db.session.commit()
        avatar.s3 = {'filename': file.filename, 'entity': 'avatar'}
        db.session.commit()
        db.session.refresh(avatar)
    except Exception as e:
        raise HTTPException(400, "An error occurred while creating image metadata.")

    upload_success = s3_file_upload(file, content_type=file.content_type, key=avatar.s3_key)
    if not upload_success:
        db.session.delete(avatar)
        db.session.commit()
        raise HTTPException(400, "An error occurred while saving avatar.")

    # save new avatar as user default
    user.avatar_url = avatar.s3_url
    db.session.commit()
    db.session.refresh(user)

    return user


@route.get("")
def fetch(user: User = Depends(authenticate)):
    return user.to_dict()
