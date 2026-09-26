-- Catalog variants pivot — STAGE 1 (additive). Safe to run before the
-- variant-aware code ships. Then run:
--   python -m catalog_pivot.migrate --env prod --execute   (workshop)
-- Stage 2 (catalog_variants_stage2.sql) runs only after the new code is deployed.

CREATE TABLE catalogvariant (
  id SERIAL PRIMARY KEY,
  catalog_product_id INTEGER NOT NULL REFERENCES catalogproduct(id),
  name VARCHAR(250) NOT NULL,
  name_key VARCHAR(250) NOT NULL,
  weight NUMERIC,
  weight_unit VARCHAR(10),
  kcal INTEGER,
  image_url VARCHAR(1000),
  kind VARCHAR(20),
  aliases JSON,
  sort_order INTEGER DEFAULT 0,
  hidden BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now(),
  CONSTRAINT uq_catalogvariant_product_namekey UNIQUE (catalog_product_id, name_key)
);
CREATE INDEX ix_catalogvariant_catalog_product_id ON catalogvariant (catalog_product_id);

ALTER TABLE catalogproduct
  ADD COLUMN brand_key VARCHAR(100),
  ADD COLUMN product_key VARCHAR(250);
CREATE INDEX ix_catalogproduct_brand_key ON catalogproduct (brand_key);

-- One live product per normalized identity. Partial so old variant rows
-- (status = 'migrated', deleted in stage 2) don't collide with their base.
CREATE UNIQUE INDEX uq_catalogproduct_keys
  ON catalogproduct (brand_key, product_key)
  WHERE status <> 'migrated' AND brand_key IS NOT NULL;

ALTER TABLE item
  ADD COLUMN catalog_variant_id INTEGER REFERENCES catalogvariant(id);
