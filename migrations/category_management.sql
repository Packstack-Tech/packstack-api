-- Category management cleanup (Sept 2026)
--
-- Run AFTER rename_footwear.sql, and BEFORE deploying the API that ships with
-- it: the new code expects category.forked_from_id to exist.
--
-- One transaction, safe to re-run. Merges every kind of duplicate that made the item form's
-- category picker show more (and repeated) entries than Manage Categories, then
-- adds the constraints that stop them coming back:
--
--   1. category.forked_from_id column
--   2. normalize whitespace in names
--   3. duplicate shared categories         -> lowest id
--   4. user categories named like a shared -> the shared one
--   5. duplicate names within one user     -> that user's lowest id
--   6. duplicate itemcategory rows          -> lowest id per (user, category)
--   7. user categories with no itemcategory -> given one, so they show up in
--                                              the user's list and can be deleted
--   8. unique indexes
--
-- Every merge moves items; nothing is uncategorized or deleted except the
-- now-empty duplicate rows. NOTICE lines report what each step did.

BEGIN;

-- 1 -------------------------------------------------------------------------
ALTER TABLE category ADD COLUMN IF NOT EXISTS forked_from_id INTEGER REFERENCES category(id);

-- 2 -------------------------------------------------------------------------
UPDATE category
   SET name = regexp_replace(btrim(name), '\s+', ' ', 'g')
 WHERE name IS DISTINCT FROM regexp_replace(btrim(name), '\s+', ' ', 'g');

-- Lifespan overrides are keyed by category name; keep them matching.
DELETE FROM categorybenchmark b
 WHERE b.category_name IS DISTINCT FROM regexp_replace(btrim(b.category_name), '\s+', ' ', 'g')
   AND EXISTS (SELECT 1 FROM categorybenchmark t
                WHERE t.user_id = b.user_id
                  AND t.category_name = regexp_replace(btrim(b.category_name), '\s+', ' ', 'g'));
UPDATE categorybenchmark
   SET category_name = regexp_replace(btrim(category_name), '\s+', ' ', 'g')
 WHERE category_name IS DISTINCT FROM regexp_replace(btrim(category_name), '\s+', ' ', 'g');

-- Merge `loser` into `keeper` for every user who has either, then delete loser.
CREATE FUNCTION pg_temp.merge_category(loser INTEGER, keeper INTEGER) RETURNS VOID AS $$
DECLARE
    loser_name  TEXT;
    keeper_name TEXT;
    loser_owner INTEGER;
BEGIN
    SELECT name, user_id INTO loser_name, loser_owner FROM category WHERE id = loser;
    SELECT name INTO keeper_name FROM category WHERE id = keeper;

    -- Users who have both: move items onto their keeper row, drop the loser row.
    UPDATE item i
       SET category_id = (SELECT min(k.id) FROM itemcategory k
                           WHERE k.user_id = l.user_id AND k.category_id = keeper)
      FROM itemcategory l
     WHERE i.category_id = l.id
       AND l.category_id = loser
       AND EXISTS (SELECT 1 FROM itemcategory k
                    WHERE k.user_id = l.user_id AND k.category_id = keeper);

    DELETE FROM itemcategory l
     WHERE l.category_id = loser
       AND EXISTS (SELECT 1 FROM itemcategory k
                    WHERE k.user_id = l.user_id AND k.category_id = keeper);

    -- Users who only have the loser: point their row at the keeper.
    UPDATE itemcategory SET category_id = keeper WHERE category_id = loser;

    -- Lifespan overrides are keyed by name; carry the loser's across.
    IF loser_owner IS NOT NULL AND loser_name IS DISTINCT FROM keeper_name THEN
        DELETE FROM categorybenchmark b
         WHERE b.user_id = loser_owner AND b.category_name = loser_name
           AND EXISTS (SELECT 1 FROM categorybenchmark t
                        WHERE t.user_id = loser_owner AND t.category_name = keeper_name);
        UPDATE categorybenchmark SET category_name = keeper_name
         WHERE user_id = loser_owner AND category_name = loser_name;
    END IF;

    UPDATE category SET forked_from_id = keeper WHERE forked_from_id = loser;
    DELETE FROM category WHERE id = loser;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    r RECORD;
    n INTEGER;
BEGIN
    -- 3 ---------------------------------------------------------------------
    n := 0;
    FOR r IN
        SELECT c.id AS loser, k.keeper
          FROM category c
          JOIN (SELECT lower(name) AS lname, min(id) AS keeper
                  FROM category WHERE user_id IS NULL GROUP BY lower(name)) k
            ON k.lname = lower(c.name)
         WHERE c.user_id IS NULL AND c.id <> k.keeper
    LOOP
        PERFORM pg_temp.merge_category(r.loser, r.keeper);
        n := n + 1;
    END LOOP;
    RAISE NOTICE '3. duplicate shared categories merged: %', n;

    -- 4 ---------------------------------------------------------------------
    n := 0;
    FOR r IN
        SELECT c.id AS loser, s.id AS keeper
          FROM category c
          JOIN category s ON s.user_id IS NULL AND lower(s.name) = lower(c.name)
         WHERE c.user_id IS NOT NULL
    LOOP
        PERFORM pg_temp.merge_category(r.loser, r.keeper);
        n := n + 1;
    END LOOP;
    RAISE NOTICE '4. user categories merged into same-named shared: %', n;

    -- 5 ---------------------------------------------------------------------
    n := 0;
    FOR r IN
        SELECT c.id AS loser, k.keeper
          FROM category c
          JOIN (SELECT user_id, lower(name) AS lname, min(id) AS keeper
                  FROM category WHERE user_id IS NOT NULL
                 GROUP BY user_id, lower(name)) k
            ON k.user_id = c.user_id AND k.lname = lower(c.name)
         WHERE c.id <> k.keeper
    LOOP
        PERFORM pg_temp.merge_category(r.loser, r.keeper);
        n := n + 1;
    END LOOP;
    RAISE NOTICE '5. duplicate user categories merged: %', n;

    -- 6 ---------------------------------------------------------------------
    UPDATE item i
       SET category_id = d.keeper
      FROM (SELECT id, min(id) OVER (PARTITION BY user_id, category_id) AS keeper
              FROM itemcategory) d
     WHERE i.category_id = d.id AND d.id <> d.keeper;
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE '6. items moved off duplicate itemcategory rows: %', n;

    DELETE FROM itemcategory ic
     USING (SELECT id, min(id) OVER (PARTITION BY user_id, category_id) AS keeper
              FROM itemcategory) d
     WHERE ic.id = d.id AND d.id <> d.keeper;
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE '6. duplicate itemcategory rows deleted: %', n;

    -- 7 ---------------------------------------------------------------------
    INSERT INTO itemcategory (user_id, category_id, sort_order)
    SELECT c.user_id, c.id,
           COALESCE((SELECT max(sort_order) FROM itemcategory x WHERE x.user_id = c.user_id), -1)
             + row_number() OVER (PARTITION BY c.user_id ORDER BY c.id)
      FROM category c
     WHERE c.user_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM itemcategory ic
                        WHERE ic.user_id = c.user_id AND ic.category_id = c.id);
    GET DIAGNOSTICS n = ROW_COUNT;
    RAISE NOTICE '7. orphaned user categories added to their owner''s list: %', n;
END $$;

-- 8 -------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_category_shared_name
    ON category (lower(name)) WHERE user_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_category_user_name
    ON category (user_id, lower(name)) WHERE user_id IS NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_itemcategory_user_category') THEN
        ALTER TABLE itemcategory
            ADD CONSTRAINT uq_itemcategory_user_category UNIQUE (user_id, category_id);
    END IF;
END $$;

COMMIT;
