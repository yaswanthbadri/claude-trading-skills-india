#!/usr/bin/env python3
"""One always-on process that runs every job, so nothing depends on a laptop.

The Mac version used launchd, which only fires while the machine is awake and
logged in — that is why reports arrive late or not at all. This scheduler is a
plain loop: it works identically on a VPS, in Docker, or under systemd.

What it runs (all times IST, trading days only unless marked):
    08:50  candle cache refresh (pre-market)
    09:00  the day's plan
    09:10  cache refresh
    09:15  intraday bulletin
    10:00  cache        10:15  intraday        ... hourly to 15:00 / 15:15
    15:40  cache refresh after the close
    16:00  closing review + scorecard of our own calls
    19:30  cache backstop
    every 5 minutes, 24x7  news and index alerts (marked allday)

Guarantees
    * Times are Asia/Kolkata even if the server is UTC.
    * A missed run is not replayed hours later as if it were live. Each job has
      a grace window; outside it the job is logged as missed, not faked.
    * Restarts do not duplicate work: completed runs are recorded per day.
    * The Telegram listener runs as a supervised child, so one container is
      enough.

Usage:
    python3 tools/scheduler.py             # run forever
    python3 tools/scheduler.py --dry-run   # print the timetable and exit
    python3 tools/scheduler.py --now daily # run one job immediately
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_calendar as cal

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
LOGS = REPO / "logs"
STATE = LOGS / "scheduler_state.json"
PYTHON = sys.executable
IST = ZoneInfo("Asia/Kolkata")

# job name, HH:MM, script, args, allday (runs on holidays too), catch-up minutes
JOBS = [
    ("cache-premarket", "08:50", "build_cache.py", [], False, 20),
    ("daily", "09:00", "daily_bulletin.py", ["--mode", "daily"], False, 90),
    ("cache-open", "09:10", "build_cache.py", [], False, 10),
] + [
    ("cache-%02d" % h, "%02d:00" % h, "build_cache.py", [], False, 10)
    for h in range(10, 16)
] + [
    ("intraday-0915", "09:15", "daily_bulletin.py", ["--mode", "intraday"], False, 10),
    ("intraday-1015", "10:15", "daily_bulletin.py", ["--mode", "intraday"], False, 10),
    ("intraday-1115", "11:15", "daily_bulletin.py", ["--mode", "intraday"], False, 10),
    ("intraday-1215", "12:15", "daily_bulletin.py", ["--mode", "intraday"], False, 10),
    ("intraday-1315", "13:15", "daily_bulletin.py", ["--mode", "intraday"], False, 10),
    ("intraday-1415", "14:15", "daily_bulletin.py", ["--mode", "intraday"], False, 10),
    ("intraday-1515", "15:15", "daily_bulletin.py", ["--mode", "intraday"], False, 10),
    ("cache-close", "15:40", "build_cache.py", [], False, 30),
    ("eod", "16:00", "daily_bulletin.py", ["--mode", "eod"], False, 180),
    ("cache-night", "19:30", "build_cache.py", [], False, 240),
]

ALERT_EVERY = 300          # seconds
HEARTBEAT_EVERY = 60       # write health.json at most this often


def now_ist() -> datetime:
    return datetime.now(IST)


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"days": {}}


def save_state(state: dict) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    state["updated"] = now_ist().isoformat(timespec="seconds")
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)


def log(msg: str) -> None:
    line = f"{now_ist():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)


def run_job(name: str, script: str, args: list[str], timeout: int = 1800) -> bool:
    t0 = time.time()
    cmd = [PYTHON, str(TOOLS / script), *args]
    log(f"▶ {name}: {' '.join(Path(c).name for c in cmd[1:])}")
    try:
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
        tail = (r.stdout or "").strip().splitlines()[-3:]
        for line in tail:
            log(f"   {line[:300]}")
        if r.returncode != 0:
            err = (r.stderr or "").strip().splitlines()[-3:]
            log(f"✖ {name} exit {r.returncode}: {' | '.join(err)[:400]}")
            return False
        log(f"✔ {name} in {time.time() - t0:.0f}s")
        return True
    except subprocess.TimeoutExpired:
        log(f"✖ {name} timed out after {timeout}s")
        return False
    except Exception as exc:  # noqa: BLE001
        log(f"✖ {name} crashed: {exc}")
        return False


def due_jobs(state: dict, now: datetime) -> list[tuple]:
    """Jobs whose slot has arrived, have not run today, and are inside grace."""
    day = now.date().isoformat()
    done = state["days"].setdefault(day, {})
    trading = cal.is_trading_day(now.date())
    out = []

    for name, hhmm, script, args, allday, catchup in JOBS:
        if name in done:
            continue
        if not allday and not trading:
            continue
        h, m = (int(x) for x in hhmm.split(":"))
        slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
        delta = (now - slot).total_seconds() / 60
        if delta < 0:
            continue
        # On time, or late but still inside the catch-up window.
        if delta <= max(catchup, 10):
            out.append((slot, name, script, args, delta))
    out.sort(key=lambda x: x[0])
    return out


def health(state: dict) -> dict:
    day = now_ist().date().isoformat()
    done = state["days"].get(day, {})
    return {
        "server_time": now_ist().isoformat(timespec="seconds"),
        "trading_day": cal.is_trading_day(),
        "market_open": cal.is_market_open(),
        "today": {k: v for k, v in sorted(done.items())},
        "pid": os.getpid(),
    }


def write_health(state: dict) -> None:
    try:
        (LOGS / "health.json").write_text(json.dumps(health(state), indent=1))
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="run one job by name immediately, then exit")
    args = ap.parse_args()

    if args.dry_run:
        print(f"{'job':18} {'time':>6}  script")
        for name, hhmm, script, jargs, allday, catchup in JOBS:
            flag = "  (every day)" if allday else ""
            print(f"{name:18} {hhmm:>6}  {script} {' '.join(jargs)}{flag}")
        print(f"\nalerts every {ALERT_EVERY}s, 24x7")
        return 0

    if args.now:
        if args.now == "alerts":
            ok = run_job("alerts", "alert_monitor.py", [], timeout=240)
            return 0 if ok else 1
        job = next((j for j in JOBS if j[0] == args.now), None)
        if not job:
            print(f"unknown job {args.now!r}; choose from "
                  f"{', '.join([j[0] for j in JOBS] + ['alerts'])}", file=sys.stderr)
            return 2
        ok = run_job(job[0], job[2], job[3])
        return 0 if ok else 1

    LOGS.mkdir(parents=True, exist_ok=True)
    log(f"scheduler up · {len(JOBS)} jobs · alerts every {ALERT_EVERY}s · TZ {IST}")

    state = load_state()
    stop = {"flag": False}

    def _stop(signum, _frame):
        stop["flag"] = True
        log(f"signal {signum} — shutting down")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    last_alert = 0.0
    last_health = 0.0

    # Supervise the Telegram listener so a single process/container is enough.
    listener = None
    listener_started = 0.0

    while not stop["flag"]:
        now = now_ist()

        if listener is None or listener.poll() is not None:
            if listener is not None:
                log(f"bot listener exited ({listener.returncode}) — restarting")
            listener = subprocess.Popen(
                [PYTHON, str(TOOLS / "bot_listener.py")],
                cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            )
            listener_started = time.time()

        for slot, name, script, jargs, late in due_jobs(state, now):
            if late > 10:
                log(f"↷ {name} is {late:.0f} min late — running as catch-up")
            ok = run_job(name, script, jargs)
            state["days"].setdefault(now.date().isoformat(), {})[name] = (
                now_ist().strftime("%H:%M:%S") + ("" if ok else " FAILED"))
            save_state(state)

        if time.time() - last_alert >= ALERT_EVERY:
            last_alert = time.time()
            run_job("alerts", "alert_monitor.py", [], timeout=240)

        if time.time() - last_health >= HEARTBEAT_EVERY:
            last_health = time.time()
            write_health(state)
            # Keep the state file to the last 14 days.
            cutoff = (now.date() - timedelta(days=14)).isoformat()
            for d in [d for d in state["days"] if d < cutoff]:
                state["days"].pop(d, None)
            save_state(state)

        time.sleep(5)

    if listener and listener.poll() is None:
        listener.terminate()
    log("scheduler stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
