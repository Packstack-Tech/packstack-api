"""The one rule for splitting a pack item's weight into base / worn /
consumable, and the one normalizer for worn quantity on writes.

Every surface that shows pack weight must go through `split_weight`: the
authenticated app's `weight_breakdown`, the AI-review export, and the MCP
server. The web, mobile and public-site clients implement the same rule in
their own `wornQuantity` / weight helpers -- keep them in step with this file.

Rule, per pack item (unit = one item's weight, q = quantity, w = worn units):

    worn        += unit * w
    consumable  += unit * (q - w)   if the item is consumable
    base        += unit * (q - w)   otherwise
    total       += unit * q

History: until 2026-09-30 the apps counted one unit of a worn item as worn and
the public page / AI export counted every unit, so one pack had two base
weights. The apps' rule also subtracted a worn consumable from base twice.
"""
from decimal import Decimal
from typing import Optional

CONVERSION_TO_GRAMS = {"g": 1, "kg": 1000, "oz": 28.3495, "lb": 453.592}


def _f(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


def item_unit_grams(item) -> float:
    if item is None:
        return 0.0
    return _f(item.weight) * CONVERSION_TO_GRAMS.get(item.unit, 1)


def pack_quantity(pi) -> float:
    return _f(pi.quantity) or 1.0


def effective_worn(pi) -> float:
    """Worn units for a pack item, tolerating legacy rows.

    A row written before worn_quantity existed (or by the old API between the
    column being added and the backfill) has worn = true and worn_quantity = 0;
    that reads as one unit worn, capped at the quantity.
    """
    q = pack_quantity(pi)
    wq = _f(getattr(pi, "worn_quantity", None))
    if wq > 0:
        return min(wq, q)
    if getattr(pi, "worn", False):
        return min(1.0, q)
    return 0.0


def split_weight(pi) -> dict:
    """Grams for one pack item: {base, worn, consumable, total}."""
    item = pi.item
    unit = item_unit_grams(item)
    q = pack_quantity(pi)
    w = effective_worn(pi)
    rest = unit * (q - w)
    consumable = bool(item.consumable) if item is not None else False
    return {
        "worn": unit * w,
        "consumable": rest if consumable else 0.0,
        "base": 0.0 if consumable else rest,
        "total": unit * q,
    }


def normalize_worn(quantity, worn: Optional[bool], worn_quantity=None,
                   existing_worn_quantity=None) -> tuple[float, bool]:
    """(worn_quantity, worn) to store for a pack item write.

    - `worn_quantity` given and > 0 (new clients): trusted, clamped to
      0..quantity. It wins over a contradictory `worn: false`.
    - `worn` true with no count, or a count of 0 (old clients; `worn`-only
      MCP calls; a new client echoing back a legacy row it never edited --
      those load as worn=true, worn_quantity=0 until the backfill): keep the
      stored count if there is one, else one unit. Old mobile builds resend
      the whole pack on every checklist tick; without this they would wipe
      every count a user set on web. New clients never send worn=true with a
      0 count on purpose: their stores keep worn == (worn_quantity > 0).
    - otherwise: 0.
    """
    q = _f(quantity) or 1.0
    if worn_quantity is not None and (_f(worn_quantity) > 0 or not worn):
        w = _f(worn_quantity)
    elif worn:
        prev = _f(existing_worn_quantity)
        w = prev if prev > 0 else 1.0
    else:
        w = 0.0
    w = max(0.0, min(w, q))
    return w, w > 0
