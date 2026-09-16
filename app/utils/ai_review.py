"""Render a trip and its packs as LLM-friendly markdown.

This is the payload behind GET /trip/{id}/ai-review and the public pack page's
"Copy for AI" button. The goal is a document a user can paste into any
assistant and get a shakedown back, so it carries the *whole* picture in one
place -- trip context, precomputed totals, and the gear list -- and nothing an
LLM can't use (internal ids, sort orders, removed flags).

Design notes:

- Weights are precomputed in both grams and ounces. Models are unreliable at
  summing mixed-unit, fractional-quantity lists; doing the arithmetic here
  removes a whole class of wrong answers.
- Base / worn / consumable follow the public page's rule (worn wins, then
  consumable, then base) so the totals here match what the user sees on
  packstack.io. compute_pack_summary in pack_summary.py uses a slightly
  different rule and is left alone -- it feeds the authenticated app.
- Trip fields are stored canonically in metric (km, m, degC) and rendered in
  the owner's display units, with the other unit in parentheses, so a reader
  in either system gets a number they recognize.
- Categories resolve to the shared category name (Category rows with
  user_id NULL) rather than the user's ItemCategory wrapper, and the raw
  `terrain`/`pace`/`temp_category` enums are mapped to the same labels the
  public page shows.
"""

from datetime import date
from typing import Iterable, Optional

CONVERSION_TO_GRAMS = {"g": 1.0, "kg": 1000.0, "oz": 28.3495, "lb": 453.592}
OZ_PER_GRAM = 1 / 28.3495

TERRAIN_LABELS = {
    "paved": "Paved",
    "gravel": "Gravel / Dirt",
    "rugged": "Rugged / Rocky",
    "sand": "Sand",
    "swamp": "Swamp / Marsh",
}
PACE_LABELS = {"easy": "Easy", "moderate": "Moderate", "fast": "Fast"}
TEMP_CATEGORY_LABELS = {"cold": "Cold", "moderate": "Moderate", "hot": "Hot"}


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def _label(value: Optional[str], table: dict) -> Optional[str]:
    if not value:
        return None
    return table.get(value, value)


def _fmt_num(value: float, decimals: int = 1) -> str:
    """Trim trailing zeros: 3.0 -> '3', 3.25 -> '3.25' (at 2dp)."""
    s = f"{value:.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _fmt_weight_pair(grams: float) -> str:
    """'1,234 g (43.5 oz / 2.72 lb)' -- always both systems."""
    oz = grams * OZ_PER_GRAM
    lb = oz / 16
    return f"{grams:,.0f} g ({oz:,.1f} oz / {lb:,.2f} lb)"


def _fmt_item_weight(grams: float) -> str:
    """Compact per-row weight: '85 g / 3.0 oz'."""
    return f"{grams:,.0f} g / {grams * OZ_PER_GRAM:,.1f} oz"


def _fmt_date(d: Optional[date]) -> Optional[str]:
    if not d:
        return None
    return d.strftime("%b %d, %Y").replace(" 0", " ")


def _md_cell(value) -> str:
    """Escape a value for a markdown table cell."""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _product_label(item) -> str:
    """'Brand Product Variant', mirroring ProductName.tsx on the public page."""
    brand = getattr(item, "brand", None)
    product = getattr(item, "product", None)
    variant = getattr(item, "product_variant", None)
    parts = [
        brand.name if brand and brand.name else None,
        product.name if product and product.name else None,
        variant.name if variant and variant.name else None,
    ]
    return " ".join(p for p in parts if p)


def _shared_category_name(item) -> str:
    cat = getattr(item, "category", None)
    if cat is not None and getattr(cat, "category", None) is not None and cat.category.name:
        return cat.category.name
    return "Uncategorized"


def _category_sort_key(item) -> int:
    cat = getattr(item, "category", None)
    return int(getattr(cat, "sort_order", 0) or 0) if cat is not None else 10**6


def item_weight_grams(item) -> float:
    return float(item.weight or 0) * CONVERSION_TO_GRAMS.get(item.unit, 1.0)


# ---------------------------------------------------------------------------
# Trip section
# ---------------------------------------------------------------------------

def _trip_nights(trip) -> Optional[int]:
    if trip.start_date and trip.end_date:
        return max((trip.end_date - trip.start_date).days, 0)
    return None


def _trip_rows(trip, user) -> list:
    """(label, value) pairs for the trip header, only for fields that are set."""
    imperial_distance = (getattr(user, "unit_distance", "MI") or "MI") == "MI"
    fahrenheit = (getattr(user, "unit_temperature", "F") or "F") == "F"

    rows = []
    if trip.location:
        rows.append(("Location", trip.location))

    start, end = _fmt_date(trip.start_date), _fmt_date(trip.end_date)
    nights = _trip_nights(trip)
    if start:
        dates = start if not end or end == start else f"{start} – {end}"
        if nights is not None:
            days = nights + 1
            dates += f" ({days} day{'s' if days != 1 else ''}, {nights} night{'s' if nights != 1 else ''})"
        rows.append(("Dates", dates))

    if trip.distance:
        km = float(trip.distance)
        mi = km / 1.60934
        primary = f"{_fmt_num(mi)} mi ({_fmt_num(km)} km)" if imperial_distance else f"{_fmt_num(km)} km ({_fmt_num(mi)} mi)"
        if nights is not None and nights + 1 > 0:
            per_day = (mi if imperial_distance else km) / (nights + 1)
            primary += f", about {_fmt_num(per_day)} {'mi' if imperial_distance else 'km'}/day"
        rows.append(("Distance", primary))

    if trip.daily_elevation_gain:
        m = float(trip.daily_elevation_gain)
        ft = m / 0.3048
        rows.append((
            "Daily elevation gain",
            f"{ft:,.0f} ft ({m:,.0f} m) per day" if imperial_distance else f"{m:,.0f} m ({ft:,.0f} ft) per day",
        ))

    if trip.temp_min is not None or trip.temp_max is not None:
        def both(c):
            if c is None:
                return "—"
            f = c * 9 / 5 + 32
            return f"{f:.0f}°F ({c:.0f}°C)" if fahrenheit else f"{c:.0f}°C ({f:.0f}°F)"
        rows.append(("Temperature range", f"{both(trip.temp_min)} to {both(trip.temp_max)}"))

    for label, value, table in (
        ("Conditions", trip.temp_category, TEMP_CATEGORY_LABELS),
        ("Terrain", trip.terrain, TERRAIN_LABELS),
        ("Pace", trip.pace, PACE_LABELS),
    ):
        text = _label(value, table)
        if text:
            rows.append((label, text))

    return rows


# ---------------------------------------------------------------------------
# Pack sections
# ---------------------------------------------------------------------------

def _summarize(pack_items: Iterable) -> dict:
    base = worn = consumable = total = 0.0
    calories = 0.0
    count = 0
    for pi in pack_items:
        item = pi.item
        if item is None:
            continue
        qty = float(pi.quantity or 1)
        w = item_weight_grams(item) * qty
        total += w
        if pi.worn:
            worn += w
        elif item.consumable:
            consumable += w
        else:
            base += w
        calories += float(item.calories or 0) * qty
        count += 1
    return {
        "base": base, "worn": worn, "consumable": consumable, "total": total,
        "calories": round(calories), "count": count,
    }


def _summary_lines(s: dict) -> list:
    lines = [
        f"- Base weight (packed, non-consumable): **{_fmt_weight_pair(s['base'])}**",
        f"- Worn weight: {_fmt_weight_pair(s['worn'])}",
        f"- Consumables (food, fuel, water): {_fmt_weight_pair(s['consumable'])}",
        f"- Total: {_fmt_weight_pair(s['total'])}",
    ]
    if s["calories"]:
        lines.append(f"- Food calories: {s['calories']:,} kcal")
    return lines


def _group_by_category(pack_items) -> list:
    """[(category_name, [pack_items])] ordered like the public page."""
    groups = {}
    order = {}
    for pi in pack_items:
        if pi.item is None:
            continue
        name = _shared_category_name(pi.item)
        groups.setdefault(name, []).append(pi)
        order.setdefault(name, _category_sort_key(pi.item))
    for items in groups.values():
        items.sort(key=lambda pi: float(pi.sort_order or 0))
    return sorted(groups.items(), key=lambda kv: (order[kv[0]], kv[0]))


def _item_table(pack_items) -> list:
    header = "| Item | Brand / Product | Qty | Weight (each) | Weight (total) | Flags | Notes |"
    sep = "|---|---|---:|---:|---:|---|---|"
    rows = [header, sep]
    for pi in pack_items:
        item = pi.item
        qty = float(pi.quantity or 1)
        each_g = item_weight_grams(item)
        flags = []
        if pi.worn:
            flags.append("worn")
        if item.consumable:
            flags.append("consumable")
        if not item.weight:
            flags.append("no weight entered")
        rows.append("| " + " | ".join([
            _md_cell(item.name or "(unnamed)"),
            _md_cell(_product_label(item)),
            _fmt_num(qty, 2),
            _fmt_item_weight(each_g) if item.weight else "—",
            _fmt_item_weight(each_g * qty) if item.weight else "—",
            ", ".join(flags),
            _md_cell(item.notes),
        ]) + " |")
    return rows


def _pack_section(pack, heading_level: int, include_totals: bool) -> list:
    h = "#" * heading_level
    lines = [f"{h} {pack.title}", ""]
    if not pack.items:
        lines += ["_This pack has no items yet._", ""]
        return lines
    if include_totals:
        lines += _summary_lines(_summarize(pack.items)) + [""]
    for category, items in _group_by_category(pack.items):
        cat_total = sum(item_weight_grams(pi.item) * float(pi.quantity or 1) for pi in items)
        lines.append(f"{h}# {category} — {_fmt_item_weight(cat_total)}")
        lines.append("")
        lines += _item_table(items)
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_ai_review_markdown(trip, packs: list, user, public_url: Optional[str] = None) -> str:
    """Return the full markdown document for a trip and its packs.

    `user` only needs `.unit_distance` and `.unit_temperature` (the trip
    owner's display preferences). Packs with no items are still listed so the
    reviewer knows they exist.
    """
    title = trip.location or trip.title
    lines = [f"# Gear list: {title}", ""]

    if trip.location and trip.title and trip.title != trip.location:
        lines += [f"**Trip:** {trip.title}", ""]

    lines += [
        "_Exported from Packstack. Everything below — trip details, weight totals, "
        "and the itemized gear list — is provided for review. Weights are given "
        "in both grams and ounces and the totals are precomputed; please use them "
        "as-is rather than re-summing._",
        "",
    ]
    if public_url:
        lines += [f"Public page: {public_url}", ""]

    # Trip details -----------------------------------------------------
    lines += ["## Trip details", ""]
    rows = _trip_rows(trip, user)
    if rows:
        lines += [f"- **{label}:** {value}" for label, value in rows]
    else:
        lines.append("_No trip details were entered._")
    lines.append("")

    if trip.notes:
        lines += ["### Trip description", "", trip.notes.strip(), ""]

    # Totals -----------------------------------------------------------
    packs = list(packs)
    all_items = [pi for p in packs for pi in (p.items or [])]
    multi = len(packs) > 1

    lines += ["## Weight summary", ""]
    if not all_items:
        lines += ["_No gear has been added to this trip yet._", ""]
    else:
        overall = _summarize(all_items)
        if multi:
            lines.append(
                f"Combined across {len(packs)} packs ({overall['count']} items). "
                "Per-pack totals are listed under each pack below."
            )
            lines.append("")
        else:
            lines.append(f"{overall['count']} items.")
            lines.append("")
        lines += _summary_lines(overall) + [""]

    lines += [
        "Definitions: **base weight** is everything carried in the pack that is "
        "not worn and not consumed; **worn** items are on the body while hiking; "
        "**consumables** are food, fuel and water that get used up. Rows flagged "
        "\"no weight entered\" have no weight on file and are excluded from the "
        "totals, so treat them as an unknown.",
        "",
    ]

    # Gear -------------------------------------------------------------
    lines += ["## Gear", ""]
    if not packs:
        lines += ["_No packs on this trip._", ""]
    elif multi:
        for pack in packs:
            lines += _pack_section(pack, heading_level=3, include_totals=True)
    else:
        # Single pack: skip the redundant per-pack totals and use the pack
        # title as a plain heading so categories sit at h3.
        lines += _pack_section(packs[0], heading_level=3, include_totals=False)

    return "\n".join(lines).rstrip() + "\n"
