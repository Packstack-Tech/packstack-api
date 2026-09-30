-- Gear quantity + over-pack settings.
-- ADD COLUMN ... DEFAULT is catalog-only on Postgres >= 11 (no table rewrite),
-- so this is safe to run against the live item table before the code deploys.

-- Owned quantity of a closet item. Old clients never send it; the API treats
-- an omitted value as "unchanged" so the default here is never clobbered.
ALTER TABLE item
  ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1;

ALTER TABLE item
  ADD CONSTRAINT ck_item_quantity_positive CHECK (quantity >= 1);

-- Over-pack check: whether a trip packing more of an item than the user owns
-- is ignored, warned about, or blocks the add. Enforced client-side only.
ALTER TABLE "user"
  ADD COLUMN overpack_mode VARCHAR(10) NOT NULL DEFAULT 'warn',
  ADD COLUMN overpack_include_consumables BOOLEAN NOT NULL DEFAULT true;
