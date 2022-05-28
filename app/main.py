from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi_sqlalchemy import DBSessionMiddleware

from consts import DATABASE_URL
from api import user, resources, item, trip, category, pack

app = FastAPI()
app.add_middleware(DBSessionMiddleware, db_url=DATABASE_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        'http://localhost:3000',
        'http://127.0.0.1:3000',
        'https://packstack.io',
        'https://beta.packstack.io'
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
