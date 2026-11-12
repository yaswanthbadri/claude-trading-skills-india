#!/usr/bin/env python3
"""Collect bulletin data as structured JSON.

Everything here is machine-readable. No fixed-width CLI tables are stored or
passed on to the renderers — the renderers build real HTML tables and plain
mobile message cards from these fields.

Usage (as a module):
    from bulletin_data import collect
    data = collect(capital=500_000, risk_pct=1.0)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
SK = REPO / "skills"
PYTHON = sys.executable

# Local OHLCV cache built by tools/build_cache.py. Scanning 2,200+ CSVs takes
# about ten seconds, against several minutes of network calls.
CACHE = REPO / "data" / "candles"
BENCH = REPO / "data" / "nifty.csv"
TOP_SCAN = 120         # how many ranked names the deep analysis can draw from
MAX_PER_SECTOR = 2     # two banks is a sector bet, three is a profile
MIN_TURNOVER_CR = 5.0  # ₹5 crore/day: small orders still get a clean fill

# Fallback universe when the cache is missing or stale. Nifty 50 + Next 50 +
# liquid midcaps, deliberately wide.
WATCHLIST = (
    "ADANIENT,ADANIPORTS,APOLLOHOSP,ASIANPAINT,AXISBANK,BAJAJ-AUTO,BAJFINANCE,"
    "BAJAJFINSV,BEL,BHARTIARTL,BSE,CIPLA,COALINDIA,DRREDDY,EICHERMOT,ETERNAL,"
    "GRASIM,HCLTECH,HDFCBANK,HDFCLIFE,HINDALCO,HINDUNILVR,ICICIBANK,INDIGO,INFY,"
    "ITC,JIOFIN,JSWSTEEL,KOTAKBANK,LT,M&M,MARUTI,MAXHEALTH,NESTLEIND,NTPC,ONGC,"
    "POWERGRID,RELIANCE,SBILIFE,SHRIRAMFIN,SBIN,SUNPHARMA,TCS,TATACONSUM,TMPV,"
    "TATASTEEL,TECHM,TITAN,TRENT,ULTRACEMCO,"
    "ABB,AMBUJACEM,BANDHANBNK,BANKBARODA,BERGER,BOSCHLTD,BRITANNIA,CHOLAFIN,"
    "COLPAL,CONCOR,CUMMINSIND,DABUR,DIVISLAB,GAIL,GODREJCP,HAL,HAVELLS,HEROMOTOCO,"
    "ICICIPRULI,ICICIGI,INDHOTEL,INDUSINDBK,IOC,IRCTC,JINDALSTEL,JUBLFOOD,LICI,"
    "LTIM,LUPIN,MRF,MUTHOOTFIN,NAUKRI,NMDC,OBEROIRLTY,OFSS,PAGEIND,PEL,PFC,"
    "PIDILITIND,PNB,POLYCAB,RECLTD,SAIL,SBICARD,SHREECEM,SIEMENS,TATACHEM,"
    "TATAPOWER,TORNTPHARM,TVSMOTOR,UBL,VOLTAS,ZYDUSLIFE"
)


def _run_json(cmd, timeout=300):
    """Run a skill script with --json and return parsed JSON (or None)."""
    try:
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        if not out:
            return None
        # Some scripts print warnings before the JSON payload.
        start = out.find("{")
        if start > 0:
            out = out[start:]
        return json.loads(out)
    except Exception:
        return None


FEEDS = [
    "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
]

NOISE = (
    "share price highlights",
    "stock price history",
    "stocks to watch",
    "market wrap",
)


def _headlines(limit=8):
    """Pull India-market headlines from ET RSS feeds."""
    seen, out = set(), []
    for feed in FEEDS:
        if len(out) >= limit:
            break
        try:
            r = requests.get(feed, timeout=20,
                             headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            if r.status_code != 200:
                continue
        except Exception:
            continue
        for m in re.finditer(
            r"<item>.*?<title><!\[CDATA\[(.*?)\]\]>\s*</title>.*?<link>(.*?)</link>",
            r.text, re.S,
        ):
            title = re.sub(r"\s+", " ", m.group(1)).strip()
            url = m.group(2).strip()
            low = title.lower()
            if not title or title in seen or any(n in low for n in NOISE):
                continue
            seen.add(title)
            out.append({"title": title, "url": url})
            if len(out) >= limit:
                break
    return out


def _round(x, n=2):
    try:
        return round(float(x), n)
    except Exception:
        return None


def cache_info() -> dict | None:
    """Describe the local candle cache, or None when it is missing or stale."""
    if not CACHE.is_dir() or not BENCH.exists():
        return None
    files = list(CACHE.glob("*.csv"))
    if len(files) < 300:
        return None
    try:
        with BENCH.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 300))
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
        last_bar = tail[-1].split(",")[0]
    except Exception:
        return None

    from datetime import date, timedelta
    today = date.today()
    try:
        last = date.fromisoformat(last_bar)
    except Exception:
        return None

    # Weekend/holiday tolerant: the cache only needs yesterday's close.
    age_days = (today - last).days
    fresh = age_days <= 4 or last == today
    return {"path": str(CACHE), "bench": str(BENCH), "files": len(files),
            "last_bar": last_bar, "age_days": age_days, "fresh": fresh}


def _scan_args(info: dict | None):
    """CLI arguments for the skills: cached CSVs when available, else yfinance."""
    if info and info.get("fresh"):
        return (["--csv-dir", info["path"], "--benchmark-csv", info["bench"]],
                f"{info['files']:,} cached symbols")
    return (["--symbols", WATCHLIST, "--exchange", "NSE", "--benchmark", "NIFTY"],
            f"{len(WATCHLIST.split(','))} watchlist symbols (live fetch)")


def _sectors() -> dict[str, str]:
    try:
        import universe
        return universe.sector_map()
    except Exception:
        return {}


def _diversify(rows: list[dict], want: int, per_sector: int = MAX_PER_SECTOR) -> list[dict]:
    """Keep the ranking, but never let one theme fill the whole slate.

    Three PSU banks is one macro bet dressed up as three ideas. Once a sector
    has `per_sector` names, the next one is skipped in favour of a different
    corner of the market.
    """
    out, count = [], {}
    for r in rows:
        sec = (r.get("sector") or "").strip()
        if sec:
            if count.get(sec, 0) >= per_sector:
                continue
            count[sec] = count.get(sec, 0) + 1
        out.append(r)
        if len(out) >= want:
            break
    return out


def collect(capital: float = 500_000, risk_pct: float = 1.0,
            candidates: int = 6, motion_top: int = 8) -> dict:
    """Gather the full bulletin dataset."""
    data: dict = {"generated_at": datetime.now().isoformat(timespec="seconds")}

    # ---- exchange calendar --------------------------------------------------
    try:
        import market_calendar as cal
        today = datetime.now().date()
        trading = cal.is_trading_day(today)
        start, end = cal.session_times(today)
        data["calendar"] = {
            "trading_day": trading,
            "session": f"{start:%H:%M}–{end:%H:%M} IST",
            "next_trading_day": cal.next_trading_day(today).isoformat(),
        }
    except Exception:
        data["calendar"] = {"trading_day": True, "session": "09:15–15:30 IST",
                            "next_trading_day": None}

    # ---- index + volatility -------------------------------------------------
    ta = _run_json([PYTHON, str(SK / "technical-analysis" / "scripts" / "ta.py"),
                    "summary", "--symbol", "NIFTY", "--json"]) or {}
    vix_raw = _run_json([PYTHON, str(SK / "technical-analysis" / "scripts" / "ta.py"),
                         "summary", "--symbol", "INDIAVIX", "--json"]) or {}
    vix = _round(vix_raw.get("close"), 2)

    signals = (ta.get("dashboard") or {}).get("signals") or {}
    def sig(key):
        v = signals.get(key)
        return v[0] if isinstance(v, list) and v else (v or "n/a")

    data["index"] = {
        "name": "NIFTY 50",
        "close": _round(ta.get("close")),
        "change_pct": _round(ta.get("change_pct")),
        "high_52w": _round(ta.get("high_52w")),
        "low_52w": _round(ta.get("low_52w")),
        "from_high_pct": _round(ta.get("from_high_pct")),
        "sma20": _round((ta.get("smas") or {}).get("sma20")),
        "sma50": _round((ta.get("smas") or {}).get("sma50")),
        "sma200": _round((ta.get("smas") or {}).get("sma200")),
        "structure": ta.get("structure") or "n/a",
        "rsi": sig("RSI(14)"),
        "macd": sig("MACD(12,26,9)"),
        "adx": sig("ADX(14)"),
        "supports": [s.get("level") for s in (ta.get("levels") or {}).get("supports", [])][:3],
        "resistances": [r.get("level") for r in (ta.get("levels") or {}).get("resistances", [])][:3],
    }
    data["vix"] = vix

    # ---- regime -------------------------------------------------------------
    reg = _run_json([PYTHON, str(SK / "market-regime" / "scripts" / "regime.py"),
                     "status", "--symbol", "NIFTY", "--vix", str(vix or 17.5), "--json"]) or {}
    r = reg.get("regime") or {}
    risk = reg.get("risk") or {}
    trend = reg.get("trend") or {}
    dist = reg.get("distribution") or {}
    data["regime"] = {
        "label": r.get("label", "Unknown"),
        "exposure_pct": r.get("exposure_ceiling_pct"),
        "max_positions": r.get("max_new_positions"),
        "stance": r.get("stance", ""),
        "risk_score": risk.get("score"),
        "risk_parts": risk.get("components") or {},
        "risk_max": risk.get("max") or {},
        "trend": {
            "close": _round(trend.get("close")),
            "sma50": _round(trend.get("sma50")),
            "sma200": _round(trend.get("sma200")),
            "sma50_pct": _round(trend.get("vs_sma50_pct")),
            "sma200_pct": _round(trend.get("vs_sma200_pct")),
            "sma50_slope": trend.get("sma50_slope"),
        },
        "distribution_counts": (dist.get("counts") or {}),
        "as_of": reg.get("as_of"),
        "sessions": reg.get("sessions"),
        "what_changes": reg.get("what_would_change") or reg.get("triggers") or [],
    }

    # ---- sector rotation ----------------------------------------------------
    sec = _run_json([PYTHON, str(SK / "sector-rotation" / "scripts" / "sectors.py"),
                     "rotation", "--json"]) or {}
    sectors = []
    for s in sec.get("sectors", []):
        sectors.append({
            "key": s.get("key"),
            "quadrant": s.get("quadrant"),
            "r1w": _round((s.get("returns") or {}).get("1W"), 1),
            "r1m": _round((s.get("returns") or {}).get("1M"), 1),
            "r3m": _round((s.get("returns") or {}).get("3M"), 1),
            "rel1m": _round((s.get("relative") or {}).get("1M"), 1),
            "rs_ratio": _round(s.get("rs_ratio"), 2),
            "rs_momentum": _round(s.get("rs_momentum"), 2),
            "trend": s.get("trend"),
            "weeks": s.get("weeks_in_quadrant"),
            "transitions": (s.get("transitions") or [])[-1:] ,
        })
    size = []
    for s in sec.get("size", []):
        size.append({
            "key": s.get("key"),
            "quadrant": s.get("quadrant"),
            "r1m": _round((s.get("returns") or {}).get("1M"), 1),
            "rel3m": _round((s.get("relative") or {}).get("3M"), 1),
            "trend": s.get("trend"),
        })
    data["sectors"] = {
        "rows": sectors,
        "size": size,
        "summary": sec.get("summary", ""),
        "rotation": sec.get("rotation") or {},
        "size_read": sec.get("size_read") or {},
        "as_of": (sec.get("benchmark") or {}).get("last_date"),
    }

    # Series with a price-level break inside the indicator lookback are not
    # readable: a 200-day average spanning two different price scales is not a
    # 200-day average. They sit out until enough clean bars exist.
    try:
        import integrity
        blocked = integrity.active_breaks()
    except Exception:
        blocked = {}
    if blocked:
        print(f"[integrity] {len(blocked)} series withheld from the scan",
              file=sys.stderr)

    # ---- momentum -----------------------------------------------------------
    cinfo = cache_info()
    scan_args, universe_label = _scan_args(cinfo)
    # "Stale" must mean late, not "before the open". Compare against the newest
    # bar that should exist at this hour.
    _last_bar = (cinfo or {}).get("last_bar")
    _expected = None
    try:
        import market_calendar as _cal
        _expected = _cal.last_session_bar().isoformat()
    except Exception:
        pass

    data["universe"] = {"label": universe_label,
                        "cached": bool(cinfo and cinfo.get("fresh")),
                        "files": (cinfo or {}).get("files"),
                        "last_bar": _last_bar,
                        "expected_bar": _expected,
                        "stale": bool(_last_bar and _expected and _last_bar < _expected)}

    mom = _run_json([PYTHON, str(SK / "momentum-scanner" / "scripts" / "momentum.py"),
                     "scan", *scan_args, "--top", str(motion_top), "--json"]) or {}
    movers = []
    for w in mom.get("watchlist", [])[:motion_top]:
        if w.get("symbol") in blocked:
            continue
        movers.append({
            "symbol": w.get("symbol"),
            "close": _round(w.get("close")),
            "rs": w.get("rs_rating"),
            "from_high": _round(w.get("from_high_pct"), 1),
            "score": _round(w.get("score"), 1),
            "signals": w.get("signals") or [],
            "reason": w.get("reason"),
            "rs_line": w.get("rs_line"),
        })
    data["momentum"] = {"movers": movers, "as_of": mom.get("as_of"),
                        "scanned": len(mom.get("universe") or [])}

    # ---- breakouts ----------------------------------------------------------
    bo = _run_json([PYTHON, str(SK / "breakout-planner" / "scripts" / "breakout.py"),
                    "scan", *scan_args, "--top", str(TOP_SCAN), "--json"], timeout=600) or {}
    sec_map = _sectors()
    setups = []
    for res in bo.get("results", []):
        if res.get("name") in blocked:
            continue
        b = res.get("base") or {}
        tt = res.get("trend_template") or {}
        liq = res.get("liquidity") or {}
        setups.append({
            "symbol": res.get("name"),
            "sector": sec_map.get(res.get("name") or "", ""),
            "close": _round(res.get("close")),
            "verdict": res.get("verdict"),
            "rank": _round(res.get("rank_score"), 1),
            "stage2": bool(tt.get("stage2")),
            "tt_passed": tt.get("passed"),
            "tt_total": tt.get("total"),
            "liquidity_cr": _round(liq.get("avg_traded_value_cr"), 1),
            "base_type": b.get("type"),
            "base_grade": b.get("grade"),
            "base_state": b.get("state"),
            "base_weeks": _round(b.get("weeks"), 1),
            "base_depth": _round(b.get("depth_pct"), 1),
            "pivot": _round(b.get("pivot")),
            "from_pivot_pct": _round(b.get("from_pivot_pct"), 2),
        })
    data["breakouts"] = {"setups": setups, "as_of": (bo.get("results") or [{}])[0].get("last_date"),
                         "errors": bo.get("errors") or []}

    # Pick tradable candidates: prefer Stage-2, near pivot, decent liquidity.
    def rank_key(s):
        ok = 0
        ok += 40 if s["stage2"] else 0
        ok += 20 if (s["tt_passed"] or 0) >= 5 else 0
        ok += 15 if (s["base_grade"] or "D") in ("A", "B") else 0
        fp = s["from_pivot_pct"]
        ok += 15 if fp is not None and -4 <= fp <= 1 else 0
        ok += 10 if (s["liquidity_cr"] or 0) >= 20 else 0
        return -ok

    ordered = sorted(setups, key=rank_key)
    tradable = [s for s in ordered
                if s["from_pivot_pct"] is not None and s["from_pivot_pct"] > -12
                and (s["liquidity_cr"] or 0) >= MIN_TURNOVER_CR]
    picks = _diversify(tradable, candidates)

    # ---- trade plans --------------------------------------------------------
    def _plan_cmd(sym: str) -> list[str]:
        """Read the plan from the cache when possible; no network, same prices."""
        csv_path = CACHE / f"{sym}.csv"
        if cinfo and cinfo.get("fresh") and csv_path.exists():
            return [PYTHON, str(SK / "breakout-planner" / "scripts" / "breakout.py"),
                    "plan", "--csv", str(csv_path), "--benchmark-csv", BENCH,
                    "--capital", str(capital), "--risk-pct", str(risk_pct), "--json"]
        return [PYTHON, str(SK / "breakout-planner" / "scripts" / "breakout.py"),
                "plan", "--symbol", sym, "--benchmark", "NIFTY",
                "--capital", str(capital), "--risk-pct", str(risk_pct), "--json"]

    plans = []
    for s in picks:
        p = _run_json(_plan_cmd(s["symbol"]))
        pl = (p or {}).get("plan")
        if not pl:
            continue
        targets = pl.get("targets") or []
        plans.append({
            "symbol": s["symbol"],
            "sector": s.get("sector"),
            "stage2": s["stage2"],
            "tt_passed": s["tt_passed"],
            "tt_total": s["tt_total"],
            "base_type": s["base_type"],
            "base_grade": s["base_grade"],
            "base_state": s["base_state"],
            "close": _round(pl.get("pivot")) and s["close"],
            "pivot": _round(pl.get("pivot")),
            "trigger": _round(pl.get("trigger")),
            "limit": _round(pl.get("limit")),
            "stop": _round(pl.get("stop")),
            "stop_method": pl.get("stop_method"),
            "stop_pct": _round(pl.get("loss_pct_from_trigger")),
            "qty": pl.get("qty"),
            "position_value": _round(pl.get("position_value")),
            "allocation_pct": _round(pl.get("allocation_pct"), 1),
            "risk_if_stopped": _round(pl.get("risk_if_stopped")),
            "risk_budget": _round(pl.get("risk_budget")),
            "charges_stop_out": _round(pl.get("charges_stop_out")),
            "targets": [
                {"r": t.get("R"), "price": _round(t.get("price")),
                 "pct": _round(t.get("pct_from_entry"), 1),
                 "net": _round(t.get("net_pnl"))}
                for t in targets
            ],
            "warnings": pl.get("warnings") or [],
            "sizing_notes": pl.get("sizing_notes") or [],
            "orders": pl.get("orders") or {},
            "tick_note": pl.get("tick_note"),
            "liquidity_cr": s["liquidity_cr"],
            "from_pivot_pct": s["from_pivot_pct"],
        })
    # A breakout three sessions before results is a coin flip, not a setup.
    # The date goes on the card so the gap risk is visible before the order.
    try:
        import results_dates as rd
        cal = rd.annotate([p["symbol"] for p in plans])
        for p in plans:
            row = cal.get(p["symbol"]) or {}
            p["results_date"] = row.get("date")
            p["results_sessions"] = row.get("sessions")
            p["results_source"] = row.get("source")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] results dates unavailable: {exc}", file=sys.stderr)
        for p in plans:
            p["results_date"] = None
            p["results_sessions"] = None
            p["results_source"] = "unknown"

    data["plans"] = plans
    data["params"] = {"capital": capital, "risk_pct": risk_pct,
                      "watchlist": WATCHLIST, "candidates": candidates}

    # ---- deep "path to 100%" analysis ---------------------------------------
    # Runs on the same scan (no second bulk download) and adds each candidate's
    # own-history doubling base rate, multi-timeframe and level detail.
    try:
        import deep_candidates as dc

        raw_setups = bo.get("results") or []
        pool_syms = [r["name"] for r in raw_setups
                     if ((r.get("trend_template") or {}).get("stage2")
                         or ((r.get("trend_template") or {}).get("passed") or 0) >= 5)]
        rs_map = {}
        if pool_syms:
            # Rating must come from a wide universe — a percentile within three
            # names is meaningless (it would read 1, 50, 99).
            rs_json = _run_json(
                [PYTHON, str(SK / "momentum-scanner" / "scripts" / "momentum.py"),
                 "rs", *scan_args, "--json"], timeout=600) or {}
            rs_map = {x.get("symbol"): x.get("rating")
                      for x in (rs_json.get("ratings") or [])}

        data["deep"] = dc.analyse(
            raw_setups,
            {p["symbol"]: p for p in plans},
            capital=capital, risk_pct=risk_pct, top_n=4,
            regime_label=data["regime"].get("label", ""),
            rs_ratings=rs_map,
            universe_size=(cinfo or {}).get("files") or len(WATCHLIST.split(",")),
        )
    except Exception as e:  # never let this section break the bulletin
        data["deep"] = {"candidates": [], "note": f"deep analysis unavailable: {e}"}

    # ---- headlines ----------------------------------------------------------
    data["headlines"] = _headlines()
    return data


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2, default=str)[:2000])
