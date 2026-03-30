import jwt
import sentry_sdk

from fastapi import Header, HTTPException, Request, Response
from fastapi_sqlalchemy import db
from utils.consts import JWT_ALGORITHM, JWT_SECRET, DEVELOPMENT
from models.base import User

COOKIE_MAX_AGE = 10 * 365 * 24 * 60 * 60  # 10 years


def authenticate(*, request: Request, Authorization: str = Header(None)):
    token = request.cookies.get("access_token")

    if not token:
        if Authorization and Authorization.startswith("Bearer "):
            token = Authorization.split(" ")[1]

    if not token:
        raise HTTPException(
            status_code=401, detail="Invalid or missing token.")

    try:
        decoded_token = decode_jwt(token)
    except Exception:
        raise HTTPException(
            status_code=401, detail="Invalid or missing token.")

    user = db.session.query(User).filter_by(
        id=decoded_token['user_id']).first()
    if not user:
        raise HTTPException(status_code=401, detail="Account does not exist.")

    sentry_sdk.set_user({"id": str(user.id), "email": user.email, "username": user.username})

    return user


def set_auth_cookie(response: Response, token: str):
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=not DEVELOPMENT,
        samesite="lax",
        max_age=COOKIE_MAX_AGE,
        path="/",
    )


def generate_jwt(user):
    payload = {"user_id": user.id}
    token = jwt.encode(payload, JWT_SECRET, JWT_ALGORITHM)
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token


def decode_jwt(token):
    return jwt.decode(token, key=JWT_SECRET, algorithms=[JWT_ALGORITHM], options={"verify_exp": False})
