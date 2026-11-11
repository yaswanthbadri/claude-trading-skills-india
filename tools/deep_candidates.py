#!/usr/bin/env python3
"""Deep "path to 100%" analysis for a short list of stocks.

The section this feeds is deliberately honest: no setup *guarantees* a 100%
gain. What this module does is quantify, per stock, the three things that
actually decide whether a doubler happens:

  1. STRUCTURE  - Stage-2 trend template, base quality, where the pivot is,
                  how young the advance is (most doublers start in the first
                  18 months of a Stage-2 run, not the fifth year).
  2. EVIDENCE   - the stock's own history: from similar Stage-2 conditions,
                  how often did it gain 50% / 100% within 12 months, and how
                  long did that take. Computed from its own daily bars, so it
                  is a base rate, not a promise.
  3. ARITHMETIC - entry, stop, T1, T2, the exact price that equals +100%, the
                  R multiple that implies, the share count, and the rupee
                  result of a 1/3 - 1/3 - 1/3 ladder.

Everything is computed locally from OHLCV plus the breakout skill's structured
JSON. Stdlib + pandas only.

Usage (as a module):
    from deep_candidates import analyse
    rows = analyse(breakout_scan_results, plans_by_symbol, capital=500000)
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SK = REPO / "skills"
PYTHON = sys.executable

HISTORY_PERIOD = "10y"
MIN_BARS = 300            # need ~15 months of bars for a meaningful base rate
MIN_BASE_RATE_BARS = 252  # forward window for the doubling study


# ---------------------------------------------------------------------------
# small indicators (kept local so the module has no extra dependencies)
# ---------------------------------------------------------------------------
def _sma(s, n):
    return s.rolling(n).mean()


def _atr(df, n=14):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev = close.shift(1)
    tr = (high - low).to_frame("a")
    tr["b"] = (high - prev).abs()
    tr["c"] = (low - prev).abs()
    return tr.max(axis=1).rolling(n).mean()


def _rsi(close, n=14):
    delta = close.diff()
    up = delta.clip(lower=0)
    down = (-delta).clip(lower=0)
    # Wilder smoothing
    roll_up = up.ewm(alpha=1 / n, adjust=False).mean()
    roll_down = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, float("nan"))
    return 100 - 100 / (1 + rs)


def _swing_levels(df, close, look=5, window=250):
    """Cluster recent swing highs/lows into support and resistance."""
    d = df.tail(window)
    highs, lows = [], []
    h, l = d["High"].values, d["Low"].values
    for i in range(look, len(d) - look):
        if h[i] == max(h[i - look:i + look + 1]):
            highs.append(float(h[i]))
        if l[i] == min(l[i - look:i + look + 1]):
            lows.append(float(l[i]))

    def cluster(vals):
        vals = sorted(vals)
        out = []
        for v in vals:
            if out and abs(v - out[-1][-1]) / max(out[-1][-1], 1e-9) <= 0.015:
                out[-1].append(v)
            else:
                out.append([v])
        return [sum(g) / len(g) for g in out]

    sup = [x for x in cluster(lows) if x < close]
    res = [x for x in cluster(highs) if x > close]
    sup.sort(reverse=True)
    res.sort()
    return sup[:3], res[:3]


def _stage2_age_bars(df):
    """Consecutive bars the close has held above a rising 200 DMA."""
    close = df["Close"]
    sma200 = _sma(close, 200)
    above = (close > sma200).fillna(False).values
    rising = (sma200.diff(20) >= 0).fillna(False).values
    age = 0
    for i in range(len(above) - 1, 0, -1):
        if above[i] and rising[i]:
            age += 1
        else:
            break
    return age


def _weekly_trend(df):
    """Higher-timeframe read: 10/30-week MAs and the 30-week slope."""
    try:
        w = df["Close"].resample("W-FRI").last().dropna()
        if len(w) < 40:
            return {"label": "n/a", "detail": "not enough weekly bars"}
        w10, w30 = _sma(w, 10).iloc[-1], _sma(w, 30).iloc[-1]
        slope = (_sma(w, 30).iloc[-1] / _sma(w, 30).iloc[-9] - 1) * 100
        last = w.iloc[-1]
        up = last > w10 > w30 and slope > 0
        label = "up" if up else ("up, flattening" if last > w30 and slope > -1 else "down")
        return {
            "label": label,
            "detail": f"week close {last:,.2f} · 10W {w10:,.2f} · 30W {w30:,.2f} "
                      f"· 30W slope {slope:+.1f}% over 8 weeks",
        }
    except Exception:
        return {"label": "n/a", "detail": ""}


def _doubling_study(df):
    """How often this stock gained 50% / 100% after a similar setup.

    Sampled quarterly so overlapping windows do not inflate the count.
    """
    close = df["Close"].dropna()
    if len(close) < MIN_BARS:
        return {"samples": 0, "note": "too little history"}

    sma50, sma150, sma200 = _sma(close, 50), _sma(close, 150), _sma(close, 200)
    hi252 = close.rolling(252).max()
    lo252 = close.rolling(252).min()

    # current-style setup: Stage-2 stack, not extended, within 12% of the high
    cond = (
        (close > sma50) & (sma50 > sma150) & (sma150 > sma200)
        & (sma200.diff(22) > 0)
        & (close / hi252 >= 0.88)
        & (close / sma50 <= 1.15)
    )
    vals = cond.fillna(False).values
    c = close.values
    n = len(c)

    samples, hit50, hit100, bars50, bars100, worst = 0, 0, 0, [], [], []
    for i in range(252, n - 60, 63):
        if not vals[i]:
            continue
        end = min(i + MIN_BASE_RATE_BARS, n)
        fwd = c[i + 1:end]
        if len(fwd) < 60:
            continue
        samples += 1
        base = c[i]
        peak = fwd.max()
        worst.append(fwd.min() / base - 1)
        if peak / base - 1 >= 0.5:
            hit50 += 1
            j = int((fwd / base - 1 >= 0.5).argmax())
            bars50.append(j + 1)
        if peak / base - 1 >= 1.0:
            hit100 += 1
            j = int((fwd / base - 1 >= 1.0).argmax())
            bars100.append(j + 1)

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else None

    return {
        "samples": samples,
        "hit50": hit50,
        "hit100": hit100,
        "history_years": round(len(close) / 252, 0),
        "rate50_pct": round(100 * hit50 / samples, 0) if samples else None,
        "rate100_pct": round(100 * hit100 / samples, 0) if samples else None,
        "median_bars_to_100": med(bars100),
        "median_bars_to_50": med(bars50),
        "worst_fwd_pct": round(float(100 * min(worst)), 1) if worst else None,
        "window_months": MIN_BASE_RATE_BARS // 21,
        "note": (f"{samples} similar setups in the last {len(close) // 252} years"
                 if samples else "no comparable setup in this stock's history"),
    }


# ---------------------------------------------------------------------------
# history fetch
# ---------------------------------------------------------------------------
def _history(symbols: list[str], period: str = HISTORY_PERIOD):
    """One bulk download; returns {symbol: DataFrame} with yfinance columns."""
    import warnings
    import yfinance as yf

    warnings.filterwarnings("ignore")
    tickers = [f"{s}.NS" for s in symbols]
    df = yf.download(tickers, period=period, interval="1d",
                     progress=False, auto_adjust=False, threads=True, group_by="ticker")
    out = {}
    for s, t in zip(symbols, tickers):
        try:
            sub = df[t] if len(symbols) > 1 else df
            sub = sub[["Open", "High", "Low", "Close", "Volume"]].dropna()
            if len(sub) > 120:
                out[s] = sub
        except Exception:
            continue
    return out


def _local_plan(entry: float, stop: float, capital: float, risk_pct: float):
    """Minimal plan for structures the breakout skill has no base for."""
    r = entry - stop
    if r <= 0 or entry <= 0:
        return None
    budget = capital * risk_pct / 100
    cost_pct = 0.25                       # round-trip delivery charges, approx
    qty = int(budget // (r + entry * cost_pct / 100))
    if qty < 1:
        return None
    charges = entry * qty * cost_pct / 100 * 2
    return {
        "trigger": round(entry, 2), "limit": round(entry * 1.02, 2),
        "stop": round(stop, 2), "stop_method": "structure",
        "stop_pct": round(100 * (stop / entry - 1), 2),
        "qty": qty, "position_value": round(qty * entry, 2),
        "risk_if_stopped": round(qty * r, 0),
        "charges_stop_out": round(charges, 0),
        "targets": [
            {"r": 2.0, "price": round(entry + 2 * r, 2),
             "pct": round(100 * (2 * r / entry), 1)},
            {"r": 3.0, "price": round(entry + 3 * r, 2),
             "pct": round(100 * (3 * r / entry), 1)},
        ],
        "warnings": ["No valid base: this is a trend-continuation entry on a "
                     "20-DMA pullback, not a breakout."],
    }


def _cache_path(symbol: str):
    p = REPO / "data" / "candles" / f"{symbol}.csv"
    return p if p.exists() else None


def _run_plan(symbol: str, capital: float, risk_pct: float):
    cache = _cache_path(symbol)
    bench = REPO / "data" / "nifty.csv"
    if cache and bench.exists():
        cmd = [PYTHON, str(SK / "breakout-planner" / "scripts" / "breakout.py"),
               "plan", "--csv", str(cache), "--benchmark-csv", str(bench),
               "--capital", str(capital), "--risk-pct", str(risk_pct), "--json"]
    else:
        cmd = [PYTHON, str(SK / "breakout-planner" / "scripts" / "breakout.py"),
               "plan", "--symbol", symbol, "--benchmark", "NIFTY",
               "--capital", str(capital), "--risk-pct", str(risk_pct), "--json"]
    try:
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=180)
        out = (r.stdout or "").strip()
        out = out[out.find("{"):] if "{" in out else out
        return (json.loads(out) or {}).get("plan")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------
def _score(raw, rs_rating, study, age_bars, regime_ok):
    """0-100 'path to 100%' score. Every component is explained in the output."""
    parts = {}
    tt = raw.get("trend_template") or {}
    base = raw.get("base") or {}
    liq = raw.get("liquidity") or {}

    parts["stage2"] = 15 if tt.get("stage2") else (7 if (tt.get("passed") or 0) >= 4 else 0)
    grade = base.get("grade") or "D"
    parts["base"] = {"A": 15, "B": 11, "C": 6}.get(grade, 2)
    weeks = base.get("weeks") or 0
    parts["base"] += 3 if weeks >= 5 else 0
    parts["base"] += 2 if len(base.get("contractions") or []) >= 2 else 0

    parts["rs"] = round(20 * (rs_rating / 99), 1) if rs_rating else 0
    parts["liquidity"] = min(10, (liq.get("avg_traded_value_cr") or 0) / 20)

    if study.get("samples"):
        parts["history"] = round(0.20 * (study.get("rate100_pct") or 0), 1)
        parts["history"] = min(20, parts["history"] * 1.6)
    else:
        parts["history"] = 0

    fp = base.get("from_pivot_pct")
    if base.get("type") in (None, "none"):
        parts["timing"] = 5      # no base: neutral, scored on the trend instead
    elif fp is None:
        parts["timing"] = 0
    elif fp >= -5:
        parts["timing"] = 10
    elif fp >= -10:
        parts["timing"] = 6
    else:
        parts["timing"] = 2

    fh = raw.get("from_high_pct")
    if fh is None:
        tt_h, close = tt.get("high_52w"), raw.get("close")
        fh = ((close / tt_h - 1) * 100) if (tt_h and close) else None
    parts["near_high"] = 5 if fh is not None and fh > -10 else 2

    # Young Stage-2 advances have the most room; five-year-old trends are late.
    if age_bars:
        parts["age"] = 5 if 60 <= age_bars <= 400 else (3 if age_bars < 60 else 1)
    else:
        parts["age"] = 1

    parts["regime"] = 5 if regime_ok else 0

    total = sum(parts.values())
    grade_out = "A" if total >= 70 else ("B" if total >= 55 else "C")
    return round(total, 0), grade_out, {k: round(v, 1) for k, v in parts.items()}


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------
def analyse(raw_results: list, plans_by_symbol: dict | None = None,
            capital: float = 500_000, risk_pct: float = 1.0,
            top_n: int = 4, regime_label: str = "", focus: list | None = None,
            rs_ratings: dict | None = None, universe_size: int | None = None) -> dict:
    """Rank candidates and produce the full per-stock deep analysis."""
    plans_by_symbol = plans_by_symbol or {}
    rs_ratings = rs_ratings or {}
    regime_ok = "uptrend" in (regime_label or "").lower()

    pool = []
    for raw in raw_results or []:
        sym = raw.get("name")
        if not sym:
            continue
        if focus and sym not in focus:
            continue
        tt = raw.get("trend_template") or {}
        liq = raw.get("liquidity") or {}
        # 6/6 is a textbook Stage 2; 5/6 is one criterion short and still worth
        # a deep look (usually the RS or the 52-week-high leg). Below 4 we skip.
        passed = tt.get("passed") or 0
        if not (tt.get("stage2") or passed >= 5):
            continue
        if (liq.get("avg_traded_value_cr") or 0) < 10:
            continue          # ₹10 cr/day floor: small qty needs easy fills
        pool.append(raw)

    if not pool:
        return {"candidates": [], "note": "no Stage-2 name with enough liquidity today",
                "screened": len(raw_results or [])}

    # Two-stage shortlist: rank on structure first (cheap), then download history
    # only for the names that can actually make the cut.
    provisional = []
    for raw in pool:
        s0, _, _ = _score(raw, rs_ratings.get(raw["name"]), {}, 0, regime_ok)
        provisional.append((s0, raw))
    provisional.sort(key=lambda x: -x[0])
    short = [r for _, r in provisional[:max(top_n * 4, 16)]]

    hist = _history([r["name"] for r in short])

    scored = []
    for raw in short:
        sym = raw["name"]
        df = hist.get(sym)
        study = _doubling_study(df) if df is not None else {"samples": 0, "note": "no history"}
        age = _stage2_age_bars(df) if df is not None else 0
        score, grade, parts = _score(raw, rs_ratings.get(sym), study, age, regime_ok)
        scored.append((score, grade, parts, raw, df, study, age))

    scored.sort(key=lambda x: -x[0])
    shortlist = scored[:top_n]

    out = []
    for score, grade, parts, raw, df, study, age in shortlist:
        sym = raw["name"]
        base = raw.get("base") or {}
        tt = raw.get("trend_template") or {}

        has_base = (
            bool(base.get("pivot"))
            and (base.get("type") not in (None, "none"))
            and "loose" not in str(base.get("type", "")).lower()
            and "unclassified" not in str(base.get("type", "")).lower()
            and (base.get("grade") or "D") != "D"
        )
        plan = plans_by_symbol.get(sym)
        if has_base and not plan:
            p = _run_plan(sym, capital, risk_pct)
            if p:
                plan = {
                    "trigger": p.get("trigger"), "limit": p.get("limit"),
                    "stop": p.get("stop"), "stop_method": p.get("stop_method"),
                    "qty": p.get("qty"), "position_value": p.get("position_value"),
                    "risk_if_stopped": p.get("risk_if_stopped"),
                    "charges_stop_out": p.get("charges_stop_out"),
                    "stop_pct": p.get("loss_pct_from_trigger"),
                    "targets": [{"r": t.get("R"), "price": t.get("price"),
                                 "pct": t.get("pct_from_entry")}
                                for t in (p.get("targets") or [])],
                    "warnings": p.get("warnings") or [],
                }

        structure = "breakout" if has_base else "pullback"
        entry = (plan or {}).get("trigger") or base.get("pivot")
        stop = (plan or {}).get("stop")

        if not has_base or not entry or not stop:
            # No usable base: treat it as a trend-continuation buy on a pullback
            # to the 20 DMA, with the stop under the 50 DMA / 1.8 ATR.
            if df is None:
                continue
            close_now = float(df["Close"].iloc[-1])
            sma20 = float(_sma(df["Close"], 20).iloc[-1])
            sma50 = float(_sma(df["Close"], 50).iloc[-1])
            atr_now = float(_atr(df).iloc[-1])
            # Buy the pullback, but never set a limit more than 4% under the last
            # price: a deeper entry would be a different trade, not this one.
            floor_px = round(close_now * 0.96, 2)
            entry = round(sma20, 2) if sma20 >= floor_px else floor_px
            stop = round(min(sma50, entry - 1.8 * atr_now), 2)
            plan = _local_plan(entry, stop, capital, risk_pct)
            structure = "pullback"
            if not plan:
                continue

        r_share = (entry - stop) if (stop and entry > stop) else None
        t1 = next((t["price"] for t in (plan or {}).get("targets") or []
                   if t.get("r") == 2.0), None)
        t2 = next((t["price"] for t in (plan or {}).get("targets") or []
                   if t.get("r") == 3.0), None)

        # measured move from the base: pivot + base depth
        base_low = min([c.get("low") for c in base.get("contractions") or []
                        if c.get("low")] or [base.get("last_low") or 0]) or None
        measured = None
        if (has_base and base.get("pivot") and base.get("start_high")
                and base_low):
            measured = base["pivot"] + (base["start_high"] - base_low)

        t2_driver = "3R"
        if measured and t2 and measured > t2:
            t2, t2_driver = measured, "measured move"
        elif measured and not t2:
            t2, t2_driver = measured, "measured move"

        hundred = entry * 2
        r_to_100 = (hundred - entry) / r_share if r_share else None

        qty = (plan or {}).get("qty") or 0
        charges = (plan or {}).get("charges_stop_out") or 0

        # 1/3 at T1, 1/3 at T2, 1/3 held to +100%
        ladder = None
        if qty and t1 and t2:
            third = max(1, qty // 3)
            rest = qty - 2 * third
            gross = (third * (t1 - entry) + third * (t2 - entry)
                     + rest * (hundred - entry))
            ladder = {
                "qty": qty, "third": third, "runner": rest,
                "net_at_t1": round(third * (t1 - entry) - charges * third / qty, 0),
                "net_at_t2": round(third * (t2 - entry) - charges * third / qty, 0),
                "net_at_100": round(rest * (hundred - entry) - charges * rest / qty, 0),
                "net_total": round(gross - charges, 0),
                "pct_of_capital": round(100 * (gross - charges) / capital, 1),
                "blended_r": round((gross - charges) / (qty * r_share), 1) if r_share else None,
            }

        rsi_now = round(float(_rsi(df["Close"]).iloc[-1]), 1) if df is not None else None
        atr_pct = None
        if df is not None:
            a = float(_atr(df).iloc[-1])
            atr_pct = round(100 * a / float(df["Close"].iloc[-1]), 2)
        support, resistance = ([], [])
        if df is not None:
            support, resistance = _swing_levels(df, float(df["Close"].iloc[-1]))
        weekly = _weekly_trend(df) if df is not None else {"label": "n/a", "detail": ""}

        out.append({
            "symbol": sym,
            "structure": structure,
            "score": score,
            "grade": grade,
            "score_parts": parts,
            "close": raw.get("close"),
            "stage2": bool(tt.get("stage2")),
            "tt_passed": tt.get("passed"),
            "tt_total": tt.get("total"),
            "rs_rating": rs_ratings.get(sym),
            "sma50": tt.get("sma50"),
            "sma150": tt.get("sma150"),
            "sma200": tt.get("sma200"),
            "high_52w": tt.get("high_52w"),
            "low_52w": tt.get("low_52w"),
            "from_high_pct": raw.get("from_high_pct"),
            "liquidity_cr": (raw.get("liquidity") or {}).get("avg_traded_value_cr"),
            "base": {
                "type": base.get("type"), "grade": base.get("grade"),
                "weeks": base.get("weeks"), "depth_pct": base.get("depth_pct"),
                "pivot": base.get("pivot"), "state": base.get("state"),
                "from_pivot_pct": base.get("from_pivot_pct"),
                "contractions": len(base.get("contractions") or []),
                "volume_dryup": base.get("volume_dryup_ratio"),
                "notes": base.get("notes") or [],
            },
            "stage2_age_months": round(age / 21, 1) if age else None,
            "rsi": rsi_now,
            "extended": bool((rsi_now or 0) > 70),
            "atr_pct": atr_pct,
            "weekly": weekly,
            "support": [round(x, 0) for x in support],
            "resistance": [round(x, 0) for x in resistance],
            "plan": {
                "entry": entry, "limit": (plan or {}).get("limit"),
                "stop": stop, "stop_method": (plan or {}).get("stop_method"),
                "stop_pct": (plan or {}).get("stop_pct"),
                "r_share": round(r_share, 2) if r_share else None,
                "t1": round(t1, 2) if t1 else None,
                "t1_pct": round(100 * (t1 / entry - 1), 1) if t1 else None,
                "t2": round(t2, 2) if t2 else None,
                "t2_pct": round(100 * (t2 / entry - 1), 1) if t2 else None,
                "t2_driver": t2_driver,
                "hundred": round(hundred, 2),
                "hundred_pct": 100.0,
                "r_to_100": round(r_to_100, 1) if r_to_100 else None,
                "qty": qty,
                "position_value": (plan or {}).get("position_value"),
                "risk_if_stopped": (plan or {}).get("risk_if_stopped"),
                "charges": charges,
                "ladder": ladder,
                "warnings": (plan or {}).get("warnings") or [],
            },
            "study": study,
        })

    return {
        "candidates": out,
        "screened": universe_size or len(raw_results or []),
        "pool": len(pool),
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "disclaimer": ("A 100% target is an objective, not a forecast. The base rate is "
                       "computed from this stock's own past; it says nothing about the next "
                       "12 months and is not adjusted for the survivorship of today's scan."),
    }


# ---------------------------------------------------------------------------
# ad-hoc CLI
# ---------------------------------------------------------------------------
def _scan(symbols: list[str]):
    import subprocess
    r = subprocess.run(
        [PYTHON, str(SK / "breakout-planner" / "scripts" / "breakout.py"),
         "scan", "--symbols", ",".join(symbols), "--exchange", "NSE",
         "--benchmark", "NIFTY", "--top", str(max(len(symbols), 5)), "--json"],
        cwd=REPO, capture_output=True, text=True, timeout=600,
    )
    out = (r.stdout or "").strip()
    out = out[out.find("{"):] if "{" in out else out
    try:
        return (json.loads(out) or {}).get("results") or []
    except Exception:
        return []


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Deep path-to-100% analysis")
    ap.add_argument("--symbols", required=True, help="comma list, e.g. HAL,TCS,DLF")
    ap.add_argument("--capital", type=float, default=500_000)
    ap.add_argument("--risk-pct", type=float, default=1.0)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    results = _scan(syms)
    deep = analyse(results, {}, capital=args.capital, risk_pct=args.risk_pct,
                   top_n=args.top, focus=syms)

    if args.json:
        print(json.dumps(deep, indent=2, default=str))
        return 0

    for c in deep.get("candidates") or []:
        p = c["plan"]
        b = c["base"]
        print(f"\n{c['symbol']}  grade {c['grade']} {c['score']:.0f}/100  "
              f"RS {c.get('rs_rating') or 'n/a'}  ({c['structure']})")
        fp = b.get("from_pivot_pct")
        print(f"  base      {b.get('type')} {b.get('grade')} · "
              f"{round(b.get('weeks') or 0, 1)}w · "
              f"{round(fp, 2) if fp is not None else '—'}% from pivot")
        print(f"  entry     {p['entry']}   (limit {p.get('limit')})")
        print(f"  stop      {p['stop']}   "
              f"({round(p.get('stop_pct') or 0, 2)}%)")
        print(f"  T1 / T2   {p['t1']} / {p['t2']}  ({p.get('t2_driver')})")
        print(f"  +100%     {p['hundred']}  = {p.get('r_to_100')}R")
        print(f"  size      {p['qty']} shares · ₹{round(p.get('position_value') or 0):,} · "
              f"risk ₹{round(p.get('risk_if_stopped') or 0):,}")
        print(f"  history   {c['study'].get('hit100')} of {c['study'].get('samples')} "
              f"similar setups doubled")
    if not deep.get("candidates"):
        print(deep.get("note") or "No candidate cleared the filter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
