import enum


class Plan(enum.Enum):
    FREE = 1
    BASIC = 2
    PREMIUM = 3


class UnitSystem(enum.Enum):
    IMPERIAL = 1
    METRIC = 2


class WeightUnit(enum.Enum):
    OUNCES = 1
    POUNDS = 2
    GRAMS = 3
    KILOGRAMS = 4


class Currency(enum.Enum):
    USD = 1
