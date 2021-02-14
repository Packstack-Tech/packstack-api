import datetime
import os
import jwt
from random import choice

from passlib.hash import pbkdf2_sha256 as sha256
from sqlalchemy import create_engine, Boolean, Column, ForeignKey, Integer, String, DateTime, TIMESTAMP, func, Numeric, \
    UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship

JWT_SECRET = os.getenv('JWT_SECRET')
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM')
DATABASE_URL = os.getenv('DATABASE_URL')

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class User(Base):
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password = Column(String, nullable=False)

    active = Column(Boolean, default=False)
    plan = Column(String)
    email_verified = Column(Boolean, default=False)
    stripe_customer_id = Column(String)
    stripe_sub_id = Column(String)

    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    password_resets = relationship("PasswordReset", backref="user")

    def to_dict(self):
        return {
            "email": self.email,
            "active": self.active,
            "plan": self.plan,
            "email_verified": self.email_verified,
        }

    @staticmethod
    def generate_hash(password):
        return sha256.hash(password)

    @staticmethod
    def verify_hash(password, hash):
        return sha256.verify(password, hash)

    @staticmethod
    def generate_jwt(user):
        return jwt.encode({"user_id": user.id}, JWT_SECRET, JWT_ALGORITHM)

    @staticmethod
    def decode_jwt(token):
        return jwt.decode(token, key=JWT_SECRET, algorithms=[JWT_ALGORITHM], options={"verify_exp": False})


class PasswordReset(Base):
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    callback_id = Column(String)

    def __init__(self, user_id):
        self.callback_id = self.generate_callback_id()
        self.user_id = user_id

    @staticmethod
    def generate_callback_id():
        return ''.join(choice('0123456789ABCDEF') for i in range(16))


# create database tables if not present
Base.metadata.create_all(engine)
