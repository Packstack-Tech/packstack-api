"""Catalog enrichment — research a brand/product/variant and file it.

Pure module: takes a SQLAlchemy session, no Celery, no engine. Used by
  * tasks/enrich_product.py   — the Celery task (one item per job)
  * workshop/catalog_enrich    — the batch driver (many items per run)
so both paths run exactly the same code. See claude/catalog-variants-pivot.md.

Flow per item:
  ensure_product  — CatalogProduct.product_id FK → normalized key → AI research
                    (product-level fields + base weight; canonical names
                    re-resolved before insert; confidence / URL check /
                    rejection; rejected rows keep their keys so they are
                    never re-researched)
  ensure_variant  — resolve by key/alias → spec-like text ignored → one narrow
                    model question (is it a variant? does it change weight?)
  link_items      — the item plus unlinked, unlocked siblings on the same
                    legacy product / variant
"""

import json
import logging
import os
import re
import statistics
import time
from difflib import SequenceMatcher

import anthropic
import requests
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models.base import Brand, Product, ProductVariant, Item, CatalogProduct, CatalogVariant, CatalogProductLegacyAlias
from models.keys import canonical_variant_key, product_keys
from catalog.resolver import resolve_product, resolve_variant, record_alias
from catalog.variant_parse import parse_variant, ParsedVariant, Axis, KINDS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AI client
# ---------------------------------------------------------------------------

DEFAULT_MODEL = os.environ.get("ENRICH_MODEL", "claude-sonnet-5")          # tier 2: web research
FAST_MODEL = os.environ.get("ENRICH_MODEL_FAST", "claude-haiku-4-5-20251001")          # tier 1 + variant classifier
WEB_SEARCH_MAX_USES = int(os.environ.get("ENRICH_WEB_SEARCH_MAX_USES", "2"))
ENRICH_THINKING = os.environ.get("ENRICH_THINKING", "0") == "1"                # adaptive thinking on tier 2
MAX_TOKENS = int(os.environ.get("ENRICH_MAX_TOKENS", "1500"))
# Tier-1 answer is trusted when its weight is within this fraction of the
# median weight users entered for the product.
FAST_WEIGHT_TOLERANCE = float(os.environ.get("ENRICH_FAST_WEIGHT_TOLERANCE", "0.15"))

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


def _log_usage(label: str, model: str, response) -> None:
    """One line per call so cost is visible in the worker logs."""
    u = getattr(response, "usage", None)
    if u is None:
        return
    searches = 0
    stu = getattr(u, "server_tool_use", None)
    if stu is not None:
        searches = getattr(stu, "web_search_requests", 0) or 0
    logger.info(
        "usage %s model=%s in=%s cache_read=%s cache_write=%s out=%s web_searches=%s",
        label, model, u.input_tokens,
        getattr(u, "cache_read_input_tokens", 0) or 0,
        getattr(u, "cache_creation_input_tokens", 0) or 0,
        u.output_tokens, searches,
    )


def ai_complete(system: str, user: str, tools: list | None = None, max_retries: int = 3,
                model: str | None = None, thinking: bool | None = None, label: str = "ai"):
    """One model call. The system prompt (and tools before it) is marked for
    prompt caching: it is identical across calls, so repeats are ~90% cheaper."""
    client = _get_ai_client()
    resolved_model = model or DEFAULT_MODEL
    kwargs = dict(
        model=resolved_model,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    use_thinking = ENRICH_THINKING if thinking is None else thinking
    if use_thinking and supports_adaptive_thinking(resolved_model):
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

    _log_usage(label, resolved_model, response)
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
    "You may have web search with a small budget. Use at most two searches: first the brand and "
    'product together (e.g. "Nemo Tensor Insulated weight specs"), and only if that does not give the '
    'weight, a retail search (e.g. "Nemo Tensor Insulated site:rei.com"). Do not search for things you '
    "already know. From the best available product page(s), extract:\n"
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
    "max_uses": WEB_SEARCH_MAX_USES,
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
        label="product/research",
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "catalog_entry":
            return block.input
    return None


# ---------------------------------------------------------------------------
# Junk pre-filter — no model call for things that can never be catalog gear
# ---------------------------------------------------------------------------

_JUNK_BRANDS = {
    "", "generic", "unknown", "unbranded", "no brand", "nobrand", "none", "n/a", "na", "misc",
    "miscellaneous", "various", "myog", "diy", "homemade", "home made", "custom", "other", "-", "--",
    "?", "??", "???", "tbd", "test", "x", "brand", "manufacturer", "amazon", "aliexpress", "ali", "temu",
    "ebay", "walmart", "target", "costco", "supermarket", "grocery", "dollar store", "dollarstore",
}
# A product name that is only a generic noun is a category, not a product.
_GENERIC_PRODUCT_WORDS = {
    "tent", "tarp", "backpack", "pack", "bag", "stuff sack", "stuffsack", "dry bag", "drybag", "pillow",
    "quilt", "sleeping bag", "sleeping pad", "pad", "stove", "pot", "spoon", "spork", "knife", "headlamp",
    "lighter", "matches", "socks", "shirt", "pants", "shorts", "jacket", "hat", "gloves", "towel",
    "toothbrush", "toothpaste", "sunscreen", "water bottle", "bottle", "rope", "cord", "stakes", "food",
    "snacks", "phone", "charger", "cable", "battery", "batteries", "wallet", "keys", "misc", "stuff",
}


def is_junk(brand_name: str | None, product_name: str | None) -> str | None:
    """Reason string when this brand/product should be rejected without a
    model call, else None."""
    b = (brand_name or "").strip().casefold()
    pn = (product_name or "").strip()
    p = pn.casefold()
    if b in _JUNK_BRANDS:
        return f"junk brand {brand_name!r}"
    if len(re.sub(r"[^\w]", "", p)) < 3:
        return f"product name too short {pn!r}"
    if p in _GENERIC_PRODUCT_WORDS or p.rstrip("s") in _GENERIC_PRODUCT_WORDS:
        return f"generic product word {pn!r}"
    if re.fullmatch(r"[\d\s.,x×*/-]+(g|kg|oz|lb|l|ml|cm|mm|in)?", p):
        return f"product name is a measurement {pn!r}"
    if not re.search(r"[A-Za-z0-9]", pn):
        return f"product name has no Latin characters {pn!r}"
    return None


# ---------------------------------------------------------------------------
# Tier 1 — recall from model knowledge, no tools
# ---------------------------------------------------------------------------

FAST_SYSTEM_PROMPT = (
    "You are a backpacking and outdoor gear product database answering FROM MEMORY. You have no web "
    "access. Given a brand and product name typed by a user (possibly misspelled or with a size/color "
    "in the name), return the canonical product information if you know this product well.\n\n"
    "Be honest about uncertainty: set `confidence` to how sure you are of the WEIGHT specifically "
    "(0.9+ only for well-known products whose spec you clearly recall; 0.5 or less if you are "
    "reconstructing or guessing). Never invent a weight — if you do not know it, set weight_grams to "
    "null and confidence low. product_name is the BASE product: strip size, length, gender, person "
    "count, color or capacity descriptors into variant_name. weight_grams is the base/default "
    "configuration.\n\n"
    "If the input is not a real, identifiable outdoor product, mark it invalid.\n\n"
    f"category MUST be one of: {', '.join(CATEGORIES)}. Subcategories per category:\n{_subcategory_block}\n"
    "Pick a subcategory from the list for the chosen category, or null. For Food, report kcal per serving."
)

FAST_TOOL_SCHEMA = {
    **TOOL_SCHEMA,
    "name": "catalog_entry_recall",
    "input_schema": {
        **TOOL_SCHEMA["input_schema"],
        "properties": {
            **TOOL_SCHEMA["input_schema"]["properties"],
            "confidence": {
                "type": "number",
                "description": "0-1: how sure you are that weight_grams is the manufacturer's spec.",
            },
        },
        "required": TOOL_SCHEMA["input_schema"]["required"] + ["confidence"],
    },
}


def _call_ai_recall(brand_name: str, product_name: str, variant_hint: str | None) -> dict | None:
    response = ai_complete(
        system=FAST_SYSTEM_PROMPT,
        user=_build_user_prompt(brand_name, product_name, variant_hint),
        tools=[FAST_TOOL_SCHEMA],
        model=FAST_MODEL,
        thinking=False,
        label="product/recall",
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "catalog_entry_recall":
            return block.input
    return None


def _recall_is_trustworthy(result: dict, median_weight: float | None) -> bool:
    """Accept a from-memory answer only when users' own weights corroborate it
    (or, with no user weights, the model is very sure and named a URL)."""
    if not result.get("is_valid_product"):
        return True   # a confident "not a product" needs no web search to confirm
    w = result.get("weight_grams")
    if w is None:
        return False
    if median_weight and median_weight > 0:
        return abs(float(w) - median_weight) / median_weight <= FAST_WEIGHT_TOLERANCE
    return (result.get("confidence") or 0) >= 0.9 and bool(result.get("product_url"))


# ---------------------------------------------------------------------------
# Variant research — a deliberately narrow question
# ---------------------------------------------------------------------------

VARIANT_SYSTEM_PROMPT = (
    "You parse the text a user typed as a product's VARIANT for a backpacking gear catalog into "
    "structured parts. You are given one known product (brand, name, base weight, the variants already "
    "on file) and the user's text.\n\n"
    "A catalog variant is a MANUFACTURER option that can change the product's weight. Split the text into:\n"
    "- axes: zero or more of kind=gender (Men's / Women's / Unisex / Kids), size (XS..XXXL, S/M, US 10, 32x32), "
    "length (Regular / Long / Short / Tall), width (Wide / Narrow), capacity (person count like 2P, volume "
    "like 2L / 32 oz, temperature rating like 20°F, pack count like 2-Pack, mAh, lumens, dimensions), "
    "generation (2021, Gen 2, v2). Use the manufacturer's spelling. If an axis matches a variant already on "
    "file, use that variant's exact value.\n"
    "- color: the colourway, in the user's language ('Gemini Green', 'Karbongrau'). Null if none.\n"
    "- note: anything else that is a personal remark or accessory, not an option ('w/ keys', 'with footprint', "
    "'approx 750g', 'for sleeping'). Null if none.\n"
    "A fabric spec (20D, Ultra 200X), a fill power, an edition name or a model number is NOT an axis: put it in "
    "note. Never invent an axis the text does not contain.\n"
    "- weight_grams: ONLY when an axis is present and you know the manufacturer's weight for THAT option. "
    "If a weight users entered is given and looks plausible, use it. With web search, use at most one search. "
    "Otherwise null — never guess, never copy the base weight."
)

VARIANT_TOOL_SCHEMA = {
    "name": "variant_parse",
    "description": "Structured parts of a user-typed variant for a known catalog product.",
    "input_schema": {
        "type": "object",
        "properties": {
            "axes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(KINDS)},
                        "value": {"type": "string"},
                    },
                    "required": ["kind", "value"],
                },
            },
            "color": {"type": ["string", "null"]},
            "note": {"type": ["string", "null"]},
            "weight_grams": {"type": ["number", "null"]},
        },
        "required": ["axes", "color", "note"],
    },
}


def _parsed_from_verdict(text: str, verdict: dict | None) -> ParsedVariant:
    pv = ParsedVariant(source=text.strip())
    if not verdict:
        return pv
    seen = set()
    for a in verdict.get("axes") or []:
        kind, value = a.get("kind"), (a.get("value") or "").strip()
        if kind in KINDS and value and kind not in seen:
            pv.axes.append(Axis(kind, value)); seen.add(kind)
    pv.color = (verdict.get("color") or "").strip() or None
    pv.note = (verdict.get("note") or "").strip() or None
    return pv


def _variant_user_median_g(session, cp: CatalogProduct, legacy_variant_id: int | None) -> float | None:
    """Median of the weights users entered for items on this legacy variant of
    this product — usually a better source than a web page, and free."""
    if legacy_variant_id is None or cp.product_id is None:
        return None
    rows = session.query(Item.weight, Item.unit).filter(
        Item.product_id == cp.product_id, Item.product_variant_id == legacy_variant_id,
        Item.weight.isnot(None), Item.weight != 0).all()
    if not rows:
        return None
    conv = {"g": 1, "kg": 1000, "oz": 28.3495, "lb": 453.592}
    return statistics.median(float(w) * conv.get(u, 1) for w, u in rows)


def _call_ai_variant(cp: CatalogProduct, variant_text: str, user_median_g: float | None = None,
                     with_search: bool = False) -> dict | None:
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
        + (f"\nWeight users entered for items with this variant: ~{user_median_g:.0f} g" if user_median_g else "")
    )
    response = ai_complete(
        system=VARIANT_SYSTEM_PROMPT,
        user=user,
        tools=[WEB_SEARCH_TOOL, VARIANT_TOOL_SCHEMA] if with_search else [VARIANT_TOOL_SCHEMA],
        model=DEFAULT_MODEL if with_search else VARIANT_MODEL,
        thinking=False,
        label="variant/search" if with_search else "variant/classify",
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "variant_parse":
            return block.input
    return None


VARIANT_MODEL = os.environ.get("ENRICH_VARIANT_MODEL", FAST_MODEL)


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


def ensure_product(session, brand: Brand, product: Product, variant_hint: str | None,
                   on_product_created=None) -> CatalogProduct | None:
    """Return the live CatalogProduct for this legacy brand/product, researching
    and inserting it if unknown. Returns None when the product is rejected or
    could not be researched. `on_product_created(catalog_product_id)` runs
    after a fresh insert (the Celery wrapper queues the image job here)."""
    # 1. This legacy product was already mapped (the FK is set on insert /
    #    canonical match). Legacy names often carry a variant suffix
    #    ("Tensor Insulated Regular") that the key lookup can't see.
    cp = session.query(CatalogProduct).filter(
        CatalogProduct.product_id == product.id,
        CatalogProduct.status != "migrated",
    ).first()
    # 1b. ...or it was a sibling merged into another product (Exos 48 -> Exos).
    if cp is None or cp.status == "merged":
        alias = session.get(CatalogProductLegacyAlias, product.id)
        if alias is not None:
            cp = session.get(CatalogProduct, alias.catalog_product_id)
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

    junk = is_junk(brand.name, product.name)
    if junk:
        logger.info("REJECTED (no model call, %s): %s", junk, label)
        _insert_rejected(session, brand_name=brand.name, product_name=product.name,
                         brand_id=brand.id, product_id=product.id, item_count=item_count, confidence=0.0)
        return None

    median_weight = _get_median_weight(session, brand.id, product.id)

    # Tier 1: recall, no tools. Trusted only when users' weights corroborate.
    result = None
    recall = _call_ai_recall(brand.name, product.name, variant_hint)
    if recall and _recall_is_trustworthy(recall, median_weight):
        logger.info("TIER1 accepted for %s (weight=%s, users' median=%s)", label,
                    recall.get("weight_grams"), median_weight)
        result = recall
    else:
        logger.info("TIER1 %s for %s -> web research",
                    "unsure" if recall else "no result", label)
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
        median_weight=median_weight, item_count=item_count,
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
    if on_product_created:
        on_product_created(cp.id)
    return cp


# ---------------------------------------------------------------------------
# Variant step
# ---------------------------------------------------------------------------

def parse_variant_text(cp: CatalogProduct, variant_text: str,
                       user_median_g: float | None = None) -> ParsedVariant:
    """Rules first; the model only for text the rules could not fully place.
    `parsed.decided_by` is 'rules' or 'ai' so callers can audit."""
    parsed = parse_variant(variant_text)
    parsed.decided_by = "rules"
    if parsed.needs_ai:
        verdict = _call_ai_variant(cp, variant_text, user_median_g=user_median_g)
        parsed = _parsed_from_verdict(variant_text, verdict)
        parsed.decided_by = "ai"
        if verdict and verdict.get("weight_grams") is not None:
            parsed.weight_g = float(verdict["weight_grams"])
    return parsed


def ensure_variant(session, cp: CatalogProduct, variant_text: str | None,
                   legacy_variant_id: int | None = None) -> CatalogVariant | None:
    """Return the CatalogVariant of `cp` for the user's variant text. Records
    the user's spelling as an alias. Returns None when the text carries no
    weight-bearing option (colour only, a note, a spec)."""
    cv, _ = ensure_variant_parsed(session, cp, variant_text, legacy_variant_id)
    return cv


def ensure_variant_parsed(session, cp: CatalogProduct, variant_text: str | None,
                          legacy_variant_id: int | None = None) -> tuple[CatalogVariant | None, ParsedVariant | None]:
    """ensure_variant plus the parse, so callers can move the colour the user
    typed onto their item (`parsed.color`)."""
    if not variant_text or not variant_text.strip():
        return None, None
    variant_text = variant_text.strip()

    # 1. already on file under this spelling (key or recorded alias): no parse
    cv = resolve_variant(session, cp, variant_text)
    if cv is not None:
        record_alias(cv, variant_text)
        session.commit()
        return cv, None

    # 2. split the text into axes / colour / note
    user_median = _variant_user_median_g(session, cp, legacy_variant_id)
    parsed = parse_variant_text(cp, variant_text, user_median_g=user_median)
    if not parsed.is_variant:
        logger.info("Not a variant (%s): %r -> color=%r note=%r", parsed.decided_by, variant_text, parsed.color, parsed.note)
        return None, parsed
    name = parsed.name

    # 3. the canonical name may already exist ("2 Person" -> "2P")
    cv = resolve_variant(session, cp, name)
    if cv is None:
        weight = parsed.weight_g
        if weight is None and user_median:
            weight = round(user_median, 1)            # users' measurements beat a web page
        elif weight is None:
            searched = _call_ai_variant(cp, name, with_search=True)
            if searched and searched.get("weight_grams") is not None:
                weight = float(searched["weight_grams"])
        cv = CatalogVariant(
            catalog_product_id=cp.id, name=name, name_key=canonical_variant_key(name),
            weight=weight, weight_unit="g" if weight is not None else None,
            kind=parsed.kind, aliases=[],
            sort_order=len([v for v in (cp.variants or []) if not v.hidden]),
        )
        session.add(cv)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            cv = resolve_variant(session, cp, name)
            if cv is None:
                return None, parsed
        else:
            logger.info("Inserted variant %r for %s (weight=%s, kind=%s, by=%s)",
                        name, cp.display_name, weight, cv.kind, parsed.decided_by)
    record_alias(cv, variant_text)
    session.commit()
    return cv, parsed


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
# One item, end to end
# ---------------------------------------------------------------------------

def enrich_item(session, item_id: int, on_product_created=None) -> tuple[CatalogProduct | None, CatalogVariant | None]:
    """Research the catalog identity of one item's brand/product/variant and
    link it. Cheap when the product is already known: at most one narrow
    model call for an unseen variant, none when everything resolves by key.
    Returns (product, variant) — both None when nothing could be linked."""
    item = session.query(Item).get(item_id)
    if item is None:
        logger.warning("Item %s not found, skipping", item_id)
        return None, None
    if item.catalog_locked:
        logger.info("Item %s is catalog-locked, skipping", item_id)
        return None, None
    brand = session.query(Brand).get(item.brand_id) if item.brand_id else None
    product = session.query(Product).get(item.product_id) if item.product_id else None
    if not brand or not product:
        return None, None
    legacy_variant = session.query(ProductVariant).get(item.product_variant_id) if item.product_variant_id else None
    variant_text = legacy_variant.name if legacy_variant else None

    cp = ensure_product(session, brand, product, variant_text, on_product_created)
    if cp is None:
        return None, None
    cv, parsed = ensure_variant_parsed(session, cp, variant_text, legacy_variant.id if legacy_variant else None)
    if parsed is not None and parsed.color and not item.color:
        item.color = parsed.color[:80]      # the colourway belongs to the item, not the catalog

    item = session.query(Item).get(item_id)   # refresh after commits
    if not item.catalog_locked:
        item.catalog_product_id = cp.id
        item.catalog_variant_id = cv.id if cv else None
        session.commit()
    linked = link_items(session, cp, cv, product, legacy_variant.id if legacy_variant else None)
    logger.info("Item %d -> catalog %d%s (+%d others linked)",
                item_id, cp.id, f" / variant {cv.id}" if cv else "", linked)
    return cp, cv
