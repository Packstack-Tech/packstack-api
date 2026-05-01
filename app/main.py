from fastapi import FastAPI
import sentry_sdk
from fastapi.middleware.cors import CORSMiddleware
from fastapi_sqlalchemy import DBSessionMiddleware
from sqlalchemy import create_engine

from utils.consts import DATABASE_URL, DEVELOPMENT, APP_HOST
from api import user, resources, item, item_lifecycle, benchmark, trip, category, pack, kit, hiker_profile, webhook

ENGINE_KWARGS = dict(
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=300,
)

engine = create_engine(DATABASE_URL, **ENGINE_KWARGS)

# if DEVELOPMENT:
from models.base import Base
Base.metadata.create_all(engine)

if not DEVELOPMENT:
    sentry_sdk.init(
        dsn="https://d794ed7cb82ca2c3e95cf1ceb96c3bd9@o313912.ingest.us.sentry.io/4510944527515648",
        # Add data like request headers and IP for users,
        # see https://docs.sentry.io/platforms/python/data-management/data-collected/ for more info
        send_default_pii=True,
    )

app = FastAPI()
app.add_middleware(DBSessionMiddleware, custom_engine=engine)

allowed_origins = [o.strip() for o in APP_HOST.split(',') if o.strip()] if APP_HOST else []

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(
    user.route,
    prefix="/user",
    tags=["user"],
    responses={404: {"description": "Not found"}}
)


app.include_router(
    resources.route,
    prefix="/resources",
    tags=["resources"],
    responses={404: {"description": "Not found"}}
)


app.include_router(
    item.route,
    prefix="/item",
    tags=["item"],
    responses={404: {"description": "Not found"}}
)

app.include_router(
    item_lifecycle.route,
    prefix="/item",
    tags=["item-lifecycle"],
    responses={404: {"description": "Not found"}}
)

app.include_router(
    benchmark.route,
    prefix="/benchmark",
    tags=["benchmark"],
    responses={404: {"description": "Not found"}}
)


app.include_router(
    trip.route,
    prefix="/trip",
    tags=["trip"],
    responses={404: {"description": "Not found"}}
)

app.include_router(
    pack.route,
    prefix="/pack",
    tags=["pack"],
    responses={404: {"description": "Not found"}}
)


app.include_router(
    category.route,
    prefix="/category",
    tags=["category"],
    responses={404: {"description": "Not found"}}
)

app.include_router(
    kit.route,
    prefix="/kit",
    tags=["kit"],
    responses={404: {"description": "Not found"}}
)

app.include_router(
    hiker_profile.route,
    prefix="/hiker-profile",
    tags=["hiker-profile"],
    responses={404: {"description": "Not found"}}
)


app.include_router(
    webhook.route,
    prefix="/webhook",
    tags=["webhook"],
    responses={404: {"description": "Not found"}}
)


@app.get("/health-check")
def health_check():
    return "Packstack API is available"
