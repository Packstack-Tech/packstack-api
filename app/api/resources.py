from fastapi import APIRouter
from fastapi_sqlalchemy import db

from models.base import Condition, Geography
from models.enums import WeightUnit, Currency, Plan, UnitSystem, Month
from utils.utils import enum_to_dict

route = APIRouter()


@route.get("")
def fetch():
    conditions = db.session.query(Condition).all()
    geographies = db.session.query(Geography).all()

    return {
        "conditions": conditions,
        "currencies": enum_to_dict(Currency),
        "geographies": geographies,
        "plans": enum_to_dict(Plan),
        "months": enum_to_dict(Month),
        "unitSystem": enum_to_dict(UnitSystem),
        "weightUnits": enum_to_dict(WeightUnit)
    }
