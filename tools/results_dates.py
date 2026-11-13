#!/usr/bin/env python3
"""When does this stock report, and does that date fall inside your holding?

A Stage-2 breakout three sessions before results is not a breakout. It is a
coin flip with a 5-15% gap on one side, and no stop can protect you from a gap
through the level while the market is shut.

Two sources, and the difference is stated in the report:

    confirmed  yfinance has a scheduled earnings date for this company
    estimated  no schedule, so the next date is inferred from its own reporting
               cadence (median gap between past results, usually ~91 days)
    unknown    no usable history; the report says nothing rather than guess

Estimates are labelled as estimates. An inferred date is still worth knowing,
because a trade closed one week early costs a few percent of upside, while a
gap through a stop costs a full R.

Cached for 20 hours: a symbol is fetched once a day at most, so a report that
shows the same six names costs nothing after the morning run.

    python3 tools/results_dates.py --symbols CENTUM,TCS,M&M
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CACHE = REPO / "data" / "results_dates.json"
MAX_AGE_HOURS = 20
MAX_FETCH = 14           # per run: keeps a cold cache from stalling the report
GAP_PCT = 3.5            # a gap this big is usually a reaction to something
MIN_GAP_SAMPLES = 3      # below this there is no cadence to learn
HISTORY_DAYS = 200       # older than this and the cadence is not a cadence
DEFAULT_GAP_DAYS = 91


def _load() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except Exception:
            pass
    return {"version": 1, "symbols": {}}


def _save(store: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1, default=str))
    tmp.replace(CACHE)


def _fresh(entry: dict | None) -> bool:
    if not entry or not entry.get("at"):
        return False
    try:
        age = datetime.now() - datetime.fromisoformat(entry["at"])
    except Exception:
        return False
    return age < timedelta(hours=MAX_AGE_HOURS)


# ---------------------------------------------------------------------------
# one symbol
# ---------------------------------------------------------------------------
def _calendar_dates(tk) -> list[date]:
    try:
        cal = tk.calendar or {}
    except Exception:
        return []
    return [d for d in (cal.get("Earnings Date") or []) if isinstance(d, date)]


def _from_calendar(tk, today: date) -> date | None:
    future = [d for d in _calendar_dates(tk) if d >= today]
    return min(future) if future else None


def _from_history(tk, today: date,
                  known_last: date | None = None) -> tuple[date | None, str]:
    try:
        df = tk.get_earnings_dates(limit=12)
    except Exception:
        return None, "unknown"
    if df is None or getattr(df, "empty", True):
        return None, "unknown"

    past = sorted({i.date() if hasattr(i, "date") else i
                   for i in df.index
                   if (i.date() if hasattr(i, "date") else i) <= today},
                  reverse=True)
    if not past:
        return None, "unknown"

    # The history endpoint lags the calendar. M&M reported on 5 Nov and the
    # history still ended in September, which put the estimate two months
    # early. Anchor on whichever source knows about the most recent print.
    last = past[0]
    if known_last and known_last > last:
        last = known_last
    if (today - last).days > HISTORY_DAYS:
        # A 2017 date tells us nothing about this quarter.
        return None, "unknown"

    gaps = [(past[i] - past[i + 1]).days for i in range(len(past) - 1)]
    gaps = [g for g in gaps if 60 <= g <= 130]
    gap = int(statistics.median(gaps)) if gaps else DEFAULT_GAP_DAYS

    nxt = last + timedelta(days=gap)
    while nxt < today:
        nxt += timedelta(days=gap)
    return nxt, "estimated"


def lookup(symbol: str, store: dict, today: date) -> dict:
    entry = store["symbols"].get(symbol)
    if _fresh(entry):
        return {"date": entry.get("date"), "source": entry.get("source") or "unknown"}
    return {"date": None, "source": "unknown"}     # placeholder until fetched


def fetch(symbol: str, today: date) -> dict:
    try:
        import yfinance as yf
        tk = yf.Ticker(f"{symbol}.NS")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] {symbol}: {exc}", file=sys.stderr)
        return {"date": None, "source": "unknown"}

    cal_dates = _calendar_dates(tk)
    d = min([x for x in cal_dates if x >= today], default=None)
    if d:
        return {"date": d.isoformat(), "source": "confirmed"}

    known_last = max([x for x in cal_dates if x < today], default=None)
    d, source = _from_history(tk, today, known_last=known_last)
    return {"date": d.isoformat() if d else None, "source": source}


# ---------------------------------------------------------------------------
# the offline estimator
# ---------------------------------------------------------------------------
def _gap_days(rows: list[dict], today: date) -> list[date]:
    """Days that opened far away from the previous close.

    A results print moves a stock several percent overnight. Over three years
    of daily bars, those gaps fall into a quarterly rhythm, and the rhythm is
    the estimate. It is not as good as a confirmed date and it is not the only
    cause of a gap — but it needs no network, which means it works for 2,400
    names instead of the fourteen a live lookup can afford.
    """
    out = []
    for i in range(1, len(rows)):
        prev = rows[i - 1]["close"]
        if not prev:
            continue
        gap = (rows[i]["open"] - prev) / prev * 100
        if abs(gap) >= GAP_PCT:
            try:
                d = date.fromisoformat(rows[i]["date"][:10])
            except ValueError:
                continue
            if d <= today:
                out.append(d)
    return out


def estimate_offline(symbol: str, today: date | None = None) -> dict:
    """The next likely results date, learned from the stock's own gaps.

    A results print moves a stock several percent overnight. Over three years
    of daily bars those gaps fall into a quarterly rhythm, and the rhythm is
    the estimate.

    The gaps are not only results — a block deal, an index rebalance or a
    regulatory order gaps too — so the days are chained backwards at roughly
    quarterly intervals rather than simply taking the median gap between
    them. What survives is the quarterly skeleton; the noise drops out.
    """
    today = today or date.today()
    try:
        import tech_detail
        rows = tech_detail.load_candles(symbol)
    except Exception:
        return {"date": None, "source": "unknown", "samples": 0}
    if len(rows) < 250:
        return {"date": None, "source": "unknown", "samples": 0}

    days = _gap_days(rows, today)
    if len(days) < MIN_GAP_SAMPLES:
        return {"date": None, "source": "unknown", "samples": len(days)}

    # Chain backwards: from each anchor, the most recent earlier gap that is
    # one quarter away. Anything closer is noise, anything further is a
    # missed print.
    chain = []
    anchor = days[-1]
    remaining = list(days[:-1])
    while remaining and len(chain) < 12:
        cand = None
        for d in reversed(remaining):
            delta = (anchor - d).days
            if 55 <= delta <= 130:
                cand = d
                break
        if cand is None:
            break
        chain.append(cand)
        remaining = [d for d in remaining if d < cand]
        anchor = cand
    if len(chain) < MIN_GAP_SAMPLES - 1:
        return {"date": None, "source": "unknown", "samples": len(days)}

    diffs = [(chain[0] - c).days for c in chain[1:]]
    gap = int(statistics.median(diffs))
    nxt = days[-1] + timedelta(days=gap)
    while nxt < today:
        nxt += timedelta(days=gap)
    return {"date": nxt.isoformat(), "source": "estimated",
            "samples": len(days), "cadence_days": gap,
            "last_gap": days[-1].isoformat()}


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------
def trading_days_until(target: str | None, today: date | None = None) -> int | None:
    """Sessions between today and target, ignoring weekends and NSE holidays."""
    if not target:
        return None
    today = today or date.today()
    try:
        goal = date.fromisoformat(target)
    except Exception:
        return None
    if goal < today:
        return 0
    try:
        import market_calendar as cal
        is_td = cal.is_trading_day
    except Exception:
        is_td = lambda d: d.weekday() < 5
    n, d = 0, today
    while d < goal:
        d += timedelta(days=1)
        if is_td(d):
            n += 1
    return n


def annotate(symbols: list[str]) -> dict[str, dict]:
    """symbol -> {date, source, sessions}. Fills the cache for what is missing."""
    today = date.today()
    store = _load()
    wanted = [s for s in dict.fromkeys(symbols) if s]
    fetched = 0

    for sym in wanted:
        if _fresh(store["symbols"].get(sym)):
            continue
        if fetched >= MAX_FETCH:
            break
        res = fetch(sym, today)
        store["symbols"][sym] = {"at": datetime.now().isoformat(timespec="seconds"),
                                 **res}
        fetched += 1

    if fetched:
        _save(store)

    out = {}
    for sym in wanted:
        e = store["symbols"].get(sym) or {}
        d = e.get("date")
        out[sym] = {"date": d, "source": e.get("source") or "unknown",
                    "sessions": trading_days_until(d, today)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True,
                    help="comma-separated NSE symbols, e.g. CENTUM,TCS")
    args = ap.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    for sym, row in annotate(syms).items():
        when = row["date"] or "unknown"
        days = row["sessions"]
        tail = f" · in {days} session(s)" if days is not None else ""
        print(f"{sym:14} {when:12} {row['source']:9}{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
