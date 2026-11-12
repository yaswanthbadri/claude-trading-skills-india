#!/usr/bin/env python3
"""Last line of defence: fill in whatever the schedule missed.

launchd only fires while the Mac is awake. If the lid was shut at 09:00, macOS
usually coalesces the missed job and runs it on wake — but not always, and a
crashed run leaves nothing behind at all. This script looks at what *should*
exist by now and creates only what is missing.

It never re-runs something that already happened, so it cannot duplicate a
report or a push. Cheap enough to run every ten minutes.

    python3 tools/watchdog.py            # act
    python3 tools/watchdog.py --dry-run  # say what it would do

What it checks
    09:30+   a morning plan exists for today (trading days)
    16:10+   a closing review exists for today
    always   the candle cache holds the newest bar that should exist

Late is not the same as useful: a missing morning plan is only rebuilt before
13:00, and a missing review before 19:00. After that it is logged as a gap
rather than published as if it were live.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_calendar as cal

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "reports"
LOGS = REPO / "logs"
DATA = REPO / "data"
PYTHON = sys.executable


def _log(msg: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with (LOGS / "watchdog.log").open("a") as f:
        f.write(line + "\n")


def _reports_today(mode: str) -> list[Path]:
    day = date.today().isoformat()
    return sorted(REPORTS.glob(f"bulletin_{mode}_{day}_*.json"))


def _cache_last_bar() -> str | None:
    p = DATA / "nifty.csv"
    if not p.exists():
        return None
    try:
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 300))
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
        return tail[-1].split(",")[0]
    except Exception:
        return None


def _run(mode: str, dry: bool) -> bool:
    if dry:
        _log(f"would run --mode {mode}")
        return True
    cmd = [PYTHON, str(REPO / "tools" / "daily_bulletin.py"), "--mode", mode]
    _log(f"↻ running --mode {mode}")
    try:
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=900)
        if r.returncode:
            _log(f"✖ {mode} failed: {(r.stderr or '').strip()[:300]}")
            return False
        _log(f"✔ {mode} rebuilt")
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"✖ {mode} crashed: {exc}")
        return False


def _refresh_cache(dry: bool) -> bool:
    if dry:
        _log("would refresh the candle cache")
        return True
    _log("↻ refreshing the candle cache")
    try:
        r = subprocess.run([PYTHON, str(REPO / "tools" / "build_cache.py")],
                           cwd=REPO, capture_output=True, text=True, timeout=1800)
        if r.returncode:
            _log(f"✖ cache failed: {(r.stderr or '').strip()[:300]}")
            return False
        _log("✔ cache refreshed")
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"✖ cache crashed: {exc}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now()
    trading = cal.is_trading_day()
    actions = []

    if trading:
        if now.hour * 60 + now.minute >= 9 * 60 + 30 and not _reports_today("daily"):
            if now.hour * 60 + now.minute <= 13 * 60:
                actions.append(("daily", "morning plan missing"))
            else:
                _log("gap: no morning plan today and it is too late to publish one")

        if now.hour * 60 + now.minute >= 16 * 60 + 10 and not _reports_today("eod"):
            if now.hour * 60 + now.minute <= 19 * 60:
                actions.append(("eod", "closing review missing"))
            else:
                _log("gap: no closing review today and it is too late to publish one")

    expected = cal.last_session_bar(now=now).isoformat()
    last = _cache_last_bar()
    if last and last < expected:
        _log(f"cache is behind: has {last}, needs {expected}")
        _refresh_cache(args.dry_run)

    for mode, why in actions:
        _log(f"{why} — rebuilding")
        _run(mode, args.dry_run)

    if not actions and (last or "") >= expected:
        _log("nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
