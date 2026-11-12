#!/usr/bin/env python3
"""The bot's front desk: /start, /stop, /status, /report.

Without this, a second person can open the bot and get silence — messages only
ever went to the owner's chat id. Long polling means no public URL, no webhook,
no open port; it works from a laptop or a ₹300 VPS behind NAT.

Commands
    /start [invite]   join the broadcast list
    /stop             leave it (you can rejoin any time)
    /status           how fresh the last report is, how many people get it
    /report           resend the most recent full HTML report
    /help             this list

If TELEGRAM_INVITE_CODE is set in .env, /start requires it. Leave it unset and
anyone who finds the bot can subscribe.

Usage:
    python3 tools/bot_listener.py            # run forever
    python3 tools/bot_listener.py --once     # drain pending updates and exit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_calendar as cal
import notify
import subscribers

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "reports"
OFFSET_FILE = REPO / "logs" / "bot_offset.json"
API = "https://api.telegram.org/bot{token}"

HELP = (
    "<b>Commands</b>\n"
    "/start — receive every report and alert\n"
    "/stop — pause delivery\n"
    "/status — freshness and audience\n"
    "/report — resend the latest full report\n"
    "/help — this message\n\n"
    "<i>Educational analysis only — not SEBI-registered advice.</i>"
)


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
                                 "allowed_updates": '["message"]'},
                         timeout=timeout + 15)
        r.raise_for_status()
        return (r.json() or {}).get("result") or []
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] getUpdates: {exc}", file=sys.stderr)
        time.sleep(5)
        return []


def _reply(tok: str, chat_id, text: str) -> None:
    notify.send(text, chat_id=chat_id)


def _latest_report() -> Path | None:
    files = sorted(REPORTS.glob("bulletin_*.html"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _status_text() -> str:
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
    return (
        f"<b>Status</b>\n"
        f"{report}\n"
        f"market: {day}\n"
        f"delivering to <b>{subscribers.active_count()}</b> chat(s)\n"
        f"server time: {datetime.now():%d %b %H:%M}"
    )


def handle(tok: str, update: dict) -> None:
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text.startswith("/"):
        return

    who = msg.get("from") or {}
    name = " ".join(x for x in [who.get("first_name"), who.get("last_name")] if x)
    username = who.get("username") or ""
    cmd = text.split()[0].lower().lstrip("/").split("@")[0]
    arg = text.split()[1].strip() if len(text.split()) > 1 else ""

    if cmd == "start":
        code = os.environ.get("TELEGRAM_INVITE_CODE", "").strip()
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
               "• instantly — big news and index moves\n\n"
               f"{HELP}")
        return

    if cmd == "stop":
        subscribers.deactivate(chat_id)
        _reply(tok, chat_id, "🔕 <b>Paused.</b> Send /start whenever you want back in.")
        return

    if cmd == "status":
        subscribers.touch(chat_id)
        _reply(tok, chat_id, _status_text())
        return

    if cmd in ("report", "latest"):
        subscribers.touch(chat_id)
        rep = _latest_report()
        if not rep:
            _reply(tok, chat_id, "No report yet — the next scheduled run will create one.")
            return
        notify.send_document(tok, chat_id, rep,
                             "Latest full report (styled HTML)")
        return

    if cmd in ("help", "commands"):
        _reply(tok, chat_id, HELP)
        return

    _reply(tok, chat_id, "Unknown command.\n\n" + HELP)


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
            {"command": "start", "description": "Receive every report and alert"},
            {"command": "stop", "description": "Pause delivery"},
            {"command": "status", "description": "Freshness and audience"},
            {"command": "report", "description": "Resend the latest full report"},
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
