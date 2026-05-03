-- Canonical Units Migration
-- Converts existing trip and hiker profile data to canonical metric units.
-- All numeric trip fields will be stored as: km, meters, °C
-- All hiker profile fields will be stored as: kg, cm
--
-- IMPORTANT: Run this migration BEFORE deploying the new frontend code.
-- The new frontend expects all DB values to be in metric.
--
-- This migration is idempotent for metric users (multiplying by 1 effectively).
-- For imperial users, it converts their data to metric.

BEGIN;

-- Step 1: Convert trip distance (miles -> km) and elevation (feet -> meters)
-- for users whose unit_distance is 'MI'
UPDATE trip
SET
  distance = ROUND(CAST(distance * 1.60934 AS NUMERIC), 2),
  daily_elevation_gain = ROUND(CAST(daily_elevation_gain * 0.3048 AS NUMERIC), 0)
FROM "user"
WHERE trip.user_id = "user".id
  AND "user".unit_distance = 'MI'
  AND (trip.distance IS NOT NULL OR trip.daily_elevation_gain IS NOT NULL);

-- Step 2: Convert trip temperatures (°F -> °C)
-- for users whose unit_temperature is 'F' or NULL (default is F)
UPDATE trip
SET
  temp_min = ROUND(CAST((temp_min - 32) * 5.0 / 9.0 AS NUMERIC), 0),
  temp_max = ROUND(CAST((temp_max - 32) * 5.0 / 9.0 AS NUMERIC), 0)
FROM "user"
WHERE trip.user_id = "user".id
  AND (COALESCE("user".unit_temperature, 'F') = 'F')
  AND (trip.temp_min IS NOT NULL OR trip.temp_max IS NOT NULL);

-- Step 3: Convert hiker profile weight (lb -> kg) and height (in -> cm)
-- for users whose unit_weight is 'IMPERIAL'
UPDATE hikerprofile
SET
  weight = ROUND(CAST(weight * 0.453592 AS NUMERIC), 1),
  height = ROUND(CAST(height * 2.54 AS NUMERIC), 1)
FROM "user"
WHERE hikerprofile.user_id = "user".id
  AND "user".unit_weight = 'IMPERIAL'
  AND (hikerprofile.weight IS NOT NULL OR hikerprofile.height IS NOT NULL);

-- Step 4: Set unit_temperature for users who have it NULL/empty
-- so the new settings UI has a known starting value
UPDATE "user"
SET unit_temperature = 'F'
WHERE unit_temperature IS NULL OR unit_temperature = '';

COMMIT;
