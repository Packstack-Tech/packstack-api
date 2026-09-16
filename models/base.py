import datetime
import uuid

from random import choice

from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, JSON, DATE, String, DateTime, Text, TIMESTAMP, func, \
    Numeric, UniqueConstraint, UUID
from sqlalchemy.ext.declarative import declarative_base, declared_attr
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship

DO_CDN = "https://assets.packstack.io"


class Base(object):
    @declared_attr
    def __tablename__(self):
        return self.__name__.lower()

    def update(self, kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


Base = declarative_base(cls=Base)


class User(Base):
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    username = Column(String(15), unique=True, nullable=False, index=True)
    password = Column(String, nullable=True)
    google_id = Column(String, unique=True, index=True)
    apple_id = Column(String, unique=True, index=True)
    apple_refresh_token = Column(String, nullable=True)
    google_refresh_token = Column(String, nullable=True)
    email_verified = Column(Boolean, default=False)

    stripe_customer_id = Column(String)
    stripe_sub_id = Column(String)
    is_subscribed = Column(Boolean, default=False, nullable=False, server_default="false")

    display_name = Column(String(50))
    bio = Column(String(500))
    unit_weight = Column(String(10), default="METRIC")
    unit_distance = Column(String(10), default="MI")
    unit_temperature = Column(String(10), default="F")
    currency = Column(String(10), default="USD")
    hide_table_headers = Column(Boolean, default=False)

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
    avatar = relationship("Image",
                          lazy="select",
                          primaryjoin="and_(User.id == Image.user_id, "
                          "Image.avatar == True)",
                          order_by="desc(Image.created_at)",
                          cascade="all, delete-orphan",
                          uselist=False)

    inventory = relationship("Item",
                             lazy="select",
                             primaryjoin="User.id == Item.user_id",
                             cascade="all, delete-orphan")

    trips = relationship("Trip",
                         backref="user",
                         lazy="select",
                         primaryjoin="User.id == Trip.user_id",
                         order_by="desc(Trip.end_date)",
                         cascade="all, delete-orphan")

    hiker_profiles = relationship("HikerProfile",
                                  lazy="select",
                                  primaryjoin="User.id == HikerProfile.user_id",
                                  cascade="all, delete-orphan")

    def to_dict(self):
        active_trips = sorted(
            [t for t in self.trips if not t.removed],
            key=lambda t: t.created_at,
            reverse=True,
        )
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "avatar": self.avatar,
            "bio": self.bio,
            "unit_weight": self.unit_weight,
            "unit_distance": self.unit_distance,
            "unit_temperature": self.unit_temperature,
            "currency": self.currency,
            "banned": self.banned,
            "deactivated": self.deactivated,
            "email_verified": self.email_verified,
            "is_subscribed": self.is_subscribed,

            "instagram_url": self.instagram_url,
            "youtube_url": self.youtube_url,
            "twitter_url": self.twitter_url,
            "facebook_url": self.facebook_url,
            "snap_url": self.snap_url,
            "personal_url": self.personal_url,

            "trips": active_trips,
        }



class Item(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"))
    brand_id = Column(Integer, ForeignKey("brand.id"))
    product_id = Column(Integer, ForeignKey("product.id"))
    product_variant_id = Column(Integer, ForeignKey("productvariant.id"))
    category_id = Column(Integer, ForeignKey("itemcategory.id"))
    catalog_product_id = Column(Integer, ForeignKey("catalogproduct.id"))
    sort_order = Column(Integer, default=0)
    removed = Column(Boolean, default=False)
    deleted = Column(Boolean, default=False)

    name = Column(String(100))
    weight = Column(Numeric)
    unit = Column(String(10))
    price = Column(Numeric)
    calories = Column(Numeric, nullable=True)
    consumable = Column(Boolean, default=False)
    product_url = Column(String(1000))
    notes = Column(Text)

    # Lifecycle: acquisition
    acquired_date = Column(DATE)
    acquisition_type = Column(String(20))
    purchase_retailer = Column(String(200))

    # Lifecycle: condition & status
    condition = Column(String(20))
    status = Column(String(20), default="active")

    # Lifecycle: retirement
    retired_date = Column(DATE)
    retired_reason = Column(String(50))
    replaced_by_id = Column(Integer, ForeignKey("item.id"))

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    brand = relationship("Brand", lazy="joined")
    product = relationship("Product", lazy="joined")
    product_variant = relationship("ProductVariant", lazy="joined")
    catalog_product = relationship("CatalogProduct", lazy="joined")
    category = relationship("ItemCategory",
                            lazy="joined",
                            foreign_keys=[category_id],
                            uselist=False)
    replaced_by = relationship("Item", remote_side="Item.id", uselist=False)


class Category(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"))
    name = Column(String(50))


class ItemCategory(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey('user.id'))
    category_id = Column(Integer, ForeignKey('category.id'))
    sort_order = Column(Integer, default=0)

    category = relationship("Category", lazy="joined", uselist=False)


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


class ProductVariant(Base):
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("product.id"), nullable=False)
    name = Column(String(250), nullable=False)

    # Ensure variant is unique per product
    __table_args__ = (UniqueConstraint(
        'name', 'product_id', name='uc_name_product_id'),)


class CatalogProduct(Base):
    id = Column(Integer, primary_key=True, index=True)

    brand_name = Column(String(100), nullable=False, index=True)
    product_name = Column(String(250), nullable=False)
    variant_name = Column(String(250))
    display_name = Column(String(500), nullable=False)

    weight = Column(Numeric)
    weight_unit = Column(String(10))
    product_url = Column(String(1000))
    description = Column(String(2000))
    image_url = Column(String(1000))
    category_suggestion = Column(String(100))
    subcategory = Column(String(100))
    catalog_url_slug = Column(String(500))
    additional_specs = Column(JSON)
    kcal = Column(Integer)

    brand_id = Column(Integer, ForeignKey("brand.id"))
    product_id = Column(Integer, ForeignKey("product.id"))
    product_variant_id = Column(Integer, ForeignKey("productvariant.id"))

    status = Column(String(20), default="pending", index=True)
    source_item_count = Column(Integer)
    ai_confidence = Column(Numeric)

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        UniqueConstraint('brand_name', 'product_name', 'variant_name',
                         name='uq_catalog_brand_product_variant'),
        Index('ix_catalog_search', 'status', 'brand_name', 'product_name'),
    )


class TrailEnrichment(Base):
    """Cached AI trail research, keyed by normalized location string.

    Trail specs (distance, elevation, terrain, pace, trail system) are
    properties of the trail, not the trip, so they're cached indefinitely.
    Temperatures vary by season and are cached per calendar month in
    monthly_temps: {"7": {"temp_min": 5, "temp_max": 18, "temp_category": "moderate"}}
    """
    id = Column(Integer, primary_key=True, index=True)

    location_key = Column(String(250), nullable=False, unique=True, index=True)
    canonical_location = Column(String(500), nullable=False)

    distance = Column(Numeric)              # km
    daily_elevation_gain = Column(Numeric)  # m
    terrain = Column(String(20))
    pace = Column(String(10))
    trail_system = Column(String(20))
    monthly_temps = Column(JSON)

    source = Column(String(20), default="ai")

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())


class ItemLog(Base):
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("item.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    event_type = Column(String(30), nullable=False)
    note = Column(Text)
    event_date = Column(DATE, nullable=False)

    old_condition = Column(String(20))
    new_condition = Column(String(20))
    old_weight = Column(Numeric)
    new_weight = Column(Numeric)
    cost = Column(Numeric)

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)

    __table_args__ = (
        Index('ix_itemlog_item_date', 'item_id', 'event_date'),
    )


class CategoryBenchmark(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    category_name = Column(String(50), nullable=False)

    lifespan_years = Column(Numeric)
    expected_nights = Column(Numeric)
    expected_distance = Column(Numeric)
    distance_unit = Column(String(10))

    __table_args__ = (
        UniqueConstraint('user_id', 'category_name', name='uq_user_category_benchmark'),
    )


class Kit(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    name = Column(String(200), nullable=False)

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())

    items = relationship("KitItem", lazy="joined",
                         cascade="all, delete-orphan")


class KitItem(Base):
    kit_id = Column(Integer, ForeignKey("kit.id"), primary_key=True)
    item_id = Column(Integer, ForeignKey("item.id"), primary_key=True)
    quantity = Column(Numeric, default=1)

    item = relationship("Item",
                        lazy="joined",
                        foreign_keys=[item_id],
                        uselist=False)


class HikerProfile(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    name = Column(String(255), nullable=False)
    weight = Column(Numeric, nullable=True)
    height = Column(Numeric, nullable=True)
    year_of_birth = Column(Integer, nullable=True)
    sex = Column(String(10), nullable=True)
    body_type = Column(String(20), nullable=True)
    is_default = Column(Boolean, default=False, nullable=False)

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())


class PackItem(Base):
    pack_id = Column(Integer, ForeignKey("pack.id"), primary_key=True)
    item_id = Column(Integer, ForeignKey("item.id"), primary_key=True)
    quantity = Column(Numeric, default=1)
    worn = Column(Boolean, default=False)
    checked = Column(Boolean, default=False)
    sort_order = Column(Numeric, default=0)

    # Relationship
    item = relationship("Item",
                        lazy="joined",
                        foreign_keys=[item_id],
                        uselist=False)


class Post(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    notes = Column(String(2500), nullable=False)
    removed = Column(Boolean, default=False)

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())


class Pack(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    trip_id = Column(Integer, ForeignKey("trip.id"))
    hiker_profile_id = Column(Integer, ForeignKey("hikerprofile.id"), nullable=True)
    title = Column(String(500), nullable=False)

    # Relationships
    items = relationship("PackItem", lazy="joined",
                         cascade="all, delete-orphan")


class Trip(Base):
    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(UUID(as_uuid=True), default=uuid.uuid4)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)

    title = Column(String(500), nullable=False)
    location = Column(String(500))
    start_date = Column(DATE)
    end_date = Column(DATE)

    temp_min = Column(Integer)
    temp_max = Column(Integer)
    temp_category = Column(String(10))
    distance = Column(Numeric)
    daily_elevation_gain = Column(Numeric)
    terrain = Column(String(20))
    pace = Column(String(10))
    notes = Column(String(2500))
    trail_system = Column(String(20))
    enrich_status = Column(String(20))
    published = Column(Boolean, default=False)
    removed = Column(Boolean, default=False)

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())



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
    sort_order = Column(Integer, default=0)
    caption = Column(String(500))

    trip_id = Column(Integer, ForeignKey("trip.id"))
    item_id = Column(Integer, ForeignKey("item.id"))
    post_id = Column(Integer, ForeignKey("post.id"))

    s3_key = Column(String)
    s3_key_thumb = Column(String)
    s3_url = Column(String)
    s3_url_thumb = Column(String)

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)

    @hybrid_property
    def s3(self):
        return self.s3_key

    # Defines the asset url
    @s3.setter
    def s3(self, metadata):
        entity = metadata['entity']  # trip, image, post or avatar
        extension = '.png'

        # entity path segment
        entity_id = self.trip_id or self.item_id or self.post_id
        entity_path = ''
        if entity_id:
            entity_path = f'/{entity_id}'

        full_filename = f'{self.id}{extension}'
        thumb_filename = f'{self.id}-thumb{extension}'

        s3_key_path = f'user/{self.user_id}/{entity}{entity_path}'
        s3_key = f'{s3_key_path}/{full_filename}'
        s3_key_thumb = f'{s3_key_path}/{thumb_filename}'

        self.s3_key = s3_key
        self.s3_key_thumb = s3_key_thumb
        self.s3_url = f'{DO_CDN}/{s3_key}'
        self.s3_url_thumb = f'{DO_CDN}/{s3_key_thumb}'


class TripCondition(Base):
    trip_id = Column(Integer, ForeignKey("trip.id"), primary_key=True)
    condition_id = Column(Integer, ForeignKey(
        "condition.id"), primary_key=True)


class TripGeography(Base):
    trip_id = Column(Integer, ForeignKey("trip.id"), primary_key=True)
    geography_id = Column(Integer, ForeignKey(
        "geography.id"), primary_key=True)


class Comment(Base):
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False)
    post_id = Column(Integer, ForeignKey("post.id"))
    trip_id = Column(Integer, ForeignKey("trip.id"))
    comment = Column(String(1000), nullable=False)
    removed = Column(Boolean, default=False)

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now())


class Follow(Base):
    user_id = Column(Integer, ForeignKey("user.id"), primary_key=True)
    following_id = Column(Integer, ForeignKey("user.id"), primary_key=True)


class LikePost(Base):
    user_id = Column(Integer, ForeignKey("user.id"), primary_key=True)
    post_id = Column(Integer, ForeignKey("post.id"), primary_key=True)


class LikeTrip(Base):
    user_id = Column(Integer, ForeignKey("user.id"), primary_key=True)
    trip_id = Column(Integer, ForeignKey("trip.id"), primary_key=True)


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
    trip_id = Column(Integer, ForeignKey("trip.id"))

    created_at = Column(
        DateTime, default=datetime.datetime.utcnow, nullable=False)


class EmailVerification(Base):
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("user.id"))
    callback_id = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)

    def __init__(self, user_id):
        self.callback_id = self.generate_callback_id()
        self.user_id = user_id

    @staticmethod
    def generate_callback_id():
        return ''.join(choice('0123456789ABCDEF') for i in range(16))


class AuthOtp(Base):
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    email = Column(String, nullable=False, index=True)
    otp_code = Column(String(6), nullable=False)
    username = Column(String(15), nullable=True)
    is_registration = Column(Boolean, default=False, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
