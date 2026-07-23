import logging
import re

from models.base import Trip, TrailEnrichment
from celery_app import celery_app
from tasks.enrich_product import get_session, ai_complete

logger = logging.getLogger(__name__)

# Haiku 4.5 with web-search grounding: structured spec extraction doesn't
# need Sonnet, and most enrichments never reach the AI at all (cache).
TRIP_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Prompt & tool schema
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an expert hiking and backpacking trail researcher. You have deep knowledge of "
    "trails, geography, elevation profiles, terrain types, and seasonal weather patterns worldwide.\n\n"
    "When given a trail or location name (which may be misspelled or abbreviated), research it "
    "and return accurate metadata.\n\n"
    "You have access to web search. Use it to find authoritative trail information from sources "
    "like AllTrails, the National Park Service, Forest Service, or official trail organizations.\n\n"
    "IMPORTANT RULES:\n"
    "- Correct any misspellings in the trail/location name.\n"
    "- For temperature estimates, use the trip month if provided to give season-appropriate values.\n"
    "- If no trip month is provided, skip temperature fields.\n"
    "- Distance and elevation should use the unit system specified.\n\n"
    "Terrain types (pick the most representative):\n"
    "- paved: Paved roads or paths\n"
    "- gravel: Gravel, dirt, or well-maintained trail\n"
    "- rugged: Rocky, loose rock, or rough terrain\n"
    "- sand: Sandy terrain (beach, desert)\n"
    "- swamp: Boggy, marshy, or wetland terrain\n\n"
    "Pace (typical for the trail):\n"
    "- easy: Flat, well-groomed, casual pace (~2 mph)\n"
    "- moderate: Some elevation, average trail (~3 mph)\n"
    "- fast: Well-maintained, experienced hiker pace (~4 mph)\n\n"
    "Temperature category:\n"
    "- cold: Below 32°F / 0°C\n"
    "- moderate: 32-85°F / 0-29°C\n"
    "- hot: Above 85°F / 29°C\n\n"
    "Trail systems:\n"
    "If the trail is a section of (or the entirety of) a major named trail system, return its "
    "standard abbreviation. Recognized systems include: PCT, AT, CDT, GR20, SWCP, TMB, TRT, "
    "JMT, W-Trek, Kungsleden, Te Araroa, Camino, GR10, GR11, Lycian Way, Bibbulmun, "
    "Overland Track, and similar well-known long-distance routes.\n"
    "Only set trail_system if you are confident the location is part of that system. "
    "If uncertain, return null."
)

TEMPS_ONLY_SYSTEM_PROMPT = (
    "You are an expert on seasonal weather patterns for hiking trails worldwide. "
    "Given a trail/location and a calendar month, estimate typical temperatures.\n\n"
    "Temperature category:\n"
    "- cold: Below 32°F / 0°C\n"
    "- moderate: 32-85°F / 0-29°C\n"
    "- hot: Above 85°F / 29°C\n\n"
    "Report temperatures in Celsius."
)

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 2,
}

_TEMP_PROPERTIES = {
    "temp_min": {
        "type": ["integer", "null"],
        "description": "Expected low temperature in Celsius. Null if unknown.",
    },
    "temp_max": {
        "type": ["integer", "null"],
        "description": "Expected high temperature in Celsius. Null if unknown.",
    },
    "temp_category": {
        "type": ["string", "null"],
        "enum": ["cold", "moderate", "hot", None],
        "description": "Overall temperature category. Null if unknown.",
    },
}

TOOL_SCHEMA = {
    "name": "trail_metadata",
    "description": "Structured trail/trip metadata for a backpacking trip.",
    "input_schema": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "Corrected/canonical trail or location name.",
            },
            "distance": {
                "type": ["number", "null"],
                "description": "Total trail distance in kilometers. Null if unknown.",
            },
            "daily_elevation_gain": {
                "type": ["number", "null"],
                "description": "Typical daily elevation gain in meters. Null if unknown.",
            },
            "terrain": {
                "type": ["string", "null"],
                "enum": ["paved", "gravel", "rugged", "sand", "swamp", None],
                "description": "Primary terrain type.",
            },
            "pace": {
                "type": ["string", "null"],
                "enum": ["easy", "moderate", "fast", None],
                "description": "Recommended pace for this trail.",
            },
            "trail_system": {
                "type": ["string", "null"],
                "description": "Abbreviation of the major trail system this trail belongs to (e.g. PCT, AT, CDT, GR20). Null if not part of a recognized system or uncertain.",
            },
            **_TEMP_PROPERTIES,
        },
        "required": ["location"],
    },
}

TEMPS_TOOL_SCHEMA = {
    "name": "trail_temps",
    "description": "Seasonal temperature estimate for a trail in a given month.",
    "input_schema": {
        "type": "object",
        "properties": _TEMP_PROPERTIES,
        "required": ["temp_min", "temp_max", "temp_category"],
    },
}

SPEC_FIELDS = ["distance", "daily_elevation_gain", "terrain", "pace", "trail_system"]
TEMP_FIELDS = ["temp_min", "temp_max", "temp_category"]

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
]


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def normalize_location(location: str) -> str:
    """Normalize a user-typed location into a cache key."""
    key = location.lower().strip()
    key = re.sub(r"[^a-z0-9]+", "-", key)
    return key.strip("-")[:250]


def _lookup_cache(session, location: str) -> TrailEnrichment | None:
    return (
        session.query(TrailEnrichment)
        .filter(TrailEnrichment.location_key == normalize_location(location))
        .first()
    )


def _store_cache(session, location_keys: list[str], canonical_location: str,
                 specs: dict, month: int | None, temps: dict | None) -> None:
    monthly = {str(month): temps} if (month and temps) else {}
    for key in dict.fromkeys(location_keys):  # dedupe, preserve order
        if not key:
            continue
        existing = session.query(TrailEnrichment).filter(
            TrailEnrichment.location_key == key
        ).first()
        if existing:
            if month and temps:
                existing.monthly_temps = {**(existing.monthly_temps or {}), str(month): temps}
            continue
        session.add(TrailEnrichment(
            location_key=key,
            canonical_location=canonical_location,
            distance=specs.get("distance"),
            daily_elevation_gain=specs.get("daily_elevation_gain"),
            terrain=specs.get("terrain"),
            pace=specs.get("pace"),
            trail_system=specs.get("trail_system"),
            monthly_temps=monthly,
            source="ai",
        ))
    session.commit()


def _apply_specs(trip: Trip, values: dict) -> None:
    """Fill in missing spec fields only — never override user-provided data."""
    for field in SPEC_FIELDS:
        value = values.get(field)
        if getattr(trip, field, None) is None and value is not None:
            setattr(trip, field, value)


def _apply_temps(trip: Trip, temps: dict) -> None:
    for field in TEMP_FIELDS:
        value = temps.get(field)
        if getattr(trip, field, None) is None and value is not None:
            setattr(trip, field, value)


def _extract_tool_result(response, tool_name: str) -> dict | None:
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    return None


# ---------------------------------------------------------------------------
# AI calls
# ---------------------------------------------------------------------------

def _full_research(location: str, month: int | None) -> dict | None:
    lines = [
        f"Trail/Location: {location}",
        "Unit system: distance in kilometers, elevation in meters, temperature in Celsius",
    ]
    if month:
        lines.append(f"Trip month: {MONTH_NAMES[month - 1]}")
    else:
        lines.append("Trip month: NOT PROVIDED (skip temperature fields)")

    response = ai_complete(
        system=SYSTEM_PROMPT,
        user="\n".join(lines),
        tools=[WEB_SEARCH_TOOL, TOOL_SCHEMA],
        model=TRIP_MODEL,
    )
    return _extract_tool_result(response, "trail_metadata")


def _temps_only(location: str, month: int) -> dict | None:
    """Cheap no-search call to fill a month gap on a cached trail."""
    response = ai_complete(
        system=TEMPS_ONLY_SYSTEM_PROMPT,
        user=f"Trail/Location: {location}\nMonth: {MONTH_NAMES[month - 1]}",
        tools=[TEMPS_TOOL_SCHEMA],
        model=TRIP_MODEL,
    )
    return _extract_tool_result(response, "trail_temps")


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------

@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def enrich_trip(self, trip_id: int):
    with get_session() as session:
        trip = session.query(Trip).get(trip_id)
        if not trip:
            logger.warning("Trip %s not found, skipping enrichment", trip_id)
            return

        if not trip.location:
            logger.info("Trip %s has no location, skipping enrichment", trip_id)
            trip.enrich_status = "completed"
            return

        trip.enrich_status = "processing"
        session.commit()

        original_location = trip.location
        month = trip.start_date.month if trip.start_date else None
        needs_temps = month is not None and any(
            getattr(trip, f, None) is None for f in TEMP_FIELDS
        )

        logger.info("Enriching trip %s: %s", trip_id, original_location)

        try:
            cached = _lookup_cache(session, original_location)

            if cached:
                logger.info("Cache hit for '%s' -> %s", original_location, cached.canonical_location)
                if cached.canonical_location:
                    trip.location = cached.canonical_location
                _apply_specs(trip, {
                    "distance": float(cached.distance) if cached.distance is not None else None,
                    "daily_elevation_gain": (
                        float(cached.daily_elevation_gain)
                        if cached.daily_elevation_gain is not None else None
                    ),
                    "terrain": cached.terrain,
                    "pace": cached.pace,
                    "trail_system": cached.trail_system,
                })

                if needs_temps:
                    temps = (cached.monthly_temps or {}).get(str(month))
                    if temps is None:
                        temps = _temps_only(cached.canonical_location, month)
                        if temps:
                            cached.monthly_temps = {
                                **(cached.monthly_temps or {}),
                                str(month): temps,
                            }
                    if temps:
                        _apply_temps(trip, temps)

                trip.enrich_status = "completed"
                logger.info("Enrichment completed (cached) for trip %s", trip_id)
                return

            # Cache miss: full research pass
            result = _full_research(original_location, month if needs_temps else None)
            if result is None:
                logger.warning("No structured response for trip %s", trip_id)
                trip.enrich_status = "failed"
                return

            canonical = result.get("location") or original_location
            trip.location = canonical
            _apply_specs(trip, result)

            temps = None
            if needs_temps:
                temps = {f: result.get(f) for f in TEMP_FIELDS}
                if any(v is not None for v in temps.values()):
                    _apply_temps(trip, temps)
                else:
                    temps = None

            _store_cache(
                session,
                location_keys=[
                    normalize_location(original_location),
                    normalize_location(canonical),
                ],
                canonical_location=canonical,
                specs=result,
                month=month,
                temps=temps,
            )

            trip.enrich_status = "completed"
            logger.info("Enrichment completed for trip %s", trip_id)

        except Exception as exc:
            logger.exception("Enrichment failed for trip %s", trip_id)
            trip.enrich_status = "failed"
            session.commit()
            raise self.retry(exc=exc)
