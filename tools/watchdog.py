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


def _tell(text: str, dry: bool = False) -> None:
    """Say something to the owner. A failure nobody hears about is a silent one.

    Repairs go to the owner only: they are operational, not market content, and
    the other subscribers should not learn that the Mac was asleep.
    """
    if dry:
        _log("would notify (dry run, nothing sent): " + text.splitlines()[0])
        return
    try:
        import os

        import notify
        notify.load_env()
        chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        if not chat:
            return
        res = notify.send(text, chat_id=chat)
        _log(f"notified owner: {res}")
    except Exception as exc:  # noqa: BLE001
        _log(f"[warn] could not notify: {exc}")


def _reports_today(mode: str) -> list[Path]:
    day = date.today().isoformat()
    return sorted(REPORTS.glob(f"bulletin_{mode}_{day}_*.json"))


def _delivered(mode: str) -> bool:
    """Was today's report actually sent to the subscribers?

    A report file on disk is not a delivery. The build can finish and the
    Telegram call can fail — a timeout, a rate limit, a blocked chat — and
    the file will still be sitting there looking perfectly fine. This reads
    the delivery log, because that is the only record of what the reader
    actually received.
    """
    day = date.today().isoformat()
    log = LOGS / "telegram.log"
    if not log.exists():
        return False
    try:
        tail = log.read_text(errors="ignore")[-200_000:]
    except Exception:
        return False
    for line in tail.splitlines():
        if f"Sent bulletin_{mode}_{day}_" not in line:
            continue
        # "to 2/2 chat(s)" is a delivery. "to 1/2 chat(s); failed [...]" is a
        # partial delivery, which is still a delivery for the others.
        if "chat(s)" in line:
            return True
    return False


def _delivery_failures(mode: str) -> list[str]:
    """Chat ids that did not get today's report."""
    day = date.today().isoformat()
    log = LOGS / "telegram.log"
    out = []
    if not log.exists():
        return out
    try:
        tail = log.read_text(errors="ignore")[-200_000:]
    except Exception:
        return out
    for line in tail.splitlines():
        if f"Sent bulletin_{mode}_{day}_" in line and "failed" in line:
            out.append(line.strip()[:300])
    return out


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
        # A report that was built but never delivered is still missing.
        daily_files = _reports_today("daily")
        daily_sent = _delivered("daily")
        if now.hour * 60 + now.minute >= 9 * 60 + 30 and (not daily_files or not daily_sent):
            if now.hour * 60 + now.minute <= 13 * 60:
                actions.append(("daily", "morning plan missing or undelivered"))
            else:
                _log("gap: no morning plan today and it is too late to publish one")
                _tell("⚠️ <b>No morning plan today</b>\n"
                      "The 09:00 run never happened and it is now too late to "
                      "publish levels that would already be stale, so nothing "
                      "was sent. Yesterday's plan is the last valid one.",
                      dry=args.dry_run)

        eod_files = _reports_today("eod")
        eod_sent = _delivered("eod")
        if now.hour * 60 + now.minute >= 16 * 60 + 10 and (not eod_files or not eod_sent):
            if now.hour * 60 + now.minute <= 19 * 60:
                actions.append(("eod", "closing review missing or undelivered"))
            else:
                _log("gap: no closing review today and it is too late to publish one")
                _tell("⚠️ <b>No closing review today</b>\n"
                      "The 16:00 run never happened. The scorecard will catch up "
                      "on the next session; no verdicts were invented.",
                      dry=args.dry_run)

    # A delivery that failed is worth knowing about even when the report
    # itself was fine: somebody did not get it.
    for mode in ("daily", "eod"):
        if now.hour * 60 + now.minute < 16 * 60 + 10:
            break
        for line in _delivery_failures(mode):
            _log(f"delivery problem: {line}")
            _tell(f"⚠️ <b>Delivery problem</b>\n{line}\n"
                  "<i>The report was built but a chat did not receive it. "
                  "Send /report to resend the latest one.</i>",
                  dry=args.dry_run)

    expected = cal.last_session_bar(now=now).isoformat()
    last = _cache_last_bar()
    if last and last < expected:
        _log(f"cache is behind: has {last}, needs {expected}")
        _refresh_cache(args.dry_run)

    for mode, why in actions:
        _log(f"{why} — rebuilding")
        ok = _run(mode, args.dry_run)
        if args.dry_run:
            continue
        if ok:
            _tell(f"🔧 <b>Desk repair</b>\n{why}. Rebuilt just now ({mode}).\n"
                  f"<i>This usually means the machine was asleep at the scheduled "
                  f"time.</i>")
        else:
            _tell(f"✖ <b>Desk repair failed</b>\n{why}, and the rebuild did not "
                  f"succeed. Check logs/watchdog.log.")

    if not actions and (last or "") >= expected:
        _log("nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
