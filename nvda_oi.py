#!/usr/bin/env python3
"""Daily open interest report for one underlying, straight from the OCC.

Sources (both public, no key):
  * https://marketdata.theocc.com/series-search?symbolType=U&symbol=NVDA
      Tab-delimited text: one row per (expiration, strike) with call and put
      open interest. Contains no date.
  * https://marketdata.theocc.com/mdapi/open-interest?report_date=MM/DD/YYYY
      JSON behind the OCC "Open Interest" page. `lastBusDateOI.activityDate` is
      the latest trading day the OCC has published open interest for, whatever
      report_date is asked. Used as the "as of" date.

Exit codes: 0 ok, 1 error, 75 the OCC has not published the expected day yet
(try again later).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
CHART_DIR = HERE / "charts"
DASHBOARD_TEMPLATE = HERE / "dashboard_template.html"
HISTORY_TEMPLATE = HERE / "history_template.html"

SERIES_URL = "https://marketdata.theocc.com/series-search?symbolType=U&symbol={symbol}"
OI_DATE_URL = "https://marketdata.theocc.com/mdapi/open-interest?report_date={date:%m/%d/%Y}"
# The OCC's CDN answers 403 to urllib's default User-Agent.
USER_AGENT = "Mozilla/5.0 (compatible; occ-oi-report/1.0; personal daily report)"

ET = ZoneInfo("America/New_York")
OCC_TZ = ZoneInfo("America/Chicago")  # activityDate is midnight Chicago time

EXIT_OK, EXIT_ERROR, EXIT_NOT_PUBLISHED = 0, 1, 75  # 75 = EX_TEMPFAIL; argparse uses 2

SERIES_COLUMNS = ["ProductSymbol", "year", "Month", "Day", "Integer", "Dec",
                  "C/P", "Call", "Put", "Position Limit"]
CSV_COLUMNS = ["expiry", "strike", "side", "oi"]


class OCCError(Exception):
    """The OCC could not be reached or returned something unusable."""


# --------------------------------------------------------------------------- #
# Trading calendar
# --------------------------------------------------------------------------- #

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """n-th (1-based) given weekday of a month; n=-1 means the last one."""
    if n > 0:
        first = dt.date(year, month, 1)
        return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> dt.date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _observed(day: dt.date) -> dt.date:
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def market_holidays(year: int) -> set[dt.date]:
    """Scheduled US options market holidays (NYSE rules).

    Unscheduled closures (e.g. a national day of mourning) are not known here;
    on such a day the script simply reports that nothing new was published.
    """
    days = {
        _nth_weekday(year, 1, 0, 3),                     # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),                     # Presidents' Day
        _easter(year) - dt.timedelta(days=2),            # Good Friday
        _nth_weekday(year, 5, 0, -1),                    # Memorial Day
        _observed(dt.date(year, 6, 19)),                 # Juneteenth
        _observed(dt.date(year, 7, 4)),                  # Independence Day
        _nth_weekday(year, 9, 0, 1),                     # Labor Day
        _nth_weekday(year, 11, 3, 4),                    # Thanksgiving
        _observed(dt.date(year, 12, 25)),                # Christmas
    }
    new_year = dt.date(year, 1, 1)
    if new_year.weekday() != 5:  # a Saturday New Year's Day is not observed
        days.add(_observed(new_year))
    return days


def is_trading_day(day: dt.date) -> bool:
    return day.weekday() < 5 and day not in market_holidays(day.year)


def previous_trading_day(day: dt.date) -> dt.date:
    day -= dt.timedelta(days=1)
    while not is_trading_day(day):
        day -= dt.timedelta(days=1)
    return day


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def http_get(url: str, retries: int = 4, timeout: int = 30) -> bytes:
    """GET with exponential backoff on network errors, 403/429 and 5xx."""
    last = None
    for attempt in range(retries + 1):
        if attempt:
            delay = 2 ** attempt
            print(f"  retry {attempt}/{retries} in {delay}s ({last})", file=sys.stderr)
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code not in (403, 429) and exc.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = str(getattr(exc, "reason", exc))
    raise OCCError(f"could not fetch {url}: {last}")


def fetch_asof_date(today: dt.date) -> dt.date:
    """Latest trading day the OCC has published open interest for."""
    body = http_get(OI_DATE_URL.format(date=today))
    try:
        payload = json.loads(body)
        millis = payload["entity"]["lastBusDateOI"]["activityDate"]
        return dt.datetime.fromtimestamp(millis / 1000, OCC_TZ).date()
    except (ValueError, KeyError, TypeError) as exc:
        raise OCCError(f"unexpected open-interest date response: {body[:200]!r}") from exc


def fetch_series_text(symbol: str) -> str:
    return http_get(SERIES_URL.format(symbol=symbol)).decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Series:
    product: str      # "NVDA" standard, "2NVDA" FLEX, "NVDA1" adjusted, ...
    expiry: dt.date
    strike: float
    has_call: bool
    has_put: bool
    call_oi: int
    put_oi: int


def parse_series(text: str) -> list[Series]:
    """Parse a series-search response into one Series per (product, expiry, strike)."""
    lines = text.splitlines()
    header_at = next((i for i, line in enumerate(lines)
                      if line.startswith("ProductSymbol")), None)
    if header_at is None:
        # "No data exists for XYZ symbol", "Symbol is required.", an HTML error page...
        raise OCCError(f"no series table in the OCC response: {text.strip()[:120]!r}")
    header = [f.strip() for f in lines[header_at].split("\t") if f.strip()]
    if header != SERIES_COLUMNS:
        raise OCCError(f"the OCC changed the series-search columns: {header}")

    out = []
    for lineno, line in enumerate(lines[header_at + 1:], start=header_at + 2):
        fields = [f.strip() for f in line.split("\t") if f.strip()]
        if not fields:
            continue
        if len(fields) != len(SERIES_COLUMNS):
            raise OCCError(f"line {lineno}: expected {len(SERIES_COLUMNS)} fields, got {line!r}")
        product, year, month, day, whole, frac, flags, call, put, _limit = fields
        if len(frac) != 3:
            raise OCCError(f"line {lineno}: unexpected strike decimals {frac!r}")
        try:
            out.append(Series(
                product=product,
                expiry=dt.date(int(year), int(month), int(day)),
                strike=float(f"{int(whole)}.{frac}"),
                has_call="C" in flags,
                has_put="P" in flags,
                call_oi=int(call),
                put_oi=int(put),
            ))
        except ValueError as exc:
            raise OCCError(f"line {lineno}: cannot parse {line!r} ({exc})") from exc
    return out


def to_long(series: list[Series]) -> list[tuple[str, str, str, int]]:
    """Long format (expiry, strike, side, oi); a side is listed only if it trades."""
    rows = []
    for s in sorted(series, key=lambda s: (s.expiry, s.strike)):
        if s.has_call:
            rows.append((s.expiry.isoformat(), f"{s.strike:g}", "call", s.call_oi))
        if s.has_put:
            rows.append((s.expiry.isoformat(), f"{s.strike:g}", "put", s.put_oi))
    return rows


def sanity_check(series: list[Series], long_rows, asof: dt.date) -> list[str]:
    """Return a list of problems; empty means the data looks sane."""
    problems = []
    calls = sum(s.call_oi for s in series)
    puts = sum(s.put_oi for s in series)
    by_side = defaultdict(int)
    by_expiry = defaultdict(int)
    for expiry, _strike, side, oi in long_rows:
        by_side[side] += oi
        by_expiry[expiry] += oi
    if (by_side["call"], by_side["put"]) != (calls, puts):
        problems.append(f"long-format totals {dict(by_side)} != table totals "
                        f"(call {calls}, put {puts}): OI on a side flagged as not listed")
    if sum(by_expiry.values()) != calls + puts:
        problems.append("per-expiration subtotals do not add up to the grand total")
    if calls + puts == 0:
        problems.append("total open interest is zero")
    keys = [(s.expiry, s.strike) for s in series]
    if len(keys) != len(set(keys)):
        problems.append("duplicate (expiration, strike) rows")
    if any(s.call_oi < 0 or s.put_oi < 0 for s in series):
        problems.append("negative open interest")
    if any(s.strike <= 0 for s in series):
        problems.append("non-positive strike")
    expired = sorted({s.expiry for s in series if s.expiry < asof})
    if expired:
        problems.append(f"expirations before the as-of date: {expired[:3]}")
    far = sorted({s.expiry for s in series if s.expiry > asof + dt.timedelta(days=366 * 6)})
    if far:
        problems.append(f"expirations more than 6 years out: {far[:3]}")
    weekend = sorted({s.expiry for s in series if s.expiry.weekday() >= 5})
    if weekend:
        problems.append(f"expirations on a weekend: {weekend[:3]}")
    return problems


# --------------------------------------------------------------------------- #
# History files
# --------------------------------------------------------------------------- #

def rel(path: Path) -> Path:
    """Path relative to the project folder when it is inside it."""
    try:
        return path.resolve().relative_to(HERE)
    except ValueError:
        return path


def csv_path_for(symbol: str, asof: dt.date) -> Path:
    return DATA_DIR / f"{symbol}_oi_{asof:%Y%m%d}.csv"


def read_csv_rows(path: Path):
    with path.open(newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        return [(e, k, side, int(oi)) for e, k, side, oi in reader]


def write_csv_rows(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)
    tmp.replace(path)


def saved_days(symbol: str) -> dict[dt.date, Path]:
    """Every saved dated CSV for `symbol`, keyed by its as-of date."""
    days = {}
    for path in DATA_DIR.glob(f"{symbol}_oi_????????.csv"):
        try:
            days[dt.datetime.strptime(path.stem[-8:], "%Y%m%d").date()] = path
        except ValueError:
            continue
    return days


def latest_saved_before(symbol: str, asof: dt.date):
    """(date, path) of the newest saved CSV dated before `asof`, or None."""
    earlier = {day: path for day, path in saved_days(symbol).items() if day < asof}
    if not earlier:
        return None
    day = max(earlier)
    return day, earlier[day]


def load_history(symbol: str, asof: dt.date, rows):
    """(day, rows) for every saved day in date order; this pull's rows stand in for `asof`,
    whether or not they get saved."""
    days = saved_days(symbol)
    days[asof] = None
    for day in sorted(days):
        yield day, rows if day == asof else read_csv_rows(days[day])


def history_series(days, cutoff: dt.date):
    """Open interest per option per day, for the expirations on or after `cutoff`.

    Returns (dates, expiries). `dates` are the ISO days that hold any of those options.
    Each expiry is [expiry, first, options]: `first` indexes `dates`, and each option is
    [strike, side, values] where values[i] is its open interest on dates[first + i], or
    None on a day it was not listed (yet). Trailing missing days are left off.
    """
    cutoff = cutoff.isoformat()
    dates, seen = [], {}  # (expiry, strike, side) -> [first date index, values from there]
    for day, rows in days:
        live = [row for row in rows if row[0] >= cutoff]
        if not live:
            continue
        i = len(dates)
        dates.append(day.isoformat())
        for expiry, strike, side, oi in live:
            first, values = seen.setdefault((expiry, strike, side), [i, []])
            values.extend([None] * (i - first - len(values)))
            values.append(oi)

    by_expiry = defaultdict(list)
    for (expiry, strike, side), (first, values) in seen.items():
        by_expiry[expiry].append((strike, side, first, values))
    expiries = []
    for expiry in sorted(by_expiry):
        options = sorted(by_expiry[expiry], key=lambda o: (float(o[0]), o[1]))
        start = min(first for _, _, first, _ in options)
        expiries.append([expiry, start, [[strike, side, [None] * (first - start) + values]
                                         for strike, side, first, values in options]])
    return dates, expiries


def log_first_seen(symbol: str, asof: dt.date, now: dt.datetime) -> None:
    """Record when each trading day's data was first saved, to tune the schedule."""
    path = DATA_DIR / "availability_log.csv"
    new = not path.exists()
    with path.open("a", newline="") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(["symbol", "asof", "saved_at_et"])
        writer.writerow([symbol, asof.isoformat(), now.strftime("%Y-%m-%d %H:%M:%S")])


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def top_contracts(long_rows, n: int):
    return sorted(long_rows, key=lambda r: r[3], reverse=True)[:n]


def contract_label(symbol: str, expiry: str, strike: str, side: str) -> str:
    """'NVDA 10/02/26 250.0C', the way option chains usually name a contract."""
    when = dt.date.fromisoformat(expiry)
    strike = strike if "." in strike else strike + ".0"
    return f"{symbol} {when:%m/%d/%y} {strike}{side[0].upper()}"


def side_totals(long_rows) -> tuple[int, int]:
    calls = sum(oi for *_, side, oi in long_rows if side == "call")
    puts = sum(oi for *_, side, oi in long_rows if side == "put")
    return calls, puts


def oi_stats(long_rows) -> list[tuple[str, str]]:
    calls, puts = side_totals(long_rows)
    return [("Call open interest total", f"{calls:,}"),
            ("Put open interest total", f"{puts:,}"),
            ("Open interest total", f"{calls + puts:,}"),
            ("Put-call open interest ratio", f"{puts / calls:.2f}" if calls else "n/a")]


def scope_text(expiry) -> str:
    return f"{expiry} expiration" if expiry else "all expirations"


def print_report(symbol, asof_line, now, standard, view_rows, others, expiry, top, problems):
    print(f"{symbol} open interest (source: OCC series-search)")
    print(f"  Data as of : {asof_line}")
    print(f"  Retrieved  : {now:%a %Y-%m-%d %H:%M} ET")
    if others:
        other_oi = sum(s.call_oi + s.put_oi for s in others)
        products = ", ".join(sorted({s.product for s in others}))
        print(f"  Excluded   : {len(others):,} FLEX/adjusted series ({products}), "
              f"{other_oi:,} contracts of OI")

    print(f"\n  Open interest stats: {symbol}, {scope_text(expiry)}, calls & puts")
    for name, value in oi_stats(view_rows):
        print(f"    {name:<30}{value:>12}")

    print(f"\n  Highest open interest options: {symbol}, {scope_text(expiry)}")
    for rank, (day, strike, side, oi) in enumerate(top_contracts(view_rows, top), 1):
        print(f"    {rank:>3}  {contract_label(symbol, day, strike, side):<24}{oi:>10,}")

    print("\n  All expirations")
    print(f"    {'expiry':<13}{'strikes':>7}{'call OI':>12}{'put OI':>12}{'total':>12}{'P/C':>7}")
    by_expiry = defaultdict(list)
    for s in standard:
        by_expiry[s.expiry].append(s)
    for day in sorted(by_expiry):
        c = sum(s.call_oi for s in by_expiry[day])
        p = sum(s.put_oi for s in by_expiry[day])
        mark = "*" if day == expiry else " "
        print(f"   {mark}{day.isoformat():<13}{len(by_expiry[day]):>7,}{c:>12,}{p:>12,}"
              f"{c + p:>12,}{(f'{p / c:.2f}' if c else 'n/a'):>7}")
    c, p = sum(s.call_oi for s in standard), sum(s.put_oi for s in standard)
    print(f"    {'total':<13}{len(standard):>7,}{c:>12,}{p:>12,}{c + p:>12,}"
          f"{(f'{p / c:.2f}' if c else 'n/a'):>7}")

    print()
    if problems:
        print("  SANITY CHECKS FAILED:")
        for problem in problems:
            print(f"    - {problem}")
    else:
        strikes = [s.strike for s in standard]
        print(f"  Sanity checks: OK (totals reconcile, {len(by_expiry)} expirations "
              f"{min(by_expiry)} to {max(by_expiry)}, strikes {min(strikes):g}-{max(strikes):g})")


def save_chart(symbol, asof_text, view_rows, expiry, top, path: Path) -> None:
    """Stats table plus a ranking of the highest-OI contracts, as one PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    from matplotlib.ticker import FuncFormatter

    surface, ink, muted, rule = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
    side_color = {"call": "#2a78d6", "put": "#eb6834"}
    rows = top_contracts(view_rows, top)
    stats = oi_stats(view_rows)
    values = [oi for *_, oi in rows]
    colors = [side_color[side] for _, _, side, _ in rows]
    showing = f"Showing results for {symbol}, {scope_text(expiry)}, calls & puts"

    # Layout in inches, top to bottom; converted to figure fractions below.
    width, left, right = 10.0, 0.4, 0.4
    stats_h, bars_h = 0.36 * len(stats), 0.3 * len(rows) + 0.2
    height = 1.15 + stats_h + 1.25 + bars_h + 0.95
    fig = plt.figure(figsize=(width, height), dpi=150)
    fig.patch.set_facecolor(surface)

    def y(inches_from_top):
        return 1 - inches_from_top / height

    x0 = left / width
    fig.text(x0, y(0.45), "Open Interest Stats", fontsize=15, fontweight="bold", color=ink)
    fig.text(x0, y(0.8), f"{showing} · OCC data as of {asof_text}", fontsize=10, color=muted)

    table = fig.add_axes([x0, y(1.15 + stats_h), 1 - (left + right) / width, stats_h / height])
    table.set_xlim(0, 1)
    table.set_ylim(len(stats), 0)
    table.axis("off")
    for i, (name, value) in enumerate(stats):
        table.text(0.005, i + 0.5, name, va="center", fontsize=10.5, fontweight="bold", color=ink)
        table.text(0.995, i + 0.5, value, va="center", ha="right", fontsize=10.5, color=ink)
        table.plot([0, 1], [i + 1, i + 1], color=rule, linewidth=0.8, clip_on=False)

    bars_top = 1.15 + stats_h + 1.25
    fig.text(x0, y(bars_top - 0.65), "Highest Open Interest Options", fontsize=15,
             fontweight="bold", color=ink)
    fig.text(x0, y(bars_top - 0.3), showing, fontsize=10, color=muted)

    label_w = 2.05  # room for 'NVDA 10/02/26 227.5C' left of the bars
    ax = fig.add_axes([(left + label_w) / width, y(bars_top + bars_h),
                       1 - (left + label_w + right) / width, bars_h / height])
    ax.set_facecolor(surface)
    positions = list(range(len(rows)))
    ax.barh(positions, values, height=0.42, color=colors)
    ax.set_ylim(len(rows) - 0.4, -0.6)
    ax.set_yticks(positions)
    ax.set_yticklabels([contract_label(symbol, *row[:3]) for row in rows], fontsize=9, color=ink)
    ax.set_xlim(0, max(values) * 1.1)
    for pos, value in zip(positions, values):
        ax.text(value + max(values) * 0.008, pos, f"{value:,}", va="center",
                fontsize=8.5, fontweight="bold", color=ink)

    def thousands(v, _):
        return "0" if v == 0 else (f"{v / 1e6:g}M" if v >= 1e6 else f"{v / 1e3:g}K")
    ax.xaxis.set_major_formatter(FuncFormatter(thousands))
    ax.tick_params(axis="x", labelsize=9, colors=muted, length=0)
    ax.tick_params(axis="y", length=0, pad=10)
    ax.grid(axis="x", color=rule, linewidth=0.8)
    ax.set_axisbelow(True)
    for name, spine in ax.spines.items():
        spine.set_visible(name == "left")
        spine.set_color(ink)
        spine.set_linewidth(0.8)
    ax.set_xlabel("Open interest", fontsize=10, color=muted, loc="right")

    sides = [side for side in ("call", "put") if side_color[side] in colors]
    if len(sides) > 1:
        ax.legend(handles=[Patch(color=side_color[s], label=f"{s.capitalize()}s") for s in sides],
                  loc="lower right", frameon=False, fontsize=9, labelcolor=ink)

    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=surface)
    plt.close(fig)


def page_path(kind: str, symbol: str) -> Path:
    """The generated web pages, side by side so they can link to each other by name."""
    return HERE / f"{kind}_{symbol}.html"


def write_page(template: Path, symbol: str, payload: dict, path: Path) -> None:
    """Fill a page template with its data and write it as a self-contained file."""
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    content = (template.read_text(encoding="utf-8")
               .replace("__OI_SYMBOL__", symbol).replace("__OI_DATA__", blob))
    # The template is page content only; wrap it in a document.
    path.write_text('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
                    f"</head>\n<body>\n{content}</body>\n</html>\n", encoding="utf-8")


def save_dashboard(symbol, asof_line, now, all_rows, others, warning, path: Path) -> None:
    """Write the interactive page: the template with the whole chain embedded."""
    write_page(DASHBOARD_TEMPLATE, symbol, {
        "symbol": symbol,
        "asofText": asof_line,
        "retrieved": f"{now:%a %Y-%m-%d %H:%M} ET",
        "warning": warning,
        "excluded": (f"{len(others):,} FLEX/adjusted series holding "
                     f"{sum(s.call_oi + s.put_oi for s in others):,} contracts") if others else "",
        "historyPage": page_path("history", symbol).name,
        "rows": all_rows,
    }, path)


def save_history(symbol, now, asof, all_rows, warning, path: Path) -> int:
    """Write the history page: each option's open interest on every saved day, for the
    expirations that have not passed. Returns the number of days on it."""
    dates, expiries = history_series(load_history(symbol, asof, all_rows), now.date())
    write_page(HISTORY_TEMPLATE, symbol, {
        "symbol": symbol,
        "retrieved": f"{now:%a %Y-%m-%d %H:%M} ET",
        "warning": warning,
        "latestPage": page_path("dashboard", symbol).name,
        "dates": dates,
        "expiries": expiries,
    }, path)
    return len(dates)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    def expiry_choice(text):
        if text.lower() in ("nearest", "all"):
            return text.lower()
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{text!r} is not a YYYY-MM-DD date, 'nearest' or 'all'")

    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--symbol", default="NVDA", help="underlying symbol (default: NVDA)")
    p.add_argument("--expiry", type=expiry_choice, default="nearest", metavar="YYYY-MM-DD",
                   help="expiration to rank: a date, 'nearest' (default: the first "
                        "expiration not yet past) or 'all' for the whole chain")
    p.add_argument("--top", type=int, default=20,
                   help="number of top-OI contracts to list and chart (default: 20)")
    p.add_argument("--csv", nargs="?", const="auto", metavar="PATH",
                   help="save long-format data (expiry, strike, side, oi) for the whole "
                        "chain; default PATH is data/SYMBOL_oi_YYYYMMDD.csv (as-of date)")
    p.add_argument("--if-new", action="store_true",
                   help="exit quietly if the dated CSV for the latest OCC data already "
                        "exists (for scheduled runs that fire more than once)")
    p.add_argument("--allow-stale", action="store_true",
                   help="report whatever the OCC currently has, even if the previous "
                        "trading day is not published yet")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing dated CSV whose contents differ")
    p.add_argument("--no-chart", action="store_true", help="skip the chart")
    args = p.parse_args(argv)
    if args.top < 1:
        p.error("--top must be at least 1")
    return args


def run(args) -> int:
    symbol = args.symbol.strip().upper()
    now = dt.datetime.now(ET)
    today = now.date()
    expected = previous_trading_day(today)
    # After the close, today's session is the newest one the OCC could publish.
    after_close = is_trading_day(today) and now.time() >= dt.time(16, 0)
    newest = today if after_close else expected
    if args.if_new and csv_path_for(symbol, newest).exists():
        print(f"Nothing to do: {rel(csv_path_for(symbol, newest))} already holds the "
              f"latest completed session ({newest:%a %Y-%m-%d}).")
        return EXIT_OK
    if not is_trading_day(today):
        print(f"Note: today ({today:%a %Y-%m-%d}) is not a trading day; the latest "
              f"completed session is {expected:%a %Y-%m-%d}.")

    try:
        asof, verified = fetch_asof_date(today), True
    except OCCError as exc:
        asof, verified = expected, False
        print(f"WARNING: could not read the OCC as-of date ({exc}).", file=sys.stderr)

    if asof < expected and not args.allow_stale:
        print(f"The OCC has not published open interest for {expected:%a %Y-%m-%d} yet "
              f"(latest available: {asof:%a %Y-%m-%d}). Try again later, or pass "
              f"--allow-stale to see the older data.")
        return EXIT_NOT_PUBLISHED
    # After the close, series-search can switch to the day's new numbers while the report
    # date still names the previous session (seen at 21:06 ET on 2026-10-08), so until the
    # date reaches today the data cannot be dated and must not be saved under `asof`.
    if after_close and asof < today and not args.allow_stale:
        print(f"After the close, series-search may already hold {today:%a %Y-%m-%d} open "
              f"interest while the OCC still dates its data {asof:%a %Y-%m-%d}. Try again "
              f"once the OCC dates it, or tomorrow morning; pass --allow-stale to see it anyway.")
        return EXIT_NOT_PUBLISHED

    dated_csv = csv_path_for(symbol, asof)
    if args.if_new and verified and dated_csv.exists():
        print(f"Nothing new: the latest OCC data is {asof:%a %Y-%m-%d} and "
              f"{rel(dated_csv)} already exists.")
        return EXIT_OK

    text = fetch_series_text(symbol)
    (HERE / f"oi_raw_{symbol}.txt").write_text(text)
    parsed = parse_series(text)
    standard = [s for s in parsed if s.product == symbol]
    others = [s for s in parsed if s.product != symbol]
    if not standard:
        raise OCCError(f"no standard {symbol} option series in the OCC response")
    all_rows = to_long(standard)

    # series-search carries no date, so cross-check it against saved history.
    previous = latest_saved_before(symbol, asof)
    if previous and read_csv_rows(previous[1]) == all_rows and not args.allow_stale:
        claim = "The OCC reports open interest" if verified else "Expected open interest"
        print(f"{claim} as of {asof:%a %Y-%m-%d}, but series-search still returns exactly "
              f"the {previous[0]:%a %Y-%m-%d} data. Not refreshed yet; try again later.")
        return EXIT_NOT_PUBLISHED
    conflict = dated_csv.exists() and read_csv_rows(dated_csv) != all_rows
    if not verified:
        asof_line = (f"{asof:%a %Y-%m-%d} close, UNVERIFIED (OCC date endpoint unavailable; "
                     f"assumed previous trading day)")
    else:
        asof_line = f"{asof:%a %Y-%m-%d} close (OCC open-interest report date)"
    if asof < expected:
        asof_line += f"  ** STALE: {expected} not published yet **"

    expiries = sorted({s.expiry for s in standard})
    if args.expiry == "all":
        expiry = None
    elif args.expiry == "nearest":
        expiry = next((day for day in expiries if day >= today), expiries[-1])
    elif args.expiry in expiries:
        expiry = args.expiry
    else:
        raise OCCError(f"no {symbol} expiration on {args.expiry}. Available: "
                       + ", ".join(day.isoformat() for day in expiries))
    view_rows = [r for r in all_rows if expiry is None or r[0] == expiry.isoformat()]

    problems = sanity_check(standard, all_rows, asof)
    print_report(symbol, asof_line, now, standard, view_rows, others, expiry, args.top, problems)
    if conflict:
        print(f"\n  WARNING: series-search now differs from {rel(dated_csv)}, "
              f"saved earlier for the same as-of date. The OCC may be mid-update or "
              f"have issued a correction.")

    print()
    if not args.no_chart:
        suffix = f"_exp{expiry:%Y%m%d}" if expiry else "_all"
        chart = CHART_DIR / f"{symbol}_oi_{asof:%Y%m%d}{suffix}.png"
        try:
            save_chart(symbol, f"{asof:%a %Y-%m-%d} close", view_rows, expiry, args.top, chart)
            print(f"  Chart: {rel(chart)}")
        except ImportError:
            print("  Chart skipped: matplotlib is not installed "
                  "(pip install -r requirements.txt).", file=sys.stderr)
    warning = ""
    if problems:
        warning = "Sanity checks failed: " + "; ".join(problems)
    elif asof < expected:
        warning = f"Stale: the OCC has not published {expected:%a %Y-%m-%d} yet."
    page = page_path("dashboard", symbol)
    try:
        save_dashboard(symbol, asof_line.split("  **")[0], now, all_rows, others, warning, page)
        print(f"  Page:  {rel(page)}")
    except FileNotFoundError:
        print(f"  Page skipped: {DASHBOARD_TEMPLATE.name} is missing.", file=sys.stderr)
    page = page_path("history", symbol)
    try:
        days = save_history(symbol, now, asof, all_rows, warning, page)
        print(f"  Page:  {rel(page)} ({days} trading day{'s' * (days != 1)} of history)")
    except FileNotFoundError:
        print(f"  Page skipped: {HISTORY_TEMPLATE.name} is missing.", file=sys.stderr)

    if args.csv:
        target = dated_csv if args.csv == "auto" else Path(args.csv).expanduser()
        if problems:
            print("  CSV not saved: sanity checks failed.", file=sys.stderr)
        elif target == dated_csv and conflict and not args.force:
            print(f"  CSV not overwritten: {rel(target)} exists with different "
                  f"contents (use --force to replace it).")
        else:
            is_new = not target.exists()
            write_csv_rows(target, all_rows)
            if target == dated_csv and is_new:
                log_first_seen(symbol, asof, now)
            print(f"  CSV:   {rel(target)} ({len(all_rows):,} rows, whole chain)")
    return EXIT_ERROR if problems else EXIT_OK


def main(argv=None) -> int:
    try:
        return run(parse_args(argv))
    except OCCError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
