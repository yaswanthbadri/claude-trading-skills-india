#!/usr/bin/env python3
"""Indian market calendar helpers (NSE/BSE equity + derivatives).

A small, dependency-free module used by the scheduler-facing scripts so they do
not fire on weekends or exchange holidays.

Source for 2026: skills/indian-market-structure/references/holidays-2026.md
Update HOLIDAYS when NSE publishes the next calendar.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

# Fixed-date holidays that repeat every year.
FIXED_HOLIDAYS = {(1, 26), (5, 1), (8, 15), (10, 2), (12, 25)}

# Announcement-based holidays for 2026.
HOLIDAYS_2026 = {
    date(2026, 1, 15),   # municipal elections
    date(2026, 3, 3),    # Holi
    date(2026, 3, 26),   # Ram Navami
    date(2026, 3, 31),   # Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Ambedkar Jayanti
    date(2026, 5, 28),   # Bakri Id
    date(2026, 6, 26),   # Muharram
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 10),  # Diwali Balipratipada
    date(2026, 11, 24),  # Guru Nanak Jayanti
}

# Special live sessions: {date: (open, close)}
SPECIAL_SESSIONS = {
    date(2026, 2, 1): (time(9, 15), time(15, 0)),   # Union Budget Sunday
    date(2026, 11, 8): (time(18, 15), time(19, 15)),  # Muhurat trading
}

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
PRE_OPEN_START = time(9, 0)


def is_trading_day(d: date | None = None) -> bool:
    d = d or date.today()
    if d.weekday() >= 5 and d not in SPECIAL_SESSIONS:
        return False
    if d in SPECIAL_SESSIONS:
        return True
    if d in HOLIDAYS_2026:
        return False
    return (d.month, d.day) not in FIXED_HOLIDAYS


def session_times(d: date | None = None):
    d = d or date.today()
    return SPECIAL_SESSIONS.get(d, (MARKET_OPEN, MARKET_CLOSE))


def is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if not is_trading_day(now.date()):
        return False
    start, end = session_times(now.date())
    return start <= now.time() <= end


def is_pre_market(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if not is_trading_day(now.date()):
        return False
    return PRE_OPEN_START <= now.time() < MARKET_OPEN


def next_trading_day(d: date | None = None) -> date:
    d = (d or date.today()) + timedelta(days=1)
    for _ in range(15):
        if is_trading_day(d):
            return d
        d += timedelta(days=1)
    return d


def previous_trading_day(d: date | None = None) -> date:
    d = (d or date.today()) - timedelta(days=1)
    for _ in range(15):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return d


if __name__ == "__main__":
    now = datetime.now()
    print(f"today           {now.date()} ({now.strftime('%A')})")
    print(f"trading day     {is_trading_day(now.date())}")
    print(f"market open     {is_market_open(now)}")
    print(f"session         {session_times(now.date())[0]}–{session_times(now.date())[1]}")
    print(f"previous        {previous_trading_day(now.date())}")
    print(f"next            {next_trading_day(now.date())}")
