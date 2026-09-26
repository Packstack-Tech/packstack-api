"""Normalization keys for catalog identity.

One definition shared by the API, the Celery enrichment task, and the
workshop scripts. `brand_key` / `product_key` on CatalogProduct and
`name_key` on CatalogVariant are computed with these functions and backed
by unique indexes, so two spellings of the same product cannot coexist.

Differences from the older normalize_name in tasks/enrich_product.py:
  * '+' is preserved (X-Mid Pro 2 vs 2+, Lone Peak 9 vs 9+ are different)
  * non-Latin scripts are kept rather than collapsing to ""
"""

import re

_BRAND_SUFFIXES = ("gear", "inc", "llc", "ltd", "co", "company", "outdoors", "outdoor", "equipment")

# Variant spellings that collapse to one canonical key.
VARIANT_ALIASES = {
    "reg": "regular", "r": "regular",
    "lg": "large", "l": "large",
    "sm": "small", "s": "small",
    "med": "medium", "m": "medium",
    "xlarge": "xl", "extralarge": "xl",
    "womens": "women's", "wmns": "women's", "women": "women's", "w": "women's",
    "mens": "men's", "men": "men's",
    "regularwide": "regular / wide", "regwide": "regular / wide",
    "longwide": "long / wide",
    "1person": "1p", "2person": "2p", "3person": "3p", "4person": "4p",
    "oneperson": "1p", "twoperson": "2p", "threeperson": "3p",
}


def normalize_name(value):
    if not value:
        return ""
    v = value.casefold().strip()
    if v.startswith("the "):
        v = v[4:]
    v = v.replace("+", "plus")
    return re.sub(r"[\W_]+", "", v)


def normalize_brand(value):
    v = (value or "").casefold().strip().replace("+", "plus")
    words = re.sub(r"[\W_]+", " ", v).split()
    while len(words) > 1 and words[-1] in _BRAND_SUFFIXES:
        words.pop()
    return "".join(words)


def canonical_variant_key(value):
    k = normalize_name(value)
    return normalize_name(VARIANT_ALIASES.get(k, k))


def has_gear(brand_name):
    return bool(re.search(r"\bgear\b", brand_name or "", re.IGNORECASE))


def product_keys(brand_name, product_name):
    """(brand_key, product_key) for a CatalogProduct."""
    return normalize_brand(brand_name), normalize_name(product_name)
