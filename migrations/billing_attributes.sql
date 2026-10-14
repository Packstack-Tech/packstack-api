-- Billing details copied from RevenueCat onto the user (utils/user_sync.py).
-- Nullable, no defaults: a catalog-only change on Postgres, safe on the live
-- table.
--
-- Run this BEFORE deploying the API that maps these columns. The ORM selects
-- every mapped column, so an API that knows them running against a table
-- without them fails every user query.

ALTER TABLE "user"
  ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(20),
  ADD COLUMN IF NOT EXISTS subscription_plan VARCHAR(20),
  ADD COLUMN IF NOT EXISTS subscription_product_id VARCHAR(200),
  ADD COLUMN IF NOT EXISTS subscription_store VARCHAR(20),
  ADD COLUMN IF NOT EXISTS subscription_expires_at TIMESTAMP,
  ADD COLUMN IF NOT EXISTS subscription_started_at TIMESTAMP,
  ADD COLUMN IF NOT EXISTS subscription_synced_at TIMESTAMP;
