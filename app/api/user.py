from fastapi import APIRouter, Depends, HTTPException
from fastapi_sqlalchemy import db
from pydantic import BaseModel

from models.base import User
from models.enums import WeightUnit, Currency
from utils.auth import authenticate

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

    # Verify password
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


@route.get("")
def status(user: User = Depends(authenticate)):
    return user.to_dict()
