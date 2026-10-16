-- Ad attribution + conversion log (utils/ad_conversions.py).
-- New tables only, so order doesn't matter: main.py's create_all also creates
-- them on startup. Kept here as the record, and for environments that don't
-- run create_all.

CREATE TABLE IF NOT EXISTS userattribution (
  user_id INTEGER PRIMARY KEY REFERENCES "user"(id),
  gclid VARCHAR(255),
  gbraid VARCHAR(255),
  wbraid VARCHAR(255),
  utm_source VARCHAR(255),
  utm_medium VARCHAR(255),
  utm_campaign VARCHAR(255),
  utm_content VARCHAR(255),
  utm_term VARCHAR(255),
  landing_path VARCHAR(255),
  clicked_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS adconversion (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES "user"(id),
  kind VARCHAR(20) NOT NULL,
  transaction_id VARCHAR(255) NOT NULL,
  value NUMERIC,
  currency VARCHAR(3),
  event_at TIMESTAMP NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  sent_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT uq_adconversion_kind_txn UNIQUE (kind, transaction_id)
);
CREATE INDEX IF NOT EXISTS ix_adconversion_user_id ON adconversion (user_id);
