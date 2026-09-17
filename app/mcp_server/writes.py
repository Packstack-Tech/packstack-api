"""Write tools for the Packstack MCP server (Phase 2).

Every tool here:
  1. requires the packstack:write scope (the HTTP layer already answers 403
     insufficient_scope before we get here — see server.RequireAuth — this is
     the belt to that braces);
  2. requires a subscription, checked at call time so a lapsed subscriber
     is refused even with a valid token;
  3. reuses the REST layer's gates (trip / pack / kit limits, ownership) by
     calling the same helper functions, and translates their HTTPExceptions
     into ToolErrors the model can read;
  4. returns the affected object in the same shapes the read tools use, so
     the model can confirm what changed without a second call.

Nothing here deletes anything. Archive is reversible; removing an item from a
pack leaves it in the closet.
"""

import datetime
import logging
from typing import Any, Literal, Optional

from fastapi import HTTPException
from fastapi_sqlalchemy import db
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from sqlalchemy import func, literal, or_
from sqlalchemy.orm import noload

from api.item import _find_catalog_product
from api.item_lifecycle import (
    VALID_ACQUISITION_TYPES, VALID_CONDITIONS, VALID_EVENT_TYPES, VALID_RETIRED_REASONS,
    VALID_STATUSES,
)
from api.kit import _enforce_kit_limit
from api.pack import _enforce_pack_limit
from api.trip import _enforce_trip_limit
from mcp_server.context import (
    Caller, ToolError, current_caller, item_summary, run_sync, to_grams, weight_fields,
)
from mcp_server.tools import _own_trip, _pack_detail, _packs_query, _trip_header
from models.base import (
    CatalogProduct, Item, ItemLog, Kit, KitItem, Pack, PackItem, Trip,
)
from utils.entity_helpers import (
    resolve_brand, resolve_category, resolve_product, resolve_product_variant,
)
from utils.item_category import get_or_create_item_category
from tasks.enrich_product import normalize_brand, normalize_name
from utils.utils import clone_model

logger = logging.getLogger(__name__)

WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
WRITE_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

UPGRADE_URL = "https://app.packstack.io"
UPGRADE_MESSAGE = (
    "Making changes through a connected app requires a Packstack subscription. "
    f"Reading still works. Upgrade at {UPGRADE_URL}."
)

TERRAIN_VALUES = ("paved", "gravel", "rugged", "sand", "swamp")
PACE_VALUES = ("easy", "moderate", "fast")
CONDITION_VALUES = ("cold", "moderate", "hot")
UNIT_VALUES = ("g", "kg", "oz", "lb")

# The set server.RequireAuth consults for HTTP-level step-up. Filled by
# register_write_tools.
WRITE_TOOL_NAMES: set[str] = set()


# ---------------------------------------------------------------------------
# Gates and error translation
# ---------------------------------------------------------------------------

def require_writer() -> Caller:
    caller = current_caller()
    if not caller.can_write:
        raise ToolError(
            "This connection only has read access. Reconnect Packstack and allow "
            "'Make changes on your behalf' to enable editing."
        )
    if not caller.user.is_subscribed:
        raise ToolError(UPGRADE_MESSAGE)
    return caller


def gated(fn, *args, **kwargs):
    """Run a REST-layer helper and turn its HTTPException into a ToolError."""
    try:
        return fn(*args, **kwargs)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "Request refused."
        if exc.status_code == 402:
            raise ToolError(f"{detail} Upgrade at {UPGRADE_URL}.")
        raise ToolError(detail)


def _parse_date(value: Optional[str], field: str) -> Optional[datetime.date]:
    if value is None or value == "":
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"{field} must be an ISO date like 2026-07-05.")


def _enqueue(task, *args) -> None:
    """Fire a Celery task; a broker outage must not fail the user's write."""
    try:
        task.delay(*args)
    except Exception:  # pragma: no cover - depends on broker availability
        logger.warning("Could not enqueue %s%r", getattr(task, "name", task), args, exc_info=True)


def _own_pack(caller: Caller, pack_id: int) -> Pack:
    pack = (db.session.query(Pack).filter_by(id=pack_id, user_id=caller.user.id)
            .options(noload(Pack.items)).first())
    if pack is None:
        raise ToolError(f"Pack {pack_id} was not found in this account. Call get_trip to see a trip's packs and their pack_ids.")
    return pack


def _fresh_pack_detail(pack: Pack, caller: Caller) -> dict[str, Any]:
    """Re-read a pack with its items after a write. `_own_pack` loads the pack
    with items noload-ed, and SQLAlchemy will not overwrite an already-loaded
    relationship on the identity-mapped instance without populate_existing."""
    fresh = _packs_query(pack.trip_id).filter(Pack.id == pack.id).populate_existing().one()
    return _pack_detail(fresh, caller)


def _own_items(caller: Caller, item_ids: list[int]) -> dict[int, Item]:
    ids = list({int(i) for i in item_ids})
    rows = (db.session.query(Item).filter(Item.id.in_(ids), Item.user_id == caller.user.id, Item.deleted == False)  # noqa: E712
            .options(noload(Item.catalog_product)).all())
    found = {i.id: i for i in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ToolError(f"These item_ids are not in this gear closet: {missing}. Use search_gear to find items.")
    return found


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_write_tools(mcp: MCPServer) -> None:

    def write_tool(name: str, description: str, annotations: ToolAnnotations = WRITE):
        WRITE_TOOL_NAMES.add(name)
        return mcp.tool(name=name, description=description, annotations=annotations)

    # ----------------------------------------------------------------- trips

    @write_tool(
        "create_trip",
        "Create a trip (shown on Packstack's Packs tab). `title` is required; give `location` "
        "whenever known — Packstack researches the trail in the background and fills in "
        "distance, elevation and typical temperatures if you leave them out. Dates are ISO "
        "(2026-07-05). Temperatures are in the user's unit (see get_me); distance in the "
        "user's distance unit; `daily_elevation_gain` in feet for imperial users, meters "
        "otherwise. `terrain`: paved | gravel | rugged | sand | swamp. `pace`: easy | moderate | "
        "fast. `conditions`: cold | moderate | hot. A default pack named 'Main Pack' is created. "
        "Requires a subscription.",
    )
    async def create_trip(
        title: str,
        location: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        temperature_low: Optional[float] = None,
        temperature_high: Optional[float] = None,
        distance: Optional[float] = None,
        daily_elevation_gain: Optional[float] = None,
        terrain: Optional[str] = None,
        pace: Optional[str] = None,
        conditions: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict[str, Any]:
        def work():
            caller = require_writer()
            gated(_enforce_trip_limit, caller.user)
            fields = _trip_fields(caller, location=location, start_date=start_date, end_date=end_date,
                                  temperature_low=temperature_low, temperature_high=temperature_high,
                                  distance=distance, daily_elevation_gain=daily_elevation_gain,
                                  terrain=terrain, pace=pace, conditions=conditions, notes=notes)
            trip = Trip(user_id=caller.user.id, title=title.strip() or (location or "Untitled trip"), **fields)
            db.session.add(trip)
            db.session.flush()
            db.session.add(Pack(user_id=caller.user.id, trip_id=trip.id, title="Main Pack"))
            if trip.location:
                trip.enrich_status = "pending"
            db.session.commit()
            db.session.refresh(trip)
            if trip.enrich_status == "pending":
                from tasks.enrich_trip import enrich_trip
                _enqueue(enrich_trip, trip.id)
            packs = _packs_query(trip.id).all()
            return {"trip": _trip_header(trip, caller.user), "packs": [_pack_detail(p, caller) for p in packs],
                    "note": "Trail research is running in the background; distance, elevation and temperatures may fill in within a minute." if trip.enrich_status == "pending" else None}
        return await run_sync(work)

    @write_tool(
        "update_trip",
        "Update fields on an existing trip. Only the fields you pass change; others are left "
        "alone. Same units and allowed values as create_trip. Pass an empty string to clear "
        "notes. Changing the location or dates re-runs trail research. Requires a subscription.",
        WRITE_IDEMPOTENT,
    )
    async def update_trip(
        trip_id: int,
        title: Optional[str] = None,
        location: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        temperature_low: Optional[float] = None,
        temperature_high: Optional[float] = None,
        distance: Optional[float] = None,
        daily_elevation_gain: Optional[float] = None,
        terrain: Optional[str] = None,
        pace: Optional[str] = None,
        conditions: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict[str, Any]:
        def work():
            caller = require_writer()
            trip = _own_trip(caller, trip_id)
            fields = _trip_fields(caller, location=location, start_date=start_date, end_date=end_date,
                                  temperature_low=temperature_low, temperature_high=temperature_high,
                                  distance=distance, daily_elevation_gain=daily_elevation_gain,
                                  terrain=terrain, pace=pace, conditions=conditions, notes=notes,
                                  partial=True)
            if title is not None and title.strip():
                fields["title"] = title.strip()
            if not fields:
                raise ToolError("Nothing to update — pass at least one field.")
            re_enrich = ("location" in fields and fields["location"] != trip.location) or \
                        ("start_date" in fields and fields["start_date"] != trip.start_date) or \
                        ("end_date" in fields and fields["end_date"] != trip.end_date)
            for k, v in fields.items():
                setattr(trip, k, v)
            if re_enrich and trip.location:
                trip.enrich_status = "pending"
            db.session.commit()
            db.session.refresh(trip)
            if re_enrich and trip.enrich_status == "pending":
                from tasks.enrich_trip import enrich_trip
                _enqueue(enrich_trip, trip.id)
            return {"trip": _trip_header(trip, caller.user), "updated_fields": sorted(fields)}
        return await run_sync(work)

    @write_tool(
        "clone_trip",
        "Copy a trip and all of its packs as a starting point for a new one. Pass a new `title`; "
        "dates are not copied. Counts toward the free-tier trip limit. Requires a subscription.",
    )
    async def clone_trip(trip_id: int, title: str) -> dict[str, Any]:
        def work():
            caller = require_writer()
            gated(_enforce_trip_limit, caller.user)
            source = _own_trip(caller, trip_id)
            data = clone_model(source, ["title", "location", "created_at", "updated_at", "uuid",
                                        "start_date", "end_date", "enrich_status", "published"])
            new_trip = Trip(**data, title=title.strip(), location=source.location)
            db.session.add(new_trip)
            db.session.flush()
            for pack in db.session.query(Pack).filter_by(trip_id=source.id).all():
                new_pack = Pack(**clone_model(pack, ["trip_id", "hiker_profile_id", "created_at", "updated_at"]), trip_id=new_trip.id)
                db.session.add(new_pack)
                db.session.flush()
                for pi in pack.items:
                    db.session.add(PackItem(pack_id=new_pack.id, item_id=pi.item_id, quantity=pi.quantity,
                                            worn=pi.worn, checked=False, sort_order=pi.sort_order))
            db.session.commit()
            db.session.refresh(new_trip)
            packs = _packs_query(new_trip.id).all()
            return {"trip": _trip_header(new_trip, caller.user), "packs": [_pack_detail(p, caller) for p in packs]}
        return await run_sync(work)

    # ----------------------------------------------------------------- packs

    @write_tool(
        "create_pack",
        "Add another pack to a trip — Packstack users mostly use one pack per person on a group "
        "trip (e.g. 'Jerad', 'Anne'), sometimes as loadout variants. Optional `copy_from_pack_id` "
        "copies that pack's items. Free accounts are limited to one pack per trip. Requires a "
        "subscription.",
    )
    async def create_pack(trip_id: int, title: str, copy_from_pack_id: Optional[int] = None) -> dict[str, Any]:
        def work():
            caller = require_writer()
            trip = _own_trip(caller, trip_id)
            gated(_enforce_pack_limit, caller.user, trip.id)
            pack = Pack(user_id=caller.user.id, trip_id=trip.id, title=title.strip() or "Pack")
            db.session.add(pack)
            db.session.flush()
            if copy_from_pack_id is not None:
                src = _own_pack(caller, copy_from_pack_id)
                for pi in db.session.query(PackItem).filter_by(pack_id=src.id).all():
                    db.session.add(PackItem(pack_id=pack.id, item_id=pi.item_id, quantity=pi.quantity,
                                            worn=pi.worn, checked=False, sort_order=pi.sort_order))
            db.session.commit()
            return _fresh_pack_detail(pack, caller)
        return await run_sync(work)

    @write_tool(
        "rename_pack",
        "Rename a pack. Requires a subscription.",
        WRITE_IDEMPOTENT,
    )
    async def rename_pack(pack_id: int, title: str) -> dict[str, Any]:
        def work():
            caller = require_writer()
            pack = _own_pack(caller, pack_id)
            if not title.strip():
                raise ToolError("Title cannot be empty.")
            pack.title = title.strip()
            db.session.commit()
            return {"pack_id": pack.id, "title": pack.title, "trip_id": pack.trip_id}
        return await run_sync(work)

    @write_tool(
        "add_items_to_pack",
        "Add gear-closet items to a pack. `items` is a list of {item_id, quantity?, worn?}. An "
        "item already in the pack has its quantity/worn updated rather than duplicated. Use "
        "search_gear to find item_ids; use create_item first if the gear isn't in the closet yet. "
        "Requires a subscription.",
        WRITE_IDEMPOTENT,
    )
    async def add_items_to_pack(pack_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
        def work():
            caller = require_writer()
            pack = _own_pack(caller, pack_id)
            specs = _parse_pack_item_specs(items)
            owned = _own_items(caller, [s["item_id"] for s in specs])
            existing = {pi.item_id: pi for pi in db.session.query(PackItem).filter_by(pack_id=pack.id).all()}
            max_sort = max([float(pi.sort_order or 0) for pi in existing.values()] or [0.0])
            added, updated = [], []
            for s in specs:
                pi = existing.get(s["item_id"])
                if pi is None:
                    max_sort += 1
                    db.session.add(PackItem(pack_id=pack.id, item_id=s["item_id"], quantity=s.get("quantity", 1),
                                            worn=bool(s.get("worn", False)), checked=False, sort_order=max_sort))
                    added.append(owned[s["item_id"]].name)
                else:
                    if "quantity" in s:
                        pi.quantity = s["quantity"]
                    if "worn" in s:
                        pi.worn = bool(s["worn"])
                    updated.append(owned[s["item_id"]].name)
            db.session.commit()
            detail = _fresh_pack_detail(pack, caller)
            detail.update({"added": added, "updated": updated})
            return detail
        return await run_sync(work)

    @write_tool(
        "update_pack_items",
        "Change quantity, worn or checked (packed) flags for items already in a pack. `items` is "
        "a list of {item_id, quantity?, worn?, checked?}; only the keys you pass change. Mark "
        "clothing being hiked in as worn so it leaves base weight. Requires a subscription.",
        WRITE_IDEMPOTENT,
    )
    async def update_pack_items(pack_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
        def work():
            caller = require_writer()
            pack = _own_pack(caller, pack_id)
            specs = _parse_pack_item_specs(items, allow_checked=True)
            existing = {pi.item_id: pi for pi in db.session.query(PackItem).filter_by(pack_id=pack.id).all()}
            missing = [s["item_id"] for s in specs if s["item_id"] not in existing]
            if missing:
                raise ToolError(f"These item_ids are not in pack {pack.id}: {missing}. Use add_items_to_pack to add them.")
            for s in specs:
                pi = existing[s["item_id"]]
                if "quantity" in s:
                    pi.quantity = s["quantity"]
                if "worn" in s:
                    pi.worn = bool(s["worn"])
                if "checked" in s:
                    pi.checked = bool(s["checked"])
            db.session.commit()
            return _fresh_pack_detail(pack, caller)
        return await run_sync(work)

    @write_tool(
        "remove_items_from_pack",
        "Take items out of a pack. The gear stays in the user's closet and in any other packs. "
        "Requires a subscription.",
        WRITE_IDEMPOTENT,
    )
    async def remove_items_from_pack(pack_id: int, item_ids: list[int]) -> dict[str, Any]:
        def work():
            caller = require_writer()
            pack = _own_pack(caller, pack_id)
            ids = list({int(i) for i in item_ids})
            removed = (db.session.query(PackItem)
                       .filter(PackItem.pack_id == pack.id, PackItem.item_id.in_(ids))
                       .delete(synchronize_session=False))
            db.session.commit()
            detail = _fresh_pack_detail(pack, caller)
            detail["removed_count"] = removed
            return detail
        return await run_sync(work)

    @write_tool(
        "add_kit_to_pack",
        "Add every item in a kit to a pack in one step (items already present keep their "
        "quantity). Requires a subscription.",
        WRITE_IDEMPOTENT,
    )
    async def add_kit_to_pack(pack_id: int, kit_id: int) -> dict[str, Any]:
        def work():
            caller = require_writer()
            pack = _own_pack(caller, pack_id)
            kit = db.session.query(Kit).filter_by(id=kit_id, user_id=caller.user.id).first()
            if kit is None:
                raise ToolError(f"Kit {kit_id} was not found. Call list_kits.")
            existing = {pi.item_id for pi in db.session.query(PackItem).filter_by(pack_id=pack.id).all()}
            max_sort = db.session.query(func.max(PackItem.sort_order)).filter_by(pack_id=pack.id).scalar() or 0
            added = 0
            for ki in kit.items:
                if ki.item_id in existing or ki.item is None:
                    continue
                max_sort = float(max_sort) + 1
                db.session.add(PackItem(pack_id=pack.id, item_id=ki.item_id, quantity=ki.quantity or 1,
                                        worn=False, checked=False, sort_order=max_sort))
                added += 1
            db.session.commit()
            detail = _fresh_pack_detail(pack, caller)
            detail["added_count"] = added
            return detail
        return await run_sync(work)

    # ------------------------------------------------------------------ gear

    @write_tool(
        "create_item",
        "Add a piece of gear to the user's closet. Prefer passing `catalog_product_id` from "
        "search_catalog: brand, product and weight are then taken from the catalog and no new "
        "product record is created. Without it, give `brand` and `product` as free text — but if "
        "they resemble an existing catalog product or something already in the closet, this tool "
        "REFUSES and lists the candidates (with catalog_product_id / item_id) so you can pick one "
        "or use the existing item. Pass `create_new_product: true` only after confirming with the "
        "user that it really is a new product. `weight` is in `unit` (g | kg | oz | lb; defaults "
        "to the user's unit). `category` is a name (created if new). Requires a subscription.",
    )
    async def create_item(
        name: str,
        catalog_product_id: Optional[int] = None,
        brand: Optional[str] = None,
        product: Optional[str] = None,
        variant: Optional[str] = None,
        weight: Optional[float] = None,
        unit: Optional[str] = None,
        category: Optional[str] = None,
        consumable: bool = False,
        calories: Optional[float] = None,
        price: Optional[float] = None,
        product_url: Optional[str] = None,
        notes: Optional[str] = None,
        create_new_product: bool = False,
    ) -> dict[str, Any]:
        def work():
            caller = require_writer()
            if not name.strip():
                raise ToolError("name is required.")
            unit_ = (unit or caller.unit).lower()
            if unit_ not in UNIT_VALUES:
                raise ToolError("unit must be one of g, kg, oz, lb.")

            item = Item(user_id=caller.user.id, name=name.strip(), unit=unit_, consumable=bool(consumable),
                        calories=calories, price=price, product_url=(product_url or "").strip() or None,
                        notes=(notes or "").strip() or None, weight=weight)

            if catalog_product_id is not None:
                cat = db.session.query(CatalogProduct).filter_by(id=catalog_product_id, status="approved").first()
                if cat is None:
                    raise ToolError(f"catalog_product_id {catalog_product_id} was not found. Use search_catalog.")
                item.brand_id = cat.brand_id or resolve_brand(db.session, cat.brand_name)
                item.product_id = cat.product_id or resolve_product(db.session, cat.product_name, item.brand_id)
                if cat.variant_name:
                    item.product_variant_id = cat.product_variant_id or resolve_product_variant(db.session, cat.variant_name, item.product_id)
                item.catalog_product_id = cat.id
                if item.weight is None and cat.weight:
                    item.weight = float(cat.weight)
                    item.unit = cat.weight_unit or "g"
                if not item.product_url and cat.product_url:
                    item.product_url = cat.product_url
                if item.calories is None and cat.kcal:
                    item.calories = cat.kcal
                dedupe_note = None
            elif brand and product:
                candidates = _duplicate_candidates(caller, brand, product, variant)
                if candidates and not create_new_product:
                    raise ToolError(
                        "This looks like it may already exist. " + candidates +
                        " Pass catalog_product_id to use a catalog product, add the existing item_id "
                        "to the pack instead, or pass create_new_product=true if it is really new."
                    )
                item.brand_id = resolve_brand(db.session, brand)
                item.product_id = resolve_product(db.session, product, item.brand_id)
                if variant:
                    item.product_variant_id = resolve_product_variant(db.session, variant, item.product_id)
                match = _find_catalog_product(db.session, item.brand_id, item.product_id, item.product_variant_id)
                if match:
                    item.catalog_product_id = match.id
                dedupe_note = None if match else "New product recorded; Packstack will research it in the background."
            else:
                if brand and not product:
                    item.brand_id = resolve_brand(db.session, brand)
                dedupe_note = None

            if category:
                cat_id = resolve_category(db.session, category, caller.user.id)
                item.category_id = get_or_create_item_category(db.session, cat_id, caller.user.id)

            db.session.add(item)
            db.session.commit()
            db.session.refresh(item)

            if item.brand_id and item.product_id and not item.catalog_product_id:
                from tasks.enrich_product import enrich_product
                _enqueue(enrich_product, item.brand_id, item.product_id, item.product_variant_id)

            out = item_summary(item, caller)
            out["note"] = dedupe_note
            return out
        return await run_sync(work)

    @write_tool(
        "update_item",
        "Edit a gear item's own fields: name, weight (+unit), category (by name), consumable flag, "
        "calories, price, product_url, notes. Only passed fields change; pass an empty string to "
        "clear notes or product_url. This is where to fix a mis-flagged consumable or a wrong "
        "weight. To change brand/product, create a new item instead. Requires a subscription.",
        WRITE_IDEMPOTENT,
    )
    async def update_item(
        item_id: int,
        name: Optional[str] = None,
        weight: Optional[float] = None,
        unit: Optional[str] = None,
        category: Optional[str] = None,
        consumable: Optional[bool] = None,
        calories: Optional[float] = None,
        price: Optional[float] = None,
        product_url: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict[str, Any]:
        def work():
            caller = require_writer()
            item = _own_items(caller, [item_id])[item_id]
            changed = []
            if name is not None and name.strip():
                item.name = name.strip(); changed.append("name")
            if unit is not None:
                if unit.lower() not in UNIT_VALUES:
                    raise ToolError("unit must be one of g, kg, oz, lb.")
                item.unit = unit.lower(); changed.append("unit")
            if weight is not None:
                item.weight = weight; changed.append("weight")
            if category is not None:
                if category.strip():
                    cat_id = resolve_category(db.session, category, caller.user.id)
                    item.category_id = get_or_create_item_category(db.session, cat_id, caller.user.id)
                else:
                    item.category_id = None
                changed.append("category")
            if consumable is not None:
                item.consumable = bool(consumable); changed.append("consumable")
            if calories is not None:
                item.calories = calories; changed.append("calories")
            if price is not None:
                item.price = price; changed.append("price")
            if product_url is not None:
                item.product_url = product_url.strip() or None; changed.append("product_url")
            if notes is not None:
                item.notes = notes.strip() or None; changed.append("notes")
            if not changed:
                raise ToolError("Nothing to update — pass at least one field.")
            db.session.commit()
            db.session.refresh(item)
            out = item_summary(item, caller)
            out["updated_fields"] = changed
            return out
        return await run_sync(work)

    @write_tool(
        "archive_items",
        "Archive gear the user no longer carries. Archived items stay in the closet's archive "
        "(status 'archived' in search_gear) and can be restored; nothing is deleted. Requires a "
        "subscription.",
        WRITE_IDEMPOTENT,
    )
    async def archive_items(item_ids: list[int]) -> dict[str, Any]:
        def work():
            caller = require_writer()
            owned = _own_items(caller, item_ids)
            for i in owned.values():
                i.removed = True
            db.session.commit()
            return {"archived": [{"item_id": i.id, "name": i.name} for i in owned.values()]}
        return await run_sync(work)

    @write_tool(
        "restore_items",
        "Bring archived gear back to the active closet. Requires a subscription.",
        WRITE_IDEMPOTENT,
    )
    async def restore_items(item_ids: list[int]) -> dict[str, Any]:
        def work():
            caller = require_writer()
            owned = _own_items(caller, item_ids)
            for i in owned.values():
                i.removed = False
            db.session.commit()
            return {"restored": [{"item_id": i.id, "name": i.name} for i in owned.values()]}
        return await run_sync(work)

    @write_tool(
        "log_item_lifecycle",
        "Record gear history on an item. Either update lifecycle fields (acquired_date, "
        "acquisition_type: purchased | gifted | traded | diy, purchase_retailer, condition: new | "
        "good | fair | worn, status: active | wishlist | retired | sold | lost, retired_date, "
        "retired_reason: worn_out | upgraded | lost | sold | gifted) and/or add a log entry "
        "(event_type: acquired | condition_change | repair | maintenance | weight_check | retired | "
        "sold | note, with event_date, note, cost). A condition change is logged automatically. "
        "Requires a subscription.",
    )
    async def log_item_lifecycle(
        item_id: int,
        acquired_date: Optional[str] = None,
        acquisition_type: Optional[str] = None,
        purchase_retailer: Optional[str] = None,
        condition: Optional[str] = None,
        status: Optional[str] = None,
        retired_date: Optional[str] = None,
        retired_reason: Optional[str] = None,
        event_type: Optional[str] = None,
        event_date: Optional[str] = None,
        note: Optional[str] = None,
        cost: Optional[float] = None,
    ) -> dict[str, Any]:
        def work():
            caller = require_writer()
            item = _own_items(caller, [item_id])[item_id]
            for value, allowed, label in ((condition, VALID_CONDITIONS, "condition"), (status, VALID_STATUSES, "status"),
                                          (acquisition_type, VALID_ACQUISITION_TYPES, "acquisition_type"),
                                          (retired_reason, VALID_RETIRED_REASONS, "retired_reason"),
                                          (event_type, VALID_EVENT_TYPES, "event_type")):
                if value is not None and value not in allowed:
                    raise ToolError(f"{label} must be one of: {', '.join(sorted(allowed))}.")
            changed = []
            old_condition = item.condition
            if acquired_date is not None:
                item.acquired_date = _parse_date(acquired_date, "acquired_date"); changed.append("acquired_date")
            if acquisition_type is not None:
                item.acquisition_type = acquisition_type; changed.append("acquisition_type")
            if purchase_retailer is not None:
                item.purchase_retailer = purchase_retailer.strip() or None; changed.append("purchase_retailer")
            if condition is not None:
                item.condition = condition; changed.append("condition")
            if status is not None:
                item.status = status; changed.append("status")
            if retired_date is not None:
                item.retired_date = _parse_date(retired_date, "retired_date"); changed.append("retired_date")
            if retired_reason is not None:
                item.retired_reason = retired_reason; changed.append("retired_reason")
            logs = 0
            if condition is not None and condition != old_condition:
                db.session.add(ItemLog(item_id=item.id, user_id=caller.user.id, event_type="condition_change",
                                       event_date=datetime.date.today(), old_condition=old_condition, new_condition=condition))
                logs += 1
            if event_type is not None:
                db.session.add(ItemLog(item_id=item.id, user_id=caller.user.id, event_type=event_type,
                                       event_date=_parse_date(event_date, "event_date") or datetime.date.today(),
                                       note=(note or "").strip() or None, cost=cost))
                logs += 1
            if not changed and not logs:
                raise ToolError("Nothing to record — pass lifecycle fields and/or an event_type.")
            db.session.commit()
            db.session.refresh(item)
            out = item_summary(item, caller)
            out.update({"updated_fields": changed, "log_entries_added": logs})
            return out
        return await run_sync(work)

    # ------------------------------------------------------------------ kits

    @write_tool(
        "create_kit",
        "Create a reusable kit (bundle of closet items, e.g. 'Cook kit'). `items` is a list of "
        "{item_id, quantity?}. Free accounts are limited to one kit. Requires a subscription.",
    )
    async def create_kit(name: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        def work():
            caller = require_writer()
            gated(_enforce_kit_limit, caller.user)
            specs = _parse_pack_item_specs(items)
            _own_items(caller, [s["item_id"] for s in specs])
            kit = Kit(name=name.strip() or "Kit", user_id=caller.user.id)
            db.session.add(kit)
            db.session.flush()
            for s in specs:
                db.session.add(KitItem(kit_id=kit.id, item_id=s["item_id"], quantity=s.get("quantity", 1)))
            db.session.commit()
            return _kit_detail(db.session.query(Kit).get(kit.id), caller)
        return await run_sync(work)

    @write_tool(
        "update_kit",
        "Rename a kit and/or REPLACE its item list. When `items` is given it is the complete new "
        "list — anything omitted is removed from the kit (the gear stays in the closet). Requires "
        "a subscription.",
        WRITE_IDEMPOTENT,
    )
    async def update_kit(kit_id: int, name: Optional[str] = None, items: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        def work():
            caller = require_writer()
            kit = db.session.query(Kit).filter_by(id=kit_id, user_id=caller.user.id).first()
            if kit is None:
                raise ToolError(f"Kit {kit_id} was not found. Call list_kits.")
            if name is not None and name.strip():
                kit.name = name.strip()
            if items is not None:
                specs = _parse_pack_item_specs(items)
                _own_items(caller, [s["item_id"] for s in specs])
                db.session.query(KitItem).filter_by(kit_id=kit.id).delete(synchronize_session=False)
                db.session.flush()
                for s in specs:
                    db.session.add(KitItem(kit_id=kit.id, item_id=s["item_id"], quantity=s.get("quantity", 1)))
            db.session.commit()
            db.session.expire(kit)
            return _kit_detail(db.session.query(Kit).get(kit.id), caller)
        return await run_sync(work)


# ---------------------------------------------------------------------------
# Helpers used by the tools above
# ---------------------------------------------------------------------------

def _trip_fields(caller: Caller, *, location, start_date, end_date, temperature_low, temperature_high,
                 distance, daily_elevation_gain, terrain, pace, conditions, notes, partial: bool = False) -> dict:
    """Validate and convert user-unit inputs to the canonical metric storage
    (km, m, °C). With partial=True, None means "leave alone"."""
    imperial_dist = (caller.user.unit_distance or "MI") == "MI"
    fahrenheit = (caller.user.unit_temperature or "F") == "F"
    out: dict[str, Any] = {}

    def put(key, value):
        if value is not None or not partial:
            out[key] = value

    if location is not None:
        put("location", location.strip() or None)
    if start_date is not None:
        put("start_date", _parse_date(start_date, "start_date"))
    if end_date is not None:
        put("end_date", _parse_date(end_date, "end_date"))
    if "start_date" in out and "end_date" in out and out["start_date"] and out["end_date"] and out["end_date"] < out["start_date"]:
        raise ToolError("end_date is before start_date.")
    for key, val in (("temp_min", temperature_low), ("temp_max", temperature_high)):
        if val is not None:
            put(key, round((val - 32) * 5 / 9) if fahrenheit else round(val))
    if distance is not None:
        put("distance", round(distance * 1.60934, 2) if imperial_dist else round(distance, 2))
    if daily_elevation_gain is not None:
        put("daily_elevation_gain", round(daily_elevation_gain * 0.3048, 1) if imperial_dist else round(daily_elevation_gain, 1))
    for key, val, allowed in (("terrain", terrain, TERRAIN_VALUES), ("pace", pace, PACE_VALUES), ("temp_category", conditions, CONDITION_VALUES)):
        if val is not None:
            v = val.strip().lower()
            if v not in allowed:
                raise ToolError(f"{key if key != 'temp_category' else 'conditions'} must be one of: {', '.join(allowed)}.")
            put(key, v)
    if notes is not None:
        put("notes", notes.strip() or None)
    return {k: v for k, v in out.items() if not (partial and v is None and k not in ("notes", "location"))}


def _parse_pack_item_specs(items: list[dict[str, Any]], allow_checked: bool = False) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise ToolError("items must be a non-empty list of {item_id, ...} objects.")
    specs = []
    for raw in items:
        if isinstance(raw, int):
            raw = {"item_id": raw}
        if not isinstance(raw, dict) or "item_id" not in raw:
            raise ToolError("Each entry in items needs an item_id.")
        spec: dict[str, Any] = {"item_id": int(raw["item_id"])}
        if "quantity" in raw and raw["quantity"] is not None:
            q = float(raw["quantity"])
            if q <= 0:
                raise ToolError("quantity must be greater than 0.")
            spec["quantity"] = q
        if "worn" in raw and raw["worn"] is not None:
            spec["worn"] = bool(raw["worn"])
        if allow_checked and "checked" in raw and raw["checked"] is not None:
            spec["checked"] = bool(raw["checked"])
        specs.append(spec)
    return specs


def _duplicate_candidates(caller: Caller, brand: str, product: str, variant: Optional[str]) -> str:
    """Human-readable list of likely duplicates, or '' when none.

    Matching uses the same normalization as the catalog enrichment task
    (punctuation/whitespace stripped, corporate suffixes dropped from brands),
    done in SQL for the catalog and in Python for the (small) closet.
    """
    b_key, p_key = normalize_brand(brand), normalize_name(product)
    if not b_key or not p_key:
        return ""
    brand_sql = func.regexp_replace(func.lower(CatalogProduct.brand_name), r"[^a-z0-9]+", "", "g")
    product_sql = func.regexp_replace(func.lower(CatalogProduct.product_name), r"[^a-z0-9]+", "", "g")
    cats = [c for c in (db.session.query(CatalogProduct)
                        .filter(CatalogProduct.status == "approved",
                                brand_sql.like(f"{b_key}%"),
                                # query contains the catalog name, or the catalog name contains the query
                                or_(product_sql.like(f"%{p_key}%"),
                                    literal(p_key).like(func.concat("%", product_sql, "%"))))
                        .order_by(CatalogProduct.product_name, CatalogProduct.variant_name).limit(12).all())
            if normalize_brand(c.brand_name) == b_key][:6]

    def product_matches(name: Optional[str]) -> bool:
        k = normalize_name(name)
        return bool(k) and (k == p_key or p_key in k or k in p_key)

    closet = [i for i in db.session.query(Item).filter_by(user_id=caller.user.id, deleted=False)
              .options(noload(Item.catalog_product)).all()
              if (i.brand and normalize_brand(i.brand.name) == b_key and i.product and product_matches(i.product.name))
              or normalize_name(i.name) == b_key + p_key][:6]
    if not cats and not closet:
        return ""
    parts = []
    if cats:
        parts.append("Catalog matches: " + "; ".join(
            f"{c.brand_name} {c.product_name}{' ' + c.variant_name if c.variant_name else ''} "
            f"(catalog_product_id {c.id}{', ' + weight_fields(to_grams(c.weight, c.weight_unit), caller)['display'] if c.weight else ''})"
            for c in cats) + ".")
    if closet:
        parts.append("Already in the closet: " + "; ".join(
            f"'{i.name}' — {i.brand.name if i.brand else ''} {i.product.name if i.product else ''} (item_id {i.id}{', archived' if i.removed else ''})".replace("  ", " ")
            for i in closet) + ".")
    return " ".join(parts)


def _kit_detail(kit: Kit, caller: Caller) -> dict[str, Any]:
    items, total = [], 0.0
    for ki in kit.items:
        if ki.item is None:
            continue
        g = to_grams(ki.item.weight, ki.item.unit) * float(ki.quantity or 1)
        total += g
        row = item_summary(ki.item, caller)
        row["quantity"] = float(ki.quantity or 1)
        items.append(row)
    return {"kit_id": kit.id, "name": kit.name, "total_weight": weight_fields(total, caller, big=True), "items": items}
