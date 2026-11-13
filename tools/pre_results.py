#!/usr/bin/env python3
"""Buy the setup before the number, not after the gap.

Being told "TCS reported and rose 6%" is worth nothing: the move has already
happened, and anyone buying now is buying the gap. What is worth something is
knowing three things before the bell:

    when        the company reports, confirmed from the company's own schedule
    whether     the chart is ready for it — a Stage-2 trend, a tight base, a
                level to buy at with a stop under it
    what        the market has done after this stock's last few prints, and
                after prints in general, measured rather than guessed

That last one is the whole point. A results trade is not a breakout trade:
the gap can jump straight through the stop, and no stop order protects you
while the exchange is shut. So the numbers have to say how often that
happens and what the payoff looks like afterwards.

    python3 tools/pre_results.py             # the names inside the window
    python3 tools/pre_results.py --json
    python3 tools/pre_results.py --within 10
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))


def _rows(symbol: str) -> list[dict]:
    try:
        import tech_detail
        return tech_detail.load_candles(symbol)
    except Exception:
        return []


def _forward(rows: list[dict], idx: int, horizons=(1, 5, 10)) -> dict:
    """The move after a bar, at a few horizons."""
    out = {}
    base = rows[idx]["close"]
    if not base:
        return out
    for h in horizons:
        j = idx + h
        if j < len(rows):
            out[h] = (rows[j]["close"] / base - 1) * 100
    # the overnight gap is the part a stop cannot protect against
    if idx + 1 < len(rows):
        out["gap"] = (rows[idx + 1]["open"] / base - 1) * 100
    return out


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 2),
        "median": round(statistics.median(values), 2),
        "hit": round(100 * sum(1 for v in values if v > 0) / len(values), 1),
        "best": round(max(values), 2),
        "worst": round(min(values), 2),
    }


def reaction(symbol: str, dates: list[str], horizons=(1, 5, 10)) -> dict:
    """What the market did after this stock's past prints."""
    rows = _rows(symbol)
    if not rows:
        return {"n": 0}
    index = {r["date"]: i for i, r in enumerate(rows)}
    samples = []
    for d in dates:
        i = index.get(d)
        if i is None or i + 1 >= len(rows):
            continue
        fwd = _forward(rows, i, horizons)
        if fwd:
            samples.append(fwd)
    out = {"n": len(samples), "dates": [d for d in dates if d in index]}
    for h in horizons:
        out[f"h{h}"] = _stats([s[h] for s in samples if h in s])
    out["gap"] = _stats([s["gap"] for s in samples if "gap" in s])
    return out


def market_reaction(horizons=(1, 5, 10)) -> dict:
    """What the market did after prints in general.

    Measured from every print in the calendar: the pooled sample of gaps and
    forward moves. This is the number that tells you whether a results trade
    is worth the risk before you take it.
    """
    import results_calendar as rc
    store = rc.load()
    pooled = {h: [] for h in horizons}
    gaps = []
    n = 0
    for sym, row in (store.get("symbols") or {}).items():
        rows = _rows(sym)
        if not rows:
            continue
        index = {r["date"]: i for i, r in enumerate(rows)}
        for hrow in (row.get("history") or []):
            i = index.get(hrow.get("date"))
            if i is None or i + 1 >= len(rows):
                continue
            fwd = _forward(rows, i, horizons)
            if not fwd:
                continue
            n += 1
            for h in horizons:
                if h in fwd:
                    pooled[h].append(fwd[h])
            if "gap" in fwd:
                gaps.append(fwd["gap"])
    out = {"n": n}
    for h in horizons:
        out[f"h{h}"] = _stats(pooled[h])
    out["gap"] = _stats(gaps)
    return out


# ---------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------
def setup_for(symbol: str) -> dict:
    """The chart read for one name: is it ready for a catalyst?"""
    import subprocess
    py = sys.executable
    br = REPO / "skills" / "breakout-planner" / "scripts" / "breakout.py"
    csv_path = REPO / "data" / "candles" / f"{symbol}.csv"
    bench = REPO / "data" / "nifty.csv"
    args = [py, str(br), "check"]
    if csv_path.exists() and bench.exists():
        args += ["--csv", str(csv_path), "--benchmark-csv", str(bench)]
    else:
        args += ["--symbol", symbol, "--benchmark", "NIFTY"]
    args += ["--json"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=90,
                           cwd=str(REPO))
        if r.returncode or not r.stdout.strip():
            return {}
        d = json.loads(r.stdout)
    except Exception:
        return {}
    tt = d.get("trend_template") or {}
    b = d.get("base") or {}
    return {
        "symbol": symbol,
        "close": d.get("close"),
        "stage2": bool(tt.get("stage2")),
        "tt_passed": tt.get("passed"), "tt_total": tt.get("total"),
        "base_type": b.get("type"), "base_grade": b.get("grade"),
        "base_state": b.get("state"),
        "pivot": b.get("pivot"), "from_pivot_pct": b.get("from_pivot_pct"),
        "depth_pct": b.get("depth_pct"), "weeks": b.get("weeks"),
        "liquidity_cr": (d.get("liquidity") or {}).get("avg_traded_value_cr"),
    }


def scan(within: int = 7, with_setup: bool = True) -> list[dict]:
    """Every name with a print inside the window, and how ready it is."""
    import results_calendar as rc
    rows = rc.upcoming(within)
    out = []
    for r in rows:
        row = dict(r)
        if with_setup:
            row["setup"] = setup_for(r["symbol"])
            hist_dates = [h["date"] for h in (r.get("history") or [])]
            row["reaction"] = reaction(r["symbol"], hist_dates)
        out.append(row)
    return out


def ready(row: dict) -> bool:
    """Ready means: Stage 2, a tight base, and the price near the level."""
    s = row.get("setup") or {}
    if not s:
        return False
    if not s.get("stage2"):
        return False
    if str(s.get("base_grade") or "").upper() not in ("A", "B"):
        return False
    fp = s.get("from_pivot_pct")
    if fp is not None and not (-4.0 <= fp <= 1.5):
        return False
    return True


def card(rows: list[dict], market: dict | None = None) -> list[str]:
    """The report section and the alert body."""
    out = []
    if not rows:
        return ["No confirmed or estimated results inside the window."]
    for r in rows:
        s = r.get("setup") or {}
        react = r.get("reaction") or {}
        tag = "confirmed" if r.get("confirmed") else "estimated"
        when = f"{r['date']} ({r['sessions']} session(s), {tag})"
        line = [f"📅 <b>{r['symbol']}</b> reports {when}"]
        if s:
            grade = s.get("base_grade") or "?"
            line.append(f"   chart: {'Stage 2 ✓' if s.get('stage2') else 'not Stage 2'}, "
                        f"{s.get('base_type') or 'no base'} grade {grade}"
                        + (f", pivot {s.get('pivot'):,.2f} "
                           f"({s.get('from_pivot_pct'):+.1f}% away)"
                           if s.get("pivot") and s.get("from_pivot_pct") is not None
                           else ""))
        if react.get("n"):
            g = react.get("gap") or {}
            h5 = react.get("h5") or {}
            line.append(f"   its last {react['n']} print(s): median overnight gap "
                        f"{g.get('median', 0):+.1f}%, 5-session median "
                        f"{h5.get('median', 0):+.1f}%")
        out.append("\n".join(line))
    if market and (market.get("h5") or {}).get("n"):
        m5 = market["h5"]
        mg = market.get("gap") or {}
        out.append("")
        out.append(f"<i>Across {market['n']:,} prints in the market: the "
                   f"overnight gap has a median of {mg.get('median', 0):+.1f}% "
                   f"and 5 sessions later the median move is "
                   f"{m5.get('median', 0):+.1f}% (positive {m5.get('hit', 0):.0f}% "
                   f"of the time). A stop does not protect you through the gap "
                   "— size for it.</i>")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--within", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-setup", action="store_true")
    args = ap.parse_args()

    rows = scan(args.within, with_setup=not args.no_setup)
    if args.json:
        print(json.dumps(rows, indent=1, default=str))
        return 0
    market = market_reaction()
    for line in card(rows, market):
        print(line)
    print()
    print("ready for a results trade:")
    for r in rows:
        if ready(r):
            s = r["setup"]
            print(f"  {r['symbol']:14} {r['date']}  {r['sessions']} session(s)  "
                  f"pivot {s.get('pivot')} ({s.get('from_pivot_pct'):+.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
