#!/usr/bin/env python3
"""Who gets the reports.

The bot used to send to one chat id from .env. That is why a second person
could start the bot and receive nothing: envery message had a hard-coded
recipient. This module is the address book.

A user is added when they send /start to the bot (see tools/bot_listener.py).
The owner is seeded from TELEGRAM_CHAT_ID so an existing install keeps working
even before anyone presses start.

data/subscribers.json
    {"version": 1,
     "users": {"1389256373": {"name": "...", "username": "...",
                              "joined": "...", "last_seen": "...",
                              "active": true, "role": "owner"}}}
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STORE = REPO / "data" / "subscribers.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load() -> dict:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text())
        except Exception:
            pass
    return {"version": 1, "users": {}}


def save(store: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1, ensure_ascii=False))
    tmp.replace(STORE)


def _seed_owner(store: dict) -> bool:
    """Make sure the env chat id is present, so an upgrade never goes silent."""
    import os
    owner = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not owner or owner in store["users"]:
        return False
    store["users"][owner] = {
        "name": "owner", "username": "", "joined": _now(), "last_seen": _now(),
        "active": True, "role": "owner",
    }
    return True


def add(chat_id: str | int, name: str = "", username: str = "",
        role: str = "member") -> dict:
    store = load()
    cid = str(chat_id)
    u = store["users"].get(cid, {})
    u.update({"name": name or u.get("name", ""), "username": username or u.get("username", ""),
              "last_seen": _now(), "active": True})
    u.setdefault("joined", _now())
    u.setdefault("role", role)
    store["users"][cid] = u
    save(store)
    return u


def deactivate(chat_id: str | int) -> bool:
    store = load()
    cid = str(chat_id)
    if cid not in store["users"]:
        return False
    store["users"][cid]["active"] = False
    store["users"][cid]["last_seen"] = _now()
    save(store)
    return True


def touch(chat_id: str | int) -> None:
    store = load()
    cid = str(chat_id)
    if cid in store["users"]:
        store["users"][cid]["last_seen"] = _now()
        save(store)


def recipients(active_only: bool = True) -> list[str]:
    """Every chat id to deliver to. Owner first, then members by join time."""
    store = load()
    if _seed_owner(store):
        save(store)
    users = store["users"]
    rows = [(cid, u) for cid, u in users.items() if u.get("active", True) or not active_only]

    def key(item):
        cid, u = item
        return (0 if u.get("role") == "owner" else 1, u.get("joined") or "")

    return [cid for cid, _ in sorted(rows, key=key)]


def active_count() -> int:
    return len(recipients())


def summary() -> list[dict]:
    store = load()
    return [
        {"chat_id": cid, "name": u.get("name") or "", "username": u.get("username") or "",
         "active": bool(u.get("active", True)), "role": u.get("role") or "member",
         "joined": u.get("joined"), "last_seen": u.get("last_seen")}
        for cid, u in store["users"].items()
    ]


if __name__ == "__main__":
    for row in summary():
        state = "on " if row["active"] else "off"
        print(f"{state} {row['chat_id']:>14}  {row['role']:6} {row['name']} "
              f"{('@' + row['username']) if row['username'] else ''}  since {row['joined']}")
