#!/usr/bin/env python3
"""Render the bulletin dataset into Markdown and a styled standalone HTML page.

The renderers work only from structured fields — there are no fixed-width CLI
tables anywhere in the output. HTML uses real <table> elements with data-labels
so rows stack into readable cards on a phone.
"""
from __future__ import annotations

import html as _h

QUAD_CLASS = {
    "Leading": "q-lead",
    "Improving": "q-impr",
    "Weakening": "q-weak",
    "Lagging": "q-lag",
}


def _n(v, digits=2):
    """Format a number with Indian-style grouping (lakh/crore friendly)."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    neg = f < 0
    s = f"{abs(f):,.{digits}f}"
    return ("-" if neg else "") + s


def _money(v, digits=0):
    if v is None:
        return "—"
    return "₹" + _n(v, digits)


def _pct(v, digits=2, signed=True):
    if v is None:
        return "—"
    s = f"{v:+.{digits}f}%" if signed else f"{v:.{digits}f}%"
    return s


def _b(text: str) -> str:
    """Escape, then turn **bold** markers into <b> tags."""
    t = _h.escape(str(text))
    while "**" in t:
        t = t.replace("**", "<b>", 1)
        if "**" in t:
            t = t.replace("**", "</b>", 1)
    return t


def _closed_note_md(d: dict) -> str:
    cal = d.get("calendar") or {}
    if cal.get("trading_day", True):
        return ""
    nxt = cal.get("next_trading_day")
    tail = f" Next session: **{nxt}**." if nxt else ""
    return ("> **Exchange closed today.** NSE/BSE did not trade, so every price and "
            f"level below is from the last session.{tail} Treat the plans as a "
            "standing watchlist, not as entries for today.\n")


def _split_signal(s: str):
    """'80.1 overbought' -> ('80.1', 'overbought')."""
    parts = (s or "").split(" ", 1)
    return (parts[0], parts[1] if len(parts) > 1 else "")


def _cls(v):
    return "pos" if (v or 0) >= 0 else "neg"


RISK_LABELS = {
    "distribution": "Distribution days",
    "trend": "Trend structure",
    "drawdown": "Drawdown from peak",
    "vix": "India VIX",
    "fresh_follow_through": "Fresh follow-through (bonus)",
}


def risk_label(k: str) -> str:
    return RISK_LABELS.get(k, k.replace("_", " ").title())


def plan_verdict(p: dict) -> tuple[str, str]:
    """Return (label, css class) for a trade plan."""
    fp = p.get("from_pivot_pct")
    if fp is not None and fp > 1.5:
        return "MISSED — above chase limit", "warn"
    if p.get("stage2") and (fp is None or fp >= -4):
        return "BUY-READY", "good"
    if (p.get("tt_passed") or 0) >= 5 and (fp is None or fp >= -4):
        return "WATCH — 5/6 trend", "warn"
    return "WATCH ONLY — not Stage 2", "warn"


# ---------------------------------------------------------------------------
# 100% club — shared narrative
# ---------------------------------------------------------------------------
def _study_sentence(c: dict) -> str:
    """Plain-language summary of the stock's own doubling base rate."""
    s = c.get("study") or {}
    n = s.get("samples") or 0
    if not n:
        return ("No comparable Stage-2 setup in this stock's history — there is no base rate "
                "to quote, so size this one smaller than the others.")
    years = int(s.get("history_years") or max(1, round(n * 63 / 252)))
    h50, h100 = s.get("hit50") or 0, s.get("hit100") or 0
    r50 = s.get("rate50_pct")
    r100 = s.get("rate100_pct")
    bars = s.get("median_bars_to_100")
    worst = s.get("worst_fwd_pct")

    head = (f"In {n} comparable setups over roughly {years} years it gained 50%+ in "
            f"{h50} ({r50:.0f}%) and 100%+ in {h100} ({r100:.0f}%).")
    if bars:
        head += f" Median time to double: about {round(bars / 21)} months."
    elif not h100:
        head += " It has never doubled within a year from a setup like this."
    if worst is not None:
        head += f" Worst drawdown along the way: {worst:.0f}%."
    head += " Small sample from one stock's own tape — context, not a probability."
    return head


def _why_bullets(c: dict) -> list[str]:
    b = c.get("base") or {}
    p = c.get("plan") or {}
    out = []
    if c.get("structure") == "breakout":
        out.append(f"**{b.get('type')} base, grade {b.get('grade')}** — {_n(b.get('weeks'), 1)} "
                   f"weeks, {_n(b.get('depth_pct'), 1)}% deep, "
                   f"{b.get('contractions')} contraction(s). Pivot {_n(b.get('pivot'))}, "
                   f"price {_pct(b.get('from_pivot_pct'))} from it.")
    else:
        out.append("**No usable base right now** — this is a trend-continuation entry: buy the "
                   "pullback toward the 20-day average, stop under the 50-day. Lower quality than "
                   "a clean breakout.")

    tt = f"{c.get('tt_passed')}/{c.get('tt_total')}"
    age = c.get("stage2_age_months")
    age_txt = (f"the advance is {age:.0f} months old" if age else "the advance age is unclear")
    out.append(f"**Stage-2 checklist {tt}**, RS rating {c.get('rs_rating') or 'n/a'}, "
               f"{age_txt}. Most doublers come from the first 18 months of a Stage-2 run, "
               "not the fifth year.")

    wk = c.get("weekly") or {}
    out.append(f"**Weekly trend is {wk.get('label')}** — {wk.get('detail')}.")

    if c.get("support") or c.get("resistance"):
        out.append(f"**Levels:** support {', '.join(_n(x, 0) for x in c.get('support') or []) or 'n/a'} · "
                   f"resistance {', '.join(_n(x, 0) for x in c.get('resistance') or []) or 'n/a'}.")

    if c.get("extended"):
        out.append(f"**Stretched right now** — RSI {_n(c.get('rsi'), 1)}. Buying here means "
                   "paying up; the limit order below is the disciplined entry.")
    else:
        out.append(f"**Not stretched** — RSI {_n(c.get('rsi'), 1)}, ATR {_n(c.get('atr_pct'), 1)}% "
                   "of price, so the stop distance is realistic for a swing.")

    vd = b.get("volume_dryup")
    if vd:
        out.append(f"**Volume dry-up ratio {_n(vd, 2)}** — "
                   + ("supply is drying up inside the base, which is what you want."
                      if vd <= 1.0 else
                      "volume is still above the base average, so sellers are not gone yet."))
    return out


def _what_breaks_it(c: dict) -> list[str]:
    p = c.get("plan") or {}
    out = [f"A close below **{_n(p.get('stop'))}** voids the idea — that is "
           f"{_n(abs(p.get('stop_pct') or 0), 1)}% below entry, and the whole thesis goes with it, "
           "not just the price."]
    if c.get("structure") == "breakout":
        out.append("A breakout on volume below ~1.5× the 50-day average usually fails within days.")
    out.append("Results, a block deal or an ASM/GSM listing inside the hold window can gap the "
               "stock straight through the stop — check the calendar before you size.")
    out.append("A market regime flip to correction (distribution days piling up, Nifty under the "
               "50-day) cuts every long. Halve size or stand aside.")
    if c.get("extended"):
        out.append("Extended RSI means the first pullback can be violent; expect to sit through it.")
    return out


def _ladder_rows(c: dict) -> list[list[str]]:
    p = c.get("plan") or {}
    lad = p.get("ladder")
    if not lad:
        return []
    return [
        ["T1", str(lad["third"]), _n(p.get("t1")), _pct(p.get("t1_pct"), 1), _money(lad["net_at_t1"])],
        ["T2", str(lad["third"]), _n(p.get("t2")), _pct(p.get("t2_pct"), 1), _money(lad["net_at_t2"])],
        ["+100%", str(lad["runner"]), _n(p.get("hundred")), "+100.0%", _money(lad["net_at_100"])],
        ["**Total**", str(lad["qty"]), "—", f"**{_pct(lad['pct_of_capital'], 1, signed=False)} of capital**",
         f"**{_money(lad['net_total'])}**"],
    ]


def _deep_md(d: dict) -> list[str]:
    deep = d.get("deep") or {}
    cands = deep.get("candidates") or []
    L = ["## 6. The 100% Club — the path to doubling", ""]
    L.append("> **Read this first.** No setup guarantees a 100% gain, and anyone who says "
             "otherwise is selling something. This section shows the three things that actually "
             "decide a doubler: the structure, the stock's own history of doubling from a "
             "similar setup, and the arithmetic of a defined-risk ladder. A 100% target is an "
             "objective you manage toward, not a forecast.")
    L.append("")
    if not cands:
        L.append(f"_{deep.get('note') or 'No candidate cleared the screen today.'}_")
        L.append("")
        return L

    L.append(f"Screened {deep.get('screened', 0)} names · {deep.get('pool', 0)} passed the "
             f"trend filter · showing the best {len(cands)}.")
    L.append("")
    for i, c in enumerate(cands, 1):
        p = c.get("plan") or {}
        b = c.get("base") or {}
        L.append(f"### {i}. {c['symbol']} — grade {c['grade']} · "
                 f"{c['score']:.0f}/100 · RS {c.get('rs_rating') or 'n/a'}")
        L.append("")
        L.append(f"_{b.get('type') or 'no base'} {b.get('grade') or ''} · "
                 f"{_n(b.get('weeks'), 1)} weeks · {c.get('structure')} entry · "
                 f"₹{_n(c.get('liquidity_cr'), 0)} crore/day_")
        L.append("")
        L.append("| Level | Price | From entry |")
        L.append("|---|---|---|")
        L.append(f"| Entry (buy above) | {_n(p.get('entry'))} | — |")
        if p.get("limit"):
            L.append(f"| Chase limit (max) | {_n(p.get('limit'))} | "
                     f"{_pct(100 * (p['limit'] / p['entry'] - 1), 1)} |")
        L.append(f"| **Stop loss** | **{_n(p.get('stop'))}** | {_pct(-(p.get('stop_pct') or 0))} |")
        if p.get("t1"):
            L.append(f"| Target 1 (2R) | {_n(p.get('t1'))} | {_pct(p.get('t1_pct'), 1)} |")
        if p.get("t2"):
            L.append(f"| Target 2 ({p.get('t2_driver')}) | {_n(p.get('t2'))} | "
                     f"{_pct(p.get('t2_pct'), 1)} |")
        L.append(f"| **100% objective** | **{_n(p.get('hundred'))}** | **+100%** |")
        L.append("")
        L.append(f"Size **{p.get('qty')} shares** · {_money(p.get('position_value'))} deployed · "
                 f"risk if stopped {_money(p.get('risk_if_stopped'))} · "
                 f"charges about {_money(p.get('charges'))} · "
                 f"**{_n(p.get('r_to_100'), 0)}R to double**.")
        L.append("")
        L.append("**Why this one**")
        L.append("")
        for w in _why_bullets(c):
            L.append(f"- {w}")
        L.append("")
        L.append("**Evidence from its own history**")
        L.append("")
        L.append(_study_sentence(c))
        L.append("")
        rows = _ladder_rows(c)
        if rows:
            L.append("**The ladder** — book a third at each target, trail the runner to +100%")
            L.append("")
            L.append("| Leg | Shares | Exit price | Move | Net ₹ |")
            L.append("|---|---|---|---|---|")
            for r in rows:
                L.append("| " + " | ".join(r) + " |")
            lad = p.get("ladder")
            L.append("")
            L.append(f"If all three legs work: **{_money(lad['net_total'])}**, about "
                     f"{_pct(lad['pct_of_capital'], 1, signed=False)} of capital, "
                     f"{_n(lad['blended_r'], 1)}R blended.")
            L.append("")
        L.append("**What breaks it**")
        L.append("")
        for w in _what_breaks_it(c):
            L.append(f"- {w}")
        L.append("")
    L.append(f"> {deep.get('disclaimer', '')}")
    L.append("")
    return L


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def render_md(d: dict) -> str:
    idx, reg = d["index"], d["regime"]
    mode = d.get("mode", "intraday")
    L = []
    a = L.append

    heading = {"daily": "Pre-market Brief", "eod": "Closing Wrap"}.get(mode, "Market Bulletin")
    a(f"# {heading} — India — {d['generated_at'][:16].replace('T', ' ')}")
    a("")
    a("> Educational analysis only. Not SEBI-registered advice. Markets carry risk of loss.")
    a("")
    a(_closed_note_md(d))

    a("## At a Glance")
    a("")
    a("| What | Value | Read |")
    a("|---|---|---|")
    a(f"| Nifty 50 | {_n(idx['close'])} ({_pct(idx['change_pct'])}) | "
      f"{_pct(idx['from_high_pct'])} from 52-week high |")
    a(f"| India VIX | {_n(d['vix'])} | {vix_read(d['vix'])} |")
    a(f"| Regime | {reg['label']} | risk {reg['risk_score']}/100, "
      f"exposure ceiling {reg['exposure_pct']}% |")
    a(f"| Trend | 50 DMA {_n(idx['sma50'])} · 200 DMA {_n(idx['sma200'])} | "
      f"{'price above both' if (idx['close'] or 0) > (idx['sma50'] or 0) else 'below 50 DMA'} |")
    a(f"| RSI(14) | {idx['rsi']} | >70 overbought, <30 oversold |")
    a(f"| ADX(14) | {idx['adx']} | >25 means a trending market |")
    a("")

    a(f"**Bottom line:** {verdict(reg)}")
    a("")
    if mode == "daily":
        sup = [x for x in (idx.get("supports") or []) if x]
        res = [x for x in (idx.get("resistances") or []) if x]
        a(f"**Plan for the session.** Previous close {_n(idx['close'])} ({_pct(idx['change_pct'])}). "
          f"Support {', '.join(_n(x) for x in sup) if sup else 'n/a'}; "
          f"resistance {', '.join(_n(x) for x in res) if res else 'n/a'}.")
    elif mode == "eod":
        movers = d.get("momentum", {}).get("movers") or []
        top = ", ".join(m["symbol"] for m in movers[:3])
        a(f"**Session wrap.** Nifty closed at {_n(idx['close'])} ({_pct(idx['change_pct'])}), "
          f"{_pct(idx['from_high_pct'])} from its 52-week high."
          + (f" Names on the move: {top}." if top else ""))
    a("")

    a("## 1. Market Regime")
    a("")
    a(f"**{reg['label']}** — risk score {reg['risk_score']}/100 (lower is safer). "
      f"Exposure ceiling {reg['exposure_pct']}% of trading capital, "
      f"max {reg['max_positions']} new positions.")
    a("")
    a(f"{reg['stance']}")
    a("")
    a("| Risk driver | Points | Of |")
    a("|---|---|---|")
    for k, v in (reg["risk_parts"] or {}).items():
        mx = (reg["risk_max"] or {}).get(k)
        a(f"| {risk_label(k)} | {v} | {mx if mx is not None else '—'} |")
    a("")
    dc = reg["distribution_counts"]
    a(f"Distribution days: {dc.get('d5', 0)} in 5 sessions, {dc.get('d15', 0)} in 15, "
      f"{dc.get('d25', 0)} in 25.")
    a("")

    a("## 2. Sector Rotation")
    a("")
    a(f"_{d['sectors']['summary']}_")
    a("")
    a("| Rank | Sector | Quadrant | 1W | 1M | 3M | vs Nifty 1M | RS | Trend |")
    a("|---|---|---|---|---|---|---|---|---|")
    for i, s in enumerate(d["sectors"]["rows"], 1):
        a(f"| {i} | {s['key']} | {s['quadrant']} | {_pct(s['r1w'], 1)} | {_pct(s['r1m'], 1)} | "
          f"{_pct(s['r3m'], 1)} | {_pct(s['rel1m'], 1)} | {_n(s['rs_ratio'])} | {s['trend']} |")
    a("")
    if d["sectors"]["size"]:
        a("**Size factor:** " + ", ".join(
            f"{s['key']} {s['quadrant']} ({_pct(s['rel3m'], 1)} vs Nifty 3M)"
            for s in d["sectors"]["size"]))
        a("")

    a("## 3. Momentum — what moved")
    a("")
    a("| # | Stock | Close | RS | Score | From 52W high | Trigger |")
    a("|---|---|---|---|---|---|---|")
    for i, m in enumerate(d["momentum"]["movers"], 1):
        a(f"| {i} | {m['symbol']} | {_n(m['close'])} | {m['rs']} | {_n(m['score'], 1)} | "
          f"{_pct(m['from_high'], 1)} | {'; '.join(m['signals']) or '—'} |")
    a("")

    a("## 4. Breakout Setups")
    a("")
    a("| # | Stock | Close | Trend | Base | Grade | Pivot | From pivot | ₹cr/day |")
    a("|---|---|---|---|---|---|---|---|---|")
    for i, s in enumerate(d["breakouts"]["setups"][:12], 1):
        a(f"| {i} | {s['symbol']} | {_n(s['close'])} | {s['tt_passed']}/{s['tt_total']} | "
          f"{s['base_type']} | {s['base_grade']} | {_n(s['pivot'])} | "
          f"{_pct(s['from_pivot_pct'])} | {_n(s['liquidity_cr'], 1)} |")
    a("")

    L_deep = _deep_md(d)
    for line in L_deep:
        a(line)

    a("## 7. Trade Plans")
    a("")
    a("### Plain English first")
    a("")
    a("- **Stage 2** — a stock in a confirmed uptrend: above a rising 50/150/200-day average, "
      "the averages stacked in order, within 25% of its 52-week high, well above its 52-week low, "
      "and beating Nifty over 3–6 months. Only Stage-2 breakouts have a decent hit rate.")
    a("- **VCP (Volatility Contraction Pattern)** — a base where each pullback is smaller than the "
      "last (say 25%, then 11%, then 6%) on shrinking volume. Sellers are running out. Grade A is "
      "the tightest, D is loose.")
    a("- **Pivot** — the price the stock must clear to break out. **Chase limit** is the most you "
      "pay (about 3% above pivot). **Stop** is where you admit the trade is wrong.")
    a("")
    ready_md = [p for p in d["plans"] if plan_verdict(p)[1] == "good"]
    if d["plans"] and not ready_md:
        a(f"> **No A-grade setup today.** None of the {len(d['plans'])} candidates clears every "
          "Stage-2 check — treat these as a watchlist, not orders.")
        a("")
    if d["plans"]:
        for p in d["plans"]:
            stage = "Stage 2 ✓" if p["stage2"] else f"trend {p['tt_passed']}/{p['tt_total']} (not Stage 2)"
            vlabel, _ = plan_verdict(p)
            a(f"### {p['symbol']} — {vlabel}")
            a("")
            a(f"_{stage} · {p['base_type']} base grade {p['base_grade']}_")
            a("")
            a(f"- Close {_n(p['close'])} · pivot {_n(p['pivot'])} · {_pct(p['from_pivot_pct'])} from pivot")
            a(f"- **Buy above {_n(p['trigger'])}** (limit {_n(p['limit'])})")
            a(f"- **Stop {_n(p['stop'])}** ({_pct(-(p['stop_pct'] or 0))} from trigger, {p['stop_method']})")
            a(f"- Size: {p['qty']} shares, {_money(p['position_value'])} "
              f"({_pct(p['allocation_pct'], 1, signed=False)} of capital)")
            a(f"- Risk if stopped: {_money(p['risk_if_stopped'])} of {_money(p['risk_budget'])} budget")
            if p["targets"]:
                tgt = " · ".join(
                    f"T{i} ({int(t['r'])}R) {_n(t['price'])} ({_pct(t['pct'], 1)}, net {_money(t['net'])})"
                    for i, t in enumerate(p["targets"], 1))
                a(f"- Targets: {tgt}")
            if p["warnings"]:
                for w in p["warnings"]:
                    a(f"- ⚠ {w}")
            a("")
    else:
        a("No tradable setup right now. Forcing a trade in a thin tape is how accounts die — "
          "wait for a pivot with volume.")
        a("")

    a("### How to place the order (apply to any plan above)")
    a("")
    if d["plans"]:
        o = d["plans"][0]["orders"]
        a(f"1. {o.get('gtt_entry', 'Use a GTT buy at the trigger with the chase limit.')}")
        a(f"2. {o.get('gtt_exit', 'After the fill, place an OCO sell: stop below, target above.')}")
        a(f"3. {o.get('day0', 'Shares settle T+1.')}")
        a("")
    a("### Checklist before any entry")
    a("")
    for i, item in enumerate([
        "Regime is a confirmed uptrend (section 1).",
        "Stock is Stage 2, or 5/6 with an improving sector.",
        "Base is VCP grade A/B, close within ~3% of pivot, not extended.",
        "Breakout volume at least 1.5x the 50-day average.",
        "No results, AGM or block deal inside ~5 sessions.",
        "Liquidity at least ₹5 crore a day; not in ASM/GSM/T2T.",
        "Risk per trade 1–2% of capital, stop placed before entry.",
        "Never chase beyond the chase limit — let the trade come to you.",
    ], 1):
        a(f"{i}. {item}")
    a("")

    a("## 8. Headlines")
    a("")
    for h in d["headlines"]:
        a(f"- [{h['title']}]({h['url']})")
    a("")

    a("## 9. Glossary")
    a("")
    a("| Term | Plain meaning |")
    a("|---|---|")
    for term, meaning in [
        ("DMA", "Daily moving average — the average closing price over N days."),
        ("RSI", "Momentum gauge from 0 to 100. Above 70 stretched, below 30 washed out."),
        ("MACD", "Trend/momentum indicator; above its signal line is bullish."),
        ("ADX", "Trend strength. Above 25 means a real trend, not chop."),
        ("VIX", "Expected volatility. Low is calm, 25+ is fear."),
        ("VCP", "Volatility contraction — a base with tightening pullbacks."),
        ("Pivot", "The price a stock must clear to break out of its base."),
        ("R-multiple", "Profit measured in units of risk. 2R means twice what you risked."),
        ("GTT", "Good-till-triggered order that stays alive across sessions."),
        ("OCO", "One-cancels-other: a stop and a target on the same position."),
        ("FII / DII", "Foreign and domestic institutional investors."),
        ("ASM / GSM", "NSE surveillance lists with extra margin and trading curbs."),
    ]:
        a(f"| {term} | {meaning} |")
    a("")
    a("---")
    a("_Auto-generated from the trading-skills calculators. Educational only — not investment advice._")
    return "\n".join(L)


def verdict(reg: dict) -> str:
    label = (reg.get("label") or "").lower()
    if "confirmed" in label:
        return ("Green light. Take valid breakouts and pullbacks at normal size — but only in "
                "Stage-2 leaders with a defined pivot.")
    if "pressure" in label:
        return "Amber light. Halve position size, tighten stops, take profits sooner."
    if "correction" in label:
        return "Red light. Raise cash, no new leverage, only A-grade setups at half size."
    return "Defensive. Capital preservation first; wait for a follow-through day."


def vix_read(v) -> str:
    if v is None:
        return "—"
    if v < 12:
        return "very calm — complacency risk"
    if v < 18:
        return "normal"
    if v < 25:
        return "stressed"
    return "panic"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
def _table(headers, rows, cls=""):
    """Responsive table. Each td carries data-label so it stacks on mobile."""
    out = [f'<div class="tw"><table class="{cls}">', "<thead><tr>"]
    for h in headers:
        out.append(f"<th>{_h.escape(h)}</th>")
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for h, cell in zip(headers, row):
            out.append(f'<td data-label="{_h.escape(h)}">{cell}</td>')
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render_html(d: dict, title: str) -> str:
    idx, reg = d["index"], d["regime"]
    css = """
:root{
  --bg:#0b0f14; --panel:#111823; --panel2:#0e141d; --line:#1f2a37;
  --text:#e6edf5; --dim:#8ea0b5; --accent:#4da3ff;
  --green:#31d0aa; --red:#ff6b6b; --amber:#ffc857; --violet:#9b8cff;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{
  background:var(--bg);
  color:var(--text);
  font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
  padding:0 0 48px;
  font-variant-numeric:tabular-nums;
}
.wrap{max-width:1040px;margin:0 auto;padding:0 14px}
/* ---------- header ---------- */
.hero{
  background:linear-gradient(160deg,#152033 0%,#0b1219 70%);
  border-bottom:1px solid var(--line);
  padding:28px 0 22px;margin-bottom:22px;
}
.hero .wrap{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;justify-content:space-between}
.brand{font-size:.78rem;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);font-weight:600}
.hero h1{font-size:1.55rem;line-height:1.25;margin-top:6px;letter-spacing:-.01em}
.hero .date{color:var(--dim);font-size:.85rem;margin-top:4px}
.pill{
  display:inline-flex;align-items:center;gap:8px;
  border-radius:999px;padding:8px 16px;font-weight:700;font-size:.85rem;
  border:1px solid rgba(49,208,170,.35);background:rgba(49,208,170,.1);color:var(--green);
}
.pill.warn{border-color:rgba(255,200,87,.35);background:rgba(255,200,87,.1);color:var(--amber)}
.pill.bad{border-color:rgba(255,107,107,.35);background:rgba(255,107,107,.1);color:var(--red)}
/* ---------- sections ---------- */
section{margin:0 0 26px}
h2{
  font-size:1.06rem;letter-spacing:.02em;text-transform:uppercase;
  color:var(--dim);font-weight:700;margin:0 0 12px;
  display:flex;align-items:center;gap:10px;
}
h2 .num{
  display:inline-flex;align-items:center;justify-content:center;
  width:26px;height:26px;border-radius:8px;background:var(--panel);
  border:1px solid var(--line);color:var(--accent);font-size:.78rem;
}
h3{font-size:1.02rem;margin:16px 0 8px}
p{margin:8px 0;color:#c9d6e4}
.lead{color:var(--text);font-size:.98rem}
.small{font-size:.82rem;color:var(--dim)}
/* ---------- metric grid ---------- */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:10px}
.metric{
  background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:14px 15px;min-width:0;
}
.metric .k{font-size:.7rem;letter-spacing:.09em;text-transform:uppercase;color:var(--dim);font-weight:600}
.metric .v{font-size:1.3rem;font-weight:700;margin-top:5px;letter-spacing:-.01em;line-height:1.15;
           overflow-wrap:anywhere}
.metric .v.sm{font-size:1.02rem}
.metric .s{font-size:.76rem;color:var(--dim);margin-top:4px;line-height:1.4}
.pos{color:var(--green)}
.neg{color:var(--red)}
/* ---------- callout ---------- */
.callout{
  border-left:3px solid var(--accent);background:var(--panel);
  border-radius:0 10px 10px 0;padding:14px 16px;margin:14px 0;
}
.callout.good{border-left-color:var(--green)}
.callout.warn{border-left-color:var(--amber)}
.callout.bad{border-left-color:var(--red)}
.callout b{color:#fff}
/* ---------- tables ---------- */
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:12px;border:1px solid var(--line)}
table{border-collapse:collapse;width:100%;font-size:.87rem;background:var(--panel2)}
th{
  text-align:left;padding:11px 12px;font-size:.7rem;letter-spacing:.07em;
  text-transform:uppercase;color:var(--dim);font-weight:700;
  background:#0c141e;border-bottom:1px solid var(--line);white-space:nowrap;
}
td{padding:11px 12px;border-bottom:1px solid rgba(31,42,55,.65);vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:rgba(77,163,255,.045)}
td.num,th.num{text-align:right}
.sym{font-weight:700;color:#fff}
/* ---------- badge ---------- */
.badge{
  display:inline-block;padding:2px 9px;border-radius:999px;
  font-size:.72rem;font-weight:700;letter-spacing:.01em;white-space:nowrap;
  border:1px solid transparent;
}
.q-lead{background:rgba(49,208,170,.14);color:var(--green);border-color:rgba(49,208,170,.3)}
.q-impr{background:rgba(77,163,255,.14);color:var(--accent);border-color:rgba(77,163,255,.3)}
.q-weak{background:rgba(255,200,87,.14);color:var(--amber);border-color:rgba(255,200,87,.3)}
.q-lag{background:rgba(255,107,107,.13);color:var(--red);border-color:rgba(255,107,107,.28)}
.g-a{background:rgba(49,208,170,.14);color:var(--green);border-color:rgba(49,208,170,.3)}
.g-b{background:rgba(77,163,255,.14);color:var(--accent);border-color:rgba(77,163,255,.3)}
.g-c{background:rgba(255,200,87,.14);color:var(--amber);border-color:rgba(255,200,87,.3)}
.g-d{background:rgba(255,107,107,.13);color:var(--red);border-color:rgba(255,107,107,.28)}
/* ---------- trade cards ---------- */
.plan{
  background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:0;margin:14px 0;overflow:hidden;
}
.plan-head{
  display:flex;flex-wrap:wrap;align-items:center;gap:8px;
  padding:15px 16px;border-bottom:1px solid var(--line);
  background:linear-gradient(120deg,rgba(77,163,255,.07),transparent 70%);
}
.plan-head .name{font-size:1.1rem;font-weight:800;letter-spacing:-.01em}
.plan-head .meta{flex-basis:100%;font-size:.78rem;color:var(--dim)}
.plan-body{padding:6px 16px 16px}
.lvls{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
.lvl{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:11px 12px}
.lvl .k{font-size:.68rem;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);font-weight:600}
.lvl .v{font-size:1.08rem;font-weight:700;margin-top:4px}
.lvl .s{font-size:.72rem;color:var(--dim);margin-top:2px}
.lvl.entry{border-color:rgba(77,163,255,.35)}
.lvl.stop{border-color:rgba(255,107,107,.32)}
.lvl.t1,.lvl.t2{border-color:rgba(49,208,170,.3)}
/* ---------- 100% club ---------- */
.deep .lvls{grid-template-columns:repeat(3,1fr)}
.lvl.hundred{border-color:rgba(255,200,87,.55);
  background:linear-gradient(180deg,rgba(255,200,87,.10),rgba(255,200,87,.01))}
.lvl.hundred .v{color:var(--amber)}
.dhd{font-size:.7rem;letter-spacing:.09em;text-transform:uppercase;color:var(--dim);
  font-weight:700;margin:16px 0 6px}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.dbox{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.dbox .k{font-size:.68rem;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);font-weight:600}
.dbox .v{font-size:.95rem;font-weight:700;margin-top:3px}
.dbox .s{font-size:.74rem;color:var(--dim);margin-top:2px;line-height:1.45}
@media (max-width:720px){
  .deep .lvls{grid-template-columns:repeat(2,1fr)}
  .dgrid{grid-template-columns:1fr}
}
.notes{margin-top:6px}
.notes li{margin:5px 0 5px 18px;font-size:.86rem;color:#c2d0df}
.notes li.warn{color:var(--amber)}
/* ---------- lists ---------- */
ul.clean,ol.clean{padding-left:20px;margin:10px 0}
ul.clean li,ol.clean li{margin:7px 0;color:#c9d6e4}
.headlines li{margin:7px 0}
.headlines a{color:var(--accent);text-decoration:none}
.headlines a:hover{text-decoration:underline}
/* ---------- glossary ---------- */
details{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:12px 0}
details summary{cursor:pointer;font-weight:700;color:var(--accent)}
details[open] summary{margin-bottom:10px}
/* ---------- footer ---------- */
footer{
  margin-top:34px;padding:18px;border-top:1px solid var(--line);
  color:var(--dim);font-size:.78rem;line-height:1.6;
}
/* ---------- mobile ---------- */
@media (max-width:720px){
  body{font-size:15px}
  .hero{padding:20px 0 16px}
  .hero h1{font-size:1.22rem}
  .pill{font-size:.78rem;padding:6px 13px}
  .grid{grid-template-columns:repeat(2,1fr)}
  .metric{padding:11px}
  .metric .v{font-size:1.08rem}
  /* stack table rows into cards */
  .tw{border:none;overflow:visible}
  table,thead,tbody,tr,td{display:block;width:100%}
  thead{display:none}
  tbody tr{
    background:var(--panel);border:1px solid var(--line);border-radius:12px;
    margin-bottom:9px;padding:6px 4px;
  }
  tbody tr:hover{background:var(--panel)}
  td{
    display:flex;justify-content:space-between;gap:14px;
    border:none;padding:6px 12px;font-size:.86rem;text-align:right;
  }
  td::before{
    content:attr(data-label);color:var(--dim);font-size:.74rem;
    letter-spacing:.05em;text-transform:uppercase;font-weight:700;
    text-align:left;flex:0 0 auto;padding-top:2px;
  }
  td:first-child{padding-top:10px}
  td:last-child{padding-bottom:10px}
  .lvls{grid-template-columns:repeat(2,1fr)}
}
@media (max-width:380px){
  .grid{grid-template-columns:1fr}
}
"""
    label = (reg.get("label") or "").lower()
    pill_cls = "good" if "confirmed" in label else ("bad" if "correct" in label else "warn")
    pill_class = "" if pill_cls == "good" else ("bad" if pill_cls == "bad" else "warn")

    mode = d.get("mode", "intraday")
    hero_title, hero_brand = {
        "daily": ("Pre-market Brief", "India · NSE / BSE · before the open"),
        "eod": ("Closing Wrap", "India · NSE / BSE · session close"),
    }.get(mode, ("Market Bulletin", "India · NSE / BSE"))

    H = []
    a = H.append
    a("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>")
    a("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    a(f"<title>{_h.escape(title)}</title><style>{css}</style></head><body>")

    a("<div class='hero'><div class='wrap'>")
    a(f"<div><div class='brand'>{_h.escape(hero_brand)}</div>")
    a(f"<h1>{_h.escape(hero_title)}</h1>")
    a(f"<div class='date'>{_h.escape(d['generated_at'][:16].replace('T', ' '))} IST</div></div>")
    a(f"<div class='pill {pill_class}'>● {_h.escape(reg['label'])} · risk {reg['risk_score']}/100</div>")
    a("</div></div>")

    a("<div class='wrap'>")

    # --- At a glance ---
    a("<section><h2><span class='num'>1</span>At a glance</h2>")
    _cal = d.get("calendar") or {}
    if not _cal.get("trading_day", True):
        _nxt = _cal.get("next_trading_day")
        a("<div class='callout warn'><b>Exchange closed today.</b> NSE/BSE did not trade — "
          "every price and level below is from the last session."
          + (f" Next session: <b>{_h.escape(_nxt)}</b>." if _nxt else "")
          + " Treat the plans as a standing watchlist, not as entries for today.</div>")
    a("<div class='grid'>")
    rsi_v, rsi_s = _split_signal(idx["rsi"])
    adx_v, adx_s = _split_signal(idx["adx"])
    a(f"<div class='metric'><div class='k'>Nifty 50</div>"
      f"<div class='v'>{_n(idx['close'])}</div>"
      f"<div class='s'><span class='{_cls(idx['change_pct'])}'>{_pct(idx['change_pct'])}</span> today · "
      f"{_pct(idx['from_high_pct'])} from 52W high</div></div>")
    a(f"<div class='metric'><div class='k'>India VIX</div>"
      f"<div class='v'>{_n(d['vix'])}</div>"
      f"<div class='s'>{vix_read(d['vix'])}</div></div>")
    a(f"<div class='metric'><div class='k'>Regime</div>"
      f"<div class='v sm'>{_h.escape(reg['label'])}</div>"
      f"<div class='s'>exposure ceiling {reg['exposure_pct']}%</div></div>")
    a(f"<div class='metric'><div class='k'>RSI (14)</div>"
      f"<div class='v'>{_h.escape(rsi_v)}</div>"
      f"<div class='s'>{_h.escape(rsi_s)} · above 70 is stretched</div></div>")
    a(f"<div class='metric'><div class='k'>ADX (14)</div>"
      f"<div class='v'>{_h.escape(adx_v)}</div>"
      f"<div class='s'>{_h.escape(adx_s)} · above 25 = trending</div></div>")
    a(f"<div class='metric'><div class='k'>50 / 200 DMA</div>"
      f"<div class='v sm'>{_n(idx['sma50'])} / {_n(idx['sma200'])}</div>"
      f"<div class='s'>price {'above' if (idx['close'] or 0) > (idx['sma50'] or 0) else 'below'} the 50 DMA</div></div>")
    a("</div>")
    a(f"<div class='callout {pill_cls}' style='margin-top:14px'><b>Bottom line.</b> "
      f"{_h.escape(verdict(reg))}</div>")
    try:
        rsi_num = float(rsi_v)
    except (TypeError, ValueError):
        rsi_num = 0
    if rsi_num >= 70:
        a("<p class='small' style='margin-top:8px'>RSI is stretched (above 70) — prefer pullbacks "
          "into the 20/50 DMA over chasing breakouts at the high.</p>")
    if mode == "daily":
        sup = [x for x in (idx.get("supports") or []) if x]
        res = [x for x in (idx.get("resistances") or []) if x]
        a(f"<div class='callout'><b>Plan for the session.</b> Previous close "
          f"{_n(idx['close'])} ({_pct(idx['change_pct'])}). Support "
          f"{' · '.join(_n(x) for x in sup) if sup else 'n/a'} · resistance "
          f"{' · '.join(_n(x) for x in res) if res else 'n/a'}.</div>")
    elif mode == "eod":
        movers_eod = d.get("momentum", {}).get("movers") or []
        names = ", ".join(m["symbol"] for m in movers_eod[:3])
        a(f"<div class='callout'><b>Session wrap.</b> Nifty closed at {_n(idx['close'])} "
          f"({_pct(idx['change_pct'])}), {_pct(idx['from_high_pct'])} from its 52-week high."
          + (f" Names on the move: {_h.escape(names)}." if names else "") + "</div>")
    a("</section>")

    # --- Regime ---
    a("<section><h2><span class='num'>2</span>Market regime</h2>")
    a(f"<p class='lead'>{_h.escape(reg['stance'])}</p>")
    rows = []
    for k, v in (reg["risk_parts"] or {}).items():
        mx = (reg["risk_max"] or {}).get(k)
        rows.append([risk_label(k), f"{v}", "—" if mx is None else f"{mx}"])
    a(_table(["Risk driver", "Points", "Maximum"], rows))
    dc = reg["distribution_counts"]
    a(f"<p class='small' style='margin-top:10px'>Distribution days — "
      f"{dc.get('d5', 0)} in the last 5 sessions, {dc.get('d15', 0)} in 15, "
      f"{dc.get('d25', 0)} in 25. A distribution day is a lower close on higher volume; "
      f"they cluster before tops.</p>")
    a("<details><summary>What would change this regime label</summary><ul class='clean'>")
    for t in (reg.get("what_changes") or []):
        a(f"<li>{_h.escape(str(t))}</li>")
    a("<li>5 more distribution days inside 25 sessions would move the label to “uptrend under pressure”.</li>")
    a("<li>A close below the 50 DMA changes the pressure read.</li>")
    a("</ul></details>")
    a("</section>")

    # --- Sectors ---
    a("<section><h2><span class='num'>3</span>Sector rotation</h2>")
    buckets = {"Leading": [], "Improving": [], "Weakening": [], "Lagging": []}
    for s in d["sectors"]["rows"]:
        buckets.setdefault(s["quadrant"] or "Lagging", []).append(s["key"])
    order = [("Leading", "q-lead", "strongest — money is already there"),
             ("Improving", "q-impr", "turning up — best place to hunt for longs"),
             ("Weakening", "q-weak", "losing momentum — tighten stops"),
             ("Lagging", "q-lag", "weakest — avoid fresh entries")]
    for q, cls, note in order:
        names = buckets.get(q) or []
        if not names:
            continue
        a(f"<p style='margin:7px 0'><span class='badge {cls}'>{q}</span> "
          f"<b>{_h.escape(', '.join(names))}</b><br>"
          f"<span class='small'>{note}</span></p>")
    a("<hr style='border:none;border-top:1px solid var(--line);margin:14px 0'>")

    rows = []
    for s in d["sectors"]["rows"]:
        q = QUAD_CLASS.get(s["quadrant"], "")
        rows.append([
            f"<span class='sym'>{_h.escape(s['key'] or '')}</span>",
            f"<span class='badge {q}'>{_h.escape(s['quadrant'] or '')}</span>",
            f"<span class='{_cls(s['r1m'])}'>{_pct(s['r1m'], 1)}</span>",
            f"<span class='{_cls(s['rel1m'])}'>{_pct(s['rel1m'], 1)}</span>",
            _h.escape(s["trend"] or "—"),
        ])
    a(_table(["Sector", "Quadrant", "1M", "vs Nifty 1M", "Trend"], rows))
    # full detail, collapsed by default
    rows2 = []
    for s in d["sectors"]["rows"]:
        rows2.append([
            f"<span class='sym'>{_h.escape(s['key'] or '')}</span>",
            _pct(s["r1w"], 1), _pct(s["r1m"], 1), _pct(s["r3m"], 1),
            _n(s["rs_ratio"]), _n(s["rs_momentum"]),
        ])
    a("<details><summary>Full sector table (1W / 3M / RS ratio / RS momentum)</summary>")
    a(_table(["Sector", "1W", "1M", "3M", "RS ratio", "RS momentum"], rows2))
    a("</details>")
    a("<p class='small' style='margin-top:10px'><b>How to read it.</b> "
      "<b>Leading</b> is the strongest — money is already there. <b>Improving</b> is turning up and is "
      "the best place to hunt for new longs. <b>Weakening</b> is losing momentum — tighten stops. "
      "<b>Lagging</b> is the weakest — avoid fresh entries.</p>")
    if d["sectors"]["size"]:
        a("<p class='small'><b>Size factor.</b> " + " · ".join(
            f"{_h.escape(s['key'] or '')} {_h.escape(s['quadrant'] or '')} "
            f"({_pct(s['rel3m'], 1)} vs Nifty over 3M)"
            for s in d["sectors"]["size"]) + "</p>")
    a("</section>")

    # --- Momentum ---
    a("<section><h2><span class='num'>4</span>Momentum — what moved</h2>")
    a(f"<p class='small'>Scanned {d['momentum']['scanned']} NSE names for momentum bursts "
      f"(a 4%+ up-close out of a quiet base on rising volume) and episodic pivots "
      f"(a gap-up on 3x+ volume). RS is a 1–99 relative-strength grade against Nifty.</p>")
    rows = []
    for i, m in enumerate(d["momentum"]["movers"], 1):
        sig = "; ".join(m["signals"]) if m["signals"] else "—"
        rows.append([f"<span class='sym'>{_h.escape(m['symbol'] or '')}</span>",
                     _n(m["close"]), str(m["rs"]),
                     f"<span class='{_cls(m['from_high'])}'>{_pct(m['from_high'], 1)}</span>",
                     _h.escape(sig)])
    a(_table(["Stock", "Close", "RS", "From 52W high", "Trigger"], rows))
    a("<p class='small'>RS is the 1–99 relative-strength grade against Nifty. "
      "“EP” is an episodic pivot — a gap-up on 3x+ volume.</p>")
    a("</section>")

    # --- Breakouts ---
    a("<section><h2><span class='num'>5</span>Breakout setups</h2>")
    a("<p class='small'>Stage-2 trend template out of 6, base type and grade, the pivot, and how far "
      "price sits below it. Being close to the pivot is what makes a setup actionable.</p>")
    rows = []
    for i, s in enumerate(d["breakouts"]["setups"][:12], 1):
        g = (s["base_grade"] or "d").lower()
        rows.append([f"<span class='sym'>{_h.escape(s['symbol'] or '')}</span>",
                     f"{s['tt_passed']}/{s['tt_total']}",
                     f"<span class='badge g-{g}'>{_h.escape(s['base_type'] or '—')} {_h.escape(s['base_grade'] or '')}</span>",
                     _n(s["pivot"]),
                     f"<span class='{_cls(s['from_pivot_pct'])}'>{_pct(s['from_pivot_pct'])}</span>",
                     _n(s["liquidity_cr"], 1)])
    a(_table(["Stock", "Trend", "Base", "Pivot", "From pivot", "₹cr/day"], rows))
    a("<p class='small'>Trend is the Stage-2 checklist out of 6. <b>From pivot</b> is how far price "
      "sits below the breakout level — closer is more actionable.</p>")
    a("</section>")

    # --- 100% club ---
    deep = d.get("deep") or {}
    cands = deep.get("candidates") or []
    a("<section><h2><span class='num'>6</span>The 100% Club</h2>")
    a("<div class='callout'><b>No setup guarantees 100%.</b> Anyone who says otherwise is "
      "selling something. What this section does is show the three things that actually decide "
      "a doubler: the <b>structure</b>, the stock's own <b>history</b> of doubling from a "
      "similar setup, and the <b>arithmetic</b> of a defined-risk ladder. A 100% target is an "
      "objective you manage toward — never a forecast.</div>")

    if not cands:
        a(f"<p class='small'>{_h.escape(deep.get('note') or 'No candidate cleared the screen today.')}</p>")
    else:
        a(f"<p class='small'>Screened <b>{deep.get('screened', 0)}</b> names · "
          f"<b>{deep.get('pool', 0)}</b> passed the trend filter (Stage 2 or 5/6) · showing the best "
          f"<b>{len(cands)}</b>. Ranked by structure, relative strength, trend age, liquidity "
          "and the stock's own doubling rate.</p>")

        for c in cands:
            p = c.get("plan") or {}
            b = c.get("base") or {}
            g = (c.get("grade") or "C").lower()
            a("<div class='plan deep'>")
            a(f"<div class='plan-head'><span class='name'>{_h.escape(c['symbol'])}</span>"
              f"<span class='badge g-{g}'>Grade {_h.escape(c['grade'] or '')} · "
              f"{c['score']:.0f}/100</span>"
              f"<span class='badge'>Stage {c.get('tt_passed')}/{c.get('tt_total')}</span>"
              f"<span class='badge'>RS {c.get('rs_rating') or 'n/a'}</span>"
              f"<span class='meta'>{_h.escape(str(b.get('type') or 'no base'))} "
              f"{_h.escape(str(b.get('grade') or ''))} · {_n(b.get('weeks'), 1)} weeks · "
              f"{_h.escape(c.get('structure') or '')} entry · "
              f"₹{_n(c.get('liquidity_cr'), 0)} crore/day</span></div>")
            a("<div class='plan-body'><div class='lvls'>")
            a(f"<div class='lvl entry'><div class='k'>Buy above</div>"
              f"<div class='v'>{_n(p.get('entry'))}</div>"
              f"<div class='s'>limit {_n(p.get('limit'))}</div></div>")
            a(f"<div class='lvl stop'><div class='k'>Stop loss</div>"
              f"<div class='v neg'>{_n(p.get('stop'))}</div>"
              f"<div class='s'>{_pct(-(p.get('stop_pct') or 0))} · "
              f"{_h.escape(str(p.get('stop_method') or ''))}</div></div>")
            a(f"<div class='lvl t1'><div class='k'>Target 1 · 2R</div>"
              f"<div class='v pos'>{_n(p.get('t1'))}</div>"
              f"<div class='s'>{_pct(p.get('t1_pct'), 1)}</div></div>")
            a(f"<div class='lvl t2'><div class='k'>Target 2 · {_h.escape(str(p.get('t2_driver') or '3R'))}</div>"
              f"<div class='v pos'>{_n(p.get('t2'))}</div>"
              f"<div class='s'>{_pct(p.get('t2_pct'), 1)}</div></div>")
            a(f"<div class='lvl hundred'><div class='k'>+100% objective</div>"
              f"<div class='v'>{_n(p.get('hundred'))}</div>"
              f"<div class='s'>{_n(p.get('r_to_100'), 0)}R from entry</div></div>")
            a(f"<div class='lvl'><div class='k'>Size / risk</div>"
              f"<div class='v'>{p.get('qty')} sh</div>"
              f"<div class='s'>{_money(p.get('position_value'))} · risk "
              f"{_money(p.get('risk_if_stopped'))}</div></div>")
            a("</div>")

            a("<div class='dhd'>Why this one</div><ul class='notes'>")
            for w in _why_bullets(c):
                a(f"<li>{_b(w)}</li>")
            a("</ul>")

            a("<div class='dhd'>Evidence from its own history</div>")
            a(f"<p class='small'>{_h.escape(_study_sentence(c))}</p>")

            rows = _ladder_rows(c)
            if rows:
                a("<div class='dhd'>The ladder — a third at each target, trail the runner to +100%</div>")
                a(_table(["Leg", "Shares", "Exit", "Move", "Net ₹"], rows))
                lad = p.get("ladder")
                a(f"<p class='small'>If all three legs work: <b>{_money(lad['net_total'])}</b>, "
                  f"about <b>{_pct(lad['pct_of_capital'], 1, signed=False)} of capital</b>, "
                  f"{_n(lad['blended_r'], 1)}R blended.</p>")

            a("<div class='dhd'>What breaks it</div><ul class='notes'>")
            for w in _what_breaks_it(c):
                a(f"<li class='warn'>⚠ {_b(w)}</li>")
            a("</ul>")
            a("</div></div>")

        a(f"<p class='small'>{_h.escape(deep.get('disclaimer') or '')}</p>")
    a("</section>")

    # --- Plans ---
    a("<section><h2><span class='num'>7</span>Trade plans</h2>")
    ready = [p for p in d["plans"] if plan_verdict(p)[1] == "good"]
    if d["plans"] and not ready:
        a("<div class='callout warn'><b>No A-grade setup today.</b> "
          f"None of the {len(d['plans'])} candidates clears every Stage-2 check, so treat the cards "
          "below as a watchlist, not orders. Wait for the trend to repair or for a clean Stage-2 name "
          "to appear.</div>")
    elif ready:
        a(f"<div class='callout good'><b>{len(ready)} buy-ready setup"
          f"{'s' if len(ready) > 1 else ''}.</b> These pass the Stage-2 trend check and sit within "
          "reach of the pivot. Size them at 1–2% risk and place the stop first.</div>")
    a("<div class='callout'><b>Plain English.</b> A <b>Stage-2</b> stock is in a confirmed uptrend — "
      "above a rising 50/150/200-day average, the averages stacked in order, within 25% of its "
      "52-week high, well above its 52-week low and beating Nifty over 3–6 months. A <b>VCP</b> is a "
      "base where each pullback is smaller than the last on shrinking volume. The <b>pivot</b> is the "
      "price it must clear; the <b>chase limit</b> is the most you should pay; the <b>stop</b> is where "
      "the idea is wrong.</div>")

    if d["plans"]:
        for p in d["plans"]:
            stage = ("<span class='badge g-a'>Stage 2</span>" if p["stage2"]
                     else f"<span class='badge g-c'>Trend {p['tt_passed']}/{p['tt_total']}</span>")
            g = (p["base_grade"] or "d").lower()
            vlabel, vcls = plan_verdict(p)
            a("<div class='plan'>")
            a(f"<div class='plan-head'><span class='name'>{_h.escape(p['symbol'] or '')}</span>"
              f"{stage}<span class='badge g-{g}'>{_h.escape(p['base_type'] or '')} {_h.escape(p['base_grade'] or '')}</span>"
              f"<span class='meta'>{_h.escape(vlabel)} · close {_n(p['close'])} · "
              f"pivot {_n(p['pivot'])} ({_pct(p['from_pivot_pct'])})</span></div>")
            a("<div class='plan-body'><div class='lvls'>")
            a(f"<div class='lvl entry'><div class='k'>Buy above</div>"
              f"<div class='v'>{_n(p['trigger'])}</div>"
              f"<div class='s'>limit {_n(p['limit'])}</div></div>")
            a(f"<div class='lvl stop'><div class='k'>Stop loss</div>"
              f"<div class='v neg'>{_n(p['stop'])}</div>"
              f"<div class='s'>{_pct(-(p['stop_pct'] or 0))} · {_h.escape(p['stop_method'] or '')}</div></div>")
            if p["targets"]:
                for i, t in enumerate(p["targets"][:2], 1):
                    a(f"<div class='lvl t{i}'><div class='k'>Target {i} · {int(t['r'])}R</div>"
                      f"<div class='v pos'>{_n(t['price'])}</div>"
                      f"<div class='s'>{_pct(t['pct'], 1)} · net {_money(t['net'])}</div></div>")
            a("</div>")
            a(f"<p class='small'>Size {p['qty']} shares · {_money(p['position_value'])} "
              f"({_pct(p['allocation_pct'], 1, signed=False)} of capital) · risk if stopped "
              f"{_money(p['risk_if_stopped'])} of a {_money(p['risk_budget'])} budget · "
              f"liquidity ₹{_n(p['liquidity_cr'], 1)} crore/day</p>")
            if p["warnings"]:
                a("<ul class='notes'>")
                for w in p["warnings"]:
                    a(f"<li class='warn'>⚠ {_h.escape(w)}</li>")
                a("</ul>")
            a("</div></div>")
    else:
        a("<div class='callout warn'>No tradable setup in this scan. Forcing a trade in a thin tape "
          "is how accounts die — wait for a pivot with volume.</div>")

    if d["plans"]:
        o = d["plans"][0]["orders"]
        a("<details><summary>How to place these orders (GTT, OCO, T+1)</summary><ol class='clean'>")
        for k in ("gtt_entry", "sl_entry_day", "gtt_exit", "day0"):
            if o.get(k):
                a(f"<li>{_h.escape(o[k])}</li>")
        a("</ol></details>")

    a("<details open><summary>Checklist before any entry</summary><ol class='clean'>")
    for item in [
        "Regime is a confirmed uptrend.",
        "Stock is Stage 2, or 5/6 with an improving sector.",
        "Base is VCP grade A/B, close within ~3% of the pivot, not extended.",
        "Breakout volume at least 1.5x the 50-day average.",
        "No results, AGM or block deal inside ~5 sessions.",
        "Liquidity at least ₹5 crore a day; not in ASM/GSM/T2T.",
        "Risk 1–2% of capital per trade, stop placed before entry.",
        "Never chase beyond the chase limit.",
    ]:
        a(f"<li>{_h.escape(item)}</li>")
    a("</ol></details>")
    a("</section>")

    # --- Headlines ---
    a("<section><h2><span class='num'>8</span>Headlines</h2>")
    if d["headlines"]:
        a("<ul class='clean headlines'>")
        for h in d["headlines"]:
            a(f"<li><a href='{_h.escape(h['url'])}' rel='noopener'>{_h.escape(h['title'])}</a></li>")
        a("</ul>")
    else:
        a("<p class='small'>Headlines unavailable right now.</p>")
    a("</section>")

    # --- Glossary ---
    a("<section><h2><span class='num'>9</span>Glossary</h2>")
    rows = [[term, meaning] for term, meaning in [
        ("DMA", "Daily moving average — average closing price over N days."),
        ("RSI", "Momentum gauge 0–100. Above 70 stretched, below 30 washed out."),
        ("MACD", "Trend/momentum indicator; above its signal line is bullish."),
        ("ADX", "Trend strength. Above 25 means a real trend, not chop."),
        ("VIX", "Expected volatility. Low is calm, 25+ is fear."),
        ("VCP", "Volatility contraction — a base with tightening pullbacks."),
        ("Pivot", "The price that must be cleared to break out of a base."),
        ("R-multiple", "Profit in units of risk. 2R = twice what you risked."),
        ("GTT", "Good-till-triggered order that stays alive across sessions."),
        ("OCO", "One-cancels-other: stop and target on the same position."),
        ("FII / DII", "Foreign and domestic institutional investors."),
        ("ASM / GSM", "NSE surveillance lists with extra margin and curbs."),
        ("Base rate", "How often this actually happened before, counted from the stock's own history."),
        ("Ladder", "Selling in tranches (1/3 at T1, 1/3 at T2, trail the rest) instead of one exit."),
        ("+100% objective", "The price equal to twice your entry. An objective, never a forecast."),
    ]]
    a(_table(["Term", "Plain meaning"], rows))
    a("</section>")

    a("<footer>Educational analysis only — not SEBI-registered investment advice. "
      "Markets carry risk of loss; past performance does not guarantee future results. "
      "Verify every level with your broker before placing an order.</footer>")
    a("</div></body></html>")
    return "\n".join(H)
