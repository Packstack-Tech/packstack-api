"""Read tools for the Packstack MCP server (Phase 1).

Design rules (spec §6): task-shaped rather than endpoint-shaped; every id
the model might need to chain appears in results; weights carry grams and
the user's display unit; descriptions are written for the model, because
they are the only thing it reads before deciding which tool to call.

Every tool is `async def` and does its database work in a worker thread via
`run_sync`, so a slow query never stalls the event loop for other requests.
"""

import datetime
from typing import Any, Literal, Optional

from fastapi_sqlalchemy import db
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload, noload

from models.base import (
    CatalogProduct, HikerProfile, Item, ItemCategory, Kit, Pack, PackItem, Trip, User,
)
from mcp_server.context import (
    Caller, ToolError, current_caller, item_summary, run_sync, to_grams, weight_fields,
)
from utils.ai_review import build_ai_review_markdown
from utils.consts import FREE_TIER_UNLIMITED, FREE_PACKS_PER_TRIP
from api.trip import FREE_TRIP_LIMIT

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

TERRAIN = {"paved": "Paved", "gravel": "Gravel / Dirt", "rugged": "Rugged / Rocky", "sand": "Sand", "swamp": "Swamp / Marsh"}
PACE = {"easy": "Easy", "moderate": "Moderate", "fast": "Fast"}
CONDITIONS = {"cold": "Cold", "moderate": "Moderate", "hot": "Hot"}


# ---------------------------------------------------------------------------
# Shared shaping
# ---------------------------------------------------------------------------

def _trip_header(trip: Trip, user: User) -> dict[str, Any]:
    imperial = (user.unit_distance or "MI") == "MI"
    fahrenheit = (user.unit_temperature or "F") == "F"

    def temp(c):
        if c is None:
            return None
        return {"celsius": c, "fahrenheit": round(c * 9 / 5 + 32), "display": f"{round(c * 9 / 5 + 32)}°F" if fahrenheit else f"{c}°C"}

    nights = None
    if trip.start_date and trip.end_date:
        nights = max((trip.end_date - trip.start_date).days, 0)

    distance = None
    if trip.distance:
        km = float(trip.distance)
        mi = km / 1.60934
        distance = {"km": round(km, 1), "miles": round(mi, 1), "display": f"{mi:.1f} mi" if imperial else f"{km:.1f} km"}

    elevation = None
    if trip.daily_elevation_gain:
        m = float(trip.daily_elevation_gain)
        ft = m / 0.3048
        elevation = {"meters_per_day": round(m), "feet_per_day": round(ft), "display": f"{ft:,.0f} ft/day" if imperial else f"{m:,.0f} m/day"}

    return {
        "trip_id": trip.id,
        "public_url": f"https://packstack.io/pack/{trip.uuid}" if trip.uuid else None,
        "title": trip.title,
        "location": trip.location,
        "start_date": trip.start_date.isoformat() if trip.start_date else None,
        "end_date": trip.end_date.isoformat() if trip.end_date else None,
        "nights": nights,
        "distance": distance,
        "daily_elevation_gain": elevation,
        "temperature_low": temp(trip.temp_min),
        "temperature_high": temp(trip.temp_max),
        "conditions": CONDITIONS.get(trip.temp_category, trip.temp_category),
        "terrain": TERRAIN.get(trip.terrain, trip.terrain),
        "pace": PACE.get(trip.pace, trip.pace),
        "notes": trip.notes or None,
    }


def _pack_totals(items: list[PackItem], caller: Caller) -> dict[str, Any]:
    base = worn = consumable = total = 0.0
    calories = 0.0
    for pi in items:
        if pi.item is None:
            continue
        qty = float(pi.quantity or 1)
        g = to_grams(pi.item.weight, pi.item.unit) * qty
        total += g
        if pi.worn:
            worn += g
        elif pi.item.consumable:
            consumable += g
        else:
            base += g
        calories += float(pi.item.calories or 0) * qty
    return {
        "base_weight": weight_fields(base, caller, big=True),
        "worn_weight": weight_fields(worn, caller, big=True),
        "consumable_weight": weight_fields(consumable, caller, big=True),
        "total_weight": weight_fields(total, caller, big=True),
        "calories": round(calories),
        "item_count": len(items),
    }


def _pack_detail(pack: Pack, caller: Caller) -> dict[str, Any]:
    groups: dict[str, list] = {}
    for pi in sorted(pack.items, key=lambda p: float(p.sort_order or 0)):
        if pi.item is None:
            continue
        cat = (pi.item.category.category.name
               if pi.item.category and pi.item.category.category else "Uncategorized")
        entry = item_summary(pi.item, caller)
        entry.update({
            "quantity": float(pi.quantity or 1),
            "worn": bool(pi.worn),
            "checked": bool(pi.checked),
            "line_weight": weight_fields(to_grams(pi.item.weight, pi.item.unit) * float(pi.quantity or 1), caller),
        })
        groups.setdefault(cat, []).append(entry)
    return {
        "pack_id": pack.id,
        "title": pack.title,
        "totals": _pack_totals(list(pack.items), caller),
        "categories": [{"category": c, "items": rows} for c, rows in groups.items()],
    }


def _packs_query(trip_id: int):
    return (db.session.query(Pack).filter_by(trip_id=trip_id)
            .options(joinedload(Pack.items).joinedload(PackItem.item).noload(Item.catalog_product))
            .order_by(Pack.id))


def _own_trip(caller: Caller, trip_id: int) -> Trip:
    trip = db.session.query(Trip).filter_by(id=trip_id, user_id=caller.user.id, removed=False).first()
    if trip is None:
        raise ToolError(f"Trip {trip_id} was not found in this account. Call list_trips to see available trips.")
    return trip


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_read_tools(mcp: MCPServer) -> None:

    @mcp.tool(
        name="get_me",
        description=(
            "Get the connected Packstack account: username, display name, preferred units "
            "(weight, distance, temperature), currency, whether the account has a subscription, "
            "and how many trips/kits it may still create on the free tier. Call this first in a "
            "conversation so weights and distances can be discussed in the user's own units."
        ),
        annotations=READ_ONLY,
    )
    async def get_me() -> dict[str, Any]:
        def work():
            caller = current_caller()
            u = caller.user
            trips = db.session.query(Trip).filter_by(user_id=u.id, removed=False).count()
            kits = db.session.query(Kit).filter_by(user_id=u.id).count()
            items = db.session.query(Item).filter_by(user_id=u.id, deleted=False, removed=False).count()

            def remaining(count, limit):
                return None if (u.is_subscribed or limit >= FREE_TIER_UNLIMITED) else max(limit - count, 0)

            return {
                "username": u.username,
                "display_name": u.display_name,
                "units": {
                    "weight_system": u.unit_weight or "METRIC",
                    "item_weight_unit": caller.unit,
                    "total_weight_unit": caller.big_unit,
                    "distance": "miles" if (u.unit_distance or "MI") == "MI" else "km",
                    "temperature": "F" if (u.unit_temperature or "F") == "F" else "C",
                },
                "currency": u.currency or "USD",
                "subscribed": bool(u.is_subscribed),
                "can_edit_via_connected_apps": bool(u.is_subscribed),
                "counts": {"trips": trips, "kits": kits, "active_gear_items": items},
                "free_tier_remaining": {
                    "trips": remaining(trips, FREE_TRIP_LIMIT),
                    "kits": remaining(kits, 1),
                    "packs_per_trip": None if u.is_subscribed or FREE_PACKS_PER_TRIP >= FREE_TIER_UNLIMITED else FREE_PACKS_PER_TRIP,
                },
                "connection_scopes": caller.scopes,
            }
        return await run_sync(work)

    @mcp.tool(
        name="list_trips",
        description=(
            "List the user's trips (Packstack calls these 'packs' on the Packs tab). Each row has "
            "trip_id, title, location, dates, temperature range, distance, number of packs, and base "
            "and total weight across all packs. Filter with `when` = 'upcoming' (end date today or "
            "later), 'past', or 'all'; `search` matches title or location. Use get_trip for the full "
            "gear list of one trip."
        ),
        annotations=READ_ONLY,
    )
    async def list_trips(when: Literal["all", "upcoming", "past"] = "all", search: Optional[str] = None) -> dict[str, Any]:
        def work():
            caller = current_caller()
            q = db.session.query(Trip).filter_by(user_id=caller.user.id, removed=False)
            today = datetime.date.today()
            if when == "upcoming":
                q = q.filter(or_(Trip.end_date.is_(None), Trip.end_date >= today))
            elif when == "past":
                q = q.filter(Trip.end_date < today)
            if search:
                like = f"%{search.strip()}%"
                q = q.filter(or_(Trip.title.ilike(like), Trip.location.ilike(like)))
            trips = q.order_by(Trip.start_date.desc().nullslast(), Trip.created_at.desc()).limit(100).all()

            rows = []
            for t in trips:
                packs = _packs_query(t.id).all()
                all_items = [pi for p in packs for pi in p.items]
                totals = _pack_totals(all_items, caller)
                header = _trip_header(t, caller.user)
                rows.append({
                    **{k: header[k] for k in ("trip_id", "title", "location", "start_date", "end_date", "nights", "distance", "temperature_low", "temperature_high")},
                    "pack_count": len(packs),
                    "pack_titles": [p.title for p in packs],
                    "base_weight": totals["base_weight"],
                    "total_weight": totals["total_weight"],
                    "item_count": totals["item_count"],
                })
            return {"count": len(rows), "trips": rows}
        return await run_sync(work)

    @mcp.tool(
        name="get_trip",
        description=(
            "Get one trip in full: trip details (dates, location, temperatures, distance, elevation, "
            "terrain, pace, notes) and every pack on it with items grouped by category, quantities, "
            "worn/consumable flags, per-item weights and base/worn/consumable/total weights. "
            "`format`='json' (default) returns structured data; 'markdown' returns the same content "
            "as a single markdown document with precomputed totals — use markdown when you are about "
            "to review or critique the whole list (a 'shakedown'). Requires a trip_id from list_trips."
        ),
        annotations=READ_ONLY,
    )
    async def get_trip(trip_id: int, format: Literal["json", "markdown"] = "json") -> dict[str, Any]:
        def work():
            caller = current_caller()
            trip = _own_trip(caller, trip_id)
            packs = _packs_query(trip.id).all()
            if format == "markdown":
                md = build_ai_review_markdown(trip, packs, caller.user,
                                              public_url=f"https://packstack.io/pack/{trip.uuid}" if trip.uuid else None)
                return {"trip_id": trip.id, "format": "markdown", "markdown": md}
            all_items = [pi for p in packs for pi in p.items]
            return {
                "trip": _trip_header(trip, caller.user),
                "totals_all_packs": _pack_totals(all_items, caller),
                "packs": [_pack_detail(p, caller) for p in packs],
            }
        return await run_sync(work)

    @mcp.tool(
        name="search_gear",
        description=(
            "Search the user's gear closet (their inventory of owned items, independent of any trip). "
            "Filters: `query` matches item name, brand or product; `category` is a category name as "
            "returned by list_categories; `consumable` true/false; `status` 'active' (default), "
            "'archived', or 'all'. Sort by 'name' (default), 'weight_desc' or 'weight_asc'. Returns up "
            "to `limit` items (default 100, max 500) with item_id, weight in grams and the user's unit, "
            "category, notes and price. Use item_id with get_item or with pack tools."
        ),
        annotations=READ_ONLY,
    )
    async def search_gear(
        query: Optional[str] = None,
        category: Optional[str] = None,
        consumable: Optional[bool] = None,
        status: Literal["active", "archived", "all"] = "active",
        sort: Literal["name", "weight_desc", "weight_asc"] = "name",
        limit: int = 100,
    ) -> dict[str, Any]:
        def work():
            caller = current_caller()
            q = (db.session.query(Item).filter_by(user_id=caller.user.id, deleted=False)
                 .options(noload(Item.catalog_product)))
            if status == "active":
                q = q.filter(Item.removed == False)  # noqa: E712
            elif status == "archived":
                q = q.filter(Item.removed == True)  # noqa: E712
            if consumable is not None:
                q = q.filter(Item.consumable == consumable)
            items = q.all()

            if query:
                needle = query.strip().lower()
                items = [i for i in items if needle in " ".join(filter(None, [
                    i.name or "", i.brand.name if i.brand else "", i.product.name if i.product else "",
                    i.product_variant.name if i.product_variant else "", i.notes or ""])).lower()]
            if category:
                cat = category.strip().lower()
                items = [i for i in items if (i.category and i.category.category and i.category.category.name.lower() == cat)
                         or (cat == "uncategorized" and not i.category)]

            def grams(i):
                return to_grams(i.weight, i.unit)
            if sort == "weight_desc":
                items.sort(key=grams, reverse=True)
            elif sort == "weight_asc":
                items.sort(key=grams)
            else:
                items.sort(key=lambda i: (i.name or "").lower())

            cap = max(1, min(int(limit), 500))
            total_g = sum(grams(i) for i in items)
            return {
                "count": len(items),
                "returned": min(len(items), cap),
                "total_weight_of_matches": weight_fields(total_g, caller, big=True),
                "items": [item_summary(i, caller) for i in items[:cap]],
            }
        return await run_sync(work)

    @mcp.tool(
        name="get_item",
        description=(
            "Get one gear item by item_id with its full details plus which trips/packs and kits "
            "currently include it, and its lifecycle info (acquired date, condition, status, "
            "retirement). Use search_gear to find item_ids."
        ),
        annotations=READ_ONLY,
    )
    async def get_item(item_id: int) -> dict[str, Any]:
        def work():
            caller = current_caller()
            item = (db.session.query(Item).filter_by(id=item_id, user_id=caller.user.id, deleted=False)
                    .options(noload(Item.catalog_product)).first())
            if item is None:
                raise ToolError(f"Item {item_id} was not found in this gear closet. Use search_gear to find items.")
            pack_rows = (db.session.query(PackItem, Pack, Trip)
                         .join(Pack, PackItem.pack_id == Pack.id)
                         .join(Trip, Pack.trip_id == Trip.id)
                         .filter(PackItem.item_id == item.id, Trip.removed == False)  # noqa: E712
                         .all())
            kits = (db.session.query(Kit).filter(Kit.user_id == caller.user.id)
                    .filter(Kit.items.any(item_id=item.id)).all())
            out = item_summary(item, caller)
            out["lifecycle"] = {
                "acquired_date": item.acquired_date.isoformat() if item.acquired_date else None,
                "acquisition_type": item.acquisition_type,
                "purchase_retailer": item.purchase_retailer,
                "condition": item.condition,
                "status": item.status or "active",
                "retired_date": item.retired_date.isoformat() if item.retired_date else None,
                "retired_reason": item.retired_reason,
            }
            out["in_packs"] = [{
                "trip_id": t.id, "trip": t.location or t.title, "pack_id": p.id, "pack": p.title,
                "quantity": float(pi.quantity or 1), "worn": bool(pi.worn),
            } for pi, p, t in pack_rows]
            out["in_kits"] = [{"kit_id": k.id, "name": k.name} for k in kits]
            return out
        return await run_sync(work)

    @mcp.tool(
        name="list_categories",
        description=(
            "List the user's gear categories in their display order, with the number of active items "
            "in each. Category names are what search_gear's `category` filter accepts."
        ),
        annotations=READ_ONLY,
    )
    async def list_categories() -> dict[str, Any]:
        def work():
            caller = current_caller()
            rows = (db.session.query(ItemCategory).filter_by(user_id=caller.user.id)
                    .order_by(ItemCategory.sort_order).all())
            counts = dict(db.session.query(Item.category_id, func.count(Item.id))
                          .filter(Item.user_id == caller.user.id, Item.deleted == False, Item.removed == False)  # noqa: E712
                          .group_by(Item.category_id).all())
            return {"categories": [{
                "name": c.category.name if c.category else "Unknown",
                "item_count": counts.get(c.id, 0),
                "shared": c.category.user_id is None if c.category else False,
            } for c in rows]}
        return await run_sync(work)

    @mcp.tool(
        name="list_kits",
        description=(
            "List the user's kits — reusable bundles of gear (e.g. 'Cook kit', 'First aid') that can "
            "be added to a pack in one step. Returns kit_id, name, item count and total weight. Use "
            "get_kit for the items."
        ),
        annotations=READ_ONLY,
    )
    async def list_kits() -> dict[str, Any]:
        def work():
            caller = current_caller()
            kits = db.session.query(Kit).filter_by(user_id=caller.user.id).order_by(Kit.name).all()
            return {"kits": [{
                "kit_id": k.id,
                "name": k.name,
                "item_count": len(k.items),
                "total_weight": weight_fields(sum(to_grams(ki.item.weight, ki.item.unit) * float(ki.quantity or 1)
                                                  for ki in k.items if ki.item), caller, big=True),
            } for k in kits]}
        return await run_sync(work)

    @mcp.tool(
        name="get_kit",
        description="Get one kit by kit_id with its items, quantities and total weight.",
        annotations=READ_ONLY,
    )
    async def get_kit(kit_id: int) -> dict[str, Any]:
        def work():
            caller = current_caller()
            kit = db.session.query(Kit).filter_by(id=kit_id, user_id=caller.user.id).first()
            if kit is None:
                raise ToolError(f"Kit {kit_id} was not found. Call list_kits to see available kits.")
            items = []
            total = 0.0
            for ki in kit.items:
                if ki.item is None:
                    continue
                g = to_grams(ki.item.weight, ki.item.unit) * float(ki.quantity or 1)
                total += g
                row = item_summary(ki.item, caller)
                row["quantity"] = float(ki.quantity or 1)
                items.append(row)
            return {"kit_id": kit.id, "name": kit.name, "total_weight": weight_fields(total, caller, big=True), "items": items}
        return await run_sync(work)

    @mcp.tool(
        name="search_catalog",
        description=(
            "Search Packstack's public gear catalog (thousands of products from many brands with "
            "verified weights) — not the user's own gear. Use it to suggest lighter alternatives, "
            "check a product's weight, or find a catalog_product_id to attach when creating gear. "
            "`query` matches brand, product or category words (e.g. 'sleeping pad', 'Durston', "
            "'Nemo Tensor'). Returns up to `limit` products (default 25, max 100) with weight in grams "
            "and the user's unit, category and product URL."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
    )
    async def search_catalog(query: str, limit: int = 25) -> dict[str, Any]:
        def work():
            caller = current_caller()
            q = (query or "").strip()
            if len(q) < 2:
                raise ToolError("Give at least two characters to search the catalog.")
            like = f"%{q}%"
            name_match = or_(
                CatalogProduct.brand_name.ilike(like),
                CatalogProduct.product_name.ilike(like),
                (CatalogProduct.brand_name + " " + CatalogProduct.product_name).ilike(like),
            )
            rows = (db.session.query(CatalogProduct)
                    .filter(CatalogProduct.status == "approved",
                            or_(name_match, CatalogProduct.subcategory.ilike(like), CatalogProduct.category_suggestion.ilike(like)))
                    .order_by(CatalogProduct.brand_name, CatalogProduct.product_name, CatalogProduct.variant_name)
                    .limit(max(1, min(int(limit), 100))).all())
            return {"count": len(rows), "products": [{
                "catalog_product_id": r.id,
                "brand": r.brand_name,
                "product": r.product_name,
                "variant": r.variant_name,
                "category": r.category_suggestion,
                "subcategory": r.subcategory,
                "weight": weight_fields(to_grams(r.weight, r.weight_unit), caller) if r.weight else None,
                "calories": r.kcal,
                "product_url": r.product_url,
            } for r in rows]}
        return await run_sync(work)

    @mcp.tool(
        name="list_hiker_profiles",
        description=(
            "List the names and ids of the user's hiker profiles (people who carry packs on group "
            "trips). Only names and ids are returned — no body measurements. Use a hiker_profile_id "
            "when assigning a pack to a person."
        ),
        annotations=READ_ONLY,
    )
    async def list_hiker_profiles() -> dict[str, Any]:
        def work():
            caller = current_caller()
            rows = db.session.query(HikerProfile).filter_by(user_id=caller.user.id).order_by(HikerProfile.name).all()
            return {"hiker_profiles": [{"hiker_profile_id": h.id, "name": h.name, "is_default": bool(h.is_default)} for h in rows]}
        return await run_sync(work)
