from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi_sqlalchemy import DBSessionMiddleware

from utils.consts import DATABASE_URL, DEVELOPMENT
from api import user, resources, item, trip, category, pack

if DEVELOPMENT:
    # Create the database tables if they don't exist
    from sqlalchemy import create_engine
    from models.base import Base
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(engine)

app = FastAPI()
app.add_middleware(DBSessionMiddleware, db_url=DATABASE_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
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
