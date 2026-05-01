import datetime

from models.base import CategoryBenchmark

DEFAULT_BENCHMARKS = {
    "Shelter":       {"lifespan_years": 5,  "expected_nights": 300},
    "Pack":          {"lifespan_years": 4,  "expected_nights": 250},
    "Sleep System":  {"lifespan_years": 6,  "expected_nights": 400},
    "Footware":      {"lifespan_years": 1,  "expected_nights": 60,  "expected_distance": 500, "distance_unit": "mi"},
    "Clothing":      {"lifespan_years": 3,  "expected_nights": 200},
    "Cookware":      {"lifespan_years": 10, "expected_nights": 800},
    "Electronics":   {"lifespan_years": 5,  "expected_nights": 500},
    "Water System":  {"lifespan_years": 5,  "expected_nights": 400},
    "Tools":         {"lifespan_years": 8,  "expected_nights": 600},
    "First Aid":     {"lifespan_years": 2,  "expected_nights": 100},
    "Safety":        {"lifespan_years": 5,  "expected_nights": 300},
    "Camera":        {"lifespan_years": 5,  "expected_nights": 300},
    "Climbing":      {"lifespan_years": 3,  "expected_nights": 150},
    "Toiletries":    {"lifespan_years": 1,  "expected_nights": 50},
    "Miscellaneous": {"lifespan_years": 5,  "expected_nights": 300},
}

_BENCHMARK_FIELDS = ("lifespan_years", "expected_nights", "expected_distance", "distance_unit")

CONDITION_SCORES = {
    "new": 0.0,
    "good": 0.25,
    "fair": 0.5,
    "worn": 0.8,
    "retired": 1.0,
}


def get_benchmark(session, user_id: int, category_name: str) -> dict:
    defaults = DEFAULT_BENCHMARKS.get(category_name, DEFAULT_BENCHMARKS["Miscellaneous"])
    override = session.query(CategoryBenchmark).filter_by(
        user_id=user_id, category_name=category_name
    ).first()
    if not override:
        return dict(defaults)
    merged = dict(defaults)
    for field in _BENCHMARK_FIELDS:
        val = getattr(override, field, None)
        if val is not None:
            merged[field] = float(val) if field != "distance_unit" else val
    return merged


def get_all_benchmarks(session, user_id: int) -> dict[str, dict]:
    overrides = session.query(CategoryBenchmark).filter_by(user_id=user_id).all()
    override_map = {o.category_name: o for o in overrides}

    result = {}
    for cat_name, defaults in DEFAULT_BENCHMARKS.items():
        merged = dict(defaults)
        override = override_map.get(cat_name)
        if override:
            for field in _BENCHMARK_FIELDS:
                val = getattr(override, field, None)
                if val is not None:
                    merged[field] = float(val) if field != "distance_unit" else val
            merged["has_override"] = True
        else:
            merged["has_override"] = False
        result[cat_name] = merged
    return result


def replacement_score(acquired_date, condition: str | None, benchmark: dict) -> float | None:
    """Return 0.0 (new) to 1.0 (replace now), or None if insufficient data."""
    factors = []

    if acquired_date:
        age_years = (datetime.date.today() - acquired_date).days / 365.25
        lifespan = benchmark.get("lifespan_years")
        if lifespan and lifespan > 0:
            factors.append(min(age_years / float(lifespan), 1.0))

    if condition:
        score = CONDITION_SCORES.get(condition)
        if score is not None:
            factors.append(score)

    if not factors:
        return None
    return round(max(factors), 3)
