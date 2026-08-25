"""Server-side username generation.

Usernames are public: the shared-trip endpoint returns the owner's username to
anyone with the link. They must therefore not be derived from the email
address, which is what the social-auth paths used to do -- turning
`someone@example.com` into the public handle `someone`, without ever showing
the user that it had happened.

The `username` column is String(15), unique. That cap is the whole reason this
is a hand-rolled wordlist rather than a library: coolname, petname and the
haikunator ports all emit names that routinely exceed 15 characters, so using
one would mean generating and rejecting until something fit. The hard part
here is the uniqueness retry against the database, which none of them provide
anyway.

Budget, worst case: 5 (adjective) + 1 (hyphen) + 6 (noun) + 3 (digits) = 15.
"""

import logging
import secrets

logger = logging.getLogger(__name__)

USERNAME_MAX_LENGTH = 15

# Outdoor-flavoured and deliberately neutral -- no word here should read as a
# judgement about the person carrying it. Kept to <=5 characters.
ADJECTIVES = (
    "swift", "brisk", "calm", "lone", "wild", "bold", "keen", "warm",
    "cool", "dusky", "misty", "sunny", "rocky", "hazy", "still", "quiet",
    "brave", "clear", "crisp", "early", "far", "free", "glad", "high",
    "idle", "light", "mild", "north", "open", "pale", "quick", "rapid",
    "salty", "sharp", "deep", "small", "snowy", "soft", "south", "spry",
    "stark", "steep", "sure", "tall", "true", "vast", "west", "windy",
    "amber", "azure", "birch", "coral", "green", "ivory", "olive", "rust",
    "sage", "slate", "teal", "umber", "lush", "ash", "dawn", "dusk",
)

# <=6 characters.
NOUNS = (
    "cedar", "pine", "birch", "aspen", "maple", "alder", "willow", "spruce",
    "ridge", "peak", "summit", "basin", "canyon", "mesa", "butte", "bluff",
    "creek", "river", "brook", "rapids", "lake", "tarn", "fjord", "delta",
    "trail", "path", "route", "pass", "saddle", "col", "spur", "bench",
    "meadow", "glade", "grove", "heath", "moor", "fen", "marsh", "tundra",
    "falcon", "heron", "osprey", "raven", "finch", "swift", "crane", "wren",
    "marten", "otter", "lynx", "sable", "stoat", "vole", "hare", "elk",
    "ember", "flint", "gneiss", "granit", "quartz", "shale", "basalt", "onyx",
)

# 3 digits keeps every name the same visual shape and multiplies the space by
# 900. Combined: 64 * 64 * 900, a little over 3.6 million.
_SUFFIX_MIN = 100
_SUFFIX_MAX = 999

# Enough that exhausting them means something is wrong (a saturated space, or
# a database that is refusing writes) rather than ordinary bad luck. At the
# sizes above, five independent draws colliding is not a case worth tuning for.
MAX_ATTEMPTS = 5


def _candidate() -> str:
    adjective = secrets.choice(ADJECTIVES)
    noun = secrets.choice(NOUNS)
    suffix = secrets.randbelow(_SUFFIX_MAX - _SUFFIX_MIN + 1) + _SUFFIX_MIN
    return f"{adjective}-{noun}{suffix}"


def generate_username(is_taken) -> str:
    """Return an unused username.

    `is_taken` is a callable taking a candidate and returning whether it
    already exists, so this stays free of any database import and can be
    exercised directly in tests.

    Never raises and never returns something over the column width. If every
    attempt collides it falls back to a random hex handle -- ugly, but a user
    who can sign in and rename themselves is a far better outcome than a
    registration that fails on a name.
    """
    for _ in range(MAX_ATTEMPTS):
        candidate = _candidate()
        try:
            if not is_taken(candidate):
                return candidate
        except Exception:
            # A lookup failure must not cost someone their registration. Fall
            # through to the random handle, which is unique with overwhelming
            # probability and will be caught by the unique index if not.
            logger.exception("Username availability check failed")
            break

    fallback = f"hiker-{secrets.token_hex(4)}"
    logger.warning(
        "Falling back to a random username handle after %s attempts",
        MAX_ATTEMPTS)
    return fallback[:USERNAME_MAX_LENGTH]
