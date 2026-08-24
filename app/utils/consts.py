import logging
import os
import sys

DEVELOPMENT = os.getenv('DEVELOPMENT', 0)

POSTGRES_USER = os.getenv('POSTGRES_USER')
POSTGRES_PASSWORD = os.getenv('POSTGRES_PASSWORD')
POSTGRES_HOST = os.getenv('POSTGRES_HOST')
POSTGRES_DB = os.getenv('POSTGRES_DB')
POSTGRES_PORT = os.getenv('POSTGRES_PORT', '5432')
DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
WORKER_DATABASE_URL = os.getenv('WORKER_DATABASE_URL') or DATABASE_URL

APP_HOST = os.getenv('APP_HOST')
JWT_SECRET = os.getenv('JWT_SECRET')
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM')
RESEND_API_KEY = os.getenv('RESEND_API_KEY')

GOOGLE_CLIENT_IDS = [
    cid.strip() for cid in os.getenv('GOOGLE_CLIENT_IDS', '').split(',') if cid.strip()
]

APPLE_CLIENT_IDS = [
    cid.strip() for cid in os.getenv('APPLE_CLIENT_IDS', '').split(',') if cid.strip()
]

APPLE_KEY_ID = os.getenv('APPLE_KEY_ID')
APPLE_TEAM_ID = os.getenv('APPLE_TEAM_ID')
_raw_apple_key = os.getenv('APPLE_PRIVATE_KEY')
APPLE_PRIVATE_KEY = _raw_apple_key.replace("\\n", "\n") if _raw_apple_key else None

GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')

REVIEW_EMAIL = os.getenv('REVIEW_EMAIL')
REVIEW_OTP = os.getenv('REVIEW_OTP')

REVENUECAT_WEBHOOK_SECRET = os.getenv('REVENUECAT_WEBHOOK_SECRET')


# --- Free-tier limits -------------------------------------------------------
#
# A limit that is unset, blank, or negative means "no limit". That default is
# deliberate and load-bearing: these gates make endpoints return 402 where they
# previously always succeeded, and any client older than the release that
# handles 402 has no error path for it. So a gate must be switched on
# deliberately -- once the client that understands it has rolled out -- and
# never merely by deploying the backend.

FREE_TIER_UNLIMITED = sys.maxsize


def _free_limit(name: str) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return FREE_TIER_UNLIMITED
    try:
        value = int(raw)
    except ValueError:
        # Fall open rather than guess. Logged because a typo here is otherwise
        # invisible: the gate simply never engages.
        logging.getLogger(__name__).warning(
            "%s=%r is not an integer; treating the limit as unlimited", name, raw)
        return FREE_TIER_UNLIMITED

    if value <= 0:
        # Zero is rejected rather than honoured. It is one keystroke from the
        # intended 1, and it would mean "no free user may create ANY of these"
        # -- which on the pack gate blocks the first pack of every new trip,
        # i.e. a total lockout of the free tier. No legitimate deployment wants
        # that, so treat it like any other bad value: fall open and complain.
        if value == 0:
            logging.getLogger(__name__).warning(
                "%s=0 would lock out the free tier entirely; "
                "treating the limit as unlimited", name)
        return FREE_TIER_UNLIMITED

    return value


# Packs a non-subscribed user may have within a single trip. Not the "3 packs"
# limit users see on the Packs tab -- that one counts Trips (FREE_TRIP_LIMIT).
FREE_PACKS_PER_TRIP = _free_limit('FREE_PACKS_PER_TRIP')

# Hiker profiles a non-subscribed user may have.
FREE_HIKER_PROFILE_LIMIT = _free_limit('FREE_HIKER_PROFILE_LIMIT')
