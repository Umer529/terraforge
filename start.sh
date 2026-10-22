#!/usr/bin/env bash
set -euo pipefail

# Free demo deployment: keep the existing database-polling worker beside the web process.
# A paid deployment should run this worker as a separate service.
python manage.py run_worker > worker.log 2>&1 &
exec gunicorn terraforge.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --workers 1 --timeout 120
