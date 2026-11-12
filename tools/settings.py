#!/usr/bin/env python3
"""The numbers the desk is configured with, in one place.

Every share count in every report derives from two values that used to be
hardcoded in three different files: trading capital and risk per trade. If they
were wrong, the sizing was wrong in a way that looked authoritative, which is
the worst kind of wrong.

    .env
        TRADING_CAPITAL=500000     rupees of capital the desk may deploy
        RISK_PCT=1.0               percent of capital risked per trade
        MAX_HEAT_PCT=6.0           total open risk allowed, all positions
        BROKER=zerodha             for the charges calculator

Anything absent falls back to the documented default, and every consumer can
tell where a value came from so the report can say so.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

DEFAULTS = {
    "TRADING_CAPITAL": 500_000.0,
    "RISK_PCT": 1.0,
    "MAX_HEAT_PCT": 6.0,
    "BROKER": "zerodha",
}


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _num(key: str) -> float:
    load_env()
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return float(DEFAULTS[key])
    try:
        return float(raw)
    except ValueError:
        return float(DEFAULTS[key])


def _text(key: str) -> str:
    load_env()
    return (os.environ.get(key) or "").strip() or str(DEFAULTS[key])


def capital() -> float:
    return _num("TRADING_CAPITAL")


def risk_pct() -> float:
    return _num("RISK_PCT")


def max_heat_pct() -> float:
    return _num("MAX_HEAT_PCT")


def broker() -> str:
    return _text("BROKER")


def is_configured(key: str) -> bool:
    """True when the value came from .env rather than the default."""
    load_env()
    return bool((os.environ.get(key) or "").strip())


def describe() -> str:
    cap = capital()
    src = "configured" if is_configured("TRADING_CAPITAL") else "default"
    return (f"₹{cap:,.0f} capital ({src}), {risk_pct():g}% risk per trade, "
            f"{max_heat_pct():g}% total heat cap")
