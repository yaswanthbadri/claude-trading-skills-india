#!/usr/bin/env python3
"""What usually happens after a setup like this one.

A chart tells you where the stock is. It cannot tell you what happened to
other stocks that looked exactly the same — and that is the only honest way to
answer "is this worth buying?".

So the desk measures it. Every trading day in the local candle cache is
scanned for a handful of recognisable situations, and what followed them is
recorded:

    up_day_volume   a +5% day on 1.5x average volume, inside an uptrend
    breakout_20d    a close above the 20-day range high on 1.5x volume
    extended_15     already 15% above the 20-day range high
    run_15_10d      a 15% run inside 10 sessions
    pullback_20dma  a pullback into the 20-day average that held

For each, the forward return at 5, 10 and 20 sessions is kept — how often it
worked, what the typical outcome was, and how wide the spread was. Those are
base rates, not forecasts: they describe what the market has done, and they
are honest about the sample size.

The same measurement is run for the single stock in front of you, because a
stock's own history is more relevant than the market's average.

    python3 tools/base_rates.py                 # market-wide table
    python3 tools/base_rates.py CUPID           # one stock's own history
    python3 tools/base_rates.py --build         # rebuild the cache
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CANDLES = REPO / "data" / "candles"
CACHE = REPO / "data" / "base_rates.json"
STORE = CACHE

HORIZONS = (5, 10, 20)
MIN_BARS = 120

SIGNALS = ("up_day_volume", "breakout_20d", "extended_15", "run_15_10d",
           "pullback_20dma")

SIGNAL_TEXT = {
    "up_day_volume": "a +5% day on 1.5x average volume in an uptrend",
    "breakout_20d": "a close above the 20-day range high on 1.5x volume",
    "extended_15": "already 15%+ above the 20-day range high",
    "run_15_10d": "a 15%+ run inside 10 sessions",
    "pullback_20dma": "a pullback into the 20-day average that held",
}


# ---------------------------------------------------------------------------
# reading the cache
# ---------------------------------------------------------------------------
def load_rows(symbol: str) -> list[dict]:
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


def _sma(vals: list[float], n: int, i: int) -> float | None:
    if i + 1 < n:
        return None
    seg = vals[i - n + 1:i + 1]
    return sum(seg) / n if seg else None


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------
def _forward(rows: list[dict], i: int) -> dict[int, float]:
    """Return at 5/10/20 sessions, and the best excursion inside 20."""
    c = rows[i]["close"]
    out: dict[int, float] = {}
    for h in HORIZONS:
        j = i + h
        if j < len(rows) and rows[j]["close"]:
            out[h] = (rows[j]["close"] / c - 1) * 100
    window = rows[i + 1: min(len(rows), i + 21)]
    if window and c:
        out["mfe"] = (max(r["high"] for r in window) / c - 1) * 100
        out["mae"] = (min(r["low"] for r in window) / c - 1) * 100
    return out


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    wins = [v for v in values if v > 0]
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 2),
        "median": round(statistics.median(values), 2),
        "hit": round(100 * len(wins) / len(values), 1),
        "p25": round(_pctile(values, 25), 2),
        "p75": round(_pctile(values, 75), 2),
    }


def _pctile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def scan_symbol(symbol: str) -> dict:
    """Every occurrence of every signal in this stock's own history."""
    rows = load_rows(symbol)
    if len(rows) < MIN_BARS:
        return {"symbol": symbol, "bars": len(rows), "signals": {}}
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    vols = [r["volume"] for r in rows]

    hits: dict[str, list[dict]] = {s: [] for s in SIGNALS}

    for i in range(60, len(rows)):
        c = closes[i]
        if not c:
            continue
        ma200 = _sma(closes, 200, i)
        ma200_prev = _sma(closes, 200, i - 20) if i - 20 >= 0 else None
        if not ma200 or not ma200_prev:
            continue
        uptrend = c > ma200 and ma200 > ma200_prev
        if not uptrend:
            continue
        vol_avg50 = sum(vols[i - 50:i]) / 50 if i >= 50 else None
        if not vol_avg50:
            continue
        vol_x = vols[i] / vol_avg50
        r1 = (c / closes[i - 1] - 1) * 100 if closes[i - 1] else 0
        r10 = (c / closes[i - 10] - 1) * 100 if i >= 10 and closes[i - 10] else 0
        hi20 = max(highs[i - 20:i]) if i >= 20 else None
        ma20 = _sma(closes, 20, i)
        prev_hi20 = max(highs[i - 21:i]) if i >= 21 else None

        tags = []
        if r1 >= 5 and vol_x >= 1.5:
            tags.append("up_day_volume")
        if prev_hi20 and c > prev_hi20 and vol_x >= 1.5:
            tags.append("breakout_20d")
        if hi20 and c >= hi20 * 1.15:
            tags.append("extended_15")
        if r10 >= 15:
            tags.append("run_15_10d")
        if ma20 and lows[i] <= ma20 and c > ma20 and r1 < 0:
            tags.append("pullback_20dma")
        if not tags:
            continue
        fwd = _forward(rows, i)
        for t in tags:
            hits[t].append({"date": rows[i]["date"], "close": c,
                            "r1": round(r1, 2), "vol_x": round(vol_x, 2),
                            **{k: round(v, 2) for k, v in fwd.items()
                               if isinstance(v, (int, float))}})

    out = {}
    for sig, rows_h in hits.items():
        if not rows_h:
            continue
        entry = {"n": len(rows_h),
                 "dates": [r["date"] for r in rows_h[-8:]]}
        for h in HORIZONS:
            vals = [r[h] for r in rows_h if h in r]
            entry[f"h{h}"] = _stats(vals)
        entry["mfe20"] = _stats([r["mfe"] for r in rows_h if "mfe" in r])
        entry["mae20"] = _stats([r["mae"] for r in rows_h if "mae" in r])
        out[sig] = entry
    return {"symbol": symbol, "bars": len(rows), "signals": out}


def build(verbose: bool = True) -> dict:
    """Scan the whole cache. About a second and a half for 2,400 names."""
    files = sorted(CANDLES.glob("*.csv"))
    per_symbol: dict[str, dict] = {}
    for f in files:
        res = scan_symbol(f.stem)
        if res.get("signals"):
            per_symbol[f.stem] = res["signals"]
    market = _pooled(files, verbose)
    return {
        "version": 1,
        "built": date.today().isoformat(),
        "symbols_scanned": len(files),
        "symbols_with_history": len(per_symbol),
        "market": market,
        "per_symbol": per_symbol,
    }


def _pooled(files: list[Path], verbose: bool = True) -> dict:
    """Pool the raw forward returns of every occurrence in every stock."""
    pools: dict[str, dict[str, list[float]]] = {
        s: {f"h{h}": [] for h in HORIZONS} | {"mfe": [], "mae": []}
        for s in SIGNALS
    }
    n = 0
    for f in files:
        sym = f.stem
        rows = load_rows(sym)
        if len(rows) < MIN_BARS:
            continue
        closes = [r["close"] for r in rows]
        highs = [r["high"] for r in rows]
        lows = [r["low"] for r in rows]
        vols = [r["volume"] for r in rows]
        for i in range(60, len(rows)):
            c = closes[i]
            if not c:
                continue
            ma200 = _sma(closes, 200, i)
            ma200_prev = _sma(closes, 200, i - 20) if i >= 20 else None
            if not ma200 or not ma200_prev:
                continue
            if not (c > ma200 and ma200 > ma200_prev):
                continue
            vol_avg50 = sum(vols[i - 50:i]) / 50 if i >= 50 else None
            if not vol_avg50:
                continue
            vol_x = vols[i] / vol_avg50
            r1 = (c / closes[i - 1] - 1) * 100 if closes[i - 1] else 0
            r10 = (c / closes[i - 10] - 1) * 100 if i >= 10 and closes[i - 10] else 0
            hi20 = max(highs[i - 20:i]) if i >= 20 else None
            prev_hi20 = max(highs[i - 21:i]) if i >= 21 else None
            ma20 = _sma(closes, 20, i)

            tags = []
            if r1 >= 5 and vol_x >= 1.5:
                tags.append("up_day_volume")
            if prev_hi20 and c > prev_hi20 and vol_x >= 1.5:
                tags.append("breakout_20d")
            if hi20 and c >= hi20 * 1.15:
                tags.append("extended_15")
            if r10 >= 15:
                tags.append("run_15_10d")
            if ma20 and lows[i] <= ma20 and c > ma20 and r1 < 0:
                tags.append("pullback_20dma")
            if not tags:
                continue
            fwd = _forward(rows, i)
            for t in tags:
                for h in HORIZONS:
                    if h in fwd:
                        pools[t][f"h{h}"].append(fwd[h])
                if "mfe" in fwd:
                    pools[t]["mfe"].append(fwd["mfe"])
                if "mae" in fwd:
                    pools[t]["mae"].append(fwd["mae"])
        n += 1
        if verbose and n % 500 == 0:
            print(f"  … {n} names", flush=True)
    out = {}
    for sig, buckets in pools.items():
        entry = {}
        for h in HORIZONS:
            entry[f"h{h}"] = _stats(buckets[f"h{h}"])
        entry["mfe20"] = _stats(buckets["mfe"])
        entry["mae20"] = _stats(buckets["mae"])
        if entry["h20"]["n"]:
            out[sig] = entry
    return out


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
def _signature() -> str:
    files = sorted(CANDLES.glob("*.csv"))
    return f"{date.today().isoformat()}:{len(files)}"


def load_cache(max_age_hours: float = 20) -> dict | None:
    if not CACHE.exists():
        return None
    try:
        data = json.loads(CACHE.read_text())
    except Exception:
        return None
    if data.get("signature") != _signature():
        return None
    return data


def save_cache(data: dict) -> None:
    # The per-symbol block is 3 MB and nothing reads it — the one stock being
    # asked about is scanned on demand. The market table is what gets reused.
    data = {k: v for k, v in data.items() if k != "per_symbol"}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, default=str))
    tmp.replace(CACHE)


def market() -> dict:
    """The market-wide base rates, building them if the cache is cold."""
    data = load_cache()
    if data:
        return data
    data = build()
    data["signature"] = _signature()
    try:
        save_cache(data)
    except Exception:
        pass
    return data


def symbol_rates(symbol: str) -> dict:
    """This stock's own history for every signal."""
    return scan_symbol(symbol.upper())


def matches(detail: dict) -> list[str]:
    """Which signals the stock is in right now."""
    out = []
    r1 = detail.get("change_pct") or 0
    vol_x = detail.get("vol_x50") or 0
    ext = detail.get("ext_above_20d_high_pct") or 0
    if r1 >= 5 and vol_x >= 1.5:
        out.append("up_day_volume")
    if (detail.get("close") or 0) > (detail.get("hi20_prev") or 0) and vol_x >= 1.5:
        out.append("breakout_20d")
    if ext >= 15:
        out.append("extended_15")
    if (detail.get("gain_since_breakout_pct") or 0) >= 15:
        out.append("run_15_10d")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("--build", action="store_true", help="rebuild the cache")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.build:
        print("Scanning the universe…")
        data = build()
        data["signature"] = _signature()
        save_cache(data)
        print(f"built: {data['symbols_with_history']} names with history")
    else:
        data = market()

    if args.json:
        print(json.dumps(data if not args.symbol else symbol_rates(args.symbol),
                         indent=1, default=str))
        return 0

    if args.symbol:
        res = symbol_rates(args.symbol)
        print(f"{args.symbol}: {res.get('bars')} bars")
        for sig, entry in (res.get("signals") or {}).items():
            print(f"\n{SIGNAL_TEXT.get(sig, sig)}  (n={entry['n']})")
            for h in HORIZONS:
                st = entry.get(f"h{h}") or {}
                if st.get("n"):
                    print(f"  +{h}d: median {st['median']:+.1f}%  mean {st['mean']:+.1f}%  "
                          f"positive {st['hit']:.0f}% of {st['n']}")
        return 0

    print("Market-wide base rates (all NSE names in the cache)\n")
    for sig, entry in (data.get("market") or {}).items():
        st = entry.get("h10") or {}
        if not st.get("n"):
            continue
        print(f"{SIGNAL_TEXT.get(sig, sig)}  (n={st['n']})")
        for h in HORIZONS:
            s = entry.get(f"h{h}") or {}
            if s.get("n"):
                print(f"  +{h}d: median {s['median']:+.1f}%  mean {s['mean']:+.1f}%  "
                      f"positive {s['hit']:.0f}%   range {s['p25']:+.1f}% to {s['p75']:+.1f}%")
        mfe = entry.get("mfe20") or {}
        if mfe.get("n"):
            print(f"  best excursion within 20d: median {mfe['median']:+.1f}%")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
