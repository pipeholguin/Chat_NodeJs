#!/usr/bin/env bash
# Run the VDHG monitor once. Schedule after the ASX close (~4:10pm Sydney), e.g. via cron:
#   30 16 * * 1-5  /path/to/vdhg-monitor/run_daily.sh
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] && { set -a; . ./.env; set +a; }          # optional: NOTIFICATION_METHOD, WEBHOOK_URL, ...
[ -f .venv/bin/activate ] && . .venv/bin/activate     # optional virtualenv

python analyse_vdhg.py "$@"
