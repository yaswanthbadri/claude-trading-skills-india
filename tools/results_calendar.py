#!/usr/bin/env python3
"""Who reports, when, and what the market did last time.

Yahoo's earnings history carries three things that matter:

    the next scheduled date   the top row is in the future, with an EPS
                              estimate and no reported figure — a confirmed
                              date, not an inference
    the surprise               the gap between the estimate and the reported
                              EPS, for every print in the history
    the dates                  the quarterly rhythm of this company

With those, a results plan stops being a guess. We know the date, we know
what the stock did after its last four prints, and we can size the trade
before the number rather than after the gap.

The universe is large and Yahoo is slow, so this covers the liquid names —
the ones a book would actually hold — and it runs after the close, when
there is no deadline and no rate limit to fight.

    python3 tools/results_calendar.py --build
    python3 tools/results_calendar.py --build --limit 500
    python3 tools/results_calendar.py TOP 20
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import statistics
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CANDLES = REPO / "data" / "candles"
CACHE = REPO / "data" / "results_calendar.json"
MAX_AGE_HOURS = 30
DEFAULT_LIMIT = 1200
THREADS = 4
CHUNK = 40          # names per round: small enough to survive a throttle
PAUSE = 1.5         # seconds between rounds
BACKOFF = 20        # extra pause when a round comes back mostly empty
NIGHTLY_BUDGET = 250    # names to touch per night: the quota, not the ambition
NEAR_TERM_DAYS = 45    # a date this close is the one the desk will act on


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except Exception:
            pass
    return {"version": 1, "built": None, "symbols": {}}


def save(store: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1, default=str))
    tmp.replace(CACHE)


def fresh(store: dict | None = None) -> bool:
    store = store or load()
    at = store.get("built")
    if not at:
        return False
    try:
        return (datetime.now() - datetime.fromisoformat(at)) < timedelta(hours=MAX_AGE_HOURS)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# the universe: the liquid names
# ---------------------------------------------------------------------------
def liquid_symbols(limit: int = DEFAULT_LIMIT) -> list[str]:
    """The most traded names in the cache. That is where a book would look."""
    scores = []
    for f in glob.glob(str(CANDLES / "*.csv")):
        sym = Path(f).stem
        try:
            with open(f) as fh:
                rows = list(csv.DictReader(fh))[-20:]
            if len(rows) < 5:
                continue
            vals = [float(r["Close"]) * float(r["Volume"]) for r in rows]
            scores.append((sum(vals) / len(vals), sym))
        except Exception:
            continue
    scores.sort(reverse=True)
    return [s for _, s in scores[:limit]]


# ---------------------------------------------------------------------------
# one symbol
# ---------------------------------------------------------------------------
def fetch(symbol: str) -> dict:
    """The next date, the cadence, and how the market took each print."""
    warnings.filterwarnings("ignore")
    try:
        import yfinance as yf
        df = yf.Ticker(f"{symbol}.NS").get_earnings_dates(limit=12)
    except Exception as exc:  # noqa: BLE001
        return {"symbol": symbol, "error": type(exc).__name__, "next": None}
    if df is None or getattr(df, "empty", True):
        return {"symbol": symbol, "error": "no earnings dates", "next": None}

    today = date.today()
    rows = []
    for idx, r in df.iterrows():
        try:
            d = idx.date() if hasattr(idx, "date") else idx
        except Exception:
            continue
        surprise = None
        try:
            v = r.get("Surprise(%)")
            surprise = None if v is None or (isinstance(v, float) and v != v) else float(v)
        except Exception:
            surprise = None
        rows.append({"date": str(d), "surprise": surprise,
                     "forward": bool(d >= today)})

    future = [r for r in rows if r["forward"]]
    past = sorted([r for r in rows if not r["forward"]], key=lambda x: x["date"])
    nxt = future[0] if future else None

    cadence = None
    if len(past) >= 2:
        ds = [date.fromisoformat(r["date"]) for r in past]
        diffs = [(ds[i] - ds[i + 1]).days for i in range(len(ds) - 1)]
        diffs = [x for x in diffs if 50 <= x <= 130]
        if diffs:
            cadence = int(statistics.median(diffs))

    estimated = None
    if nxt is None and past and cadence:
        d = date.fromisoformat(past[-1]["date"]) + timedelta(days=cadence)
        while d < today:
            d += timedelta(days=cadence)
        estimated = d.isoformat()

    return {
        "symbol": symbol,
        "next": (nxt or {}).get("date"),
        "confirmed": bool(nxt),
        "estimated": estimated,
        "cadence_days": cadence,
        "last_print": past[-1]["date"] if past else None,
        "history": past[-8:],
        "at": _now(),
    }


def _fresh_row(row: dict) -> bool:
    at = (row or {}).get("at")
    if not at:
        return False
    try:
        return (datetime.now() - datetime.fromisoformat(at)) < timedelta(hours=MAX_AGE_HOURS)
    except Exception:
        return False


def _near_term(row: dict, days: int = NEAR_TERM_DAYS) -> bool:
    """Is this date close enough that the desk will act on it?"""
    when = (row or {}).get("next") or (row or {}).get("estimated")
    if not when:
        return False
    try:
        d = date.fromisoformat(when)
    except Exception:
        return False
    return (d - date.today()).days <= days


def build(limit: int = DEFAULT_LIMIT, verbose: bool = True,
          budget: int = NIGHTLY_BUDGET) -> dict:
    """Refresh the calendar without losing what we already know.

    Yahoo throttles a burst of earnings requests, and a throttled request
    looks exactly like a company with no earnings dates. Replacing the cache
    with the result of a throttled run took the calendar from 235 names to
    59 — the desk lost two hundred confirmed dates because the network
    shrugged.

    So the night job works to a budget, not to an ambition:

      nothing is ever dropped        a known date is kept until it is
                                     replaced by a fresher one
      the near-term names first      the dates inside 45 days are the ones
                                     the desk will act on, so those get
                                     refreshed every night
      then the unseen liquid names   filling in the long tail, a slice at a
                                     time
      small rounds with a pause      and a longer pause when a round comes
                                     back empty

    A failure never removes a date we already had.
    """
    syms = liquid_symbols(limit)
    store = load()
    known = store.get("symbols") or {}
    keep = dict(known)
    refresh = [s for s, row in known.items()
               if not _fresh_row(row) or _near_term(row)]
    unseen = [s for s in syms if s not in known]
    todo = (refresh + unseen)[:budget]
    if verbose:
        print(f"calendar: {len(known):,} known, {len(refresh):,} to refresh, "
              f"{len(unseen):,} unseen · touching {len(todo):,} tonight "
              f"({len(syms):,} in the universe)", flush=True)

    t0 = time.time()
    for i in range(0, len(todo), CHUNK):
        chunk = todo[i:i + CHUNK]
        got = 0
        try:
            with ThreadPoolExecutor(max_workers=THREADS) as ex:
                for res in ex.map(fetch, chunk):
                    if res.get("next") or res.get("estimated"):
                        keep[res["symbol"]] = res
                        got += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] calendar round failed: {exc}", file=sys.stderr)
        if verbose:
            print(f"  … {min(i + CHUNK, len(todo))}/{len(todo)} "
                  f"({got} with a date, {time.time() - t0:.0f}s)", flush=True)
        time.sleep(PAUSE if got else PAUSE + BACKOFF)

    store["symbols"] = keep
    store["built"] = _now()
    store["universe"] = len(syms)
    save(store)
    return store


# ---------------------------------------------------------------------------
# reading it back
# ---------------------------------------------------------------------------
def upcoming(within_sessions: int = 7, store: dict | None = None) -> list[dict]:
    """Names with a print inside the window, soonest first."""
    store = store or load()
    import market_calendar as cal
    out = []
    for sym, row in (store.get("symbols") or {}).items():
        when = row.get("next") or row.get("estimated")
        if not when:
            continue
        try:
            d = date.fromisoformat(when)
        except Exception:
            continue
        sessions = cal.trading_days_between(date.today(), d)
        if sessions is None or sessions < 0 or sessions > within_sessions:
            continue
        out.append({"symbol": sym, "date": when, "sessions": sessions,
                    "confirmed": bool(row.get("confirmed")),
                    "cadence_days": row.get("cadence_days"),
                    "last_print": row.get("last_print"),
                    "history": row.get("history") or []})
    out.sort(key=lambda r: (r["sessions"], r["symbol"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--within", type=int, default=7)
    ap.add_argument("show", nargs="?", default=None)
    args = ap.parse_args()

    if args.build:
        print(f"Building the results calendar for the top {args.limit} names…")
        store = build(args.limit)
        print(f"calendar: {len(store['symbols'])} names with a date")
        return 0

    store = load()
    if not fresh(store):
        print(f"calendar built {store.get('built')} — stale; run --build")
    rows = upcoming(args.within, store)
    print(f"{len(rows)} print(s) in the next {args.within} sessions")
    for r in rows:
        tag = "confirmed" if r["confirmed"] else "estimated"
        print(f"  {r['symbol']:14} {r['date']}  {r['sessions']:>2} session(s)  {tag}")
    if args.show:
        row = (store.get("symbols") or {}).get(args.show)
        if row:
            print(json.dumps(row, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
