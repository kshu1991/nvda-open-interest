# NVDA open interest, straight from the OCC

Daily report of call/put open interest per strike and expiration, pulled from
the Options Clearing Corporation instead of an aggregator. No API key, no paid
service.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Python 3.9+. The only dependency is matplotlib (for the chart); everything else
is standard library.

## Usage

```bash
.venv/bin/python nvda_oi.py                       # nearest expiration: stats, ranking, chart
.venv/bin/python nvda_oi.py --csv                 # also save data/NVDA_oi_YYYYMMDD.csv
.venv/bin/python nvda_oi.py --expiry 2026-10-16   # a specific expiration
.venv/bin/python nvda_oi.py --expiry all          # rank across the whole chain
.venv/bin/python nvda_oi.py --top 10              # top 10 contracts instead of 20
.venv/bin/python nvda_oi.py --symbol AAPL         # another underlying
```

| Option | Meaning |
|---|---|
| `--symbol` | Underlying (default `NVDA`) |
| `--expiry YYYY-MM-DD` | Expiration to rank. Also accepts `nearest` (default: first expiration not yet past) or `all` |
| `--top N` | Contracts in the "highest open interest" table and chart (default 20) |
| `--csv [PATH]` | Save long-format `expiry,strike,side,oi` for the whole chain. Default path `data/SYMBOL_oi_YYYYMMDD.csv`, dated by the data's as-of day |
| `--if-new` | Do nothing if the latest session's CSV is already saved |
| `--allow-stale` | Report what the OCC has even if the previous trading day is not out yet |
| `--force` | Overwrite a dated CSV whose contents differ |
| `--no-chart` | Skip the chart |

Exit codes: `0` ok, `1` error, `75` the OCC has not published yet (try later).

Outputs: the report on stdout (stats, "highest open interest" ranking, and a
per-expiration table), the web page `dashboard_SYMBOL.html`, the same stats
and ranking as a chart in `charts/SYMBOL_oi_YYYYMMDD_expYYYYMMDD.png` (`_all`
for the whole chain), the raw response in `oi_raw_SYMBOL.txt`, and with `--csv`
the dated file in `data/`.
The CSV always holds the whole chain, whatever `--expiry` is, so history stays
complete. The OCC serves only the current snapshot, so a day that is not saved
cannot be fetched later.

## The web page

Every run rewrites `dashboard_NVDA.html`, a single self-contained file with the
whole chain embedded (no server, no network needed to view it):

```bash
open dashboard_NVDA.html
```

It shows the Open Interest Stats table and the Highest Open Interest Options
ranking, with pickers for expiration (or all expirations), calls/puts, and how
many contracts to show, plus a table of every expiration. It opens on the
nearest expiration that has not passed. The layout lives in
`dashboard_template.html`; edit that, not the generated file. Reload the page
after a data pull to see the new day.

### On a phone, or anywhere

https://kshu1991.github.io/nvda-open-interest/ is the same page, hosted by
GitHub Pages. GitHub Actions (`.github/workflows/daily.yml`) runs the pull on
GitHub's servers every 15 minutes on weekday mornings (about 8:00 to 10:00
Eastern). The first run that finds a new day commits its CSV to `data/` and
republishes the page; the rest change nothing. It does not depend on this Mac.
GitHub starts scheduled runs late at busy times and occasionally skips one,
which is why it tries repeatedly instead of at one exact time.

To run it on demand: the repository's Actions tab, "Daily open interest",
"Run workflow".

## Where the numbers come from

- `https://marketdata.theocc.com/series-search?symbolType=U&symbol=NVDA` is a
  tab-delimited file with one row per expiration and strike: product symbol,
  year, month, day, strike integer, strike decimals (thousandths), a `C P` flag
  for which sides are listed, call OI, put OI, position limit. It has no date.
- `https://marketdata.theocc.com/mdapi/open-interest?report_date=MM/DD/YYYY`
  is the JSON behind the OCC's Open Interest page. Its
  `lastBusDateOI.activityDate` is the latest trading day the OCC has published
  open interest for. The script prints that as the "as of" date.
- Only standard listed options (product symbol equal to the underlying) are
  counted. FLEX series (`1NVDA`, `2NVDA`) are excluded and their size is shown
  on the "Excluded" line.

Because series-search carries no date, the script cross-checks it against saved
history: if the OCC's date has moved on but the series data is identical to the
previous saved day, it treats the data as not refreshed yet.

## Schedule (macOS)

A LaunchAgent (`~/Library/LaunchAgents/com.nvda-oi.daily.plist`) runs
`run_daily.sh` Monday to Friday at 08:30 and 09:20, before the 09:30 open.
Each run pulls fresh data, writes the report to `logs/nvda_oi.log`, the chart
to `charts/` and the dated CSV to `data/`. The 09:20 run is both a retry (if
the OCC had not published by 08:30) and a refresh: if the OCC changed the data
in between, the CSV is replaced and the log carries a WARNING.

```bash
./schedule_macos.sh status
./schedule_macos.sh install 08:30 09:20   # change the times
./schedule_macos.sh uninstall
```

Times are the Mac's local time, which is Eastern while the system time zone is
`America/New_York`. If the Mac is asleep at a scheduled time the run happens
on wake. Monday's run picks up Friday's close. On a market holiday the run
re-reports the last session and saves nothing new.

## Measuring when the OCC publishes

```bash
.venv/bin/python probe_availability.py --interval 5 --max-hours 20
```

Optional. Polls both endpoints and appends to `data/probe_log.csv`; the first
row where `asof` and `series_sha` change is the publication time. The time each
new day was first saved is also recorded in `data/availability_log.csv`, which
shows whether the 08:30 run is early enough.
