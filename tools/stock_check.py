#!/usr/bin/env python3
"""Answer "what about this stock?" for one symbol, in one card.

The bulletin scans 2,400 names and gives you six. That is the wrong shape for
the question people actually ask twenty times a day, which is about one stock
they are already looking at.

Everything here is the same pipeline the report uses, run on one symbol:

    trend template   the six Stage-2 checks, and which ones failed
    base             type, grade, depth, weeks, pivot
    plan             trigger, chase limit, stop, R targets, size
    restrictions     price band, trade-for-trade, F&O ban
    delivery         today against its own 20-session average
    results          next earnings date, confirmed or estimated
    book             whether you already hold it, and at what R

The verdict is not recomputed here. It calls bulletin_render.plan_verdict, so
a stock cannot be BUY-READY in a chat and WATCH in the report on the same day.

    python3 tools/stock_check.py CENTUM
    python3 tools/stock_check.py TCS --json
"""
from __future__ import annotations

import argparse
import json
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

    # A plan needs a base. Most stocks do not have one, and that is a valid
    # answer: it is not a setup today. The engine says "none" rather than null,
    # so a truthiness test would spawn a plan that is guaranteed to fail.
    _raw_base = (base.get("base") or {}).get("type")
    has_base = bool(_raw_base) and str(_raw_base).lower() not in ("none", "null")
    plan = {}
    if has_base:
        plan = _run([PYTHON, str(BREAKOUT), "plan", "--symbol", sym,
                     "--benchmark", "NIFTY", "--capital", str(capital),
                     "--risk-pct", str(risk_pct), "--json"])

    tt = base.get("trend_template") or {}
    b = base.get("base") or {}
    p = plan.get("plan") or {}
    close = base.get("close")

    pivot = p.get("pivot") or b.get("pivot")
    from_pivot = (round(100 * (close / pivot - 1), 2)
                  if close and pivot else None)

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

    verdict, css = "NO DATA", "warn"
    try:
        import bulletin_render
        verdict, css = bulletin_render.plan_verdict({
            "stage2": bool(tt.get("stage2")),
            "from_pivot_pct": from_pivot,
            "base_grade": b.get("grade"),
            "tt_passed": tt.get("passed"),
            "blockers": gate.get("blockers") or [],
        })
    except Exception:
        pass

    return {
        "symbol": sym,
        "name": (base.get("name") or "").replace(".NS", "").replace(".BO", ""),
        "close": close,
        "last_date": base.get("last_date"),
        "bars": base.get("bars"),
        "verdict": verdict, "css": css,
        "stage2": bool(tt.get("stage2")),
        "tt_passed": tt.get("passed"), "tt_total": tt.get("total"),
        "tt_failed": [c["label"] for c in (tt.get("criteria") or []) if not c.get("pass")],
        "high_52w": tt.get("high_52w"), "low_52w": tt.get("low_52w"),
        "sma50": tt.get("sma50"), "sma200": tt.get("sma200"),
        "base": {"type": b.get("type") if has_base else None,
                 "grade": b.get("grade") if has_base else None,
                 "note": (b.get("notes") or [None])[0] if not has_base else None,
                          "weeks": b.get("weeks") if has_base else None,
                 "depth": b.get("depth_pct") if has_base else None,
                 "pivot": pivot, "from_pivot_pct": from_pivot,
                 "state": b.get("state")},
        "liquidity_cr": (base.get("liquidity") or {}).get("avg_traded_value_cr"),
        "plan": {"trigger": p.get("trigger"), "limit": p.get("limit"),
                 "stop": p.get("stop"), "stop_method": p.get("stop_method"),
                 "qty": p.get("qty"),
                 "position_value": p.get("position_value"),
                 "allocation_pct": p.get("allocation_pct"),
                 "warnings": p.get("warnings") or [],
                 "targets": [{"r": t.get("R"), "price": t.get("price"),
                              "pct": t.get("pct_from_entry")}
                             for t in (p.get("targets") or [])]},
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
# card
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
    import html
    return html.escape(str(s or ""))


def card(r: dict) -> str:
    if r.get("error"):
        return f"⚠️ {_esc(r['error'])}"

    import market_calendar as cal  # noqa: F401  (kept for parity of imports)
    sym = r["symbol"]
    L = [f"🔎 <b>{_esc(sym)}</b>"
         + (f" · {_esc(r['name'])}" if r.get("name") and r["name"] != sym else ""),
         f"<code>₹{_n(r['close'])}</code>"
         + (f" · {_esc(r['gate'].get('series'))}" if r["gate"].get("series") else "")
         + (f" · {r['gate']['band_pct']}% band" if r["gate"].get("band_pct") else ""),
         ""]

    tt = r["tt_total"] or 0
    L.append(f"<b>Trend</b> {r['tt_passed'] or 0}/{tt} · "
             + ("Stage 2 ✓" if r["stage2"] else "not Stage 2"))
    if r.get("tt_failed"):
        L.append(f"<i>fails: {_esc('; '.join(r['tt_failed']))}</i>")

    b = r["base"] or {}
    if b.get("type"):
        L.append(f"<b>Base</b> {_esc(b['type'])} grade {_esc(b.get('grade') or '?')} · "
                 f"{_n(b.get('weeks'), 1)} weeks · {_n(b.get('depth'), 1)}% deep")
        if b.get("pivot"):
            L.append(f"<b>Pivot</b> {_n(b['pivot'])} · now "
                     f"<b>{_pct(b.get('from_pivot_pct'))}</b> from it")
    else:
        L.append("<b>Base</b> none — no tight base, so there is nothing to buy")
        if b.get("note"):
            L.append(f"<i>{_esc(b['note'])}</i>")

    d = r.get("delivery") or {}
    if d.get("deliv_pct") is not None:
        tail = f", {d['multiple']:.1f}x its {d['sessions']}-session average" if d.get("multiple") else ""
        L.append(f"<b>Delivery</b> {d['deliv_pct']:.0f}%{tail}")

    res = r.get("results") or {}
    if res.get("date"):
        n = res.get("sessions")
        lbl = "confirmed" if res.get("source") == "confirmed" else "estimated"
        when = f"{res['date']}" + (f" ({n} sessions)" if n is not None else "")
        L.append(f"<b>Results</b> {when}, {lbl}"
                 + (" ⚠️ inside a holding window" if n is not None and n <= 5 else ""))

    if r["gate"].get("blockers"):
        L += ["", "🚫 <b>NOT TRADABLE</b>",
              _esc("; ".join(r["gate"]["blockers"]))]
    elif r["gate"].get("notes"):
        L.append(f"<i>{_esc('; '.join(r['gate']['notes']).capitalize())}</i>")

    held = r.get("held")
    if held:
        L += ["", f"💼 <b>Already on the book</b> ({_esc(held['status'].replace('_', ' '))})",
              f"entry {_n(held.get('entry'))} · stop {_n(held.get('stop'))} · "
              f"T1 {_n(held.get('t1'))}"]

    p = r.get("plan") or {}
    if p.get("trigger"):
        L += ["", f"<b>Buy above</b> {_n(p['trigger'])}",
              f"<i>limit {_n(p['limit'])} — never pay more</i>",
              f"<b>Stop</b> {_n(p['stop'])} ({_esc(p.get('stop_method'))})"]
        for i, t in enumerate((p.get("targets") or [])[:2], 1):
            L.append(f"<b>T{i}</b> {_n(t['price'])} "
                     f"({t['r']:g}R, {_pct(t.get('pct'))})")
        if p.get("qty"):
            L.append(f"<i>{p['qty']} shares · ₹{_n(p.get('position_value'), 0)} · "
                     f"{_n(p.get('allocation_pct'), 1)}% of capital</i>")
        for w in (p.get("warnings") or [])[:2]:
            L.append(f"<i>⚠️ {_esc(w)}</i>")

    L += ["", f"<b>Verdict: {_esc(r['verdict'])}</b>",
          "<i>Educational only — not advice.</i>"]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--capital", type=float)
    ap.add_argument("--risk-pct", type=float)
    args = ap.parse_args()

    r = check(args.symbol, args.capital, args.risk_pct)
    if args.json:
        print(json.dumps(r, indent=1, default=str))
    else:
        print(card(r).replace("<b>", "").replace("</b>", "")
              .replace("<i>", "").replace("</i>", "").replace("<code>", "")
              .replace("</code>", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
