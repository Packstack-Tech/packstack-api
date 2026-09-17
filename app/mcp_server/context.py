"""Per-call helpers shared by every tool: who is calling, in what units,
and how to run synchronous SQLAlchemy work without blocking the event loop.

Tools are `async def` and do their database work through `run_sync`, which
hands the callable to anyio's worker thread pool. anyio copies the current
context into the thread, so both fastapi_sqlalchemy's request-scoped
`db.session` and the SDK's `auth_context_var` are visible inside.
"""

import functools
from dataclasses import dataclass
from typing import Callable, TypeVar

import anyio
from fastapi_sqlalchemy import db
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError as _SDKToolError

from models.base import User
from oauth import tokens as oauth_tokens
from utils.weight import convert_weight

T = TypeVar("T")

GRAMS_PER_UNIT = {"g": 1.0, "kg": 1000.0, "oz": 28.3495, "lb": 453.592}


class ToolError(_SDKToolError):
    """Raised inside a tool; the SDK turns it into an isError tool result the
    model can read and relay. Keep messages actionable for the end user.

    Must subclass the SDK's ToolError: any other exception type is reported
    to the client as an opaque "Error executing tool" with the message
    withheld (so crashes never leak internals), which is exactly wrong for
    the "trip not found, call list_trips" class of message.
    """


async def run_sync(fn: Callable[..., T], *args, **kwargs) -> T:
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


@dataclass
class Caller:
    user: User
    scopes: list[str]
    client_id: str

    @property
    def unit(self) -> str:
        """Display unit for per-item weights, from the user's preference."""
        return "oz" if (self.user.unit_weight or "METRIC") == "IMPERIAL" else "g"

    @property
    def big_unit(self) -> str:
        """Display unit for totals."""
        return "lb" if self.unit == "oz" else "kg"

    @property
    def can_write(self) -> bool:
        return oauth_tokens.SCOPE_WRITE in self.scopes


def current_caller() -> Caller:
    """Resolve the authenticated user for this tool call. Sync — call via
    run_sync or from inside another sync function."""
    token = get_access_token()
    if token is None or token.subject is None:
        raise ToolError("Not authenticated.")
    user = db.session.query(User).filter_by(id=int(token.subject)).first()
    if user is None or user.deactivated or user.banned:
        raise ToolError("This Packstack account is not available.")
    return Caller(user=user, scopes=list(token.scopes), client_id=token.client_id)


# ---------------------------------------------------------------------------
# Weight formatting. Every weight a tool returns carries grams (for arithmetic
# the model can trust) and the user's display unit (for talking to the user).
# ---------------------------------------------------------------------------

def to_grams(weight, unit) -> float:
    if weight is None:
        return 0.0
    return float(weight) * GRAMS_PER_UNIT.get(unit or "g", 1.0)


def weight_fields(grams: float, caller: Caller, big: bool = False) -> dict:
    unit = caller.big_unit if big else caller.unit
    value = convert_weight(grams, "g", unit)
    return {
        "grams": round(grams, 1),
        "display": f"{value:,.2f} {unit}" if big else f"{value:,.1f} {unit}",
    }


def item_summary(item, caller: Caller) -> dict:
    """The shape every tool uses for a gear item. Never includes internal
    foreign keys, sort orders or removed/deleted flags."""
    brand = item.brand.name if item.brand else None
    product = item.product.name if item.product else None
    variant = item.product_variant.name if item.product_variant else None
    category = (item.category.category.name
                if item.category and item.category.category else None)
    grams = to_grams(item.weight, item.unit)
    return {
        "item_id": item.id,
        "name": item.name,
        "brand": brand,
        "product": " ".join(p for p in (product, variant) if p) or None,
        "category": category or "Uncategorized",
        "weight": weight_fields(grams, caller),
        "weight_entered": item.weight is not None,
        "consumable": bool(item.consumable),
        "calories": float(item.calories) if item.calories else None,
        "price": float(item.price) if item.price else None,
        "notes": item.notes or None,
        "product_url": item.product_url or None,
        "archived": bool(item.removed),
        "status": item.status or "active",
    }
