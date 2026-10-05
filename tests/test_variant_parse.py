"""catalog.variant_parse: rules-only parsing of user-typed variant text."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from catalog.variant_parse import parse_variant  # noqa: E402
from models.keys import canonical_variant_key  # noqa: E402


def axes(text):
    return [(a.kind, a.value) for a in parse_variant(text).axes]


@pytest.mark.parametrize("text,expected", [
    ("Regular", [("length", "Regular")]),
    ("reg", [("length", "Regular")]),
    ("L", [("size", "Large")]),
    ("Size M", [("size", "Medium")]),
    ("X-Large", [("size", "XL")]),
    ("Extra Large", [("size", "XL")]),
    ("2XL", [("size", "XXL")]),
    ("XXS", [("size", "XXS")]),
    ("S/M", [("size", "S/M")]),
    ("L/XL", [("size", "L/XL")]),
    ("Large/X-Large", [("size", "Large/XL")]),
    ("Women's", [("gender", "Women's")]),
    ("Women", [("gender", "Women's")]),
    ("Ladies", [("gender", "Women's")]),
    ("Herren", [("gender", "Men's")]),
    ("Men's Medium", [("gender", "Men's"), ("size", "Medium")]),
    ("Women's / S", [("gender", "Women's"), ("size", "Small")]),
    ("Regular Wide", [("length", "Regular"), ("width", "Wide")]),
    ("LW (Long/Wide)", [("length", "Long"), ("width", "Wide")]),
    ("Long (Lengthen)", [("length", "Long")]),
    ("2P", [("capacity", "2P")]),
    ("2 Person", [("capacity", "2P")]),
    ("2-Person", [("capacity", "2P")]),
    ("two person", [("capacity", "2P")]),
    ("2L", [("capacity", "2L")]),
    ("2 Liters", [("capacity", "2L")]),
    ("32 oz", [("capacity", "32 oz")]),
    ("25000mAh", [("capacity", "25000mAh")]),
    ("400 Lumen", [("capacity", "400 lm")]),
    ("20°F", [("capacity", "20°F")]),
    ("20F/-6C", [("capacity", "20°F/-6°C")]),
    ("0°F (-17°C)", [("capacity", "0°F")]),
    ("6-Pack", [("capacity", "6-Pack")]),
    ("10 Count", [("capacity", "10-Pack")]),
    ("32-inch", [("capacity", "32in")]),
    ("40 x 20 cm", [("capacity", "40 x 20 cm")]),
    ("US 10.5", [("size", "US 10.5")]),
    ("EU 43-45", [("size", "EU 43-45")]),
    ("Size 11", [("size", "11")]),
    ("2021", [("generation", "2021")]),
    ("Gen 4", [("generation", "Gen 4")]),
    ("2nd Generation", [("generation", "Gen 2")]),
    ("v2", [("generation", "v2")]),
    ("L (80 x 130 cm)", [("size", "Large")]),           # parenthetical spec of the size is dropped
    ("Standard", []),
    ("Black", []),
    ("w/ keys", []),
    ("approx 750g", []),
    ("(32g)", []),
])
def test_axes(text, expected):
    assert axes(text) == expected


def test_canonical_name_orders_axes():
    assert parse_variant("Black / M / Men's").name == "Men's / Medium"
    assert parse_variant("Men's Medium").name == parse_variant("Medium, Men's").name
    assert parse_variant("Regular").kind == "length"
    assert parse_variant("Men's Medium").kind == "combo"


@pytest.mark.parametrize("text,color", [
    ("Black", "Black"),
    ("Black (BK)", "Black"),
    ("Dark Petrol Blue", "Dark Petrol Blue"),
    ("Gemini Green", "Gemini Green"),
    ("Tarn Blue", "Tarn Blue"),
    ("schwarz", "schwarz"),
    ("Hushed Lavender/Chroma Purple, M", "Hushed Lavender / Chroma Purple"),
    ("Men Red", "Red"),
    ("Khaki - Women's S", "Khaki"),
    ("Long, Gemini Green", "Gemini Green"),
    ("Regular", None),
])
def test_color(text, color):
    assert parse_variant(text).color == color


@pytest.mark.parametrize("text", ["w/ keys", "with footprint", "approx 750g", "(32g)", "for sleeping"])
def test_notes_are_not_variants(text):
    p = parse_variant(text)
    assert not p.is_variant and not p.needs_ai and p.note


@pytest.mark.parametrize("text", [
    "Clear Scotchgard Anti-Fog Lens", "Ultra 200X", "3L, 5L, 8L", "Pasta Bolognese", "20R Mummy", "4800",
])
def test_unplaceable_goes_to_model(text):
    assert parse_variant(text).needs_ai


def test_empty():
    p = parse_variant("   ")
    assert not p.is_variant and not p.needs_ai and p.name is None


def test_alias_bugs_fixed():
    assert canonical_variant_key("S/M") != canonical_variant_key("Small")
    assert canonical_variant_key("W") != canonical_variant_key("Women's")
    assert canonical_variant_key("2 Person") == canonical_variant_key("2P") == canonical_variant_key("2-Person")
    assert canonical_variant_key("Size M") == canonical_variant_key("Medium") == canonical_variant_key("M")
    assert canonical_variant_key("Extra Large") == canonical_variant_key("XL") == canonical_variant_key("X-Large")
