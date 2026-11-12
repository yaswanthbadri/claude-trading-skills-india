#!/usr/bin/env python3
"""Did the alert actually precede a move?

Every alert this desk sends is a claim: something happened to this stock and it
matters. Until now nobody checked. logs/alerts.jsonl records what went out, but
not what the stock did next, so a whole event category that never precedes a
move would keep firing forever.

This replays each alert against the candle cache:

    T+1    close of the next session against the close on the alert day
    T+5    close five sessions later, same basis

Two things it can tell you:

    by event type   "order win" alerts average +1.8% at T+5; "analyst action"
                    averages -0.1%, which means deleting a rule, not tuning it
    by direction    a shock alert after a 6% fall should mean something. If the
                    average T+5 is +0.2%, the shock scan is describing noise.

Direction is inferred, not assumed: an alert on a stock that had already fallen
is a different claim from one on a stock making a new high.

Usage:
    python3 tools/alert_outcomes.py
    python3 tools/alert_outcomes.py --json
    python3 tools/alert_outcomes.py --min-n 3
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / "logs" / "alerts.jsonl"
CANDLES = REPO / "data" / "candles"

sys.path.insert(0, str(REPO / "tools"))


def load_alerts() -> list[dict]:
    if not AUDIT.exists():
        return []
    out = []
    for line in AUDIT.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _bars(symbol: str) -> list[dict]:
    """Close-only series, enough to look forward a week."""
    path = CANDLES / f"{symbol}.csv"
    if not path.exists():
        return []
    out = []
    try:
        import csv
        with path.open() as fh:
            for r in csv.DictReader(fh):
                try:
                    out.append({"date": r["Date"][:10], "close": float(r["Close"])})
                except Exception:
                    continue
    except Exception:
        return []
    return out


def score(alert: dict, horizons=(1, 5)) -> dict | None:
    bars = _bars(alert.get("symbol") or "")
    if not bars:
        return None
    day = (alert.get("ts") or "")[:10]
    idx = next((i for i, b in enumerate(bars) if b["date"] >= day), None)
    if idx is None:
        return None
    base = bars[idx]["close"]
    if not base:
        return None
    out = {"symbol": alert["symbol"], "event": alert.get("event") or "unknown",
           "date": bars[idx]["date"], "base": base}
    for h in horizons:
        j = idx + h
        out[f"t{h}"] = round(100 * (bars[j]["close"] / base - 1), 2) if j < len(bars) else None
    # Which way was the stock already going? A shock after a fall is a
    # different claim from a shock at a new high.
    out["prior5"] = (round(100 * (base / bars[idx - 5]["close"] - 1), 1)
                     if idx >= 5 else None)
    return out


def summarise(rows: list[dict], min_n: int = 2) -> dict:
    done = [r for r in rows if r.get("t5") is not None]
    by_event: dict[str, list[dict]] = {}
    for r in done:
        by_event.setdefault(r["event"], []).append(r)

    buckets = []
    for event, rs in by_event.items():
        t1 = [r["t1"] for r in rs if r.get("t1") is not None]
        t5 = [r["t5"] for r in rs if r.get("t5") is not None]
        buckets.append({
            "event": event,
            "n": len(rs),
            "avg_t1": round(sum(t1) / len(t1), 2) if t1 else None,
            "avg_t5": round(sum(t5) / len(t5), 2) if t5 else None,
            "positive_pct": round(100 * sum(1 for r in rs if r["t5"] > 0) / len(rs)),
        })
    buckets.sort(key=lambda b: -(b["avg_t5"] if b["avg_t5"] is not None else -99))

    return {
        "alerts": len(rows),
        "scored": len(done),
        "awaiting": len(rows) - len(done),
        "min_n": min_n,
        "buckets": buckets,
        "weak": [b for b in buckets if b["n"] >= min_n
                 and (b["avg_t5"] is None or abs(b["avg_t5"]) < 0.5)],
    }


def report(min_n: int = 2) -> dict:
    rows = [s for s in (score(a) for a in load_alerts()) if s]
    return summarise(rows, min_n)


def lines(res: dict) -> list[str]:
    if not res["alerts"]:
        return ["No alerts recorded yet — nothing to score."]
    out = [f"{res['alerts']} alerts sent, {res['scored']} old enough to judge "
           f"(T+5), {res['awaiting']} still too recent."]
    for b in res["buckets"]:
        if b["n"] < res["min_n"]:
            continue
        out.append(f"{b['event']}: {b['n']} alerts, average "
                   f"{b['avg_t5']:+.1f}% after 5 sessions "
                   f"({b['positive_pct']}% up).")
    weak = [b["event"] for b in res["weak"]]
    if weak:
        out.append("No measurable edge, consider retiring: " + ", ".join(weak) + ".")
    elif res["scored"]:
        out.append("Every event type with a sample moved the stock; no rule to cut yet.")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-n", type=int, default=2)
    args = ap.parse_args()
    res = report(args.min_n)
    if args.json:
        print(json.dumps(res, indent=1))
        return 0
    for line in lines(res):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
