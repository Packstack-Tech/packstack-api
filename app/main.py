from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi_sqlalchemy import DBSessionMiddleware

from utils.consts import DATABASE_URL, DEVELOPMENT, APP_HOST
from api import user, resources, item, trip, category, pack

# if DEVELOPMENT:
from sqlalchemy import create_engine
from models.base import Base
engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)

app = FastAPI()
app.add_middleware(DBSessionMiddleware, db_url=DATABASE_URL)

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


@app.get("/health-check")
def health_check():
    return "Packstack API is available"
