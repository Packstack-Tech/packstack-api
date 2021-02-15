from fastapi import Header, HTTPException
from jwt import PyJWTError
from fastapi_sqlalchemy import db

from models.base import User


def authenticate(*, Authorization: str = Header(None)):
    if not Authorization or not Authorization.startswith("Bearer "):
        raise HTTPException(status_code=400, detail="Invalid or missing token.")

    # Verify token is valid
    try:
        token = Authorization.split(" ")[1]
        decoded_token = User.decode_jwt(token)
    except PyJWTError:
        raise HTTPException(status_code=400, detail="Invalid or missing token.")

    # Fetch user from token payload
    user = db.session.query(User).filter_by(id=decoded_token['user_id']).first()
    if not user:
        raise HTTPException(status_code=400, detail="Account does not exist.")

    return user
