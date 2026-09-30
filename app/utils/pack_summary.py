from utils.pack_weight import CONVERSION_TO_GRAMS, effective_worn, pack_quantity, split_weight  # noqa: F401


def compute_pack_summary(pack):
    """Compute weight_breakdown and category_weights for a Pack.

    All values are returned in grams so the backend stays
    unit-preference-agnostic. Clients convert for display. The base / worn /
    consumable split is utils.pack_weight.split_weight -- the same rule the
    AI export, MCP and every client use.
    """
    breakdown = {"base_g": 0.0, "worn_g": 0.0, "consumable_g": 0.0, "total_g": 0.0}
    category_totals = {}
    total_calories = 0.0

    for pi in pack.items:
        item = pi.item
        qty = pack_quantity(pi)
        parts = split_weight(pi)

        breakdown["total_g"] += parts["total"]
        breakdown["worn_g"] += parts["worn"]
        breakdown["consumable_g"] += parts["consumable"]
        breakdown["base_g"] += parts["base"]

        total_calories += float(item.calories or 0) * qty

        cat_name = (
            item.category.category.name
            if item.category and item.category.category
            else "Uncategorized"
        )
        category_totals[cat_name] = category_totals.get(cat_name, 0) + parts["total"]

    category_weights = [
        {"label": label, "weight_g": round(weight, 2)}
        for label, weight in category_totals.items()
    ]

    return {
        "weight_breakdown": {k: round(v, 2) for k, v in breakdown.items()},
        "category_weights": category_weights,
        "total_calories": round(total_calories),
    }


def serialize_pack(pack):
    """Serialize a Pack model into a dict enriched with weight summaries."""
    summary = compute_pack_summary(pack)
    return {
        "id": pack.id,
        "user_id": pack.user_id,
        "trip_id": pack.trip_id,
        "title": pack.title,
        "items": pack.items,
        **summary,
    }


def _serialize_item_public(item):
    return {
        "id": item.id,
        "name": item.name,
        "weight": float(item.weight) if item.weight else None,
        "unit": item.unit,
        "calories": float(item.calories) if item.calories else None,
        "consumable": item.consumable,
        "notes": item.notes,
        "product_url": item.product_url,
        "category_id": item.category_id,
        "category": item.category,
        "brand": item.brand,
        "product": item.product,
        "product_variant": item.product_variant,
    }


def serialize_pack_public(pack):
    """Trimmed serialization for the public pack page. No weight summaries."""
    return {
        "id": pack.id,
        "title": pack.title,
        "items": [
            {
                "item_id": pi.item_id,
                "quantity": float(pi.quantity) if pi.quantity else 1,
                "worn": pi.worn,
                "worn_quantity": effective_worn(pi),
                "sort_order": float(pi.sort_order) if pi.sort_order else 0,
                "item": _serialize_item_public(pi.item),
            }
            for pi in pack.items
        ],
    }
