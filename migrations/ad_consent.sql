-- Consent banner for EEA/UK/CH visitors (utils/ad_conversions.py): "granted"
-- when the visitor accepted ad measurement, sent to Google with each
-- conversion. create_all doesn't add columns to an existing table, so run this
-- once on databases that already have userattribution.

ALTER TABLE userattribution ADD COLUMN IF NOT EXISTS ad_consent VARCHAR(20);
