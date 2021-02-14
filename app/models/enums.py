import enum


class Plan(enum.Enum):
    FREE = 1
    BASIC = 2
    PREMIUM = 3


class UnitSystem(enum.Enum):
    IMPERIAL = 1
    METRIC = 2


class WeightUnit(enum):
    OUNCES = 1
    POUNDS = 2
    GRAMS = 3
    KILOGRAMS = 4


class Currency(enum.Enum):
    USD = 1


class Month(enum.Enum):
    JANUARY = 1
    FEBRUARY = 2
    MARCH = 3
    APRIL = 4
    MAY = 5
    JUNE = 6
    JULY = 7
    AUGUST = 8
    SEPTEMBER = 9
    OCTOBER = 10
    NOVEMBER = 11
    DECEMBER = 12
