-- Rename the shared "Footware" category (seed typo) to "Footwear".
-- Sept 2026. Run as one transaction, then deploy the api and public site.
-- (GET /resources/seed was removed in the same change.)

BEGIN;

-- 1. The shared category row. Every user's ItemCategory points at it by id,
--    so this renames it for everyone who uses it.
UPDATE category
   SET name = 'Footwear'
 WHERE user_id IS NULL
   AND name = 'Footware';

-- 2. Catalog products. Quick-add matches category_suggestion to the shared
--    category by name; leaving these would create a private "Footware"
--    category for every user who quick-adds a shoe, sock or gaiter.
UPDATE catalogproduct
   SET category_suggestion = 'Footwear'
 WHERE category_suggestion = 'Footware';

-- 3. Per-user lifespan overrides are keyed by category name, unique per user.
--    Where a user already has a "Footwear" override (their own custom
--    category), keep it and drop the stale "Footware" one.
DELETE FROM categorybenchmark old
 USING categorybenchmark cur
 WHERE old.user_id = cur.user_id
   AND old.category_name = 'Footware'
   AND cur.category_name = 'Footwear';

UPDATE categorybenchmark
   SET category_name = 'Footwear'
 WHERE category_name = 'Footware';

-- Sanity checks: expect 1, 0, 0.
SELECT count(*) AS shared_footwear   FROM category WHERE user_id IS NULL AND name = 'Footwear';
SELECT count(*) AS catalog_footware  FROM catalogproduct WHERE category_suggestion = 'Footware';
SELECT count(*) AS bench_footware    FROM categorybenchmark WHERE category_name = 'Footware';

COMMIT;
