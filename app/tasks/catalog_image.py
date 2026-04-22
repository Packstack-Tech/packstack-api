import base64
import logging
import os
import time

import anthropic
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from contextlib import contextmanager

from models.base import CatalogProduct
from celery_app import celery_app
from utils.consts import WORKER_DATABASE_URL

logger = logging.getLogger(__name__)

VISION_MODEL = "claude-haiku-4-5-20251001"
SERPER_IMAGES_URL = "https://google.serper.dev/images"
MAX_CANDIDATES = 5

_engine = None
_ai_client = None

_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

_ALLOWED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

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
def _get_session():
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

def _get_ai_client() -> anthropic.Anthropic:
    global _ai_client
    if _ai_client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _ai_client = anthropic.Anthropic(api_key=api_key)
    return _ai_client


# ---------------------------------------------------------------------------
# Serper image search
# ---------------------------------------------------------------------------

def _search_images(query: str, num: int = 10) -> list[dict]:
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        raise RuntimeError("SERPER_API_KEY is not set")

    response = requests.post(
        SERPER_IMAGES_URL,
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        },
        json={"q": query, "num": num},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("images", [])


# ---------------------------------------------------------------------------
# Image download
# ---------------------------------------------------------------------------

def _download_images(urls: list[str]) -> list[tuple[str, bytes, str]]:
    results = []
    for url in urls:
        try:
            resp = requests.get(
                url, headers=_DOWNLOAD_HEADERS, timeout=10, allow_redirects=True
            )
            if resp.status_code != 200:
                continue

            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
            if content_type not in _ALLOWED_MEDIA_TYPES:
                if url.lower().endswith((".jpg", ".jpeg")):
                    content_type = "image/jpeg"
                elif url.lower().endswith(".png"):
                    content_type = "image/png"
                elif url.lower().endswith(".webp"):
                    content_type = "image/webp"
                elif url.lower().endswith(".gif"):
                    content_type = "image/gif"
                else:
                    continue

            if len(resp.content) < 1000:
                continue

            results.append((url, resp.content, content_type))
        except requests.RequestException:
            continue
    return results


# ---------------------------------------------------------------------------
# Vision selection
# ---------------------------------------------------------------------------

_VISION_SYSTEM = (
    "You are a product image evaluator for an outdoor gear catalog. "
    "You will be shown candidate images and a product description. "
    "Your job is to select the single best image that accurately depicts the product.\n\n"
    "Prefer images that:\n"
    "- Show the actual product clearly (not a person using it in the field)\n"
    "- Have a clean, white, or neutral background\n"
    "- Show the complete product, not a close-up of a detail\n"
    "- Match the specific product described (correct brand, model, color if known)\n\n"
    "If NONE of the images are a good match for the product, respond with 0."
)


def _select_best_image(
    product_name: str,
    candidates: list[tuple[str, bytes, str]],
    specs: dict | None = None,
) -> str | None:
    if not candidates:
        return None

    client = _get_ai_client()

    content = []
    description = f"Product: {product_name}"
    if specs:
        spec_parts = []
        if specs.get("category_suggestion"):
            spec_parts.append(f"Category: {specs['category_suggestion']}")
        if specs.get("description"):
            spec_parts.append(f"Description: {specs['description']}")
        if specs.get("weight") and specs.get("weight_unit"):
            spec_parts.append(f"Weight: {specs['weight']}{specs['weight_unit']}")
        if spec_parts:
            description += "\n" + "\n".join(spec_parts)

    content.append({"type": "text", "text": description + "\n\nCandidate images:"})

    for i, (url, raw_bytes, media_type) in enumerate(candidates, 1):
        content.append({"type": "text", "text": f"\nImage {i}:"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(raw_bytes).decode(),
            },
        })

    content.append({
        "type": "text",
        "text": (
            f"\n\nWhich image number (1-{len(candidates)}) best depicts the product "
            "described above? Reply with ONLY the number. If none are a good match, reply with 0."
        ),
    })

    for attempt in range(3):
        try:
            response = client.messages.create(
                model=VISION_MODEL,
                max_tokens=32,
                system=_VISION_SYSTEM,
                messages=[{"role": "user", "content": content}],
            )
            break
        except anthropic.RateLimitError:
            wait = 2 ** attempt
            logger.warning("Rate limited, retrying in %ds", wait)
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < 2:
                time.sleep(2 ** attempt)
            else:
                raise
    else:
        raise RuntimeError("Vision API failed after retries")

    reply = response.content[0].text.strip()

    try:
        choice = int(reply)
    except ValueError:
        logger.warning("Vision model returned non-numeric response: %s", reply)
        return None

    if choice == 0:
        return None

    if 1 <= choice <= len(candidates):
        return candidates[choice - 1][0]

    logger.warning("Vision model returned out-of-range choice: %d", choice)
    return None


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def find_product_image(self, catalog_product_id: int):
    with _get_session() as session:
        entry = session.query(CatalogProduct).get(catalog_product_id)
        if not entry:
            logger.warning("CatalogProduct %d not found", catalog_product_id)
            return

        if entry.image_url:
            logger.info("CatalogProduct %d already has image_url, skipping", catalog_product_id)
            return

        label = entry.display_name or f"id={catalog_product_id}"
        logger.info("Finding image for: %s", label)

        query = f"{entry.display_name} product on white background"

        try:
            results = _search_images(query, num=MAX_CANDIDATES * 2)
        except Exception as exc:
            logger.exception("Serper search failed for %s", label)
            raise self.retry(exc=exc)

        if not results:
            logger.info("No search results for %s", label)
            return

        image_urls = [r["imageUrl"] for r in results[:MAX_CANDIDATES * 2] if r.get("imageUrl")]
        candidates = _download_images(image_urls)
        candidates = candidates[:MAX_CANDIDATES]

        if not candidates:
            logger.info("No downloadable images for %s", label)
            return

        logger.info("Downloaded %d candidates for %s", len(candidates), label)

        specs = {
            "category_suggestion": entry.category_suggestion,
            "description": entry.description,
            "weight": str(entry.weight) if entry.weight else None,
            "weight_unit": entry.weight_unit,
        }

        try:
            image_url = _select_best_image(entry.display_name, candidates, specs)
        except Exception as exc:
            logger.exception("Vision selection failed for %s", label)
            raise self.retry(exc=exc)

        if image_url:
            entry.image_url = image_url
            session.flush()
            logger.info("Set image_url for %s: %s", label, image_url)
        else:
            logger.info("No suitable image found for %s", label)
