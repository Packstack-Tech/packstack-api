-- Legacy Product -> CatalogProduct mapping for merged sibling products
-- (Exos 38 / 48 / 58 -> Exos). See claude/catalog-variants-pivot.md.
CREATE TABLE catalogproduct_legacy_alias (
  legacy_product_id  INTEGER PRIMARY KEY REFERENCES product(id),
  catalog_product_id INTEGER NOT NULL REFERENCES catalogproduct(id),
  catalog_variant_id INTEGER REFERENCES catalogvariant(id),
  created_at         TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX ix_catalogproduct_legacy_alias_catalog_product_id
  ON catalogproduct_legacy_alias (catalog_product_id);
