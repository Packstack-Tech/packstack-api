from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi_sqlalchemy import DBSessionMiddleware

# from sqlalchemy import create_engine
# from models.base import Base

from utils.consts import DATABASE_URL
from api import user, resources, item, trip, category, pack

# Create the database tables
# engine = create_engine(DATABASE_URL)
# Base.metadata.create_all(engine)

app = FastAPI()
app.add_middleware(DBSessionMiddleware, db_url=DATABASE_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        'http://localhost:3000',
        'http://127.0.0.1:3000',
        'http://localhost:5173',
        'http://127.0.0.1:5173',
        'https://packstack.io',
        'https://*.packstack.io',
    ],
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
