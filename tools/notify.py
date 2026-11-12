#!/usr/bin/env python3
"""Telegram delivery for everyone, not just the owner.

One module owns the HTTP calls and the fan-out so a bulletin, an alert and the
bot's own replies cannot drift apart.

    notify.send(text)                  -> everyone who asked for this kind
    notify.send(text, kind="alert")    -> only the people who want alerts
    notify.send(text, chat_id=...)     -> one person (the bot's replies)
    notify.send_document(path)         -> HTML report to everyone
    notify.broadcast(chunks, kind=...) -> a list of cards, in order

The `kind` is the promise being made: "daily" is the plan, "eod" is the
review, "intraday" is an hourly card, "alert" and "level" are the urgent
ones. Each subscriber's /mode decides which promises they are sent. Silence
is a setting, not an unsubscribe.

Secrets live in .env (TELEGRAM_BOT_TOKEN). Never in code, never in git.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
ENV_FILE = REPO / ".env"
API_BASE = "https://api.telegram.org/bot{token}"
MAX_LEN = 3900


def load_env() -> None:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def token() -> str:
    load_env()
    t = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not t:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return t


def _plain(text: str) -> str:
    for tag in ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>", "<a href=", "</a>"):
        text = text.replace(tag, "")
    return text


def send_message(tok: str, chat_id: str | int, text: str,
                 disable_preview: bool = True,
                 reply_markup: dict | None = None) -> bool:
    """One message. Returns False instead of raising when a user blocked us.

    `reply_markup` is an inline keyboard, so an alert can carry its own
    buttons instead of making the reader remember a command.
    """
    url = API_BASE.format(token=tok) + "/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:MAX_LEN], "parse_mode": "HTML",
               "disable_web_page_preview": disable_preview}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    last = None
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 400:
                low = r.text.lower()
                if "parse entities" in low:
                    payload.pop("parse_mode", None)
                    payload["text"] = _plain(text)[:MAX_LEN]
                    r = requests.post(url, json=payload, timeout=30)
                elif "blocked" in low:
                    return False
                elif reply_markup and "reply_markup" in payload:
                    # Telegram's own wording for a bad keyboard varies
                    # ("BUTTON_DATA_INVALID", "callback_data is too long"),
                    # so any 400 that is not about formatting gets one retry
                    # without the buttons. A stale keyboard must never cost
                    # us the message.
                    payload.pop("reply_markup", None)
                    r = requests.post(url, json=payload, timeout=30)
            if r.status_code in (403, 400) and "blocked" in r.text.lower():
                return False
            r.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 + attempt * 2)
    print(f"[warn] sendMessage to {chat_id} failed: {last}", file=sys.stderr)
    return False


def send_document(tok: str, chat_id: str | int, path: Path, caption: str = "") -> bool:
    url = API_BASE.format(token=tok) + "/sendDocument"
    last = None
    for attempt in range(3):
        try:
            with Path(path).open("rb") as f:
                r = requests.post(
                    url,
                    data={"chat_id": chat_id, "caption": caption[:1024],
                          "parse_mode": "HTML"},
                    files={"document": (Path(path).name, f, "text/html")},
                    timeout=90,
                )
            r.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 + attempt * 3)
    print(f"[warn] sendDocument to {chat_id} failed: {last}", file=sys.stderr)
    return False


def _targets(chat_id: str | int | None) -> list[str]:
    if chat_id is not None:
        return [str(chat_id)]
    import subscribers
    return subscribers.recipients()


def send(text: str, chat_id: str | int | None = None, kind: str = "system",
         reply_markup: dict | None = None) -> dict:
    """Deliver one message to one person, or to everyone who asked for it.

    `kind` decides who receives it: a member who chose "quiet" still gets the
    plan and the review, but not this. Silence should be a preference, not an
    unsubscribe.
    """
    tok = token()
    ok, failed = 0, []
    targets = _targets(chat_id) if chat_id is not None else _audience(kind)
    for cid in targets:
        if send_message(tok, cid, text, reply_markup=reply_markup):
            ok += 1
        else:
            failed.append(cid)
        time.sleep(0.05)          # stay well inside Telegram's rate limit
    return {"sent": ok, "failed": failed, "audience": ok + len(failed)}


def _audience(kind: str) -> list[str]:
    """Everyone whose preferences allow this kind of message."""
    import subscribers
    try:
        return subscribers.audience(kind)
    except Exception:
        return subscribers.recipients()


def broadcast(chunks: list[str], document: Path | None = None,
              caption: str = "", kind: str = "daily") -> dict:
    """Send a set of cards to everyone who wants this kind, then the attachment."""
    tok = token()
    targets = _audience(kind)
    ok = 0
    failed = []
    for cid in targets:
        good = True
        for chunk in chunks:
            chunk = (chunk or "").strip()
            if not chunk:
                continue
            if not send_message(tok, cid, chunk):
                good = False
                break
            time.sleep(0.12)
        if good and document is not None:
            good = send_document(tok, cid, document, caption)
        if good:
            ok += 1
        else:
            failed.append(cid)
    return {"sent": ok, "failed": failed, "audience": len(targets)}
