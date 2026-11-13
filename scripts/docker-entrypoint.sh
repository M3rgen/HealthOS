#!/bin/sh
# Compose runs migrations in its one-shot service; standalone legacy opt-in only.
set -eu

if [ "${HEALTHOS_MIGRATE_ON_START:-0}" = "1" ]; then
    alembic upgrade head
fi

exec "$@"
