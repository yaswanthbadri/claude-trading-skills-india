#!/usr/bin/env python3
"""Inline keyboards, in one place.

An alert should not require the reader to remember a command. The message
already says KAYNES crossed its pivot; the useful next taps are "show me my
book", "check this name", and "shut up for two hours".

Telegram caps callback_data at 64 bytes and truncates silently-ish beyond
that, so every payload here is short and prefixed with its own verb:

    book                 open positions with live R
    check:CENTUM         run the single-stock card
    levels:BAJAJ-AUTO    levels for one symbol
    snooze:120           silence alerts for N minutes

`parse()` in the bot listener is the only consumer. Keep the two in step: a
button that sends a verb the dispatcher does not know is a dead button.
"""
from __future__ import annotations

MAX_CB = 64


def _cb(data: str) -> str:
    return data[:MAX_CB]


def kb(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [
        [{"text": label, "callback_data": _cb(data)} for label, data in row]
        for row in rows
    ]}


def alert(symbol: str | None = None) -> dict:
    """Attached to a stock-moving event. Snooze is the escape hatch."""
    row = [("📊 Book", "book"), ("🔇 2h", "snooze:120")]
    if symbol:
        row.insert(0, ("🔎 " + symbol[:14], f"check:{symbol}"))
    return kb([row])


def levels(symbol: str | None = None) -> dict:
    """Attached to a stop/T1/T2 breach. No snooze: a stop is not noise."""
    row = [("📊 Book", "book"), ("🐕 Heat", "heat")]
    if symbol:
        row.insert(0, ("🔎 " + symbol[:14], f"check:{symbol}"))
    return kb([row])


def watch(symbol: str) -> dict:
    """A pivot cleared on someone's own watchlist."""
    return kb([[("📊 Book", "book"),
                ("🔕 Unwatch", f"unwatch:{symbol}"),
                ("🔇 2h", "snooze:120")]])


def parse(data: str) -> tuple[str, str]:
    """'check:CENTUM' -> ('check', 'CENTUM'). Unknown verbs pass through."""
    verb, _, arg = (data or "").partition(":")
    return verb.strip().lower(), arg.strip()
