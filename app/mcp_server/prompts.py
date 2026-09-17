"""MCP prompts: reusable templates the host can surface as slash commands.

A prompt is text handed to the model on the user's behalf; it tells the model
which tools to call and how to shape the answer. Keep them opinionated — the
point is to encode how experienced backpackers actually review a list.
"""

from mcp.server.mcpserver import MCPServer


def register_prompts(mcp: MCPServer) -> None:

    @mcp.prompt(
        name="shakedown",
        title="Gear shakedown",
        description="Review one trip's gear list the way r/Ultralight does: heavy outliers, redundancies, missing safety items, and whether the kit fits the conditions.",
    )
    def shakedown(trip: str) -> str:
        return f"""Give my Packstack trip "{trip}" a gear shakedown.

Steps:
1. Call get_me so you know my units and whether I'm subscribed.
2. Call list_trips and pick the trip matching "{trip}" (by title or location). If several match, ask me which.
3. Call get_trip with format="markdown" for the full list with precomputed totals. Use those totals as-is; do not re-add weights.

Then review the list against the trip details (dates, low temperature, distance per day, terrain, elevation) and report, in this order:
- The headline numbers: base weight, worn, consumables, total, and how the base weight compares to common thresholds (ultralight ≤ 10 lb / 4.5 kg, lightweight ≤ 20 lb / 9 kg).
- Heavy outliers: the 3–5 items contributing most to base weight, with a lighter alternative for each (use search_catalog to find real products with verified weights and cite the grams saved).
- Redundancies: items that duplicate a function.
- Missing items: check navigation, insulation for the forecast low, rain protection, first aid, repair, light, fire, water treatment and capacity, sun protection, and emergency shelter. Only flag what is genuinely absent from the list.
- Fit for conditions: sleep system and shelter vs the low temperature; footwear and traction vs terrain; food and fuel vs nights.
- Anything else you'd want to know before I go — ask me directly (budget, experience, resupply, water sources, non-negotiables).

Use my preferred units in prose. Be specific and skip generic advice."""

    @mcp.prompt(
        name="plan_pack",
        title="Plan a pack from my gear closet",
        description="Propose a complete pack for a trip using only gear the user already owns, then ask before making changes.",
    )
    def plan_pack(trip: str) -> str:
        return f"""Help me build a pack for my Packstack trip "{trip}" from gear I already own.

Steps:
1. Call get_me, then list_trips to find the trip "{trip}" and get_trip to see its details and what is already packed.
2. Call search_gear with status="active" (page through with a higher limit if needed) so you know everything in my closet, and list_kits for bundles I keep together.
3. Propose a full pack for the conditions (low temperature, nights, distance, terrain), organized by category, using only items from my closet. For each category say which item you chose and why; where I own several options, pick the lightest that suits the conditions and mention the alternative.
4. List gaps — functions no item in my closet covers — and suggest catalog products via search_catalog.
5. Show the projected base weight and total.

Do not change anything yet. End by asking whether I want you to apply it to the trip."""

    @mcp.prompt(
        name="lighten",
        title="Lighten a pack",
        description="Find the biggest weight savings for a trip, ranked by grams saved, using real catalog alternatives.",
    )
    def lighten(trip: str, target_base_weight: str = "") -> str:
        target = f" My target base weight is {target_base_weight}." if target_base_weight else ""
        return f"""Help me lighten my Packstack trip "{trip}".{target}

Steps:
1. Call get_me, list_trips (find "{trip}") and get_trip with format="json".
2. Rank every non-worn, non-consumable item by weight. For the top 8, look for a lighter replacement with search_catalog and report grams saved, the replacement's weight, and a one-line tradeoff (durability, warmth, price if known).
3. Separately list items you would simply leave behind for these conditions, with grams saved.
4. Sum the savings and show the projected new base weight{" against my target" if target_base_weight else ""}.

Prefer fewer, larger savings over many tiny ones. Use my preferred units."""
