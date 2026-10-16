-- Pack versions: optimistic concurrency for PUT /pack/{id}, plus a snapshot
-- table recording what a save removed.
--
-- Run BEFORE the new API deploys. The old API never reads or writes these, so
-- it is unaffected; the triggers just start counting.
--
-- Keep the trigger bodies in step with the DDL in models/base.py, which
-- installs the same thing when create_all builds a fresh database.

ALTER TABLE pack ADD COLUMN version INTEGER NOT NULL DEFAULT 0;

-- Title or trip changes bump the version.
CREATE OR REPLACE FUNCTION pack_bump_version() RETURNS trigger AS $$
BEGIN
  IF (NEW.title, NEW.trip_id) IS DISTINCT FROM (OLD.title, OLD.trip_id) THEN
    NEW.version := OLD.version + 1;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER pack_bump_version BEFORE UPDATE ON pack
FOR EACH ROW EXECUTE FUNCTION pack_bump_version();

-- Any item insert/update/delete bumps its pack's version, including bulk
-- query deletes and the MCP tools, which bypass the ORM's unit of work.
CREATE OR REPLACE FUNCTION packitem_bump_pack_version() RETURNS trigger AS $$
BEGIN
  IF TG_OP <> 'INSERT' THEN
    UPDATE pack SET version = version + 1 WHERE id = OLD.pack_id;
  END IF;
  IF TG_OP = 'INSERT' OR NEW.pack_id IS DISTINCT FROM OLD.pack_id THEN
    UPDATE pack SET version = version + 1 WHERE id = NEW.pack_id;
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER packitem_bump_pack_version AFTER INSERT OR UPDATE OR DELETE ON packitem
FOR EACH ROW EXECUTE FUNCTION packitem_bump_pack_version();

CREATE TABLE packsnapshot (
  id SERIAL PRIMARY KEY,
  pack_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  title VARCHAR(500),
  items JSON NOT NULL,
  removed_item_ids JSON NOT NULL,
  versioned BOOLEAN NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);
CREATE INDEX ix_packsnapshot_pack_id ON packsnapshot (pack_id);
