-- Catalog lock: lets a user detach an auto-assigned catalog product and
-- prevents enrichment / item updates from re-attaching one.
ALTER TABLE item
  ADD COLUMN catalog_locked BOOLEAN NOT NULL DEFAULT false;
