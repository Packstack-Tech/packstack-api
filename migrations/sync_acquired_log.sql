-- Sync "acquired" activity-log entries with item.acquired_date (backfill)
-- Before this, PUT /item logged "acquired" only the first time a date was set
-- and never re-dated it; the lifecycle endpoint, MCP tool and item create
-- never logged it. The app now keeps them in sync (sync_acquired_log in
-- app/utils/gear_lifecycle.py); this fixes existing rows the same way:
-- re-date each item's earliest "acquired" entry, or add one if missing.
-- Safe to re-run. Items with no acquired_date are left alone.

-- Preview (optional): how many rows each step will touch.
-- SELECT count(*) AS to_redate
-- FROM item i
-- JOIN LATERAL (
--   SELECT l.event_date FROM itemlog l
--   WHERE l.item_id = i.id AND l.event_type = 'acquired'
--   ORDER BY l.created_at, l.id LIMIT 1
-- ) first_log ON true
-- WHERE i.acquired_date IS NOT NULL AND first_log.event_date <> i.acquired_date;
--
-- SELECT count(*) AS to_insert
-- FROM item i
-- WHERE i.acquired_date IS NOT NULL
--   AND NOT EXISTS (SELECT 1 FROM itemlog l WHERE l.item_id = i.id AND l.event_type = 'acquired');

BEGIN;

-- Step 1: re-date out-of-sync entries
UPDATE itemlog l
SET event_date = i.acquired_date
FROM item i
WHERE l.item_id = i.id
  AND i.acquired_date IS NOT NULL
  AND l.event_date <> i.acquired_date
  AND l.id = (
    SELECT l2.id FROM itemlog l2
    WHERE l2.item_id = i.id AND l2.event_type = 'acquired'
    ORDER BY l2.created_at, l2.id
    LIMIT 1
  );

-- Step 2: add missing entries
INSERT INTO itemlog (item_id, user_id, event_type, event_date, created_at)
SELECT i.id, i.user_id, 'acquired', i.acquired_date, now()
FROM item i
WHERE i.acquired_date IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM itemlog l WHERE l.item_id = i.id AND l.event_type = 'acquired'
  );

COMMIT;
