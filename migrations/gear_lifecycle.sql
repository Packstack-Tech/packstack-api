-- Gear Lifecycle Migration
-- Run these statements in order against the production database.
-- Step 5 (DROP COLUMN wishlist) should only be run AFTER the new code is deployed.

-- Step 1: Add lifecycle + catalog columns to item
ALTER TABLE item
  ADD COLUMN acquired_date DATE,
  ADD COLUMN acquisition_type VARCHAR(20),
  ADD COLUMN purchase_retailer VARCHAR(200),
  ADD COLUMN condition VARCHAR(20),
  ADD COLUMN status VARCHAR(20) DEFAULT 'active',
  ADD COLUMN retired_date DATE,
  ADD COLUMN retired_reason VARCHAR(50),
  ADD COLUMN replaced_by_id INTEGER REFERENCES item(id),
  ADD COLUMN catalog_product_id INTEGER REFERENCES catalogproduct(id);

-- Step 2: Create new tables
CREATE TABLE itemlog (
  id SERIAL PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES item(id),
  user_id INTEGER NOT NULL REFERENCES "user"(id),
  event_type VARCHAR(30) NOT NULL,
  note TEXT,
  event_date DATE NOT NULL,
  old_condition VARCHAR(20),
  new_condition VARCHAR(20),
  old_weight NUMERIC,
  new_weight NUMERIC,
  cost NUMERIC,
  created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX ix_itemlog_item_date ON itemlog (item_id, event_date);

CREATE TABLE categorybenchmark (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES "user"(id),
  category_name VARCHAR(50) NOT NULL,
  lifespan_years NUMERIC,
  expected_nights NUMERIC,
  expected_distance NUMERIC,
  distance_unit VARCHAR(10),
  CONSTRAINT uq_user_category_benchmark UNIQUE (user_id, category_name)
);

-- Step 3: Migrate wishlist -> status
UPDATE item SET status = 'wishlist' WHERE wishlist = true;
UPDATE item SET status = 'active' WHERE status IS NULL;

-- Step 4: Link existing items to catalog products
UPDATE item SET catalog_product_id = cp.id
FROM catalogproduct cp
WHERE item.brand_id = cp.brand_id
  AND item.product_id = cp.product_id
  AND (item.product_variant_id = cp.product_variant_id
       OR (item.product_variant_id IS NULL AND cp.product_variant_id IS NULL))
  AND cp.status = 'approved'
  AND item.catalog_product_id IS NULL;

-- Step 5: Drop wishlist column (run AFTER code deploy)
-- ALTER TABLE item DROP COLUMN wishlist;
