import datetime
from types import SimpleNamespace

from models.base import TRIP_LIMIT_LIFETIME_START, trip_counts_toward_limit

DAY = datetime.timedelta(days=1)


def trip(removed, created_at):
    return SimpleNamespace(removed=removed, created_at=created_at)


def test_active_trips_always_count():
    assert trip_counts_toward_limit(trip(False, TRIP_LIMIT_LIFETIME_START - 365 * DAY))
    assert trip_counts_toward_limit(trip(False, TRIP_LIMIT_LIFETIME_START + DAY))


def test_trips_deleted_before_cutoff_are_grandfathered():
    assert not trip_counts_toward_limit(trip(True, TRIP_LIMIT_LIFETIME_START - DAY))


def test_trips_created_after_cutoff_count_even_when_deleted():
    assert trip_counts_toward_limit(trip(True, TRIP_LIMIT_LIFETIME_START))
    assert trip_counts_toward_limit(trip(True, TRIP_LIMIT_LIFETIME_START + DAY))
