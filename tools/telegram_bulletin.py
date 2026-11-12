#!/usr/bin/env python3
"""Send the latest bulletin to Telegram as clean, readable messages.

Messages are built from the structured JSON written by daily_bulletin.py — no
fixed-width CLI tables are sent. Telegram has no table support, so each item is
rendered as a compact multi-line card that stays readable on a phone.

Also attaches the styled HTML report for desktop viewing.

Usage:
    python3 tools/telegram_bulletin.py                       # latest bulletin
    python3 tools/telegram_bulletin.py --file reports/bulletin_2026-11-11_1910.md
    python3 tools/telegram_bulletin.py --no-docs             # messages only
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import notify

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "reports"
ENV_FILE = REPO_ROOT / ".env"
API_BASE = "https://api.telegram.org/bot{token}"
MAX_LEN = 3900

QUAD_EMOJI = {"Leading": "🟢", "Improving": "🔵", "Weakening": "🟡", "Lagging": "🔴"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def load_env() -> None:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def n(v, digits=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{digits}f}"
    except (TypeError, ValueError):
        return str(v)


def pct(v, digits=2, signed=True):
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%" if signed else f"{v:.{digits}f}%"


def money(v, digits=0):
    return "—" if v is None else "₹" + n(v, digits)


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def latest_bulletin() -> Path:
    files = (glob.glob(str(REPORTS_DIR / "bulletin_*.md"))
             + glob.glob(str(REPORTS_DIR / "daily_bulletin_*.md")))
    if not files:
        raise SystemExit("No bulletin_*.md found in reports/")
    return Path(max(files, key=lambda f: Path(f).stat().st_mtime))


# ---------------------------------------------------------------------------
# telegram transport
# ---------------------------------------------------------------------------
def send_message(token: str, chat_id: str, text: str) -> None:
    url = API_BASE.format(token=token) + "/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    last = None
    for attempt in range(4):
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 400 and "parse entities" in r.text:
                # Fall back to plain text rather than dropping the message.
                payload.pop("parse_mode", None)
                payload["text"] = text.replace("<b>", "").replace("</b>", "").replace(
                    "<i>", "").replace("</i>", "")
                r = requests.post(url, json=payload, timeout=30)
            r.raise_for_status()
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 + attempt * 3)
    raise SystemExit(f"Telegram sendMessage failed after retries: {last}")


def send_document(token: str, chat_id: str, path: Path, caption: str) -> None:
    url = API_BASE.format(token=token) + "/sendDocument"
    last = None
    for attempt in range(4):
        try:
            with path.open("rb") as f:
                r = requests.post(
                    url,
                    data={"chat_id": chat_id, "caption": caption[:1024]},
                    files={"document": (path.name, f, "text/html")},
                    timeout=60,
                )
            r.raise_for_status()
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 + attempt * 3)
    print(f"[warn] document upload failed: {last}", file=sys.stderr)


def send_all(token, chat_id, chunks):
    """Send a list of messages, stopping a message before Telegram's limit."""
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        send_message(token, chat_id, chunk[:MAX_LEN])


# ---------------------------------------------------------------------------
# message builders
# ---------------------------------------------------------------------------
def msg_overview(d: dict) -> str:
    idx, reg = d["index"], d["regime"]
    label = (reg.get("label") or "").lower()
    if "confirmed" in label:
        dot, line = "🟢", ("Green light. Take valid breakouts and pullbacks at normal size — "
                           "but only in Stage-2 leaders with a defined pivot.")
    elif "pressure" in label:
        dot, line = "🟡", "Amber. Halve position size, tighten stops, bank profits sooner."
    else:
        dot, line = "🔴", "Red. Raise cash, no fresh leverage, A-grade setups only."

    stamp = d["generated_at"][:16].replace("T", " ")
    mode = d.get("mode", "intraday")
    heading = {"daily": "Pre-market Brief", "eod": "Closing Wrap"}.get(
        mode, "Market Bulletin")
    vix = d.get("vix")
    vix_tag = ("calm" if vix and vix < 12 else "normal" if vix and vix < 18
               else "stressed" if vix and vix < 25 else "panic")
    rsi_note = ""
    try:
        if float(str(idx["rsi"]).split()[0]) >= 70:
            rsi_note = "\n<i>RSI is stretched — prefer pullbacks over chasing.</i>"
    except (ValueError, IndexError):
        pass
    cal = d.get("calendar") or {}
    closed_note = ""
    if not cal.get("trading_day", True):
        nxt = cal.get("next_trading_day")
        closed_note = ("\n⚠️ <i>Exchange closed today — prices are from the last session."
                       + (f" Next session: {esc(nxt)}." if nxt else "")
                       + "</i>")
    return (
        f"📊 <b>{heading} — India</b>\n"
        f"<i>{esc(stamp)} IST</i>{closed_note}\n"
        f"───────────────\n"
        f"<b>NIFTY 50</b>  {n(idx['close'])}  ({pct(idx['change_pct'])})\n"
        f"<b>India VIX</b>  {n(vix)}  · {vix_tag}\n"
        f"<b>Regime</b>  {esc(reg['label'])}  · risk {reg['risk_score']}/100\n"
        f"<b>Exposure</b>  up to {reg['exposure_pct']}% · max {reg['max_positions']} new positions\n"
        f"<b>RSI</b> {esc(str(idx['rsi']))}  ·  <b>ADX</b> {esc(str(idx['adx']))}\n"
        f"<b>52W range</b>  {n(idx['low_52w'])} – {n(idx['high_52w'])}\n"
        f"───────────────\n"
        f"{dot} {esc(line)}{rsi_note}"
    )


def msg_sectors(d: dict) -> str:
    rows = d.get("sectors", {}).get("rows") or []
    if not rows:
        return ""
    buckets = {"Leading": [], "Improving": [], "Weakening": [], "Lagging": []}
    for s in rows:
        buckets.setdefault(s.get("quadrant") or "Lagging", []).append(s.get("key"))
    out = ["🏭 <b>Sector rotation</b>", ""]
    for q in ("Leading", "Improving", "Weakening", "Lagging"):
        names = buckets.get(q) or []
        if names:
            out.append(f"{QUAD_EMOJI[q]} <b>{q}</b>: {esc(', '.join(names))}")
    rot = d["sectors"].get("rotation") or {}
    if rot.get("tilt"):
        out.append("")
        out.append(f"<i>Tilt: {esc(rot['tilt'])} — {esc(rot.get('reason', ''))}</i>")
    sr = d["sectors"].get("size_read") or {}
    if sr.get("read"):
        out.append(f"<i>Size: {esc(sr['read'])} — {esc(sr.get('reason', ''))}</i>")
    return "\n".join(out)


def msg_movers(d: dict) -> str:
    movers = d.get("momentum", {}).get("movers") or []
    if not movers:
        return ""
    out = ["⚡ <b>Momentum movers</b>",
           f"<i>scanned {d['momentum'].get('scanned', 0)} NSE names</i>", ""]
    for m in movers[:6]:
        sig = "; ".join(m["signals"]) if m.get("signals") else "—"
        out.append(
            f"<b>{esc(m['symbol'])}</b>  {n(m['close'])}  · RS {m['rs']}  · "
            f"{pct(m['from_high'], 1)} from 52W high\n"
            f"   <i>{esc(sig)}</i>"
        )
    return "\n".join(out)


def msg_plan(p: dict) -> str:
    fp = p.get("from_pivot_pct")
    grade = (p.get("base_grade") or "D").upper()
    if fp is not None and fp > 1.5:
        verdict = "🚫 MISSED — already above the chase limit"
    elif p["stage2"] and (fp is None or fp >= -4) and grade in ("A", "B"):
        verdict = "✅ BUY-READY — Stage 2, tight base, near pivot"
    elif p["stage2"] and (fp is None or fp >= -4):
        verdict = f"👀 WATCH — Stage 2 but the base is loose (grade {grade})"
    elif (p.get("tt_passed") or 0) >= 5:
        verdict = "👀 WATCH — 5/6 trend, wait for confirmation"
    else:
        verdict = "👀 WATCH ONLY — not Stage 2, paper-trade at best"

    _sec = (p.get("sector") or "").strip()
    lines = [
        f"🎯 <b>{esc(p['symbol'])}</b>   · {esc(p['base_type'] or '')} {esc(p['base_grade'] or '')} base"
        + (f"  · {esc(_sec)}" if _sec else ""),
        f"<i>{verdict}</i>",
        f"<i>close {n(p['close'])} · pivot {n(p['pivot'])} ({pct(p['from_pivot_pct'])})</i>",
        "",
        f"<b>Buy above</b>  {n(p['trigger'])}",
        f"<i>limit {n(p['limit'])} — never pay more</i>",
        f"<b>Stop</b>  {n(p['stop'])}  ({pct(-(p['stop_pct'] or 0))}, {esc(p['stop_method'] or '')})",
    ]
    _blockers = p.get("blockers") or []
    if _blockers:
        lines.append(f"🚫 <b>{esc('; '.join(_blockers))}</b> — watch only")
    elif p.get("tradability_note"):
        lines.append(f"<i>{esc(p['tradability_note'].capitalize())}</i>")
    if p.get("deliv_note"):
        lines.append(f"<i>Delivery: {esc(p['deliv_note'])}</i>")

    _rd = p.get("results_date")
    if _rd:
        try:
            from datetime import date as _d
            _rd = _d.fromisoformat(str(_rd)).strftime("%d %b")
        except Exception:
            pass
        _n = p.get("results_sessions")
        _lbl = "confirmed" if p.get("results_source") == "confirmed" else "estimated"
        _when = f"{_rd} ({_n} sessions, {_lbl})" if _n is not None else f"{_rd} ({_lbl})"
        if _n is not None and _n <= 5:
            lines.append(f"⚠️ <b>Results {esc(_when)}</b> — gap risk inside the window")
        else:
            lines.append(f"<i>Results {esc(_when)}</i>")

    for i, t in enumerate((p.get("targets") or [])[:2], 1):
        lines.append(f"<b>Target {i}</b>  {n(t['price'])}  "
                     f"({pct(t['pct'], 1)} · {int(t['r'])}R · net {money(t['net'])})")
    lines += [
        "",
        f"<i>{p['qty']} shares · {money(p['position_value'])} "
        f"({pct(p['allocation_pct'], 1, signed=False)} of capital) · "
        f"risk {money(p['risk_if_stopped'])} of {money(p['risk_budget'])}</i>",
    ]
    for w in (p.get("warnings") or [])[:2]:
        lines.append(f"⚠ {esc(w)}")
    return "\n".join(lines)


def msg_headlines(d: dict) -> str:
    hs = d.get("headlines") or []
    out = ["📰 <b>Headlines</b>", ""]
    for h in hs[:6]:
        out.append(f"• <a href=\"{esc(h['url'])}\">{esc(h['title'])}</a>")
    out += ["", "<i>Educational analysis only — not SEBI-registered advice.</i>"]
    return "\n".join(out)


def msg_deep(d: dict) -> str:
    """The 100% club as one compact message."""
    deep = d.get("deep") or {}
    cands = deep.get("candidates") or []
    if not cands:
        return ""
    out = ["🎯 <b>The 100% Club</b>",
           "<i>Structural doublers, not forecasts. Full analysis in the attached report.</i>",
           ""]
    for c in cands[:3]:
        p = c["plan"]
        s = c.get("study") or {}
        if s.get("samples") and s.get("hit100"):
            ev = (f"{s['hit100']} of {s['samples']} similar setups doubled"
                  + (f" in ~{round((s.get('median_bars_to_100') or 0) / 21)} months"
                     if s.get("median_bars_to_100") else ""))
        elif s.get("samples"):
            ev = f"0 of {s['samples']} similar setups doubled in 10 years"
        else:
            ev = "no comparable setup in its history"
        out.append(
            f"<b>{esc(c['symbol'])}</b>  · grade {esc(c['grade'])} {c['score']:.0f}/100  · "
            f"RS {c.get('rs_rating') or '—'}\n"
            f"   Entry <b>{n(p['entry'])}</b> · SL <b>{n(p['stop'])}</b> "
            f"({n(abs(p.get('stop_pct') or 0), 1)}%)\n"
            f"   T1 {n(p.get('t1'))} ({pct(p.get('t1_pct'), 0)}) · "
            f"T2 {n(p.get('t2'))} ({pct(p.get('t2_pct'), 0)})\n"
            f"   +100% = <b>{n(p.get('hundred'))}</b> · {n(p.get('r_to_100'), 0)}R · "
            f"{p.get('qty')} sh\n"
            f"   <i>{esc(ev)}</i>"
        )
    out.append("")
    out.append("<i>A 100% target is an objective, not a promise.</i>")
    return "\n".join(out)


def msg_calls(d: dict) -> str:
    """Accountability card: how our published calls actually did."""
    card = d.get("scorecard") or {}
    if not card.get("attempted"):
        pend = card.get("pending_count") or 0
        if not pend:
            return ""
        return ("📋 <b>How our calls did</b>\n"
                f"<i>{pend} plan(s) on the book, none graded yet.</i>\n\n"
                "Verdicts start after the next close — including triggers that never fill.")
    out = ["📋 <b>How our calls did</b>",
           f"<i>last {card['sessions_window']} sessions</i>",
           ""]
    out.append(f"Filled <b>{card['fills']}/{card['attempted']}</b> triggers "
               f"({_pct0(card.get('fill_rate_pct'))})")
    if card.get("decided"):
        out.append(f"Hit a target <b>{card['wins']}/{card['decided']}</b> "
                   f"({_pct0(card.get('hit_rate_pct'))})")
    if card.get("avg_r") is not None:
        line = f"Average <b>{card['avg_r']:+.1f}R</b> · total {card['total_r']:+.1f}R"
        if card.get("avg_net_r") is not None:
            line += f" · <i>after costs {card['avg_net_r']:+.1f}R</i>"
        out.append(line)

    buckets = (card.get("attribution") or {}).get("buckets") or []
    if buckets:
        best = max(buckets, key=lambda b: b.get("avg_net_r") or -99)
        worst = min(buckets, key=lambda b: b.get("avg_net_r") or 99)
        out.append(f"Best slice <b>{esc(str(best['value']))}</b> "
                   f"({best['avg_net_r']:+.1f}R, {best['n']}) · "
                   f"worst <b>{esc(str(worst['value']))}</b> "
                   f"({worst['avg_net_r']:+.1f}R, {worst['n']})")

    closed = card.get("closed_rows") or []
    if closed:
        out.append("")
        out.append("<b>Closed today</b>")
        for r in closed[:6]:
            rtxt = f"{r['r']:+.1f}R" if r.get("r") is not None else "—"
            out.append(f"  {esc(r['symbol'])} · {esc(_calls_status(r['status']))} · {rtxt}")
    open_rows = card.get("open_rows") or []
    if open_rows:
        out.append("")
        out.append("<b>Open</b>")
        for r in open_rows[:6]:
            rtxt = f"{r['r']:+.1f}R" if r.get("r") is not None else "—"
            out.append(f"  {esc(r['symbol'])} · entry {n(r.get('fill_price') or r.get('entry'))}"
                       f" · now {rtxt}")

    focus = d.get("focus") or []
    if focus:
        out.append("")
        out.append("<b>Next session</b>")
        for f in focus[:4]:
            out.append(f"• {esc(f)}")
    return "\n".join(x for x in out if x != "")


def _pct0(v) -> str:
    return "—" if v is None else f"{v:.0f}%"


def _calls_status(s: str) -> str:
    return {"t2_hit": "T2 hit", "t1_hit": "T1 hit, runner on", "stopped": "stopped",
            "no_fill": "never triggered", "expired": "expired",
            "filled": "open"}.get(s, s)


def build_messages(d: dict, docs: bool = True) -> list[str]:
    msgs = [msg_overview(d)]
    sectors = msg_sectors(d)
    movers = msg_movers(d)
    if sectors or movers:
        msgs.append("\n\n".join(x for x in (sectors, movers) if x))

    plans = d.get("plans") or []
    ready = [p for p in plans
             if p.get("stage2")
             and (p.get("base_grade") or "D").upper() in ("A", "B")
             and (p.get("from_pivot_pct") is None or p["from_pivot_pct"] <= 1.5)]
    if plans and not ready:
        msgs.append("⚠️ <b>No A-grade setup today.</b>\n"
                    "<i>None of the candidates below clears every Stage-2 check. "
                    "Treat these as watchlist cards, not orders.</i>")
    elif ready:
        msgs.append(f"✅ <b>{len(ready)} buy-ready setup{'s' if len(ready) > 1 else ''}.</b>\n"
                    "<i>Passes the Stage-2 trend check and sits near the pivot. "
                    "Risk 1–2% per trade, stop first.</i>")

    for p in plans[:3]:
        msgs.append(msg_plan(p))

    deep_msg = msg_deep(d)
    if deep_msg:
        msgs.append(deep_msg)

    # Accountability only on the morning plan and the closing wrap; an hourly
    # bulletin repeating the same scorecard is noise.
    if d.get("mode") in ("daily", "eod"):
        calls_msg = msg_calls(d)
        if calls_msg:
            msgs.append(calls_msg)

    if not plans:
        msgs.append("🚫 <b>No tradable setup right now.</b>\n"
                    "<i>Forcing a trade in a thin tape is how accounts die. "
                    "Wait for a pivot with volume.</i>")

    msgs.append(msg_headlines(d))
    return msgs


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=None)
    ap.add_argument("--no-docs", action="store_true")
    args = ap.parse_args()

    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("Missing TELEGRAM_BOT_TOKEN.", file=sys.stderr)
        return 2

    md_path = args.file or latest_bulletin()
    if not md_path.exists():
        print(f"File not found: {md_path}", file=sys.stderr)
        return 1

    json_path = md_path.with_suffix(".json")
    if json_path.exists():
        data = json.loads(json_path.read_text())
        chunks = build_messages(data)
    else:
        # Legacy fallback: send the markdown as one plain message.
        chunks = [esc(md_path.read_text())[:MAX_LEN]]

    html_path = None if args.no_docs else md_path.with_suffix(".html")
    if html_path is not None and not html_path.exists():
        html_path = None

    res = notify.broadcast(
        chunks,
        document=html_path,
        caption="Full report (styled HTML — best on desktop)",
    )
    if not res["sent"]:
        print(f"Delivery failed for every chat: {res}", file=sys.stderr)
        return 3
    print(f"Sent {md_path.name} to {res['sent']}/{res['audience']} chat(s)"
          + (f"; failed {res['failed']}" if res["failed"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
