-- Drive schema (Postgres).
--
-- Applied by: npm run db:init   (reads DATABASE_URL from the environment)
-- Safe to re-run: every statement is IF NOT EXISTS.
--
-- Kept deliberately small: this is the storage a drive needs (people, folders,
-- files), not a whole file-sharing product. Byte contents live in object
-- storage; these tables store only what can be queried.

CREATE TABLE IF NOT EXISTS users (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email       TEXT UNIQUE NOT NULL,
  display_name TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS folders (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  parent_id   UUID REFERENCES folders(id) ON DELETE CASCADE,
  owner_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS folders_owner_idx ON folders(owner_id);
CREATE INDEX IF NOT EXISTS folders_parent_idx ON folders(parent_id);

CREATE TABLE IF NOT EXISTS files (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  folder_id   UUID REFERENCES folders(id) ON DELETE CASCADE,
  owner_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  size_bytes  BIGINT NOT NULL DEFAULT 0,
  mime_type   TEXT,
  storage_key TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS files_folder_idx ON files(folder_id);
CREATE INDEX IF NOT EXISTS files_owner_idx ON files(owner_id);

-- A name must be unique inside its folder, not globally: two people may each
-- have a folder called "Documents".
CREATE UNIQUE INDEX IF NOT EXISTS folders_unique_name_in_parent
  ON folders(owner_id, COALESCE(parent_id, '00000000-0000-0000-0000-000000000000'::uuid), name);

CREATE UNIQUE INDEX IF NOT EXISTS files_unique_name_in_folder
  ON files(folder_id, name);