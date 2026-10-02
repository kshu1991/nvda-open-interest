#!/bin/bash
# Scheduled entry point: pull the latest OCC open interest and save the dated CSV.
# Every run re-fetches. A later run the same morning replaces the CSV if the OCC
# changed the data in between (logged as a WARNING), and is the retry if an
# earlier run found nothing published yet (exit code 75).
cd "$(dirname "$0")" || exit 1
mkdir -p logs
{
  echo "=== $(TZ=America/New_York date '+%a %Y-%m-%d %H:%M:%S ET') ==="
  .venv/bin/python nvda_oi.py --symbol "${1:-NVDA}" --csv --force
  echo "exit code: $?"
} >> logs/nvda_oi.log 2>&1
