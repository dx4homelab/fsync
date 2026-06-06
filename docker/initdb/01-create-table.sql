-- initdb script: central file catalog keyed by (source, path)

CREATE TABLE IF NOT EXISTS file_index (
  id SERIAL PRIMARY KEY,
  source TEXT NOT NULL,                -- which root/host this entry came from
  path TEXT NOT NULL,                  -- relative path within that source
  metadata JSONB NOT NULL,             -- size, mtime, hash, inode, nlink, ...
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (source, path)                -- required for the ON CONFLICT upsert
);

-- GIN index to accelerate containment queries on the JSONB metadata
CREATE INDEX IF NOT EXISTS idx_file_index_metadata_gin ON file_index USING gin (metadata);

-- Expression index on the content hash for fast dedup / lookup across sources
CREATE INDEX IF NOT EXISTS idx_file_index_metadata_hash ON file_index ((metadata->>'hash'));

-- Example queries:
-- 1) Everything from one source:
--    SELECT path FROM file_index WHERE source = '/mnt/vhd';
-- 2) Find identical content anywhere (same hash across sources):
--    SELECT source, path FROM file_index WHERE metadata->>'hash' = '<hex>';
-- 3) Files larger than 100MB:
--    SELECT source, path FROM file_index WHERE (metadata->>'size')::bigint > 104857600;
