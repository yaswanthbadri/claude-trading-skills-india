#!/usr/bin/env python3
"""What else is going to happen while you hold this.

A breakout plan is a plan to hold something for weeks. Something always is
happening in those weeks: an expiry, an RBI decision, a budget, the day
results-season ends and half the market gaps. None of those are reasons not
to trade — they are reasons to size the trade knowing they are there.

This reads the event calendar (the event-calendar skill) and asks a narrow
question about the window between the entry and the second target:

    what lands inside the holding window
    does it touch this stock, or the whole market
    how much does it matter

The output is a short list, not a calendar dump. "NSE monthly expiry on the
23rd" is useful. "Friday" is not.

    python3 tools/event_risk.py --symbol ACMESOLAR --from 2026-11-16 --to 2026-12-16
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVENTS = REPO / "skills" / "event-calendar" / "scripts" / "events.py"
PYTHON = sys.executable

# What is worth telling a trader about, and how loudly.
IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}
# Events that move the whole market regardless of the stock.
MARKET_CATEGORIES = {"expiry", "expiry-week", "rbi", "budget", "fomc", "cpi",
                     "wpi", "iip", "gdp", "pmi", "index", "holiday",
                     "results-season"}


def _events(from_date: str, to_date: str) -> list[dict]:
    try:
        r = subprocess.run(
            [PYTHON, str(EVENTS), "range", "--from", from_date, "--to", to_date,
             "--json"],
            capture_output=True, text=True, timeout=60, cwd=str(REPO))
        if r.returncode or not r.stdout.strip():
            return []
        return json.loads(r.stdout).get("events") or []
    except Exception:
        return []


def _fmt(d: str) -> str:
    try:
        return date.fromisoformat(d).strftime("%d %b")
    except Exception:
        return d


def window_events(symbol: str, from_date: str, to_date: str) -> list[dict]:
    """The events inside the window, sorted by how much they matter."""
    out = []
    for e in _events(from_date, to_date):
        start = e.get("date") or ""
        end = e.get("end") or ""
        # a season window overlaps if any part of it is inside the hold
        if end and end < from_date:
            continue
        if start > to_date:
            continue
        sym = (e.get("symbol") or "").upper()
        company = bool(sym) and sym == symbol.upper()
        cat = e.get("category") or ""
        if not company and cat not in MARKET_CATEGORIES:
            continue
        impact = e.get("impact") or "low"
        out.append({
            "date": start, "end": end or None, "title": e.get("title") or "",
            "category": cat, "impact": impact, "company": company,
            "kind": e.get("kind") or e.get("status"),
            "time_ist": e.get("time_ist"),
            "why": e.get("why") or "",
        })
    out.sort(key=lambda x: (IMPACT_ORDER.get(x["impact"], 3), x["date"]))
    return out


def risk(symbol: str, from_date: str, to_date: str) -> dict:
    """The events that matter inside the window.

    A month of daily expiries is not risk, it is a calendar. What a trader
    needs is the monthly expiry, the policy decision, the budget, and
    anything about their own stock — and the results-season deadline, which
    is the week the whole market gaps.
    """
    evs = window_events(symbol, from_date, to_date)
    high = [e for e in evs if e["impact"] == "high"]
    company = [e for e in evs if e["company"]]
    material = [e for e in evs
                if e["impact"] == "high"
                or e["company"]
                or e["category"] == "results-season"]
    # The weekly expiries inside the window are noise once the monthly one is
    # in the list.
    if any(e["category"] in ("expiry", "expiry-week") and e["impact"] == "high"
           for e in material):
        material = [e for e in material
                    if e["category"] not in ("expiry", "expiry-week")
                    or e["impact"] == "high"]
    return {
        "events": evs,
        "high": high,
        "company": company,
        "material": material,
        "market": [e for e in evs if not e["company"]],
        "level": "high" if high else ("medium" if company else "none"),
        "text": _text(material),
    }


def _text(evs: list[dict]) -> str:
    if not evs:
        return ""
    bits = []
    for e in evs[:3]:
        when = _fmt(e["date"])
        if e["end"] and e["end"] != e["date"]:
            when += f"–{_fmt(e['end'])}"
        bits.append(f"{when}: {e['title']}")
    return "; ".join(bits)


def holding_window(entry_date: str, sessions: int = 20) -> tuple[str, str]:
    """The window a swing trade would be held for."""
    try:
        d = date.fromisoformat(entry_date)
    except Exception:
        d = date.today()
    return d.isoformat(), (d + timedelta(days=int(sessions * 1.45))).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="")
    ap.add_argument("--from", dest="start", default=None)
    ap.add_argument("--to", dest="end", default=None)
    ap.add_argument("--sessions", type=int, default=20)
    args = ap.parse_args()

    start = args.start
    end = args.end
    if not start or not end:
        start, end = holding_window(date.today().isoformat(), args.sessions)
    res = risk(args.symbol, start, end)
    print(f"{start} → {end}   risk: {res['level']}")
    for e in res["events"]:
        who = "THIS STOCK" if e["company"] else "market"
        print(f"  {e['date']} {e['impact']:6} {who:11} {e['title']}")
    if res["text"]:
        print(f"\n{res['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
