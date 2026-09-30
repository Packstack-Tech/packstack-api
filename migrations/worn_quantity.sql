-- Worn quantity: how many units of a pack item are worn (e.g. 1 of 5 shirts).
-- PackItem.worn stays and is kept equal to (worn_quantity > 0) by the API.
--
-- Run in TWO parts, at different times:

-- Part 1: BEFORE the new API deploys. Catalog-only on Postgres >= 11.
-- The old API doesn't know the column; its writes land with the default 0.
ALTER TABLE packitem
  ADD COLUMN worn_quantity NUMERIC NOT NULL DEFAULT 0;

-- Part 2: AFTER the new API is live (the old API keeps writing worn rows with
-- worn_quantity = 0 until it is replaced). Existing worn rows become "one unit
-- worn" -- the rule the web and mobile apps already showed users. Idempotent;
-- safe to re-run. The new API also reads (worn AND worn_quantity = 0) as one
-- unit, so the gap between deploy and this statement shows correct numbers.
--
UPDATE packitem
   SET worn_quantity = LEAST(quantity, 1)
 WHERE worn AND worn_quantity = 0;
