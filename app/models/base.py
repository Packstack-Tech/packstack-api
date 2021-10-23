import datetime
import jwt
from random import choice

from passlib.hash import pbkdf2_sha256 as sha256
from sqlalchemy import create_engine, Boolean, Column, Enum, ForeignKey, Integer, DATE, String, DateTime, TIMESTAMP, func, \
    Numeric, UniqueConstraint, select
from sqlalchemy.ext.declarative import declarative_base, declared_attr
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import sessionmaker, relationship, column_property

from consts import JWT_SECRET, JWT_ALGORITHM, DATABASE_URL, DO_BUCKET, DO_REGION
from utils.utils import group_by_category

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(object):
    @declared_attr
    def __tablename__(self):
        return self.__name__.lower()


Base = declarative_base(cls=Base)


class User(Base):
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password = Column(String, nullable=False)
    email_verified = Column(Boolean, default=False)

    stripe_customer_id = Column(String)
    stripe_sub_id = Column(String)

    display_name = Column(String(50), nullable=False)
    bio = Column(String(500))
    unit_weight = Column(String(10), default="METRIC")
    unit_distance = Column(String(10), default="MI")
    unit_temperature = Column(String(10), default="F")

    # Social profiles
    instagram_url = Column(String(500))
    facebook_url = Column(String(500))
    youtube_url = Column(String(500))
    twitter_url = Column(String(500))
    snap_url = Column(String(500))
    personal_url = Column(String(500))

    # In case an account needs to be manually banned
    banned = Column(Boolean, default=False)

    # In case user deactivates their account
    deactivated = Column(Boolean, default=False)

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    password_resets = relationship("PasswordReset", backref="user")

    avatar = relationship("Image",
                          lazy="joined",
                          primaryjoin="and_(User.id == Image.user_id, "
                          "Image.avatar == True)",
                          uselist=False)

    inventory = relationship("Item",
                             lazy="joined",
                             primaryjoin="and_(User.id == Item.user_id, "
                                         "Item.removed == False)")
    packs = relationship("Pack",
                         backref="user",
                         lazy="joined",
                         primaryjoin="and_(User.id == Pack.user_id, "
                                     "Pack.removed == False)")

    categories = relationship("Category",
                              lazy="joined",
                              primaryjoin="or_(User.id == Category.user_id, "
                                          "Category.user_id == None)")

    def to_dict(self):
        return {
            "id": self.id,
            "display_name": self.display_name,
            "avatar": self.avatar,
            "bio": self.bio,
            "unit": self.unit,
            "banned": self.banned,
            "deactivated": self.deactivated,
            "email_verified": self.email_verified,

            "instagram_url": self.instagram_url,
            "youtube_url": self.youtube_url,
            "twitter_url": self.twitter_url,
            "facebook_url": self.facebook_url,
            "snap_url": self.snap_url,
            "personal_url": self.personal_url,

            "inventory": self.inventory,
            "packs": self.packs,
            "categories": self.categories
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


class Item(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    brand_id = Column(Integer, ForeignKey("brand.id"))
    product_id = Column(Integer, ForeignKey("product.id"))
    category_id = Column(Integer, ForeignKey("category.id"))
    removed = Column(Boolean, default=False)

    name = Column(String(100))
    weight = Column(Numeric, default=0.0)
    unit = Column(String(10))
    price = Column(Numeric, default=0.0)
    consumable = Column(Boolean, default=False)
    product_url = Column(String(250))
    wishlist = Column(Boolean, default=False)
    notes = Column(String(1000))

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    brand = relationship("Brand", lazy="joined")
    product = relationship("Product", lazy="joined")
    category = relationship("Category", lazy="joined")


class Category(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"))
    name = Column(String(100), unique=True)
    consumable = Column(Boolean, default=False)

    # Ensure category is unique per user
    __table_args__ = (UniqueConstraint(
        'user_id', 'name', name='_user_category_uc'),)


class Brand(Base):
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True)
    removed = Column(Boolean, default=False)

    products = relationship("Product")


class Product(Base):
    id = Column(Integer, primary_key=True, index=True)
    brand_id = Column(Integer, ForeignKey("brand.id"))
    name = Column(String(250))
    removed = Column(Boolean, default=False)

    # Ensure product is unique per brand
    __table_args__ = (UniqueConstraint(
        'brand_id', 'name', name='_brand_product_uc'),)


class PackItem(Base):
    pack_id = Column(Integer, ForeignKey("pack.id"), primary_key=True)
    item_id = Column(Integer, ForeignKey("item.id"), primary_key=True)
    quantity = Column(Numeric, default=1)
    worn = Column(Boolean, default=False)

    # Relationship
    item = relationship("Item", lazy="joined")


class Post(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    notes = Column(String(2500), nullable=False)
    removed = Column(Boolean, default=False)

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    user = relationship("User", lazy="joined", uselist=False)
    images = relationship("Image", lazy="joined")


class Pack(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)

    start_date = Column(DATE)
    end_date = Column(DATE)
    region = Column(String(500), nullable=False)
    trail_name = Column(String(500))
    temp_min = Column(Integer)
    temp_max = Column(Integer)
    distance = Column(Numeric)
    planning_notes = Column(String(2500))
    notes = Column(String(2500))
    public = Column(Boolean, default=False)
    removed = Column(Boolean, default=False)

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())

    # Associated item count queried at load
    item_count = column_property(
        select([func.count(PackItem.pack_id)]).where(
            PackItem.pack_id == id).correlate_except(PackItem)
    )

    # Relationships
    items = relationship("PackItem")
    conditions = relationship("PackCondition", lazy="joined")
    geographies = relationship("PackGeography", lazy="joined")
    images = relationship("Image", lazy="joined")

    @hybrid_property
    def items_by_category(self):
        if not self.items:
            return []

        return group_by_category(self.items)


class Condition(Base):
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)


class Geography(Base):
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)


class Image(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    avatar = Column(Boolean, default=False)

    pack_id = Column(Integer, ForeignKey("pack.id"))
    item_id = Column(Integer, ForeignKey("item.id"))
    post_id = Column(Integer, ForeignKey("post.id"))
    s3_key = Column(String)
    s3_url = Column(String)

    @hybrid_property
    def s3(self):
        return self.s3_key

    @s3.setter
    def s3(self, metadata):
        entity = metadata['entity']  # pack, image or avatar
        filename = metadata['filename']
        disallowed_chars = ['/', ' ']
        sanitized_filename = ''.join(
            i for i in filename if i not in disallowed_chars).lower()
        entity_id = '' if self.avatar else f'/{self.pack_id}' or f'/{self.item_id}'
        s3_key = f'user/{self.user_id}/{entity}{entity_id}/{sanitized_filename}'

        self.s3_key = s3_key
        self.s3_url = f'https://{DO_BUCKET}.{DO_REGION}.digitaloceanspaces.com/{s3_key}'

    # Relationships
    likes = relationship("LikeImage", backref="image")


class PackCondition(Base):
    pack_id = Column(Integer, ForeignKey("pack.id"), primary_key=True)
    condition_id = Column(Integer, ForeignKey(
        "condition.id"), primary_key=True)

    # Relationships
    condition = relationship("Condition", lazy="joined")


class PackGeography(Base):
    pack_id = Column(Integer, ForeignKey("pack.id"), primary_key=True)
    geography_id = Column(Integer, ForeignKey(
        "geography.id"), primary_key=True)

    # Relationships
    geography = relationship("Geography", lazy="joined")


class Comment(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    post_id = Column(Integer, ForeignKey("post.id"))
    pack_id = Column(Integer, ForeignKey("pack.id"))
    comment = Column(String(1000), nullable=False)
    removed = Column(Boolean, default=False)

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    user = relationship("User", lazy="joined", uselist=False)


class Follow(Base):
    user_id = Column(Integer, ForeignKey("user.id"), primary_key=True)
    following_id = Column(Integer, ForeignKey("user.id"), primary_key=True)


class LikePost(Base):
    user_id = Column(Integer, ForeignKey("user.id"), primary_key=True)
    post_id = Column(Integer, ForeignKey("post.id"), primary_key=True)


class LikePack(Base):
    user_id = Column(Integer, ForeignKey("user.id"), primary_key=True)
    pack_id = Column(Integer, ForeignKey("pack.id"), primary_key=True)


class LikeComment(Base):
    user_id = Column(Integer, ForeignKey("user.id"), primary_key=True)
    comment_id = Column(Integer, ForeignKey("comment.id"), primary_key=True)


class LikeImage(Base):
    user_id = Column(Integer, ForeignKey("user.id"), primary_key=True)
    image_id = Column(Integer, ForeignKey("image.id"), primary_key=True)


class Reported(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), primary_key=True)
    post_id = Column(Integer, ForeignKey("post.id"))
    pack_id = Column(Integer, ForeignKey("pack.id"))

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)


class PasswordReset(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), primary_key=True)
    callback_id = Column(String)

    def __init__(self, user_id):
        self.callback_id = self.generate_callback_id()
        self.user_id = user_id

    @staticmethod
    def generate_callback_id():
        return ''.join(choice('0123456789ABCDEF') for i in range(16))


# Create database tables if not present
# Comment out when working with configured databases
Base.metadata.create_all(engine)
