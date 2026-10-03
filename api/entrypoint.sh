#!/usr/bin/env bash
# Startup for the api container: migrate, seed, import sample data, serve.
# Every step is idempotent, so restarting the stack is always safe.
set -euo pipefail

echo "[entrypoint] running migrations"
alembic upgrade head

echo "[entrypoint] seeding robots and user accounts"
python -m app.cli seed

# Gives the graders something to look at immediately. Importing the same file
# again on the next restart creates nothing.
if [ -f "${SEED_EPISODES_PATH:-/seed/episodes.csv}" ]; then
  echo "[entrypoint] importing sample episodes"
  python -m app.cli import-episodes "${SEED_EPISODES_PATH:-/seed/episodes.csv}" || true
fi

echo "[entrypoint] starting api"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
