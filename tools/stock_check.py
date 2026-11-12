#!/usr/bin/env python3
"""Answer "what about this stock?" for one symbol, in detail.

The bulletin scans 2,400 names and gives you six. That is the wrong shape for
the question people actually ask twenty times a day, which is about one stock
they are already looking at.

Everything here is the same pipeline the report uses, run on one symbol:

    trend template   the six Stage-2 checks, and which ones failed
    base             type, grade, depth, weeks, pivot
    state            what the price is doing right now — inside the base, at
                     the pivot, breaking out today, or already extended
    trade            entry, stop, T1, T2, size, for the state that is actually
                     in front of you (never a stale pivot from three weeks ago)
    technicals       RSI, ATR, ADX, the moving averages, volume, the 52-week
                     position, how far the move has run and how far it is from
                     its measured target
    base rates       what actually happened after this same situation, across
                     the whole market and in this stock's own history
    restrictions     price band, trade-for-trade, F&O ban
    delivery         today against its own 20-session average
    results          next earnings date, confirmed or estimated
    book             whether you already hold it, and at what R

The verdict is not recomputed here. It calls bulletin_render.plan_verdict and
bulletin_render.state_verdict, so a stock cannot be BUY-READY in a chat and
WATCH in the report on the same day.

    python3 tools/stock_check.py CENTUM
    python3 tools/stock_check.py TCS --json
    python3 tools/stock_check.py CUPID --html
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SK = REPO / "skills"
PYTHON = sys.executable
BREAKOUT = SK / "breakout-planner" / "scripts" / "breakout.py"

sys.path.insert(0, str(REPO / "tools"))


def _run(cmd: list[str], timeout: int = 90) -> dict:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(REPO))
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        return json.loads(r.stdout)
    except Exception:
        return {}


def _clean(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    for suffix in (".NS", ".BO"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s.replace(" ", "")


def check(symbol: str, capital: float | None = None,
          risk_pct: float | None = None) -> dict:
    sym = _clean(symbol)
    if not sym:
        return {"error": "no symbol given"}

    try:
        import settings
        capital = capital if capital is not None else settings.capital()
        risk_pct = risk_pct if risk_pct is not None else settings.risk_pct()
    except Exception:
        capital = capital if capital is not None else 500_000.0
        risk_pct = risk_pct if risk_pct is not None else 1.0

    base = _run([PYTHON, str(BREAKOUT), "check", "--symbol", sym,
                 "--benchmark", "NIFTY", "--json"])
    if not base:
        return {"error": f"no data for {sym} — check the NSE symbol"}

    tt = base.get("trend_template") or {}
    b = base.get("base") or {}
    raw_type = b.get("type")
    has_base = bool(raw_type) and str(raw_type).lower() not in ("none", "null")

    # The local cache is the official close. Yahoo's last tick is not, and the
    # difference matters when a stop is a few points away.
    tech = {}
    try:
        import tech_detail
        tech = tech_detail.detail(sym)
    except Exception:
        tech = {}
    close = tech.get("close") or base.get("close")
    live = base.get("close")

    pivot = b.get("pivot") if has_base else None
    from_pivot = None
    if close and pivot:
        from_pivot = round(100 * (close / pivot - 1), 2)

    # When did the market first close above the old pivot? That date is what
    # turns "17% above the pivot" from a number into a story: the trade
    # happened then, and this is what it did afterwards.
    cleared_on = None
    if pivot:
        # Search from the day the pivot was made, not from the beginning of
        # history: the price was above this level months ago, and that is a
        # different event from this base breaking out.
        cons = b.get("contractions") or []
        pivot_date = None
        if cons and cons[-1].get("high_date"):
            pivot_date = str(cons[-1]["high_date"])[:10]
        elif b.get("start_date"):
            pivot_date = str(b.get("start_date"))[:10]
        try:
            import tech_detail as _td
            for r in _td.load_candles(sym):
                if pivot_date and r["date"] < pivot_date:
                    continue
                if r["close"] > pivot:
                    cleared_on = r["date"]
                    break
        except Exception:
            cleared_on = None

    # ---- the classic breakout plan, for when the price is at the pivot ----
    classic = {}
    if has_base:
        pr = _run([PYTHON, str(BREAKOUT), "plan", "--symbol", sym,
                   "--benchmark", "NIFTY", "--capital", str(capital),
                   "--risk-pct", str(risk_pct), "--json"])
        classic = (pr.get("plan") or {})

    # ---- what is the price actually doing right now? ----------------------
    fwd = {}
    try:
        import forward_plan
        fwd = forward_plan.analyse(sym, tech, {"pivot": pivot}, capital,
                                   risk_pct, classic=classic)
    except Exception:
        fwd = {}

    state = fwd.get("state") or ("no_base" if not has_base else "unknown")
    state_text = fwd.get("state_text") or ""

    # ---- verdict ----------------------------------------------------------
    verdict, css = "NO DATA", "warn"
    try:
        import bulletin_render
        verdict, css = bulletin_render.state_verdict(
            state,
            stage2=bool(tt.get("stage2")),
            base_grade=b.get("grade") if has_base else None,
            tt_passed=tt.get("passed"),
            from_pivot_pct=from_pivot,
            plans=fwd.get("plans") or [],
        )
    except Exception:
        pass

    gate = {}
    try:
        import tradability
        gate = (tradability.lookup([sym]) or {}).get(sym) or {}
    except Exception:
        pass

    deliv = {}
    try:
        import delivery
        deliv = (delivery.annotate([sym]) or {}).get(sym) or {}
    except Exception:
        pass

    results = {}
    try:
        import results_dates
        results = (results_dates.annotate([sym]) or {}).get(sym) or {}
    except Exception:
        pass

    # ---- base rates --------------------------------------------------------
    rates = {"market": {}, "symbol": {}, "matches": []}
    try:
        import base_rates
        rates["matches"] = base_rates.matches(tech or {})
        cached = base_rates.load_cache()
        market = (cached or {}).get("market") or {}
        rates["market"] = {k: market.get(k) for k in rates["matches"] if market.get(k)}
        own = base_rates.symbol_rates(sym)
        rates["symbol"] = {k: (own.get("signals") or {}).get(k)
                           for k in rates["matches"]
                           if (own.get("signals") or {}).get(k)}
    except Exception:
        pass

    # Already on the book? Then the only levels that matter are the ones the
    # position was opened at, not a fresh trigger above today's price.
    held = None
    try:
        import recs
        for r in recs.load()["records"]:
            if r["symbol"] != sym:
                continue
            if r.get("status") in ("filled", "t1_hit", "pending_fill"):
                held = {"status": r.get("status"),
                        "entry": r.get("fill_price") or r.get("entry"),
                        "stop": r.get("stop"), "t1": r.get("t1"),
                        "t2": r.get("t2"), "qty": r.get("qty"),
                        "date": r.get("live_from") or r.get("date")}
                break
    except Exception:
        pass

    return {
        "symbol": sym,
        "name": (base.get("name") or "").replace(".NS", "").replace(".BO", ""),
        "close": close,
        "live": live,
        "last_date": tech.get("date") or base.get("last_date"),
        "bars": tech.get("bars") or base.get("bars"),
        "verdict": verdict, "css": css,
        "state": state, "state_text": state_text,
        "trend": {
            "stage2": bool(tt.get("stage2")),
            "passed": tt.get("passed"), "total": tt.get("total"),
            "failed": [c["label"] for c in (tt.get("criteria") or [])
                       if not c.get("pass")],
            "criteria": [{"label": c.get("label"), "pass": c.get("pass"),
                          "detail": c.get("detail")}
                         for c in (tt.get("criteria") or [])],
            "rs": tt.get("rs"),
            "rs_3m": ((tt.get("rs") or {}).get("3m") or {}).get("excess_pct"),
            "rs_6m": ((tt.get("rs") or {}).get("6m") or {}).get("excess_pct"),
            "rs_12m": ((tt.get("rs") or {}).get("12m") or {}).get("excess_pct"),
            "high_52w": tt.get("high_52w"), "low_52w": tt.get("low_52w"),
            "sma50": tt.get("sma50"), "sma150": tt.get("sma150"),
            "sma200": tt.get("sma200"),
        },
        "base": {
            "type": raw_type if has_base else None,
            "grade": b.get("grade") if has_base else None,
            "weeks": b.get("weeks") if has_base else None,
            "depth": b.get("depth_pct") if has_base else None,
            "pivot": pivot,
            "from_pivot_pct": from_pivot,
            "state": b.get("state"),
            "cleared_on": cleared_on,
            "note": (b.get("notes") or [None])[0] if not has_base else None,
            "contractions": b.get("contractions") or [],
            "volume_dryup": b.get("volume_dryup_ratio"),
        },
        "liquidity_cr": (base.get("liquidity") or {}).get("avg_traded_value_cr"),
        "tech": tech,
        "forward": fwd,
        "plans": fwd.get("plans") or [],
        "why": fwd.get("why") or [],
        "handle": fwd.get("handle"),
        "rates": rates,
        "gate": {"blockers": gate.get("blockers") or [],
                 "notes": gate.get("notes") or [],
                 "band_pct": gate.get("band_pct"),
                 "series": gate.get("series")},
        "delivery": deliv,
        "results": results,
        "held": held,
        "capital": capital,
        "risk_pct": risk_pct,
    }


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------
def _n(v, dec: int = 2) -> str:
    try:
        return f"{float(v):,.{dec}f}"
    except (TypeError, ValueError):
        return "—"


def _pct(v, dec: int = 1) -> str:
    try:
        return f"{float(v):+.{dec}f}%"
    except (TypeError, ValueError):
        return "—"


def _esc(s) -> str:
    """Escape for Telegram HTML: & < > only. Quotes are fine as they are."""
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _plan_lines(p: dict) -> list[str]:
    """The four numbers that make a plan usable, and nothing else."""
    stop_note = f"{_n(p['stop_pct'], 1)}% below entry"
    if p.get("stop_pct_from_limit"):
        stop_note += f" · {_n(p['stop_pct_from_limit'], 1)}% below the chase limit"
    elif p.get("stop_capped"):
        stop_note += ", capped at 8%"
    out = [
        f"<b>Entry</b> {_n(p['entry'])}"
        + (f" (zone {_n(p['entry_zone'][0])}–{_n(p['entry_zone'][1])})"
           if p.get("entry_zone") else ""),
        f"<b>Stop</b> {_n(p['stop'])}  ({stop_note})",
    ]
    for i, t in enumerate(p.get("targets") or [], 1):
        out.append(f"<b>T{i}</b> {_n(t['price'])}  ({t['r']:g}R, {_pct(t.get('pct'))})")
    out.append(f"<i>{p['qty']} shares · ₹{_n(p.get('position_value'), 0)} · "
               f"{_n(p.get('allocation_pct'), 1)}% of capital · "
               f"risk ₹{_n(p.get('risk'), 0)} of ₹{_n(p.get('risk_budget'), 0)}</i>")
    return out


def cards(r: dict) -> list[str]:
    """The chat answer: two messages, because detail is the point."""
    if r.get("error"):
        return [f"⚠️ {_esc(r['error'])}"]
    return [card_call(r), card_detail(r)]


def card_call(r: dict) -> str:
    """The call: what it is, what to do, what it costs."""
    sym = r["symbol"]
    tech = r.get("tech") or {}
    L = [f"🔎 <b>{_esc(sym)}</b>"
         + (f" · {_esc(r['name'])}" if r.get("name") and r["name"] != sym else "")]

    head = [f"<code>₹{_n(r.get('close'))}</code>"]
    if r.get("live") and r.get("close") and abs(r["live"] - r["close"]) > 0.3:
        head.append(f"live {_n(r['live'])}")
    if tech.get("change_pct") is not None:
        head.append(f"{_pct(tech['change_pct'])}")
    if r["gate"].get("series"):
        head.append(_esc(r["gate"]["series"]))
    if r["gate"].get("band_pct"):
        head.append(f"{r['gate']['band_pct']}% band")
    L.append(" · ".join(head))
    if r.get("last_date"):
        L.append(f"<i>close of {r['last_date']}</i>")
    L.append("")

    icon = {"ok": "🟢", "good": "🟢", "warn": "🟠", "bad": "🔴"}.get(r.get("css"), "⚪")
    L.append(f"{icon} <b>{_esc(r.get('verdict') or 'NO VERDICT')}</b>")
    if r.get("state_text"):
        L.append(f"<i>{_esc(r['state_text'])}</i>")
    L.append("")

    for w in (r.get("why") or [])[:3]:
        L.append(f"• {_esc(w)}")
    if r.get("why"):
        L.append("")

    plans = r.get("plans") or []
    if plans:
        p = plans[0]
        L.append(f"<b>{_esc(p['label'])}</b>")
        L += _plan_lines(p)
        if p.get("note"):
            L.append(f"<i>{_esc(p['note'])}</i>")
        if p.get("stop_quality") == "capped inside the base":
            L.append("⚠️ The stop had to be pulled in to fit the 8% rule, so it "
                     "now sits inside the structure. That is a reason to skip.")
        for other in plans[1:]:
            L.append("")
            L.append(f"<b>{_esc(other['label'])}</b>")
            L += _plan_lines(other)
            if other.get("note"):
                L.append(f"<i>{_esc(other['note'])}</i>")
    else:
        L.append("🚫 <b>No trade to take right now.</b>")
        if r.get("base", {}).get("note"):
            L.append(f"<i>{_esc(r['base']['note'])}</i>")

    held = r.get("held")
    if held:
        L += ["",
              f"💼 <b>Already on the book</b> ({_esc(held['status'].replace('_', ' '))})",
              f"entry {_n(held.get('entry'))} · stop {_n(held.get('stop'))} · "
              f"T1 {_n(held.get('t1'))}"]

    L += ["", "<i>Educational only — not SEBI-registered advice. "
              "Verify every level with your broker.</i>"]
    return "\n".join(L)


def card_detail(r: dict) -> str:
    """The detail: the numbers behind the call, and the base rates."""
    sym = r["symbol"]
    t = r.get("tech") or {}
    L = [f"📐 <b>{_esc(sym)} — the detail</b>", ""]

    tr = r.get("trend") or {}
    L.append(f"<b>Trend</b> {tr.get('passed') or 0}/{tr.get('total')} · "
             + ("Stage 2 ✓" if tr.get("stage2") else "not Stage 2"))
    if tr.get("failed"):
        L.append(f"<i>fails: {_esc('; '.join(tr['failed']))}</i>")
    rs_bits = []
    for k, lbl in (("rs_3m", "3m"), ("rs_6m", "6m"), ("rs_12m", "12m")):
        if tr.get(k) is not None:
            rs_bits.append(f"{lbl} {_pct(tr[k], 0)}")
    if rs_bits:
        L.append(f"<i>vs NIFTY: {_esc(' · '.join(rs_bits))}</i>")
    L.append("")

    b = r.get("base") or {}
    if b.get("type"):
        L.append(f"<b>Base</b> {_esc(b['type'])} grade {_esc(b.get('grade') or '?')} · "
                 f"{_n(b.get('weeks'), 1)} weeks · {_n(b.get('depth'), 1)}% deep")
        if b.get("pivot"):
            state = r.get("state")
            if state == "at_pivot":
                L.append(f"<b>Pivot</b> {_n(b['pivot'])} · price "
                         f"{_pct(b.get('from_pivot_pct'))} from it — this is the "
                         "live level")
            elif state == "pre_breakout":
                L.append(f"<b>Pivot</b> {_n(b['pivot'])} · price "
                         f"{_pct(b.get('from_pivot_pct'))} from it — wait for it")
            else:
                when = (f", cleared {b['cleared_on']}"
                        if b.get("cleared_on") else "")
                L.append(f"<b>Old pivot</b> {_n(b['pivot'])} · price "
                         f"{_pct(b.get('from_pivot_pct'))} from it{_esc(when)} — "
                         "that trade is over; it is not an entry")
        cons = b.get("contractions") or []
        if cons:
            bits = [f"{_n(c.get('depth_pct'), 1)}% in {c.get('bars')} bars"
                    for c in cons[-3:]]
            L.append(f"<i>contractions: {_esc(' → '.join(bits))}</i>")
    else:
        L.append("<b>Base</b> none — no tight base, so there is nothing to buy")
        if b.get("note"):
            L.append(f"<i>{_esc(b['note'])}</i>")
    h = r.get("handle")
    if h:
        L.append(f"<b>Recent shelf</b> {h['bars']} bars, {_n(h['low'])}–{_n(h['pivot'])} "
                 f"({h['depth_pct']}% deep), {h['start']} to {h['end']}")
    L.append("")

    if t:
        L.append("<b>Technicals</b>")
        bits = []
        if t.get("rsi") is not None:
            bits.append(f"RSI {t['rsi']:.1f}")
        if t.get("adx") is not None:
            bits.append(f"ADX {t['adx']:.1f}")
        if t.get("atr_pct") is not None:
            bits.append(f"ATR {t['atr_pct']:.2f}%")
        if t.get("avg_range_pct") is not None:
            bits.append(f"avg day {t['avg_range_pct']:.1f}%")
        if bits:
            L.append(" · ".join(bits))
        dist = []
        for k, lbl in (("from_sma20_pct", "20 DMA"), ("from_sma50_pct", "50 DMA"),
                       ("from_sma200_pct", "200 DMA")):
            if t.get(k) is not None:
                dist.append(f"{_pct(t[k])} vs {lbl}")
        if dist:
            L.append(" · ".join(dist))
        if t.get("position_52w_pct") is not None:
            L.append(f"52-week range position {t['position_52w_pct']:.0f}% "
                     f"(high {_n(t.get('high_52w'))}, low {_n(t.get('low_52w'))})")
        if t.get("vol_x50") is not None:
            L.append(f"Volume {_n(t.get('vol_x50'), 1)}x its 50-day average")
        if t.get("days_since_breakout") is not None:
            if t["days_since_breakout"] == 0:
                L.append("Cleared the 20-day range high today")
            else:
                L.append(f"Cleared the 20-day range high "
                         f"{t['days_since_breakout']} sessions ago "
                         f"({t.get('breakout_date')})")
                if t.get("gain_since_breakout_pct") is not None:
                    L.append(f"Since then {_pct(t['gain_since_breakout_pct'])}")
        if t.get("measured_move_target"):
            L.append(f"Measured move target {_n(t['measured_move_target'])} "
                     f"({_pct(t.get('from_measured_move_pct'))} away)")
        if t.get("distribution_pct") is not None:
            L.append(f"Distribution: {t['distribution_pct']}% of the last 20 "
                     "sessions closed in the top third of their range")
        L.append("")

    # ---- base rates -------------------------------------------------------
    rates = r.get("rates") or {}
    matches = rates.get("matches") or []
    if matches:
        L.append("<b>Base rates</b> <i>(what followed the same setup)</i>")
        try:
            import base_rates as br
            for sig in matches:
                m = (rates.get("market") or {}).get(sig) or {}
                own = (rates.get("symbol") or {}).get(sig) or {}
                L.append(f"• <b>{_esc(br.SIGNAL_TEXT.get(sig, sig))}</b>")
                h10 = (m.get("h10") or {})
                if h10.get("n"):
                    L.append(f"  market: +10d median {_pct(h10.get('median'))}, "
                             f"positive {h10.get('hit'):.0f}% of {h10['n']:,} cases")
                else:
                    L.append("  market: not enough history")
                h10o = (own.get("h10") or {})
                if h10o.get("n"):
                    L.append(f"  this stock: +10d median {_pct(h10o.get('median'))}, "
                             f"positive {h10o.get('hit'):.0f}% of {h10o['n']} times")
        except Exception:
            pass
        L.append("<i>Base rates describe what happened before. They are not "
                 "predictions, and the market figure counts stock-days, not "
                 "independent events.</i>")
        L.append("")

    # ---- restrictions and risks ------------------------------------------
    L.append("<b>Restrictions and risks</b>")
    if r["gate"].get("blockers"):
        L.append(f"🚫 {_esc('; '.join(r['gate']['blockers']))}")
    elif r["gate"].get("notes"):
        L.append(f"• {_esc('; '.join(r['gate']['notes']))}")
    else:
        L.append("• no restriction found in the NSE archives")
    d = r.get("delivery") or {}
    if d.get("deliv_pct") is not None:
        tail = (f", {d['multiple']:.1f}x its {d['sessions']}-session average"
                if d.get("multiple") else "")
        L.append(f"• Delivery {d['deliv_pct']:.0f}%{tail}")
    res = r.get("results") or {}
    if res.get("date"):
        n = res.get("sessions")
        lbl = "confirmed" if res.get("source") == "confirmed" else "estimated"
        when = f"{res['date']}" + (f" ({n} sessions)" if n is not None else "")
        L.append(f"• Results {when}, {lbl}"
                 + (" ⚠️ inside a holding window" if n is not None and n <= 5 else ""))
    if r.get("liquidity_cr"):
        L.append(f"• Average traded value ₹{r['liquidity_cr']:.0f} cr/day")
    L.append("")
    L.append("<i>Educational only — not SEBI-registered advice. "
             "Verify every level with your broker.</i>")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# the detailed HTML report
# ---------------------------------------------------------------------------
CSS = """
:root{--bg:#0b0f14;--panel:#111823;--panel2:#0e141d;--line:#1f2a37;
--text:#e6edf5;--dim:#8ea0b5;--accent:#4da3ff;--green:#31d0aa;
--red:#ff6b6b;--amber:#ffc857;--violet:#9b8cff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
padding:0 0 40px;-webkit-text-size-adjust:100%}
.wrap{max-width:860px;margin:0 auto;padding:0 14px}
.hero{background:linear-gradient(160deg,#101a27,#0b0f14);padding:22px 0 18px;
border-bottom:1px solid var(--line)}
h1{font-size:24px;font-weight:700;letter-spacing:-.2px}
h2{font-size:17px;margin:26px 0 10px;display:flex;gap:9px;align-items:baseline}
h2 .num{color:var(--accent);font-size:13px;font-weight:600}
h3{font-size:14px;color:var(--dim);margin:16px 0 6px;font-weight:600}
p{margin:8px 0}
.sub{color:var(--dim);font-size:13px;margin-top:4px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:14px;margin:12px 0}
.pill{display:inline-block;padding:5px 12px;border-radius:999px;font-size:13px;
font-weight:600}
.pill.ok{background:rgba(49,208,170,.14);color:var(--green)}
.pill.warn{background:rgba(255,200,87,.14);color:var(--amber)}
.pill.bad{background:rgba(255,107,107,.14);color:var(--red)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat{background:var(--panel2);border:1px solid var(--line);border-radius:10px;
padding:10px 12px}
.stat .k{color:var(--dim);font-size:11px;text-transform:uppercase;
letter-spacing:.6px}
.stat .v{font-size:17px;font-weight:650;margin-top:3px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{text-align:left;color:var(--dim);font-weight:600;padding:8px 6px;
border-bottom:1px solid var(--line);font-size:12px;text-transform:uppercase;
letter-spacing:.5px}
td{padding:9px 6px;border-bottom:1px solid rgba(31,42,55,.65);
vertical-align:top}
tr:last-child td{border-bottom:none}
.ok2{color:var(--green)}.bad2{color:var(--red)}.warn2{color:var(--amber)}
.note{border-left:3px solid var(--accent);padding:8px 12px;background:var(--panel2);
border-radius:0 8px 8px 0;color:var(--dim);font-size:13.5px;margin:10px 0}
ul{margin:8px 0 8px 20px}
li{margin:4px 0}
footer{color:var(--dim);font-size:12px;margin-top:28px;padding-top:14px;
border-top:1px solid var(--line)}
@media(max-width:560px){
 body{font-size:14px}h1{font-size:20px}
 table,thead,tbody,tr,td,th{display:block}
 thead{position:absolute;left:-9999px}
 tr{border-bottom:1px solid var(--line);padding:6px 0}
 td{border:none;padding:4px 0}
 td:before{content:attr(data-label);display:block;color:var(--dim);
 font-size:11px;text-transform:uppercase;letter-spacing:.5px}
}
"""


def _row(cells) -> str:
    """One table row. A cell is (label, value); extra items are ignored."""
    out = ["<tr>"]
    for cell in cells:
        label, value = cell[0], cell[1]
        out.append(f"<td data-label='{_esc(label)}'>{value}</td>")
    out.append("</tr>")
    return "".join(out)


def html_report(r: dict) -> str:
    """A full report for one stock — readable on a phone and on a desktop."""
    if r.get("error"):
        return f"<html><body><p>{_esc(r['error'])}</p></body></html>"

    sym = r["symbol"]
    t = r.get("tech") or {}
    tr = r.get("trend") or {}
    b = r.get("base") or {}
    H = []
    a = H.append
    sec = [0]

    def h2(title: str) -> None:
        sec[0] += 1
        a(f"<h2><span class='num'>{sec[0]}</span>{_esc(title)}</h2>")

    pill = {"ok": "ok", "warn": "warn", "bad": "bad"}.get(r.get("css"), "warn")
    a("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>")
    a("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    a(f"<title>{_esc(sym)} — desk report</title><style>{CSS}</style></head><body>")

    a("<div class='hero'><div class='wrap'>")
    a(f"<h1>{_esc(sym)}"
      + (f" · {_esc(r['name'])}" if r.get("name") and r["name"] != sym else "")
      + "</h1>")
    a(f"<div class='sub'>Desk report · close of {_esc(r.get('last_date') or '')}"
      f" · ₹{_n(r.get('close'))} ({_pct(t.get('change_pct'))})</div>")
    a("</div></div>")
    a("<div class='wrap'>")

    a(f"<div class='card'><span class='pill {pill}'>{_esc(r.get('verdict'))}</span>")
    if r.get("state_text"):
        a(f"<div class='sub'>{_esc(r['state_text'])}</div>")
    if r.get("why"):
        a("<ul>")
        for w in r["why"]:
            a(f"<li>{_esc(w)}</li>")
        a("</ul>")
    a("</div>")

    # --- the trade ---
    h2("The trade")
    plans = r.get("plans") or []
    if plans:
        for p in plans:
            a("<div class='card'>")
            a(f"<h3>{_esc(p['label'])}</h3>")
            a("<table><thead><tr><th>Level</th><th>Value</th><th>Note</th>"
              "</tr></thead><tbody>")
            a(_row([("Entry", f"<b>₹{_n(p['entry'])}</b>"),
                    ("Stop", f"₹{_n(p['stop'])} "
                             f"<span class='sub'>({_n(p['stop_pct'], 1)}% below entry"
                             + (", capped to 8%" if p.get("stop_capped") else "")
                             + ")</span>"),
                    ("Size", f"{p['qty']} shares · ₹{_n(p.get('position_value'), 0)} "
                             f"<span class='sub'>({_n(p.get('allocation_pct'), 1)}% of capital, "
                             f"risk ₹{_n(p.get('risk'), 0)})</span>")]))
            for i, tg in enumerate(p.get("targets") or [], 1):
                a(_row([(f"Target {i}", f"<b>₹{_n(tg['price'])}</b>"),
                        ("R multiple", f"{tg['r']:g}R"),
                        ("Gain", _pct(tg.get("pct")))]))
            a("</tbody></table>")
            if p.get("note"):
                a(f"<div class='note'>{_esc(p['note'])}</div>")
            if p.get("stop_quality") == "capped inside the base":
                a("<div class='note'>The stop had to be pulled in to fit the 8% "
                  "rule, which puts it inside the structure. Treat this plan as "
                  "worse than it looks.</div>")
            a("</div>")
    else:
        a("<div class='card'><b>No trade to take right now.</b>")
        if b.get("note"):
            a(f"<div class='sub'>{_esc(b['note'])}</div>")
        a("</div>")

    # --- trend and base ---
    h2("Trend and base")
    a("<div class='card'><table><thead><tr><th>Check</th><th>Result</th>"
      "<th>Detail</th></tr></thead><tbody>")
    for c in tr.get("criteria") or []:
        cls = "ok2" if c.get("pass") else "bad2"
        mark = "✓" if c.get("pass") else "✗"
        a(_row([(c.get("label") or "", f"<span class='{cls}'>{mark}</span>"),
                ("Result", "pass" if c.get("pass") else "fail"),
                ("Detail", _esc(c.get("detail") or ""))]))
    a("</tbody></table>")
    a("<div class='note'>Stage 2 means the six checks on this list pass. It "
      "is a filter, not a signal: it says the stock is in a strong uptrend, "
      "nothing about when to buy.</div>")
    a("</div>")

    if b.get("type"):
        a("<div class='card'><table><tbody>")
        a(_row([("Base", f"{_esc(b['type'])} grade {_esc(b.get('grade') or '?')}"),
                ("Length", f"{_n(b.get('weeks'), 1)} weeks"),
                ("Depth", f"{_n(b.get('depth'), 1)}%"),
                ("Old pivot", f"₹{_n(b.get('pivot'))}"
                 + (f" — cleared {b['cleared_on']}" if b.get("cleared_on") else "")),
                ("Price vs pivot", _pct(b.get("from_pivot_pct")))]))
        a("</tbody></table>")
    else:
        a(f"<div class='card'><b>No base.</b> "
          f"<span class='sub'>{_esc(b.get('note') or '')}</span></div>")

    if r.get("handle"):
        hh = r["handle"]
        a("<div class='card'><h3>Recent shelf</h3><table><tbody>")
        a(_row([("Range", f"₹{_n(hh['low'])} – ₹{_n(hh['pivot'])}"),
                ("Depth", f"{hh['depth_pct']}%"),
                ("Length", f"{hh['bars']} bars"),
                ("Dates", f"{hh['start']} to {hh['end']}")]))
        a("</tbody></table></div>")

    # --- technicals ---
    h2("Technicals")
    a("<div class='grid'>")
    for k, v in (("RSI (14)", _n(t.get("rsi"), 1) if t.get("rsi") is not None else "—"),
                 ("ADX (14)", _n(t.get("adx"), 1) if t.get("adx") is not None else "—"),
                 ("ATR", f"{_n(t.get('atr_pct'), 2)}%" if t.get("atr_pct") is not None else "—"),
                 ("Avg day", f"{_n(t.get('avg_range_pct'), 1)}%" if t.get("avg_range_pct") is not None else "—"),
                 ("Vol vs 50d", f"{_n(t.get('vol_x50'), 1)}x" if t.get("vol_x50") is not None else "—"),
                 ("52w position", f"{_n(t.get('position_52w_pct'), 0)}%" if t.get("position_52w_pct") is not None else "—"),
                 ("vs 20 DMA", _pct(t.get("from_sma20_pct")) if t.get("from_sma20_pct") is not None else "—"),
                 ("vs 50 DMA", _pct(t.get("from_sma50_pct")) if t.get("from_sma50_pct") is not None else "—"),
                 ("vs 200 DMA", _pct(t.get("from_sma200_pct")) if t.get("from_sma200_pct") is not None else "—"),
                 ("Distribution", f"{t.get('distribution_pct')}%" if t.get("distribution_pct") is not None else "—")):
        a(f"<div class='stat'><div class='k'>{_esc(k)}</div>"
          f"<div class='v'>{_esc(v)}</div></div>")
    a("</div>")
    if t.get("measured_move_target"):
        a(f"<div class='note'>Measured move target ₹{_n(t['measured_move_target'])} "
          f"({_pct(t.get('from_measured_move_pct'))} from here). A measured move "
          "is the height of the base added to its high — a way to size the "
          "expected move, never a promise.</div>")
    signals = t.get("signals") or {}
    if signals:
        a("<div class='card'><h3>Indicator read</h3><table><tbody>")
        for k, (txt, state) in signals.items():
            cls = {"bullish": "ok2", "bearish": "bad2"}.get(state, "warn2")
            a(_row([(k, f"<span class='{cls}'>{_esc(txt)}</span>"),
                    ("Read", state)]))
        a("</tbody></table></div>")

    # --- base rates ---
    rates = r.get("rates") or {}
    matches = rates.get("matches") or []
    if matches:
        h2("Base rates")
        a("<div class='card'><table><thead><tr><th>Situation</th><th>Market</th>"
          "<th>This stock</th></tr></thead><tbody>")
        try:
            import base_rates as br
            for sig in matches:
                m = (rates.get("market") or {}).get(sig) or {}
                own = (rates.get("symbol") or {}).get(sig) or {}
                mh = (m.get("h10") or {})
                oh = (own.get("h10") or {})
                mtxt = (f"+10d median {_pct(mh.get('median'))}, "
                        f"{mh.get('hit'):.0f}% positive of {mh['n']:,}"
                        if mh.get("n") else "not enough history")
                otxt = (f"+10d median {_pct(oh.get('median'))}, "
                        f"{oh.get('hit'):.0f}% positive of {oh['n']}"
                        if oh.get("n") else "no history")
                a(_row([(br.SIGNAL_TEXT.get(sig, sig), mtxt),
                        ("This stock", otxt)]))
        except Exception:
            pass
        a("</tbody></table>")
        a("<div class='note'>Base rates count what the market did after the "
          "same situation in the past. The market column counts stock-days, "
          "not independent events, so a big up day for the whole market "
          "contributes hundreds of them. They describe the past; they do not "
          "predict the next one.</div>")
        a("</div>")

    # --- restrictions ---
    h2("Restrictions, delivery and results")
    a("<div class='card'><table><tbody>")
    blockers = r["gate"].get("blockers") or []
    a(_row([("Series", _esc(r["gate"].get("series") or "—")),
             ("Price band", f"{r['gate']['band_pct']}%" if r["gate"].get("band_pct") else "—"),
             ("Restriction", _esc("; ".join(blockers)) if blockers
              else "no restriction found in the NSE archives")]))
    d = r.get("delivery") or {}
    if d.get("deliv_pct") is not None:
        a(_row([("Delivery", f"{d['deliv_pct']:.0f}%"),
                 ("Against its own average",
                  f"{d['multiple']:.1f}x of {d['sessions']} sessions"
                  if d.get("multiple") else "—")]))
    res = r.get("results") or {}
    if res.get("date"):
        a(_row([("Next results", _esc(str(res["date"]))),
                 ("Status", "confirmed" if res.get("source") == "confirmed" else "estimated"),
                 ("Sessions away",
                  str(res.get("sessions") if res.get("sessions") is not None else "—"))]))
    if r.get("liquidity_cr"):
        a(_row([("Avg traded value", f"₹{r['liquidity_cr']:.0f} cr/day")]))
    a("</tbody></table></div>")

    if r.get("held"):
        hh = r["held"]
        a("<div class='note'>Already on the book: "
          f"{_esc(hh['status'].replace('_', ' '))} · entry ₹{_n(hh.get('entry'))} · "
          f"stop ₹{_n(hh.get('stop'))} · T1 ₹{_n(hh.get('t1'))}</div>")

    a("<footer>Educational analysis only — not SEBI-registered investment advice. "
      "Markets carry risk of loss; past performance does not guarantee future "
      "results. Verify every level with your broker before placing an order.</footer>")
    a("</div></body></html>")
    return "".join(H)


def report_file(symbol: str, result: dict) -> Path | None:
    """Write the detailed HTML report and return its path."""
    if result.get("error"):
        return None
    try:
        out_dir = REPO / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = __import__("datetime").datetime.now().strftime("%Y-%m-%d_%H%M")
        path = out_dir / f"check_{symbol.upper()}_{stamp}.html"
        path.write_text(html_report(result), encoding="utf-8")
        return path
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", action="store_true")
    ap.add_argument("--capital", type=float)
    ap.add_argument("--risk-pct", type=float)
    args = ap.parse_args()

    r = check(args.symbol, args.capital, args.risk_pct)
    if args.json:
        print(json.dumps(r, indent=1, default=str))
        return 0
    if args.html:
        print(html_report(r))
        return 0
    for c in cards(r):
        print(re.sub(r"<b>|</b>|<i>|</i>|<code>|</code>", "", c))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
