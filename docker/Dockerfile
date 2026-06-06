FROM postgres:15-alpine

# This Dockerfile is intentionally simple — it uses the official Postgres image.
# Initialization SQL files placed in /docker-entrypoint-initdb.d/ will be executed
# when the container is created for the first time.

COPY initdb/ /docker-entrypoint-initdb.d/

EXPOSE 5432

CMD ["postgres"]
