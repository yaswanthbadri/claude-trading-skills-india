#!/usr/bin/env python3
"""What the tape is actually doing to this stock, in numbers.

The card used to say "Pivot 328.70 · now +17.1% from it" and leave it there.
That number is true and useless: it describes a level the market cleared weeks
ago. What a reader needs is the state of the stock *now* — how far it has run,
what the last three weeks did, how big its daily range is, how much volume
came with the move — because those decide whether there is anything left to
buy and where the risk sits.

Everything here is computed from the local candle cache, so it is the official
close rather than whatever the last Yahoo tick happened to be.

    python3 tools/tech_detail.py CUPID
    python3 tools/tech_detail.py CUPID --json
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CANDLES = REPO / "data" / "candles"
SKILLS = REPO / "skills"


def _sibling(skill: str, script: str):
    name = f"_tech_detail_dep_{script}"
    if name in sys.modules:
        return sys.modules[name]
    path = SKILLS / skill / "scripts" / f"{script}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ta = _sibling("technical-analysis", "ta")


def load_candles(symbol: str) -> list[dict]:
    path = CANDLES / f"{symbol}.csv"
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for row in csv.DictReader(f):
            try:
                out.append({
                    "date": row["Date"][:10],
                    "open": float(row["Open"]), "high": float(row["High"]),
                    "low": float(row["Low"]), "close": float(row["Close"]),
                    "volume": float(row["Volume"]),
                })
            except (KeyError, ValueError, TypeError):
                continue
    return out


def _last(seq):
    for v in reversed(seq):
        if v is not None:
            return v
    return None


def _pct(a, b) -> float | None:
    """a as a percentage change from b."""
    try:
        if not a or not b:
            return None
        return (a / b - 1) * 100
    except (TypeError, ZeroDivisionError):
        return None


def _ratio(a, b) -> float | None:
    """a as a percentage of b — a size, not a change."""
    try:
        if a is None or not b:
            return None
        return a / b * 100
    except (TypeError, ZeroDivisionError):
        return None


def detail(symbol: str) -> dict:
    rows = load_candles(symbol)
    if len(rows) < 60:
        return {"error": f"no usable history for {symbol}"}

    ind = ta.compute(rows)
    dash = ta.dashboard(ind)
    c = ind["close"][-1]
    prev = ind["close"][-2] if len(ind["close"]) > 1 else None
    v = ind["volume"][-1]
    h = ind["high"][-1]
    lo = ind["low"][-1]
    o = ind["open"][-1]

    sma20 = _last(ind["sma20"])
    sma50 = _last(ind["sma50"])
    sma200 = _last(ind["sma200"])
    atr14 = _last(ind["atr"])
    rsi14 = _last(ind["rsi"])
    adx14 = _last(ind["adx"])

    # Volume against its own history, not against a market average.
    vols = ind["volume"]
    v20 = sum(vols[-21:-1]) / max(1, len(vols[-21:-1])) if len(vols) > 21 else None
    v50 = sum(vols[-51:-1]) / max(1, len(vols[-51:-1])) if len(vols) > 51 else None

    # The 20-day range high the market broke out of, and how far above it we are.
    hi20_prev = max(r["high"] for r in rows[-21:-1])
    lo20_prev = min(r["low"] for r in rows[-21:-1])
    ext20 = _pct(c, hi20_prev)

    # 52-week window.
    win = rows[-250:]
    hi52 = max(r["high"] for r in win)
    lo52 = min(r["low"] for r in win)
    pos52 = (c - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else None

    # When did it last close above the 20-day range high? That is the breakout
    # date, and it is what makes "17% above the pivot" a story rather than a
    # number.
    breakout_date = None
    breakout_close = None
    for i in range(len(rows) - 1, max(60, len(rows) - 60) - 1, -1):
        prior_hi = max(r["high"] for r in rows[max(0, i - 20):i])
        if rows[i]["close"] > prior_hi and rows[i]["volume"] > 0:
            # the first such day in the run, not the latest
            continue
    # walk forward to find the FIRST breakout of the current leg
    leg_start = None
    for i in range(len(rows) - 1, max(0, len(rows) - 120) - 1, -1):
        prior_hi = max(r["high"] for r in rows[max(0, i - 20):i])
        if rows[i]["close"] > prior_hi:
            leg_start = i
            break
    if leg_start is not None:
        breakout_date = rows[leg_start]["date"]
        breakout_close = rows[leg_start]["close"]
        days_since = len(rows) - 1 - leg_start
    else:
        days_since = None

    # A measured move: the height of the base added to the pivot.
    measured = None
    if leg_start is not None and leg_start >= 20:
        base_hi = max(r["high"] for r in rows[leg_start - 20:leg_start])
        base_lo = min(r["low"] for r in rows[leg_start - 20:leg_start])
        pivot = base_hi
        measured = pivot + (pivot - base_lo)

    gain_since = _pct(c, breakout_close) if breakout_close else None

    # Distribution: how many of the last 20 sessions closed in the top third
    # of their range. Heavy distribution after a run is the warning that the
    # move is being handed over.
    top_third = 0
    counted = 0
    for r in rows[-20:]:
        rng = r["high"] - r["low"]
        if rng <= 0:
            continue
        counted += 1
        if (r["close"] - r["low"]) / rng >= 0.66:
            top_third += 1
    distribution = round(100 * top_third / counted) if counted else None

    # Average daily range as a share of price — the honest measure of how hard
    # this stock is to hold.
    ranges = [(r["high"] - r["low"]) / r["close"] * 100 for r in rows[-21:-1]
              if r["close"]]
    avg_range = sum(ranges) / len(ranges) if ranges else None

    return {
        "symbol": symbol,
        "close": c,
        "prev_close": prev,
        "change_pct": _pct(c, prev),
        "gap_pct": _pct(o, prev),
        "day_range_pct": _ratio(h - lo, c),
        "date": rows[-1]["date"],
        "bars": len(rows),
        "rsi": rsi14,
        "atr": atr14,
        "atr_pct": _ratio(atr14, c),
        "adx": adx14,
        "sma20": sma20, "sma50": sma50, "sma200": sma200,
        "from_sma20_pct": _pct(c, sma20),
        "from_sma50_pct": _pct(c, sma50),
        "from_sma200_pct": _pct(c, sma200),
        "high_52w": hi52, "low_52w": lo52,
        "position_52w_pct": pos52,
        "from_52w_high_pct": _pct(c, hi52),
        "volume": v,
        "vol_x20": (v / v20) if v20 else None,
        "vol_x50": (v / v50) if v50 else None,
        "avg_range_pct": avg_range,
        "hi20_prev": hi20_prev,
        "ext_above_20d_high_pct": ext20,
        "breakout_date": breakout_date,
        "breakout_close": breakout_close,
        "days_since_breakout": days_since,
        "gain_since_breakout_pct": gain_since,
        "measured_move_target": measured,
        "from_measured_move_pct": _pct(c, measured) if measured else None,
        "distribution_pct": distribution,
        "signals": dash["signals"],
        "counts": dash["counts"],
        "india_checks": ta.india_checks(ind),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    d = detail(args.symbol.upper())
    if args.json:
        print(json.dumps(d, indent=1, default=str))
    else:
        for k, v in d.items():
            if k in ("signals", "india_checks"):
                continue
            print(f"{k:26} {v}")
        print("\nsignals:")
        for k, (txt, state) in (d.get("signals") or {}).items():
            print(f"  {k:16} {txt}  [{state}]")
        print("\nindia checks:")
        for c in d.get("india_checks") or []:
            print(f"  {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
