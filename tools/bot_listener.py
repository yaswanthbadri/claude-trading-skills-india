#!/usr/bin/env python3
"""The desk in a chat window.

Without this, a second person can open the bot and get silence — messages only
ever went to the owner's chat id. Long polling means no public URL, no webhook,
no open port; it works from a laptop or a ₹300 VPS behind NAT.

Reading
    /check SYMBOL     one stock, one card: trend, base, pivot, plan, verdict
    /book             open positions with live R and the next target
    /heat             what the whole book is risking, and what still fits
    /status           how fresh the last report is
    /report           resend the latest full HTML report

Your own list
    /watch SYMBOL     tell me when it clears its pivot
    /unwatch SYMBOL   stop telling me
    /watchlist        everything you are stalking, and how far away it is
    /mode [x]         all | quiet | alerts | none — how much you want
    /stop             leave entirely (rejoin with /start)

Owner only
    /who              who is subscribed, and when they were last here
    /run daily|eod|alerts|backup
    /errors           last failures, without opening a terminal
    /invite           show the invite code and how to rotate it

If TELEGRAM_INVITE_CODE is set in .env, /start requires it. Leave it unset and
anyone who finds the bot can subscribe.

Usage:
    python3 tools/bot_listener.py            # run forever
    python3 tools/bot_listener.py --once     # drain pending updates and exit
"""
from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import buttons
import market_calendar as cal
import notify
import subscribers

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "reports"
LOGS = REPO / "logs"
OFFSET_FILE = REPO / "logs" / "bot_offset.json"
API = "https://api.telegram.org/bot{token}"
PYTHON = sys.executable

HELP = (
    "<b>Reading the tape</b>\n"
    "/check SYMBOL — trend, base, pivot, entry / stop / targets, verdict\n"
    "/book — open positions and what they are worth in R\n"
    "/heat — total risk on the book, room for more\n"
    "/status — how fresh the last report is\n"
    "/report — resend the latest full report\n\n"
    "<b>Your own list</b>\n"
    "/watch SYMBOL — ping me when it clears its pivot\n"
    "/unwatch SYMBOL — never mind\n"
    "/watchlist — what you are stalking\n"
    "/mode — how much you want: all | quiet | alerts | none\n"
    "/stop — leave  ·  /start — come back\n\n"
    "<b>Owner</b>\n"
    "/who · /run daily|eod|alerts|backup · /errors · /invite\n\n"
    "<i>Educational analysis only — not SEBI-registered advice.</i>"
)

MODE_HELP = {
    "all": "everything — the plan, the hourly cards, the review, alerts, levels",
    "quiet": "the 09:00 plan and the 16:00 review only",
    "alerts": "alerts and level breaches only, no scheduled reports",
    "none": "nothing until you change it back",
}


# ---------------------------------------------------------------------------
# plumbing
# ---------------------------------------------------------------------------
def _esc(s) -> str:
    return html.escape(str(s or ""))


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


def _offset() -> int:
    try:
        return int(json.loads(OFFSET_FILE.read_text()).get("offset", 0))
    except Exception:
        return 0


def _save_offset(v: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(json.dumps({"offset": v}))


def _get_updates(tok: str, offset: int, timeout: int = 50) -> list[dict]:
    try:
        r = requests.get(API.format(token=tok) + "/getUpdates",
                         params={"offset": offset, "timeout": timeout,
                                 "allowed_updates": '["message","callback_query"]'},
                         timeout=timeout + 15)
        r.raise_for_status()
        return (r.json() or {}).get("result") or []
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] getUpdates: {exc}", file=sys.stderr)
        time.sleep(5)
        return []


def _reply(tok: str, chat_id, text: str, markup: dict | None = None) -> None:
    notify.send(text, chat_id=chat_id, kind="system", reply_markup=markup)


def _answer_cb(tok: str, cb_id: str, text: str = "", alert: bool = False) -> None:
    """A pressed button must be acknowledged or Telegram spins forever."""
    try:
        requests.post(API.format(token=tok) + "/answerCallbackQuery",
                      json={"callback_query_id": cb_id, "text": text,
                            "show_alert": alert}, timeout=15)
    except Exception:
        pass


def _latest_report() -> Path | None:
    files = sorted(REPORTS.glob("bulletin_*.html"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _live(symbols: list[str]) -> dict[str, float]:
    """Last traded price per symbol, live if possible, cache if not."""
    out: dict[str, float] = {}
    if not symbols:
        return out
    try:
        import watch_scan
        out = watch_scan.live_prices(symbols)
    except Exception:
        out = {}
    for sym in symbols:
        if sym in out:
            continue
        try:
            import recs
            bars = recs._bars(sym)
            if bars:
                out[sym] = float(bars[-1]["close"])
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# the reading cards
# ---------------------------------------------------------------------------
def status_text(chat_id=None) -> str:
    rep = _latest_report()
    if rep:
        age_min = (time.time() - rep.stat().st_mtime) / 60
        if age_min < 90:
            fresh = f"{age_min:.0f} min old"
        elif age_min < 60 * 36:
            fresh = f"{age_min / 60:.1f} hours old"
        else:
            fresh = f"{age_min / 1440:.1f} days old"
        when = datetime.fromtimestamp(rep.stat().st_mtime).strftime("%d %b %H:%M")
        report = f"latest <code>{rep.stem}</code> · {when} · {fresh}"
    else:
        report = "no report generated yet"

    open_now = cal.is_market_open()
    day = "open" if open_now else ("trading day" if cal.is_trading_day() else "market holiday")
    mine = ""
    if chat_id is not None:
        m = subscribers.mode_of(chat_id)
        mine = f"\nyou get: <b>{m}</b> — {MODE_HELP.get(m, '')}"
        if subscribers.snoozed(chat_id):
            mine += "\n<i>alerts are snoozed right now</i>"
    return (
        f"<b>Status</b>\n"
        f"{report}\n"
        f"market: {day}\n"
        f"delivering to <b>{subscribers.active_count()}</b> chat(s)"
        f"{mine}\n"
        f"server time: {datetime.now():%d %b %H:%M}"
    )


def book_text() -> str:
    """Open positions, live, in R. R is the unit that makes sizes comparable."""
    try:
        import recs
        rows = [r for r in recs.load()["records"]
                if r.get("status") in ("filled", "t1_hit")]
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Could not read the book: {_esc(exc)}"
    if not rows:
        return ("💼 <b>Book is empty.</b>\n"
                "<i>No filled positions. Pending triggers are in /heat.</i>")

    prices = _live([r["symbol"] for r in rows])
    L = ["💼 <b>Open positions</b>", ""]
    total = 0.0
    for r in sorted(rows, key=lambda x: x["symbol"]):
        entry = r.get("fill_price") or r.get("entry")
        stop, t1, t2 = r.get("stop"), r.get("t1"), r.get("t2")
        last = prices.get(r["symbol"])
        rad = recs._r_for(r, last) if last else None
        if rad is not None:
            total += rad
        L.append(
            f"<b>{_esc(r['symbol'])}</b> · {_esc((r.get('status') or '').replace('_', ' '))}"
            f"\n  now {_n(last)} · entry {_n(entry)} · stop {_n(stop)}"
            f"\n  <b>{('%+.2fR' % rad) if rad is not None else '—'}</b>"
            + (f" · next T1 {_n(t1)}" if r.get("status") != "t1_hit" else
               f" · T1 banked, next T2 {_n(t2)}")
        )
    L += ["", f"<i>Sum of open R: {total:+.2f} · "
              f"{len(rows)} position(s)</i>",
          "<i>R = 1× what you agreed to lose at the stop.</i>"]
    return "\n".join(L)


def heat_text() -> str:
    try:
        import heat
        h = heat.compute()
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Could not compute heat: {_esc(exc)}"

    icon = {"ok": "🟢", "warm": "🟠", "hot": "🔴"}.get(h["status"], "⚪")
    L = [f"{icon} <b>Portfolio heat</b>", ""]
    L.append(f"At risk <b>₹{h['at_risk']:,.0f}</b> ({h['at_risk_pct']:.1f}% of "
             f"₹{h['capital']:,.0f})")
    if h["pending_count"]:
        L.append(f"If every trigger fills <b>₹{h['committed']:,.0f}</b> more "
                 f"({h['committed_pct']:.1f}% total)")
    L.append(f"Cap {h['cap_pct']:g}% · room for <b>{h['slots']}</b> more")
    if h["clusters"]:
        L.append("")
        for s in h["clusters"][:3]:
            L.append(f"⚠️ {_esc(s['sector'])}: {s['count']} names "
                     f"({_esc(', '.join(s['symbols']))})")
    L += ["", "<i>Heat = what a gap to every stop would cost, added up.</i>"]
    return "\n".join(L)


def watch_text(chat_id) -> str:
    w = subscribers.watchlist(chat_id)
    if not w:
        return ("👀 <b>Nothing on your list.</b>\n"
                "<i>Add one with <code>/watch CENTUM</code> — you get one ping "
                "when it clears its pivot, nobody else does.</i>")
    prices = _live(sorted(w))
    L = ["👀 <b>Your watchlist</b>", ""]
    for sym, e in sorted(w.items()):
        level = e.get("pivot")
        last = prices.get(sym)
        gap = (f" · {_pct(100 * (last / level - 1))} from it"
               if last and level else "")
        fired = " ✅ pinged today" if str(e.get("notified") or "").startswith(
            datetime.now().strftime("%Y-%m-%d")) else ""
        kind = {"high": "52w high", "above": "+2% level"}.get(e.get("kind") or "", "pivot")
        L.append(f"<b>{_esc(sym)}</b> — {kind} {_n(level)}{gap}{fired}")
    L += ["", "<i>/unwatch SYMBOL to drop one. The level is fixed when you add it.</i>"]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# /check
# ---------------------------------------------------------------------------
def check_cards(symbol: str) -> list[str]:
    """The chat answer: the call first, then the numbers behind it."""
    try:
        import stock_check
        return stock_check.cards(stock_check.check(symbol))
    except Exception as exc:  # noqa: BLE001
        return [f"⚠️ Check failed for {_esc(symbol)}: {_esc(exc)}"]


def check_text(symbol: str) -> str:
    return "\n\n".join(check_cards(symbol))


def _send_check(tok: str, chat_id, symbol: str) -> None:
    """Two cards and the full report as a document.

    The detail is the point — a verdict without the numbers behind it is a
    rumour — but the cards have to stay readable on a phone, so the long
    version goes out as a styled HTML file.
    """
    try:
        import stock_check
        result = stock_check.check(symbol)
        for c in stock_check.cards(result):
            _reply(tok, chat_id, c)
        path = stock_check.report_file(symbol, result)
        if path:
            notify.send_document(tok, chat_id, path,
                                 f"Full report — {symbol.upper()}")
    except Exception as exc:  # noqa: BLE001
        _reply(tok, chat_id, f"⚠️ Check failed for {_esc(symbol)}: {_esc(exc)}")


# ---------------------------------------------------------------------------
# owner helpers
# ---------------------------------------------------------------------------
def who_text() -> str:
    store = subscribers.load()
    users = store.get("users") or {}
    if not users:
        return "No subscribers yet."
    L = ["<b>Subscribers</b>", ""]
    for cid, u in sorted(users.items(), key=lambda kv: kv[1].get("joined") or ""):
        alive = "✅" if u.get("active", True) else "⏸"
        name = u.get("name") or "(no name)"
        user = f" @{u['username']}" if u.get("username") else ""
        seen = (u.get("last_seen") or "")[:16].replace("T", " ")
        L.append(f"{alive} <b>{_esc(name)}</b>{_esc(user)} · "
                 f"{u.get('role') or 'member'} · {u.get('mode') or 'all'}")
        L.append(f"   <code>{cid}</code> · watch {len(u.get('watch') or {})} · "
                 f"seen {seen or '—'}")
    L += ["", f"<i>{len(users)} total · {subscribers.active_count()} receiving</i>"]
    return "\n".join(L)


RUNNABLE = {
    "daily": ("daily_bulletin.py", ["--mode", "daily"]),
    "intraday": ("daily_bulletin.py", ["--mode", "intraday"]),
    "eod": ("daily_bulletin.py", ["--mode", "eod"]),
    "alerts": ("alert_monitor.py", []),
    "backup": ("backup.py", []),
}


def run_text(what: str) -> str:
    what = (what or "").strip().lower()
    if what not in RUNNABLE:
        return ("Usage: <code>/run daily|intraday|eod|alerts|backup</code>\n"
                "<i>It runs detached — the report arrives on its own, so the "
                "bot keeps answering.</i>")
    script, argv = RUNNABLE[what]
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"manual_{what}.log"
    try:
        with log.open("a") as fh:
            fh.write(f"\n--- manual {what} at {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
            fh.flush()
            subprocess.Popen([PYTHON, str(REPO / "tools" / script)] + argv,
                             cwd=str(REPO), stdout=fh, stderr=subprocess.STDOUT,
                             start_new_session=True)
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Could not start {what}: {_esc(exc)}"
    return (f"▶️ <b>{what}</b> started in the background.\n"
            f"<i>Output: <code>logs/manual_{what}.log</code></i>")


def errors_text(limit: int = 8) -> str:
    """The last things that actually went wrong, newest file first."""
    if not LOGS.exists():
        return "No logs directory."
    hits: list[str] = []
    for f in sorted(LOGS.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:4]:
        try:
            lines = f.read_text(errors="ignore").splitlines()
        except Exception:
            continue
        bad = [ln for ln in lines
               if any(k in ln.lower() for k in
                      ("error", "traceback", "failed", "warn", "exception"))]
        for ln in bad[-3:]:
            hits.append(f"<b>{_esc(f.stem)}</b>: <code>{_esc(ln[:300])}</code>")
        if len(hits) >= limit:
            break
    if not hits:
        return "✅ <b>No errors in the recent logs.</b>"
    return ("<b>Recent problems</b>\n\n"
            + "\n".join(hits[-limit:])
            + "\n\n<i>Newest files first, last few lines each.</i>")


def invite_text() -> str:
    code = os.environ.get("TELEGRAM_INVITE_CODE", "").strip()
    if not code:
        return ("⚠️ No invite code set — anyone who finds this bot can join.\n"
                "Set <code>TELEGRAM_INVITE_CODE=...</code> in <code>.env</code> "
                "and restart the bot.")
    return (f"🔑 Invite code: <code>{_esc(code)}</code>\n\n"
            "Share it with someone you want on the desk. They send:\n"
            f"<code>/start {_esc(code)}</code>\n\n"
            "<i>To rotate: change TELEGRAM_INVITE_CODE in .env and restart the "
            "listener — existing subscribers are unaffected.</i>")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
def handle_command(tok: str, chat_id, cmd: str, arg: str, name: str,
                   username: str) -> None:
    code = os.environ.get("TELEGRAM_INVITE_CODE", "").strip()

    if cmd == "start":
        if code and arg.lower() != code.lower():
            _reply(tok, chat_id,
                   "🔒 <b>Invite only.</b>\n"
                   "<i>Send <code>/start YOUR_CODE</code> — ask the owner for the code.</i>")
            return
        subscribers.add(chat_id, name=name, username=username)
        _reply(tok, chat_id,
               "✅ <b>You're on the list.</b>\n\n"
               "You'll get:\n"
               "• <b>09:00</b> — the day's plan: regime, setups, entry / stop / targets\n"
               "• hourly during the session — what changed\n"
               "• <b>16:00</b> — how today's calls actually did\n"
               "• instantly — big news on a named stock\n\n"
               "<i>Type /mode to turn the volume down without leaving.</i>\n\n"
               f"{HELP}")
        return

    if cmd == "stop":
        subscribers.deactivate(chat_id)
        _reply(tok, chat_id, "🔕 <b>Paused.</b> Send /start whenever you want back in.")
        return

    if cmd in ("help", "commands"):
        _reply(tok, chat_id, HELP)
        return

    if cmd == "status":
        subscribers.touch(chat_id)
        _reply(tok, chat_id, status_text(chat_id))
        return

    if cmd in ("report", "latest"):
        subscribers.touch(chat_id)
        rep = _latest_report()
        if not rep:
            _reply(tok, chat_id, "No report yet — the next scheduled run will create one.")
            return
        notify.send_document(tok, chat_id, rep, "Latest full report (styled HTML)")
        return

    # ---- anything below needs a subscription --------------------------------
    if not subscribers.user(chat_id):
        if cmd in ("check", "book", "heat", "watch", "unwatch", "watchlist", "mode"):
            _reply(tok, chat_id, "Send /start first — then all of this works.")
            return

    if cmd == "check":
        if not arg:
            _reply(tok, chat_id, "Which stock? <code>/check CENTUM</code>")
            return
        subscribers.touch(chat_id)
        _reply(tok, chat_id, "🔎 checking…")
        _send_check(tok, chat_id, arg)
        return

    if cmd in ("book", "positions"):
        subscribers.touch(chat_id)
        _reply(tok, chat_id, book_text(),
               markup=buttons.kb([[("🔥 Heat", "heat"),
                                    ("📈 Status", "status")]]))
        return

    if cmd == "heat":
        subscribers.touch(chat_id)
        _reply(tok, chat_id, heat_text(),
               markup=buttons.kb([[("💼 Book", "book")]]))
        return

    if cmd == "watch":
        if not arg:
            _reply(tok, chat_id, "Which stock? <code>/watch CENTUM</code>")
            return
        _reply(tok, chat_id, _watch_add(chat_id, arg))
        return

    if cmd == "unwatch":
        if not arg:
            _reply(tok, chat_id, "Which stock? <code>/unwatch CENTUM</code>")
            return
        ok = subscribers.unwatch(chat_id, arg)
        _reply(tok, chat_id,
               f"✅ {_esc(arg.upper())} removed from your list." if ok
               else f"{_esc(arg.upper())} was not on your list.")
        return

    if cmd in ("watchlist", "list", "watching"):
        subscribers.touch(chat_id)
        _reply(tok, chat_id, watch_text(chat_id))
        return

    if cmd == "mode":
        if not arg:
            m = subscribers.mode_of(chat_id)
            lines = [f"Your mode: <b>{m}</b> — {MODE_HELP.get(m, '')}", ""]
            lines += [f"/mode {k} — {v}" for k, v in MODE_HELP.items()]
            _reply(tok, chat_id, "\n".join(lines))
            return
        try:
            m = subscribers.set_mode(chat_id, arg)
        except ValueError:
            _reply(tok, chat_id, "Pick one of: " + ", ".join(subscribers.MODES))
            return
        if m == "unknown":
            _reply(tok, chat_id, "Send /start first — then /mode works.")
            return
        _reply(tok, chat_id, f"✅ Mode set to <b>{m}</b> — {MODE_HELP.get(m, '')}")
        return

    # ---- owner only --------------------------------------------------------
    if cmd in ("who", "run", "errors", "invite", "backup"):
        if not subscribers.is_owner(chat_id):
            _reply(tok, chat_id, "That one is the owner's.")
            return
        if cmd == "who":
            _reply(tok, chat_id, who_text())
        elif cmd == "run":
            _reply(tok, chat_id, run_text(arg))
        elif cmd == "errors":
            _reply(tok, chat_id, errors_text())
        elif cmd == "invite":
            _reply(tok, chat_id, invite_text())
        elif cmd == "backup":
            _reply(tok, chat_id, run_text("backup"))
        return

    _reply(tok, chat_id, "Unknown command.\n\n" + HELP)


def _watch_add(chat_id, symbol: str) -> str:
    """Pin the level now, so later the ping means what it said it meant."""
    sym = (symbol or "").strip().upper().replace(".NS", "")
    if not sym:
        return "Which stock?"
    try:
        import stock_check
        r = stock_check.check(sym)
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ Could not read {_esc(sym)}: {_esc(exc)}"
    if r.get("error"):
        return f"⚠️ {_esc(r['error'])}"

    last = r.get("close")
    pivot = (r.get("base") or {}).get("pivot")
    high = r.get("high_52w")
    if pivot:
        level, kind, why = float(pivot), "pivot", "the top of its base"
    elif high:
        level, kind, why = float(high), "high", "its 52-week high"
    else:
        return f"Could not work out a level for {_esc(sym)} — no base and no 52-week high."

    # Already through the level: watching a trigger that fired is pointless,
    # so watch the next 2% instead and say so.
    if last and last >= level:
        level = round(float(last) * 1.02, 2)
        kind = "high"
        why = "already above the obvious level, so the next 2% up"

    subscribers.watch(chat_id, sym, level, kind=kind)
    gain = (_pct(100 * (last / level - 1)) if last else "—")
    return (f"👀 <b>Watching {_esc(sym)}</b>\n"
            f"Ping me when it clears <b>{_n(level)}</b> — {why}.\n"
            f"Now {_n(last)} ({gain} from that level).\n\n"
            f"<i>One message, once a day, to you only. "
            f"/unwatch {_esc(sym)} to stop.</i>")


def handle_callback(tok: str, update: dict) -> None:
    cb = update.get("callback_query") or {}
    cb_id = cb.get("id")
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if not cb_id or not chat_id:
        return
    verb, arg = buttons.parse(data)

    if verb == "book":
        _answer_cb(tok, cb_id, "Book")
        _reply(tok, chat_id, book_text())
        return
    if verb == "heat":
        _answer_cb(tok, cb_id, "Heat")
        _reply(tok, chat_id, heat_text())
        return
    if verb == "status":
        _answer_cb(tok, cb_id, "Status")
        _reply(tok, chat_id, status_text(chat_id))
        return
    if verb == "check":
        _answer_cb(tok, cb_id, f"Checking {arg}…")
        _send_check(tok, chat_id, arg)
        return
    if verb == "unwatch":
        ok = subscribers.unwatch(chat_id, arg)
        _answer_cb(tok, cb_id, "Removed" if ok else "Not on your list")
        return
    if verb == "snooze":
        try:
            minutes = max(5, int(arg or 120))
        except ValueError:
            minutes = 120
        until = subscribers.snooze(chat_id, minutes)
        if until is None:
            _answer_cb(tok, cb_id, "Send /start first")
            return
        _answer_cb(tok, cb_id, f"Alerts off for {minutes // 60 or 1}h")
        _reply(tok, chat_id,
               f"🔇 <b>Alerts muted for {minutes // 60 or 1}h.</b>\n"
               "<i>Reports keep coming. Send /mode to change that too.</i>")
        return
    _answer_cb(tok, cb_id, "Unknown button")


def handle(tok: str, update: dict) -> None:
    if update.get("callback_query"):
        handle_callback(tok, update)
        return

    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text.startswith("/"):
        return

    who = msg.get("from") or {}
    name = " ".join(x for x in [who.get("first_name"), who.get("last_name")] if x)
    username = who.get("username") or ""
    parts = text.split()
    cmd = parts[0].lower().lstrip("/").split("@")[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    handle_command(tok, chat_id, cmd, arg, name, username)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="drain pending updates and exit")
    args = ap.parse_args()

    notify.load_env()
    tok = notify.token()

    # getUpdates fails with 409 if a webhook was ever set on this bot.
    try:
        requests.post(API.format(token=tok) + "/deleteWebhook",
                      json={"drop_pending_updates": False}, timeout=15)
    except Exception:
        pass

    # Make the command list visible in the Telegram UI.
    try:
        requests.post(API.format(token=tok) + "/setMyCommands", json={"commands": [
            {"command": "check", "description": "One stock: trend, base, plan, verdict"},
            {"command": "book", "description": "Open positions in R"},
            {"command": "heat", "description": "Total risk on the book"},
            {"command": "watch", "description": "Ping me when a stock clears its pivot"},
            {"command": "watchlist", "description": "What you are stalking"},
            {"command": "mode", "description": "How much you want: all | quiet | alerts"},
            {"command": "status", "description": "Freshness and audience"},
            {"command": "report", "description": "Resend the latest full report"},
            {"command": "start", "description": "Join the list"},
            {"command": "stop", "description": "Pause delivery"},
            {"command": "help", "description": "How this bot works"},
        ]}, timeout=15)
    except Exception:
        pass

    offset = _offset()
    print(f"bot listener up · {subscribers.active_count()} subscriber(s) · "
          f"invite {'on' if os.environ.get('TELEGRAM_INVITE_CODE') else 'off'}", flush=True)

    while True:
        updates = _get_updates(tok, offset, timeout=8 if args.once else 50)
        for u in updates:
            offset = max(offset, int(u.get("update_id", 0)) + 1)
            try:
                handle(tok, u)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] handler: {exc}", file=sys.stderr)
        if updates:
            _save_offset(offset)
        if args.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
