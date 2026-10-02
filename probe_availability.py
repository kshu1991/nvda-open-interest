#!/usr/bin/env python3
"""Measure when the OCC publishes a new day of open interest.

Polls the two endpoints nvda_oi.py uses and appends one row per poll to
data/probe_log.csv, so the time each one rolls forward can be read off the log:
  * asof        - lastBusDateOI from the OCC open-interest report
  * series_sha  - fingerprint of the symbol's parsed series-search data

Stops once both have changed from their starting values (plus a few extra polls
to see whether the data keeps changing), or after --max-hours.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import sys
import time

import nvda_oi

LOG = nvda_oi.DATA_DIR / "probe_log.csv"
COLUMNS = ["polled_at_et", "asof", "series_sha", "strikes", "call_oi", "put_oi", "error"]


def poll(symbol: str) -> dict:
    now = dt.datetime.now(nvda_oi.ET)
    row = dict.fromkeys(COLUMNS, "")
    row["polled_at_et"] = now.strftime("%Y-%m-%d %H:%M:%S")
    errors = []
    try:
        row["asof"] = nvda_oi.fetch_asof_date(now.date()).isoformat()
    except nvda_oi.OCCError as exc:
        errors.append(str(exc))
    try:
        series = [s for s in nvda_oi.parse_series(nvda_oi.fetch_series_text(symbol))
                  if s.product == symbol]
        rows = nvda_oi.to_long(series)
        row["series_sha"] = hashlib.sha256(repr(rows).encode()).hexdigest()[:12]
        row["strikes"] = len(series)
        row["call_oi"] = sum(s.call_oi for s in series)
        row["put_oi"] = sum(s.put_oi for s in series)
    except nvda_oi.OCCError as exc:
        errors.append(str(exc))
    row["error"] = " | ".join(errors)
    return row


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--symbol", default="NVDA")
    p.add_argument("--interval", type=float, default=5, help="minutes between polls")
    p.add_argument("--max-hours", type=float, default=20)
    p.add_argument("--settle-polls", type=int, default=6,
                   help="extra polls after both signals have rolled forward")
    args = p.parse_args()

    LOG.parent.mkdir(parents=True, exist_ok=True)
    new = not LOG.exists()
    deadline = time.time() + args.max_hours * 3600
    start, settle = None, None
    with LOG.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        if new:
            writer.writeheader()
        while time.time() < deadline:
            row = poll(args.symbol.upper())
            writer.writerow(row)
            fh.flush()
            print(",".join(str(row[c]) for c in COLUMNS), flush=True)
            if not row["error"]:
                if start is None:
                    start = row
                elif settle is None and (row["asof"] != start["asof"]
                                         and row["series_sha"] != start["series_sha"]):
                    settle = args.settle_polls
            if settle is not None:
                if settle == 0:
                    print("both signals rolled forward; done", flush=True)
                    return 0
                settle -= 1
            time.sleep(args.interval * 60)
    print("gave up: no roll-forward before --max-hours", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
