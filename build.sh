#!/usr/bin/env bash
set -euo pipefail

python manage.py migrate --noinput
python manage.py collectstatic --noinput

if [ "${SEED_DEMO:-0}" = "1" ]; then
  python manage.py seed_demo
fi
