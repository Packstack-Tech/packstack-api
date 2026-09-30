"""Over-pack detection: a trip that packs more of an item than the user owns.

One definition shared by the API, the MCP server and the AI-review export.
Web and mobile derive the same thing locally from data they already hold
(they aggregate pack items across the trip for the "All" view), so nothing
here is load-bearing for the clients -- it exists so every server-side
consumer agrees with them.

Enforcement is deliberately NOT here. The API never rejects a pack write for
over-packing; the user's `overpack_mode` is applied by clients only.
"""
from typing import Iterable


def owned_quantity(item) -> int:
    """Owned count of a closet item. Rows written before the column existed
    are backfilled to 1 by the migration, but keep the fallback for safety."""
    return int(item.quantity or 1)


def overpacked_items(packs: Iterable, include_consumables: bool = True) -> dict:
    """{item_id: {"owned": int, "packed": float, "name": str}} for every item
    whose total quantity across `packs` exceeds what the user owns.

    `packed` is a float because PackItem.quantity is Numeric (fractional
    amounts are legal for consumables, e.g. 0.5 of a fuel canister).
    """
    packed: dict[int, float] = {}
    items: dict[int, object] = {}
    for pack in packs:
        for pi in (pack.items or []):
            item = pi.item
            if item is None:
                continue
            if item.consumable and not include_consumables:
                continue
            packed[pi.item_id] = packed.get(pi.item_id, 0.0) + float(pi.quantity or 1)
            items[pi.item_id] = item

    result = {}
    for item_id, total in packed.items():
        owned = owned_quantity(items[item_id])
        if total > owned:
            result[item_id] = {"owned": owned, "packed": total, "name": items[item_id].name}
    return result
