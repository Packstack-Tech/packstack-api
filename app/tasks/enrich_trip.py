import logging

from models.base import Trip, User
from celery_app import celery_app
from tasks.enrich_product import get_session, ai_complete

logger = logging.getLogger(__name__)

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
    "- Only fill in fields marked as MISSING. Do NOT override fields marked as PROVIDED.\n"
    "- For temperature estimates, use the dates if provided to give season-appropriate values.\n"
    "- If no dates are provided, skip temperature fields.\n"
    "- Write a concise 2-3 sentence trail description for the notes field.\n"
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

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 3,
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
                "description": "Total trail distance in the specified unit system. Null if unknown.",
            },
            "daily_elevation_gain": {
                "type": ["number", "null"],
                "description": "Typical daily elevation gain in the specified unit system. Null if unknown.",
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
            "temp_min": {
                "type": ["integer", "null"],
                "description": "Expected low temperature in the specified unit. Null if dates not provided.",
            },
            "temp_max": {
                "type": ["integer", "null"],
                "description": "Expected high temperature in the specified unit. Null if dates not provided.",
            },
            "temp_category": {
                "type": ["string", "null"],
                "enum": ["cold", "moderate", "hot", None],
                "description": "Overall temperature category for the trip. Null if dates not provided.",
            },
            "trail_system": {
                "type": ["string", "null"],
                "description": "Abbreviation of the major trail system this trail belongs to (e.g. PCT, AT, CDT, GR20). Null if not part of a recognized system or uncertain.",
            },
            "notes": {
                "type": "string",
                "description": "A concise 2-3 sentence description of the trail, its highlights, and notable conditions.",
            },
        },
        "required": ["location", "notes"],
    },
}

# Fields that map from AI result to Trip columns
ENRICHABLE_FIELDS = [
    "distance", "daily_elevation_gain", "terrain", "pace",
    "temp_min", "temp_max", "temp_category", "trail_system",
]


def _build_user_prompt(trip: Trip, unit_distance: str, unit_temperature: str) -> str:
    dist_label = "miles" if unit_distance == "MI" else "kilometers"
    elev_label = "feet" if unit_distance == "MI" else "meters"
    temp_label = "Fahrenheit" if unit_temperature == "F" else "Celsius"

    lines = [
        f"Trail/Location: {trip.location}",
        f"Unit system: distance in {dist_label}, elevation in {elev_label}, temperature in {temp_label}",
        "",
    ]

    if trip.start_date and trip.end_date:
        lines.append(f"Trip dates: {trip.start_date} to {trip.end_date}")
    elif trip.start_date:
        lines.append(f"Trip start date: {trip.start_date}")
    else:
        lines.append("Trip dates: NOT PROVIDED (skip temperature fields)")

    lines.append("")
    lines.append("Current field status:")

    field_labels = {
        "distance": f"Distance ({dist_label})",
        "daily_elevation_gain": f"Daily elevation gain ({elev_label})",
        "terrain": "Terrain type",
        "pace": "Pace",
        "temp_min": f"Temperature min ({temp_label})",
        "temp_max": f"Temperature max ({temp_label})",
        "temp_category": "Temperature category",
        "trail_system": "Trail system",
    }

    for field in ENRICHABLE_FIELDS:
        value = getattr(trip, field, None)
        label = field_labels[field]
        if value is not None:
            lines.append(f"  - {label}: PROVIDED ({value}) — do NOT change")
        else:
            lines.append(f"  - {label}: MISSING — please fill in")

    if trip.notes:
        lines.append(f"  - Notes: PROVIDED — do NOT change")
    else:
        lines.append(f"  - Notes: MISSING — please write a trail description")

    return "\n".join(lines)


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

        user = session.query(User).get(trip.user_id)
        if not user:
            logger.warning("User for trip %s not found, skipping", trip_id)
            trip.enrich_status = "failed"
            return

        trip.enrich_status = "processing"
        session.commit()

        unit_distance = user.unit_distance or "MI"
        unit_temperature = user.unit_temperature or "F"

        logger.info("Enriching trip %s: %s", trip_id, trip.location)

        try:
            user_prompt = _build_user_prompt(trip, unit_distance, unit_temperature)
            response = ai_complete(
                system=SYSTEM_PROMPT,
                user=user_prompt,
                tools=[WEB_SEARCH_TOOL, TOOL_SCHEMA],
            )

            result = None
            for block in response.content:
                if block.type == "tool_use" and block.name == "trail_metadata":
                    result = block.input
                    break

            if result is None:
                logger.warning("No structured response for trip %s", trip_id)
                trip.enrich_status = "failed"
                return

            # Always apply: location (spelling corrections) and notes
            if result.get("location"):
                trip.location = result["location"]

            if result.get("notes") and not trip.notes:
                trip.notes = result["notes"]

            # Only fill in missing fields
            for field in ENRICHABLE_FIELDS:
                ai_value = result.get(field)
                current_value = getattr(trip, field, None)
                if current_value is None and ai_value is not None:
                    setattr(trip, field, ai_value)

            trip.enrich_status = "completed"
            logger.info("Enrichment completed for trip %s", trip_id)

        except Exception as exc:
            logger.exception("Enrichment failed for trip %s", trip_id)
            trip.enrich_status = "failed"
            session.commit()
            raise self.retry(exc=exc)
