CONVERSION_TO_GRAMS = {"g": 1, "kg": 1000, "oz": 28.3495, "lb": 453.592}


def compute_pack_summary(pack):
    """Compute weight_breakdown and category_weights for a Pack.

    All values are returned in grams so the backend stays
    unit-preference-agnostic. Clients convert for display.
    """
    breakdown = {"base_g": 0.0, "worn_g": 0.0, "consumable_g": 0.0, "total_g": 0.0}
    category_totals = {}
    total_calories = 0.0

    for pi in pack.items:
        item = pi.item
        qty = float(pi.quantity or 1)
        weight_g = float(item.weight or 0) * CONVERSION_TO_GRAMS.get(item.unit, 1)
        qty_weight_g = weight_g * qty

        breakdown["total_g"] += qty_weight_g
        if pi.worn:
            breakdown["worn_g"] += weight_g
        if item.consumable:
            breakdown["consumable_g"] += qty_weight_g

        total_calories += float(item.calories or 0) * qty

        cat_name = (
            item.category.category.name
            if item.category and item.category.category
            else "Uncategorized"
        )
        category_totals[cat_name] = category_totals.get(cat_name, 0) + qty_weight_g

    breakdown["base_g"] = breakdown["total_g"] - (breakdown["worn_g"] + breakdown["consumable_g"])

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
        "hiker_profile_id": pack.hiker_profile_id,
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
                "sort_order": float(pi.sort_order) if pi.sort_order else 0,
                "item": _serialize_item_public(pi.item),
            }
            for pi in pack.items
        ],
    }
