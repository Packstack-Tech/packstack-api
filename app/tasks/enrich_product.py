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

from models.base import Brand, Product, ProductVariant, Item, CatalogProduct
from celery_app import celery_app
from utils.consts import DATABASE_URL

logger = logging.getLogger(__name__)

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(DATABASE_URL)
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

DEFAULT_MODEL = "claude-sonnet-4-6"
_client = None


def _get_ai_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def ai_complete(system: str, user: str, tools: list | None = None, max_retries: int = 3):
    client = _get_ai_client()
    kwargs = dict(
        model=DEFAULT_MODEL,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": user}],
        thinking={"type": "adaptive"},
    )
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
    "Pack", "Shelter", "Toiletries", "Water System", "Food", "Footware",
    "Tools", "First Aid", "Safety", "Camera", "Climbing",
]

SYSTEM_PROMPT = (
    "You are a backpacking and outdoor gear product database. You have expert knowledge "
    "of outdoor gear brands, product lines, and specifications. When given a brand and product name "
    "(which may be misspelled, abbreviated, or include variant info in the name), you research and "
    "return the canonical product information.\n\n"
    "You have access to web search. Use it to find the manufacturer's official product page. "
    'Prefer searching the manufacturer\'s site directly (e.g. "site:nemoequipment.com Tensor Insulated"). '
    "From the product page, extract:\n"
    "- The official product URL\n"
    "- A product image URL (the main product photo, not a lifestyle/hero image)\n"
    "- The manufacturer's listed weight and any other specs (R-value, volume, packed size, temperature rating, etc.)\n\n"
    "IMPORTANT: A variant is a meaningful product option like size (S/M/L/Regular/Long), color, gender, "
    "or volume capacity. Weight measurements (e.g. \"690g\", \"14oz\", \"2lb\"), dimensions, or other specs "
    "that appear in the variant field are NOT real variants. If the provided variant is just a weight or "
    "spec measurement, set variant_name to null and incorporate that data into weight_grams or "
    "additional_specs instead.\n\n"
    "If the input is not a real, identifiable outdoor/backpacking product (e.g. \"small bag\", \"misc item\", "
    "random text), mark it as invalid.\n\n"
    f"When assigning a category, you MUST use one of these exact values: {', '.join(CATEGORIES)}."
)

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 3,
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
                "description": "Official manufacturer product page URL. Null if unknown.",
            },
            "image_url": {
                "type": ["string", "null"],
                "description": "URL of the main product photo from the manufacturer's site. Null if not found.",
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
# Helper functions (ported from workshop/catalog_enrich/run.py)
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


def _catalog_exists(session, brand_name: str, product_name: str, variant_name: str | None) -> bool:
    q = session.query(CatalogProduct.id).filter(
        func.lower(CatalogProduct.brand_name) == brand_name.lower(),
        func.lower(CatalogProduct.product_name) == product_name.lower(),
    )
    if variant_name:
        q = q.filter(func.lower(CatalogProduct.variant_name) == variant_name.lower())
    else:
        q = q.filter(CatalogProduct.variant_name.is_(None))
    return q.first() is not None


def _catalog_url_exists(session, product_url: str) -> bool:
    if not product_url:
        return False
    return session.query(CatalogProduct.id).filter(
        CatalogProduct.product_url == product_url,
        CatalogProduct.status != "rejected",
    ).first() is not None


_SPEC_PATTERN = re.compile(
    r"^\(?[\d.]+\s*(g|kg|oz|lb|lbs|mm|cm|in|ml|l)\)?$", re.IGNORECASE
)


def _sanitize_variant(result: dict) -> dict:
    variant = result.get("variant_name")
    if variant and _SPEC_PATTERN.match(variant.strip()):
        logger.info("Sanitized spec-like variant '%s' -> null", variant)
        result["variant_name"] = None
    return result


_URL_CHECK_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PackstackBot/1.0)"}
LOW_CONFIDENCE_THRESHOLD = 0.6
URL_CONFIDENCE_BOOST = 0.15


def _check_url(url: str) -> int | None:
    try:
        resp = requests.head(url, headers=_URL_CHECK_HEADERS, timeout=5, allow_redirects=True)
        return resp.status_code
    except requests.RequestException:
        return None


def _call_ai(brand_name: str, product_name: str, variant_name: str | None) -> dict | None:
    user_prompt = _build_user_prompt(brand_name, product_name, variant_name)
    response = ai_complete(
        system=SYSTEM_PROMPT,
        user=user_prompt,
        tools=[WEB_SEARCH_TOOL, TOOL_SCHEMA],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "catalog_entry":
            return block.input
    return None


def _insert_rejected(session, *, brand_name, product_name, variant_name, display_name,
                     brand_id, product_id, product_variant_id, item_count, confidence):
    entry = CatalogProduct(
        brand_name=brand_name,
        product_name=product_name,
        variant_name=variant_name,
        display_name=display_name,
        brand_id=brand_id,
        product_id=product_id,
        product_variant_id=product_variant_id,
        status="rejected",
        source_item_count=item_count,
        ai_confidence=confidence,
    )
    session.add(entry)
    session.commit()


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def enrich_product(self, brand_id: int, product_id: int, product_variant_id: int | None = None):
    with get_session() as session:
        brand = session.query(Brand).get(brand_id)
        product = session.query(Product).get(product_id)
        if not brand or not product:
            logger.warning("Brand %s or Product %s not found, skipping", brand_id, product_id)
            return

        variant = None
        if product_variant_id:
            variant = session.query(ProductVariant).get(product_variant_id)

        variant_name = variant.name if variant else None
        label = f"{brand.name} / {product.name}"
        if variant_name:
            label += f" / {variant_name}"

        if _catalog_exists(session, brand.name, product.name, variant_name):
            logger.info("SKIP (exists): %s", label)
            return

        logger.info("Enriching: %s", label)

        try:
            result = _call_ai(brand.name, product.name, variant_name)
        except Exception as exc:
            logger.exception("AI call failed for %s", label)
            raise self.retry(exc=exc)

        if result is None:
            logger.warning("No structured response for: %s", label)
            return

        item_count = _get_item_count(session, brand.id, product.id)

        if not result.get("is_valid_product"):
            logger.info("INVALID product: %s", label)
            _insert_rejected(
                session,
                brand_name=brand.name, product_name=product.name,
                variant_name=variant_name, display_name=label,
                brand_id=brand.id, product_id=product.id,
                product_variant_id=variant.id if variant else None,
                item_count=item_count, confidence=0.0,
            )
            return

        result = _sanitize_variant(result)

        product_url = result.get("product_url")
        if _catalog_url_exists(session, product_url):
            logger.info("SKIP (URL exists): %s -> %s", label, product_url)
            return

        canonical_brand = result.get("brand_name", brand.name)
        canonical_product = result.get("product_name", product.name)
        canonical_variant = result.get("variant_name", variant_name)

        if _catalog_exists(session, canonical_brand, canonical_product, canonical_variant):
            logger.info("SKIP (canonical match exists): %s", label)
            return

        median_weight = _get_median_weight(session, brand.id, product.id)
        confidence = compute_confidence(
            ai_result=result,
            original_product_name=product.name,
            original_brand_name=brand.name,
            median_weight=median_weight,
            item_count=item_count,
        )

        canonical_display = f"{canonical_brand} {canonical_product}"

        if confidence < LOW_CONFIDENCE_THRESHOLD:
            if product_url:
                status_code = _check_url(product_url)
                if status_code == 200:
                    confidence += URL_CONFIDENCE_BOOST
                    logger.info("URL verified (200), confidence boosted to %.3f", confidence)
                elif status_code in (404, 410):
                    logger.info("REJECTED: low confidence (%.3f) and URL returned %d", confidence, status_code)
                    _insert_rejected(
                        session,
                        brand_name=canonical_brand, product_name=canonical_product,
                        variant_name=canonical_variant, display_name=canonical_display,
                        brand_id=brand.id, product_id=product.id,
                        product_variant_id=variant.id if variant else None,
                        item_count=item_count, confidence=confidence,
                    )
                    return
                else:
                    logger.info("URL check inconclusive (status=%s), proceeding", status_code)
            else:
                logger.info("REJECTED: low confidence (%.3f) and no product URL", confidence)
                _insert_rejected(
                    session,
                    brand_name=canonical_brand, product_name=canonical_product,
                    variant_name=canonical_variant, display_name=canonical_display,
                    brand_id=brand.id, product_id=product.id,
                    product_variant_id=variant.id if variant else None,
                    item_count=item_count, confidence=confidence,
                )
                return

        display_parts = [canonical_brand, canonical_product]
        if canonical_variant:
            display_parts.append(canonical_variant)
        display_name = " ".join(display_parts)

        weight_grams = result.get("weight_grams")

        entry = CatalogProduct(
            brand_name=canonical_brand,
            product_name=canonical_product,
            variant_name=canonical_variant,
            display_name=display_name,
            weight=weight_grams,
            weight_unit="g" if weight_grams else None,
            product_url=result.get("product_url"),
            image_url=result.get("image_url"),
            description=result.get("description"),
            category_suggestion=result.get("category"),
            additional_specs=result.get("additional_specs"),
            brand_id=brand.id,
            product_id=product.id,
            product_variant_id=variant.id if variant else None,
            status="approved",
            source_item_count=item_count,
            ai_confidence=confidence,
        )
        session.add(entry)
        session.commit()
        logger.info("Inserted %s (confidence=%.3f)", display_name, confidence)
