This folder contains a small Docker setup to run PostgreSQL with a JSONB-backed table.

Files:
- Dockerfile: builds from the official postgres:15-alpine and copies the initdb scripts.
- docker-compose.yml: service `db` exposing port 5432 with a persistent volume.
- initdb/01-create-table.sql: creates a sample `file_index` table with a JSONB `metadata` column.

Quick start:

```bash
# from the project root
cd docker
docker compose up --build
```

The DB will be available on localhost:5432 with credentials:
- user: fsync
- password: fsyncpass
- database: fsyncdb

You can connect with psql:

```bash
psql postgresql://fsync:fsyncpass@localhost:5432/fsyncdb
```

The init script creates a sample row in `file_index`.

JSONB and indexes

The init SQL creates a GIN index on the `metadata` JSONB column and an expression index on `metadata->>'hash'`.
These indexes speed up containment queries (using `@>`) and equality lookups on the `hash` key.

Recreating the database

The init scripts run only when the database is first created. To re-run them (for example after changing `initdb/01-create-table.sql`), remove the Docker volume and restart:

```bash
docker compose down
docker volume rm fsync_pgdata
docker compose up --build
```

Replace `fsync_pgdata` with the actual volume name if different (check `docker volume ls`).
