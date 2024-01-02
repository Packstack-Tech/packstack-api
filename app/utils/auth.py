import jwt

from fastapi import Header, HTTPException
from fastapi_sqlalchemy import db
from utils.consts import JWT_ALGORITHM, JWT_SECRET
from models.base import User


def authenticate(*, Authorization: str = Header(None)):
    if not Authorization or not Authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=400, detail="Invalid or missing token.")

    # Verify token is valid
    try:
        token = Authorization.split(" ")[1]
        decoded_token = decode_jwt(token)
    except Exception:
        raise HTTPException(
            status_code=400, detail="Invalid or missing token.")

    # Fetch user from token payload
    user = db.session.query(User).filter_by(
        id=decoded_token['user_id']).first()
    if not user:
        raise HTTPException(status_code=400, detail="Account does not exist.")

    return user


def generate_jwt(user):
    return jwt.encode({"user_id": user.id}, JWT_SECRET, JWT_ALGORITHM)


def decode_jwt(token):
    return jwt.decode(token, key=JWT_SECRET, algorithms=[JWT_ALGORITHM], options={"verify_exp": False})
