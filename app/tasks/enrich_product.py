import json
import logging
import os
import re
import statistics
import time
from contextlib import contextmanager
from difflib import SequenceMatcher

import anthropic
import requests
from sqlalchemy import create_engine, func, and_
from sqlalchemy.orm import Session

from sqlalchemy.exc import IntegrityError

from models.base import Brand, Product, ProductVariant, Item, CatalogProduct, CatalogVariant
from models.keys import normalize_name, normalize_brand, canonical_variant_key, product_keys
from catalog.resolver import resolve_product, resolve_variant, record_alias
from celery_app import celery_app
from tasks.catalog_image import find_product_image
from utils.consts import WORKER_DATABASE_URL

logger = logging.getLogger(__name__)

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            WORKER_DATABASE_URL,
            pool_size=2,
            max_overflow=3,
            pool_pre_ping=True,
            pool_recycle=300,
        )
    return _engine


@contextmanager
def get_session():
    engine = _get_engine()
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# AI client
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-5"
_client = None


def _get_ai_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


# Adaptive thinking is only available on newer Sonnet/Opus models —
# passing it to Haiku returns a 400 (invalid_request_error).
ADAPTIVE_THINKING_PREFIXES = ("claude-sonnet-5", "claude-sonnet-4-6", "claude-opus")


def supports_adaptive_thinking(model: str) -> bool:
    return model.startswith(ADAPTIVE_THINKING_PREFIXES)


def ai_complete(system: str, user: str, tools: list | None = None, max_retries: int = 3,
                model: str | None = None):
    client = _get_ai_client()
    resolved_model = model or DEFAULT_MODEL
    kwargs = dict(
        model=resolved_model,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if supports_adaptive_thinking(resolved_model):
        kwargs["thinking"] = {"type": "adaptive"}
    if tools:
        kwargs["tools"] = tools

    for attempt in range(max_retries):
        try:
            response = client.messages.create(**kwargs)
            break
        except anthropic.RateLimitError:
            wait = 2 ** attempt
            logger.warning("Rate limited, retrying in %ds (attempt %d/%d)", wait, attempt + 1, max_retries)
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning("Server error %d, retrying in %ds", e.status_code, wait)
                time.sleep(wait)
            else:
                raise
    else:
        raise RuntimeError(f"Failed after {max_retries} retries")

    while response.stop_reason == "pause_turn":
        logger.info("Received pause_turn, continuing...")
        kwargs["messages"] = [
            {"role": "user", "content": user},
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": "Please continue."},
        ]
        response = client.messages.create(**kwargs)

    return response


# ---------------------------------------------------------------------------
# Prompt & tool schema (ported from workshop/catalog_enrich/prompt.py)
# ---------------------------------------------------------------------------

CATEGORIES = [
    "Clothing", "Cookware", "Miscellaneous", "Sleep System", "Electronics",
    "Pack", "Shelter", "Toiletries", "Water System", "Food", "Footwear",
    "Tools", "First Aid", "Safety", "Camera", "Climbing",
]

SUBCATEGORIES = {
    "Clothing": [
        "Base Layer", "Mid Layer", "Insulation", "Rain Gear",
        "Wind Gear", "Sock", "Underwear", "Headwear", "Glove",
        "Pant & Short", "Shirt & Top",
    ],
    "Cookware": [
        "Stove", "Fuel", "Pot & Pan", "Utensil",
        "Drinkware", "Cleaning", "Coffee/Tea",
    ],
    "Sleep System": [
        "Sleeping Bag", "Quilt", "Sleeping Pad", "Pillow", "Liner", "Bivy",
    ],
    "Electronics": [
        "Power & Cable", "Lighting", "Navigation/Comm", "Battery",
        "Solar", "Wearable", "Audio",
    ],
    "Pack": [
        "Main Pack", "Daypack", "Protection", "Organization", "Add-on",
    ],
    "Shelter": [
        "Tent", "Hammock", "Tarp", "Hardware", "Structure",
    ],
    "Toiletries": [
        "Hygiene", "Bathroom", "Sun & Bug", "Personal Care",
    ],
    "Water System": [
        "Filtration", "Purification", "Bottle", "Hydration", "Storage",
    ],
    "Food": [
        "Meal", "Snack", "Beverage", "Storage", "Hanging",
    ],
    "Footwear": [
        "Primary", "Camp Shoe", "Gaiter", "Traction",
    ],
    "Tools": [
        "Knife", "Repair", "Trekking Pole", "Processing",
    ],
    "First Aid": [
        "Bandage", "Medication", "Blister Care", "Ointment",
    ],
    "Safety": [
        "Survival", "Fire", "Protection",
    ],
    "Camera": [
        "Body & Lens", "Support", "Media", "Power",
    ],
    "Climbing": [
        "Personal", "Hardware", "Protection", "Soft Good",
    ],
}

_subcategory_block = json.dumps(SUBCATEGORIES, indent=2)

SYSTEM_PROMPT = (
    "You are a backpacking and outdoor gear product database. You have expert knowledge "
    "of outdoor gear brands, product lines, and specifications. When given a brand and product name "
    "(which may be misspelled, abbreviated, or include variant info in the name), you research and "
    "return the canonical product information.\n\n"
    "You have access to web search. Use it to find product pages from both the manufacturer's website "
    "and major retail sites (REI, Amazon, etc.), since many manufacturers do not sell directly. "
    "Try searching the manufacturer's site first "
    '(e.g. "site:nemoequipment.com Tensor Insulated"), then also search retail sites '
    '(e.g. "Nemo Tensor Insulated site:rei.com" or the product name on Amazon). '
    "From the best available product page(s), extract:\n"
    "- A product URL (prefer the manufacturer's page if available, otherwise use a retail page)\n"
    "- The listed weight and any other specs (R-value, volume, packed size, temperature rating, etc.)\n\n"
    "WEIGHT IS CRITICAL: Weight in grams is the single most important spec for our catalog. "
    "You MUST make every effort to find it. Check the manufacturer's spec table, the product "
    "page details, retail listings (REI, Amazon), and review sites. If weight is listed in "
    "oz or lb, convert to grams. Only set weight_grams to null as a last resort when the "
    "product genuinely has no published weight (e.g. consumables sold by food weight, not gear weight).\n\n"
    "IMPORTANT: product_name is the BASE product only. Strip any option descriptor the user typed into "
    "it — size (S/M/L/Regular/Long), length, gender (Men's/Women's), person count (1P/2P), color, "
    "capacity — and return it in variant_name instead. Variants are handled separately; do not fold "
    "them into the product name. weight_grams is the weight of the base/default configuration "
    "(the standard or most common size).\n\n"
    "If the input is not a real, identifiable outdoor/backpacking product (e.g. \"small bag\", \"misc item\", "
    "random text), mark it as invalid.\n\n"
    f"When assigning a category, you MUST use one of these exact values: {', '.join(CATEGORIES)}.\n\n"
    "After assigning a category, also assign a subcategory. The valid subcategories for each category are:\n"
    f"{_subcategory_block}\n"
    "You MUST pick a subcategory from the list for the chosen category. If the product does not fit any "
    "subcategory, or the category is \"Miscellaneous\", set subcategory to null.\n\n"
    "FOOD ITEMS: If the category is \"Food\", look up the calories per serving (kcal) from the product "
    "page, nutrition label, or retailer listing. Report this value as kcal. For non-food items, set kcal "
    "to null."
)

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
}

TOOL_SCHEMA = {
    "name": "catalog_entry",
    "description": "Structured product information for the gear catalog.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_valid_product": {
                "type": "boolean",
                "description": "Whether this is an identifiable, real outdoor gear product.",
            },
            "brand_name": {
                "type": "string",
                "description": "Canonical/official brand name with correct capitalisation.",
            },
            "product_name": {
                "type": "string",
                "description": "Official product name without variant info (size, color, etc).",
            },
            "variant_name": {
                "type": ["string", "null"],
                "description": "Variant descriptor (size, color, gender, volume) or null if not applicable.",
            },
            "weight_grams": {
                "type": ["number", "null"],
                "description": "Product weight in grams (manufacturer spec). Null if unknown.",
            },
            "product_url": {
                "type": ["string", "null"],
                "description": (
                    "Product page URL (prefer manufacturer's page; use a retail page "
                    "if manufacturer doesn't sell direct). Null if unknown."
                ),
            },
            "description": {
                "type": ["string", "null"],
                "description": "One-sentence product description.",
            },
            "category": {
                "type": ["string", "null"],
                "enum": CATEGORIES + [None],
                "description": "Gear category. Must be one of the predefined values.",
            },
            "subcategory": {
                "type": ["string", "null"],
                "description": (
                    "Subcategory within the assigned category. Must be one of the valid "
                    "subcategories for the chosen category, or null."
                ),
            },
            "kcal": {
                "type": ["integer", "null"],
                "description": (
                    "Calories per serving for Food category items. Null for non-food "
                    "items or if unknown."
                ),
            },
            "additional_specs": {
                "type": ["object", "null"],
                "description": (
                    "Additional product specs as key-value pairs (e.g. r_value, volume_liters, "
                    "packed_size, temperature_rating). Keys should be snake_case. Null if no "
                    "additional specs found."
                ),
            },
        },
        "required": ["is_valid_product", "brand_name", "product_name"],
    },
}


def _build_user_prompt(brand_name: str, product_name: str, variant_name: str | None = None) -> str:
    parts = [f"Brand: {brand_name}", f"Product: {product_name}"]
    if variant_name:
        parts.append(f"Variant: {variant_name}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Confidence scoring (ported from workshop/catalog_enrich/confidence.py)
# ---------------------------------------------------------------------------

def _name_similarity(original: str, canonical: str) -> float:
    return SequenceMatcher(None, original.lower().strip(), canonical.lower().strip()).ratio()


def compute_confidence(
    ai_result: dict,
    original_product_name: str,
    original_brand_name: str,
    median_weight: float | None = None,
    item_count: int = 0,
) -> float:
    score = 0.0

    product_sim = _name_similarity(original_product_name, ai_result.get("product_name", ""))
    brand_sim = _name_similarity(original_brand_name, ai_result.get("brand_name", ""))
    score += ((product_sim * 0.7) + (brand_sim * 0.3)) * 0.4

    ai_weight = ai_result.get("weight_grams")
    if median_weight and ai_weight and median_weight > 0:
        weight_diff = abs(ai_weight - median_weight) / median_weight
        if weight_diff < 0.1:
            score += 0.25
        elif weight_diff < 0.25:
            score += 0.15
        elif weight_diff < 0.5:
            score += 0.05

    if item_count >= 20:
        score += 0.2
    elif item_count >= 10:
        score += 0.15
    elif item_count >= 5:
        score += 0.1
    elif item_count >= 2:
        score += 0.05

    if ai_result.get("product_url"):
        score += 0.15

    return round(min(score, 1.0), 3)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _get_item_count(session, brand_id: int, product_id: int) -> int:
    return session.query(func.count(Item.id)).filter(
        Item.brand_id == brand_id,
        Item.product_id == product_id,
    ).scalar() or 0


def _get_median_weight(session, brand_id: int, product_id: int) -> float | None:
    items = (
        session.query(Item.weight, Item.unit)
        .filter(
            Item.brand_id == brand_id,
            Item.product_id == product_id,
            Item.weight.isnot(None),
            Item.weight != 0,
        )
        .all()
    )
    if not items:
        return None

    conversion = {"g": 1, "kg": 1000, "oz": 28.3495, "lb": 453.592}
    weights_g = [float(w) * conversion.get(u, 1) for w, u in items]
    return statistics.median(weights_g) if weights_g else None


def _catalog_url_exists(session, product_url: str) -> CatalogProduct | None:
    if not product_url:
        return None
    return session.query(CatalogProduct).filter(
        CatalogProduct.product_url == product_url,
        CatalogProduct.status != "migrated",
    ).first()


# Spec-like strings that users put in the variant field ("690g", "14 oz").
_SPEC_PATTERN = re.compile(
    r"^\s*\d+([.,]\d+)?\s*(g|gram|grams|kg|oz|ounce|ounces|lb|lbs|pound|pounds|cm|mm|in|inch|inches|l|ml|"
    r"liter|liters|litre|litres)\.?\s*$",
    re.IGNORECASE,
)


def _looks_like_spec(text: str | None) -> bool:
    return bool(text and _SPEC_PATTERN.match(text.strip()))


_URL_CHECK_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PackstackBot/1.0)"}
LOW_CONFIDENCE_THRESHOLD = 0.6
URL_CONFIDENCE_BOOST = 0.15


def _check_url(url: str) -> int | None:
    try:
        resp = requests.head(url, headers=_URL_CHECK_HEADERS, timeout=5, allow_redirects=True)
        return resp.status_code
    except requests.RequestException:
        return None


def _call_ai_product(brand_name: str, product_name: str, variant_hint: str | None) -> dict | None:
    """Research the BASE product. The variant text is passed only as a hint
    so the model can strip it from a name like 'Tensor Insulated Regular'."""
    response = ai_complete(
        system=SYSTEM_PROMPT,
        user=_build_user_prompt(brand_name, product_name, variant_hint),
        tools=[WEB_SEARCH_TOOL, TOOL_SCHEMA],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "catalog_entry":
            return block.input
    return None


# ---------------------------------------------------------------------------
# Variant research — a deliberately narrow question
# ---------------------------------------------------------------------------

VARIANT_SYSTEM_PROMPT = (
    "You classify product OPTIONS for a backpacking gear catalog. You are given one known product "
    "(brand, name, base weight in grams, and the variants already on file with their weights) and a "
    "piece of text a user typed as that product's variant.\n\n"
    "Decide:\n"
    "1. is_variant — false if the text is not a product option at all: a weight or spec measurement "
    "(\"690g\", \"R 4.2\"), a year, a note, random words. true for a real option.\n"
    "2. affects_weight — true when the option changes the product's weight: size, length, width, "
    "person count (1P/2P), capacity/volume, gender cut, fill weight, temperature rating variants that are "
    "different SKUs. false for cosmetic options: color, pattern, print, colorway, limited edition names.\n"
    "3. canonical_name — the manufacturer's spelling of the option (\"Regular\" not \"reg\", \"Women's\" not "
    "\"wmns\", \"2P\" not \"2 person\"). If the text matches one of the variants already on file, return "
    "that variant's exact name.\n"
    "4. weight_grams — ONLY when affects_weight is true: the manufacturer's weight in grams for THAT option. "
    "Use web search (manufacturer site first, then REI/Amazon). If you cannot find it, return null — never "
    "guess and never copy the base weight.\n"
    "5. kind — one of size, length, gender, color, capacity, other."
)

VARIANT_TOOL_SCHEMA = {
    "name": "variant_verdict",
    "description": "Classification of a user-typed variant for a known catalog product.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_variant": {"type": "boolean"},
            "affects_weight": {"type": "boolean"},
            "canonical_name": {"type": ["string", "null"]},
            "weight_grams": {"type": ["number", "null"]},
            "kind": {"type": ["string", "null"], "enum": ["size", "length", "gender", "color", "capacity", "other", None]},
        },
        "required": ["is_variant", "affects_weight", "canonical_name"],
    },
}


def _call_ai_variant(cp: CatalogProduct, variant_text: str) -> dict | None:
    known = [v for v in (cp.variants or []) if not v.hidden]
    known_lines = [
        f"- {v.name}: {float(v.weight):g} g" if v.weight is not None else f"- {v.name}: (cosmetic, no weight change)"
        for v in known
    ] or ["(none yet)"]
    base = f"{float(cp.weight):g} {cp.weight_unit}" if cp.weight is not None else "unknown"
    user = (
        f"Product: {cp.brand_name} {cp.product_name}\n"
        f"Base weight: {base}\n"
        f"Variants on file:\n" + "\n".join(known_lines) + "\n\n"
        f"User-typed variant text: \"{variant_text}\""
    )
    response = ai_complete(
        system=VARIANT_SYSTEM_PROMPT,
        user=user,
        tools=[WEB_SEARCH_TOOL, VARIANT_TOOL_SCHEMA],
        model=VARIANT_MODEL,
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "variant_verdict":
            return block.input
    return None


VARIANT_MODEL = os.environ.get("ENRICH_VARIANT_MODEL", DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Product step
# ---------------------------------------------------------------------------

def _insert_rejected(session, *, brand_name, product_name, brand_id, product_id, item_count, confidence):
    bk, pk = product_keys(brand_name, product_name)
    entry = CatalogProduct(
        brand_name=brand_name, product_name=product_name,
        brand_key=bk, product_key=pk,
        display_name=f"{brand_name} {product_name}",
        brand_id=brand_id, product_id=product_id,
        status="rejected", source_item_count=item_count, ai_confidence=confidence,
    )
    session.add(entry)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()   # someone else inserted this key meanwhile — fine


def ensure_product(session, brand: Brand, product: Product, variant_hint: str | None) -> CatalogProduct | None:
    """Return the live CatalogProduct for this legacy brand/product, researching
    and inserting it if unknown. Returns None when the product is rejected or
    could not be researched."""
    # 1. This legacy product was already mapped (the FK is set on insert /
    #    canonical match). Legacy names often carry a variant suffix
    #    ("Tensor Insulated Regular") that the key lookup can't see.
    cp = session.query(CatalogProduct).filter(
        CatalogProduct.product_id == product.id,
        CatalogProduct.status != "migrated",
    ).first()
    # 2. Normalized identity.
    if cp is None:
        cp = resolve_product(session, brand.name, product.name, statuses=None)
    if cp is not None:
        if cp.status == "rejected":
            logger.info("SKIP (previously rejected): %s / %s", brand.name, product.name)
            return None
        return cp

    label = f"{brand.name} / {product.name}"
    item_count = _get_item_count(session, brand.id, product.id)
    result = _call_ai_product(brand.name, product.name, variant_hint)
    if not result:
        logger.warning("AI returned no result for %s", label)
        return None
    if not result.get("is_valid_product", False):
        logger.info("REJECTED (invalid product): %s", label)
        _insert_rejected(session, brand_name=brand.name, product_name=product.name,
                         brand_id=brand.id, product_id=product.id, item_count=item_count, confidence=0.0)
        return None

    canonical_brand = (result.get("brand_name") or brand.name).strip()
    canonical_product = (result.get("product_name") or product.name).strip()

    # The model often normalizes spelling; the canonical name may already exist.
    existing = resolve_product(session, canonical_brand, canonical_product, statuses=None)
    if existing is None:
        existing = _catalog_url_exists(session, result.get("product_url"))
    if existing is not None:
        if existing.status == "rejected":
            return None
        logger.info("MATCH (canonical): %s -> catalog %d", label, existing.id)
        if existing.product_id is None:
            existing.product_id, existing.brand_id = product.id, brand.id
            session.commit()
        return existing

    confidence = compute_confidence(
        ai_result=result, original_product_name=product.name, original_brand_name=brand.name,
        median_weight=_get_median_weight(session, brand.id, product.id), item_count=item_count,
    )
    product_url = result.get("product_url")
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        status_code = _check_url(product_url) if product_url else None
        if status_code == 200:
            confidence += URL_CONFIDENCE_BOOST
            logger.info("URL verified (200), confidence boosted to %.3f", confidence)
        elif product_url is None or status_code in (404, 410):
            logger.info("REJECTED: low confidence (%.3f), url=%s status=%s", confidence, product_url, status_code)
            _insert_rejected(session, brand_name=canonical_brand, product_name=canonical_product,
                             brand_id=brand.id, product_id=product.id, item_count=item_count, confidence=confidence)
            return None
        else:
            logger.info("URL check inconclusive (status=%s), proceeding", status_code)

    weight_grams = result.get("weight_grams")
    bk, pk = product_keys(canonical_brand, canonical_product)
    cp = CatalogProduct(
        brand_name=canonical_brand, product_name=canonical_product,
        brand_key=bk, product_key=pk,
        display_name=f"{canonical_brand} {canonical_product}",
        weight=weight_grams, weight_unit="g" if weight_grams else None,
        product_url=product_url, description=result.get("description"),
        category_suggestion=result.get("category"), subcategory=result.get("subcategory"),
        additional_specs=result.get("additional_specs"), kcal=result.get("kcal"),
        brand_id=brand.id, product_id=product.id,
        status="approved", source_item_count=item_count, ai_confidence=confidence,
    )
    session.add(cp)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()   # lost a race on the unique key; use the winner
        return resolve_product(session, canonical_brand, canonical_product)
    logger.info("Inserted product %s (confidence=%.3f)", cp.display_name, confidence)
    find_product_image.delay(cp.id)
    return cp


# ---------------------------------------------------------------------------
# Variant step
# ---------------------------------------------------------------------------

def ensure_variant(session, cp: CatalogProduct, variant_text: str | None) -> CatalogVariant | None:
    """Return the CatalogVariant of `cp` for the user's variant text, asking
    the model only when the text matches nothing on file. Records the
    user's spelling as an alias. Returns None when the text is not a variant."""
    if not variant_text or not variant_text.strip():
        return None
    variant_text = variant_text.strip()
    cv = resolve_variant(session, cp, variant_text)
    if cv is not None:
        record_alias(cv, variant_text)
        session.commit()
        return cv
    if _looks_like_spec(variant_text):
        logger.info("Variant text is a spec, ignored: %r", variant_text)
        return None

    verdict = _call_ai_variant(cp, variant_text)
    if not verdict or not verdict.get("is_variant"):
        logger.info("Not a variant per model: %r (%s)", variant_text, cp.display_name)
        return None
    name = (verdict.get("canonical_name") or variant_text).strip()
    cv = resolve_variant(session, cp, name)
    if cv is None:
        weight = verdict.get("weight_grams") if verdict.get("affects_weight") else None
        cv = CatalogVariant(
            catalog_product_id=cp.id, name=name, name_key=canonical_variant_key(name),
            weight=weight, weight_unit="g" if weight is not None else None,
            kind=verdict.get("kind"), aliases=[],
            sort_order=len([v for v in (cp.variants or []) if not v.hidden]),
        )
        session.add(cv)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            cv = resolve_variant(session, cp, name)
            if cv is None:
                return None
        else:
            logger.info("Inserted variant %r for %s (weight=%s, kind=%s)", name, cp.display_name, weight, cv.kind)
    record_alias(cv, variant_text)
    session.commit()
    return cv


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------

def link_items(session, cp: CatalogProduct, cv: CatalogVariant | None, product: Product,
               legacy_variant_id: int | None) -> int:
    """Link every unlocked item on this legacy product to the catalog product,
    and those on the same legacy variant to the catalog variant. Items that
    already point at a different product are left alone (the update path
    re-derives those)."""
    base = session.query(Item).filter(
        Item.product_id == product.id,
        Item.catalog_locked.is_(False),
    )
    n = base.filter(Item.catalog_product_id.is_(None)).update(
        {"catalog_product_id": cp.id}, synchronize_session=False)
    if cv is not None and legacy_variant_id is not None:
        n += base.filter(
            Item.catalog_product_id == cp.id,
            Item.product_variant_id == legacy_variant_id,
            Item.catalog_variant_id.is_(None),
        ).update({"catalog_variant_id": cv.id}, synchronize_session=False)
    session.commit()
    return n


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def enrich_product(self, item_id: int):
    """Research the catalog identity of one item's brand/product/variant and
    link it. Cheap when the product is already known: at most one narrow
    model call for an unseen variant, none when everything resolves by key."""
    with get_session() as session:
        item = session.query(Item).get(item_id)
        if item is None:
            logger.warning("Item %s not found, skipping", item_id)
            return
        if item.catalog_locked:
            logger.info("Item %s is catalog-locked, skipping", item_id)
            return
        brand = session.query(Brand).get(item.brand_id) if item.brand_id else None
        product = session.query(Product).get(item.product_id) if item.product_id else None
        if not brand or not product:
            return
        legacy_variant = session.query(ProductVariant).get(item.product_variant_id) if item.product_variant_id else None
        variant_text = legacy_variant.name if legacy_variant else None

        cp = ensure_product(session, brand, product, variant_text)
        if cp is None:
            return
        cv = ensure_variant(session, cp, variant_text)

        item = session.query(Item).get(item_id)   # refresh after commits
        if not item.catalog_locked:
            item.catalog_product_id = cp.id
            item.catalog_variant_id = cv.id if cv else None
            session.commit()
        linked = link_items(session, cp, cv, product, legacy_variant.id if legacy_variant else None)
        logger.info("Item %d -> catalog %d%s (+%d others linked)",
                    item_id, cp.id, f" / variant {cv.id}" if cv else "", linked)
