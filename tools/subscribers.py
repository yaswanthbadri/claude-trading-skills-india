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
                              "active": true, "role": "owner",
                              "mode": "all", "snooze_until": null,
                              "watch": {"CENTUM": {"pivot": 4238.5, "added": "..."}}}}}

Delivery is per person, not per list. A friend who wants the 09:00 plan and the
16:00 review should not have to accept fourteen hourly cards to get them, and
the only alternative used to be /stop, which turned off the parts that mattered
too.

Modes
    all      everything (default)
    quiet    the daily plan and the evening review only
    alerts   alerts and level breaches only — no scheduled reports
    none     nothing until /mode is set again
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


MODES = ("all", "quiet", "alerts", "none")

# Which mode a message kind is allowed through. "quiet" is the plan and the
# review; "alerts" is the two things that are time-critical.
MODE_KINDS = {
    "all": {"daily", "eod", "intraday", "alert", "level", "system"},
    "quiet": {"daily", "eod", "system"},
    "alerts": {"alert", "level", "system"},
    "none": {"system"},
}


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


# ---------------------------------------------------------------------------
# preferences
# ---------------------------------------------------------------------------
def user(chat_id: str | int) -> dict:
    store = load()
    return store["users"].get(str(chat_id), {})


def is_owner(chat_id: str | int) -> bool:
    u = user(chat_id)
    if u.get("role") == "owner":
        return True
    import os
    from datetime import datetime as _dt
    owner = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    return bool(owner) and str(chat_id) == owner


def mode_of(chat_id: str | int) -> str:
    return (user(chat_id).get("mode") or "all").lower()


def set_mode(chat_id: str | int, mode: str) -> str:
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    store = load()
    cid = str(chat_id)
    if cid not in store["users"]:
        return "unknown"
    store["users"][cid]["mode"] = mode
    store["users"][cid]["last_seen"] = _now()
    save(store)
    return mode


def snooze(chat_id: str | int, minutes: int) -> str | None:
    """Silence alerts for a while without losing the subscription."""
    from datetime import timedelta
    store = load()
    cid = str(chat_id)
    if cid not in store["users"]:
        return None
    until = (datetime.now() + timedelta(minutes=max(1, minutes))).isoformat(timespec="seconds")
    store["users"][cid]["snooze_until"] = until
    save(store)
    return until


def snoozed(chat_id: str | int) -> bool:
    until = user(chat_id).get("snooze_until")
    if not until:
        return False
    try:
        return datetime.fromisoformat(until) > datetime.now()
    except Exception:
        return False


def wants(chat_id: str | int, kind: str) -> bool:
    """Should this person receive a message of this kind, right now?"""
    if kind in ("alert", "level") and snoozed(chat_id):
        return False
    return kind in MODE_KINDS.get(mode_of(chat_id), MODE_KINDS["all"])


def audience(kind: str) -> list[str]:
    return [cid for cid in recipients() if wants(cid, kind)]


# ---------------------------------------------------------------------------
# per-person watchlist
# ---------------------------------------------------------------------------
def watch(chat_id: str | int, symbol: str, pivot: float | None = None,
          kind: str = "pivot") -> bool:
    store = load()
    cid = str(chat_id)
    if cid not in store["users"]:
        return False
    sym = symbol.strip().upper()
    if not sym:
        return False
    w = store["users"][cid].setdefault("watch", {})
    w[sym] = {"pivot": round(float(pivot), 2) if pivot else None,
              "kind": kind if kind in ("pivot", "high", "above") else "pivot",
              "added": _now(), "notified": None}
    save(store)
    return True


def unwatch(chat_id: str | int, symbol: str) -> bool:
    store = load()
    cid = str(chat_id)
    if cid not in store["users"]:
        return False
    w = store["users"][cid].get("watch") or {}
    if symbol.strip().upper() not in w:
        return False
    del w[symbol.strip().upper()]
    save(store)
    return True


def watchlist(chat_id: str | int) -> dict:
    return dict(user(chat_id).get("watch") or {})


def mark_notified(chat_id: str | int, symbol: str) -> None:
    store = load()
    cid = str(chat_id)
    w = (store["users"].get(cid) or {}).get("watch") or {}
    if symbol in w:
        w[symbol]["notified"] = _now()
        save(store)


def all_watchers() -> list[tuple[str, str, dict]]:
    """(chat_id, symbol, entry) for every watch across every user."""
    out = []
    for cid, u in load()["users"].items():
        if not u.get("active", True):
            continue
        for sym, entry in (u.get("watch") or {}).items():
            out.append((cid, sym, entry or {}))
    return out


def summary() -> list[dict]:
    store = load()
    return [
        {"chat_id": cid, "name": u.get("name") or "", "username": u.get("username") or "",
         "active": bool(u.get("active", True)), "role": u.get("role") or "member",
         "joined": u.get("joined"), "last_seen": u.get("last_seen"),
         "mode": u.get("mode") or "all", "watch": len(u.get("watch") or {})}
        for cid, u in store["users"].items()
    ]


if __name__ == "__main__":
    for row in summary():
        state = "on " if row["active"] else "off"
        print(f"{state} {row['chat_id']:>14}  {row['role']:6} {row['name']} "
              f"{('@' + row['username']) if row['username'] else ''}  since {row['joined']}")
