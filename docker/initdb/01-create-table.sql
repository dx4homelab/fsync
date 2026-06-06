-- initdb script: create a table using JSONB

CREATE TABLE IF NOT EXISTS file_index (
  id SERIAL PRIMARY KEY,
  path TEXT NOT NULL,
  metadata JSONB NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Example insert
INSERT INTO file_index (path, metadata) VALUES ('/example.txt', '{"size": 123, "hash": "abc123"}'::jsonb);

-- Create a GIN index to accelerate searches/containment queries on JSONB
CREATE INDEX IF NOT EXISTS idx_file_index_metadata_gin ON file_index USING gin (metadata);

-- Create an expression index on a commonly queried JSON key (hash) for fast lookup
CREATE INDEX IF NOT EXISTS idx_file_index_metadata_hash ON file_index ((metadata->>'hash'));

-- Example queries you can run to search JSON data:
-- 1) Find rows where metadata contains a key/value (containment):
--    SELECT * FROM file_index WHERE metadata @> '{"hash": "abc123"}';

-- 2) Find rows where a JSON key exists:
--    SELECT * FROM file_index WHERE metadata ? 'size';

-- 3) Extract and compare a JSON scalar value:
--    SELECT * FROM file_index WHERE (metadata->>'size')::int > 100;

-- 4) Use the expression index to lookup by hash rapidly:
--    SELECT * FROM file_index WHERE metadata->>'hash' = 'abc123';
