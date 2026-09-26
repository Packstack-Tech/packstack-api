-- Catalog variants pivot — STAGE 2 (destructive). Run ONLY after:
--   1. stage 1 SQL + catalog_pivot.migrate --execute have run, and
--   2. the variant-aware API/web/mobile code is deployed
--      (nothing reads catalogproduct.variant_name / product_variant_id).

DELETE FROM catalogproduct WHERE status = 'migrated';

ALTER TABLE catalogproduct DROP CONSTRAINT uq_catalog_brand_product_variant;
ALTER TABLE catalogproduct
  DROP COLUMN variant_name,
  DROP COLUMN product_variant_id;

-- Replace the partial index with a full one now that migrated rows are gone.
DROP INDEX uq_catalogproduct_keys;
ALTER TABLE catalogproduct ALTER COLUMN brand_key SET NOT NULL;
ALTER TABLE catalogproduct ALTER COLUMN product_key SET NOT NULL;
CREATE UNIQUE INDEX uq_catalogproduct_keys ON catalogproduct (brand_key, product_key);
