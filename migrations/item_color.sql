-- Item colorway: free-form, aesthetic only. Lives on the item, not the
-- catalog, so "Black" never becomes a CatalogVariant again.
ALTER TABLE item
  ADD COLUMN color VARCHAR(80);
