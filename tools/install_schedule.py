#!/usr/bin/env python3
"""Install (or reinstall) the launchd schedules for the bulletin.

Jobs
    com.tradingskills.intraday   hourly bulletin, 09:15-15:15, Mon-Fri
    com.tradingskills.daily      morning plan, 09:00, Mon-Fri
    com.tradingskills.eod        closing review, 16:00, Mon-Fri
    com.tradingskills.alerts     news + price alerts, every 5 minutes, 24x7
    com.tradingskills.bot        Telegram listener, kept alive (interim)
    com.tradingskills.watchdog   fills gaps a sleeping Mac left behind
    com.tradingskills.cache      candle cache refresh, hourly + post-close
    com.tradingskills.backup     nightly snapshot of the track record, 19:45

Holiday handling is inside the scripts, not here (a holiday list changes).

Usage:
    python3 tools/install_schedule.py            # install / reload
    python3 tools/install_schedule.py --uninstall
    python3 tools/install_schedule.py --status
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / "tools"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
PYTHON = "/opt/homebrew/bin/python3"
LOGS = REPO / "logs"

UID = os.getuid()
DOMAIN = f"gui/{UID}"

# Weekday in launchd: 0/7 = Sunday, 1 = Monday ... 5 = Friday
WEEKDAYS = [1, 2, 3, 4, 5]

INTRADAY_HOURS = [9, 10, 11, 12, 13, 14, 15]
INTRADAY_MINUTE = 15

HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
"""

FOOTER = """</dict>
</plist>
"""


def _base(label: str, script: str, args: list[str]) -> str:
    argv = "".join(f"    <string>{a}</string>\n" for a in [PYTHON, str(TOOLS / script), *args])
    return (
        HEADER
        + f"  <key>Label</key>\n  <string>{label}</string>\n"
        + "  <key>ProgramArguments</key>\n  <array>\n"
        + argv
        + "  </array>\n"
        + f"  <key>WorkingDirectory</key>\n  <string>{REPO}</string>\n"
        + "  <key>ProcessType</key>\n  <string>Background</string>\n"
        + f"  <key>StandardOutPath</key>\n  <string>{LOGS / 'telegram.log'}</string>\n"
        + f"  <key>StandardErrorPath</key>\n  <string>{LOGS / 'telegram.log'}</string>\n"
    )


def _calendar(entries: list[dict]) -> str:
    out = "  <key>StartCalendarInterval</key>\n  <array>\n"
    for e in entries:
        out += "    <dict>\n"
        for k, v in e.items():
            out += f"      <key>{k}</key>\n      <integer>{v}</integer>\n"
        out += "    </dict>\n"
    out += "  </array>\n"
    return out


def build_plists() -> dict[str, str]:
    plists: dict[str, str] = {}

    intraday_entries = [
        {"Weekday": d, "Hour": h, "Minute": INTRADAY_MINUTE}
        for d in WEEKDAYS for h in INTRADAY_HOURS
    ]
    plists["com.tradingskills.intraday"] = (
        _base("com.tradingskills.intraday", "daily_bulletin.py", ["--mode", "intraday"])
        + _calendar(intraday_entries)
        + FOOTER
    )

    eod_entries = [{"Weekday": d, "Hour": 16, "Minute": 0} for d in WEEKDAYS]
    plists["com.tradingskills.eod"] = (
        _base("com.tradingskills.eod", "daily_bulletin.py", ["--mode", "eod"])
        + _calendar(eod_entries)
        + FOOTER
    )

    # The morning plan, 09:00 weekdays, 15 minutes before the open. The scripts
    # themselves skip holidays; launchd only handles the clock.
    daily_entries = [{"Weekday": d, "Hour": 9, "Minute": 0} for d in WEEKDAYS]
    plists["com.tradingskills.daily"] = (
        _base("com.tradingskills.daily", "daily_bulletin.py", ["--mode", "daily"])
        + _calendar(daily_entries)
        + FOOTER
    )

    # The listener answers /start, /stop, /status. Kept alive so a crash does
    # not silently cut people off. Interim: the cloud scheduler supervises this
    # itself, so unload this job after the move.
    plists["com.tradingskills.bot"] = (
        _base("com.tradingskills.bot", "bot_listener.py", [])
        + "  <key>KeepAlive</key>\n  <true/>\n"
        + "  <key>RunAtLoad</key>\n  <true/>\n"
        + f"  <key>StandardOutPath</key>\n  <string>{LOGS / 'bot.log'}</string>\n"
        + f"  <key>StandardErrorPath</key>\n  <string>{LOGS / 'bot.log'}</string>\n"
        + FOOTER
    )

    # Safety net: if the Mac was asleep at 09:00 or 16:00, this notices the
    # missing report and rebuilds it once, inside a sensible window.
    watchdog_entries = [{"Weekday": d, "Hour": h, "Minute": 25}
                        for d in WEEKDAYS for h in (9, 10, 11, 12, 16, 17, 18)]
    plists["com.tradingskills.watchdog"] = (
        _base("com.tradingskills.watchdog", "watchdog.py", [])
        + _calendar(watchdog_entries)
        + f"  <key>StandardOutPath</key>\n  <string>{LOGS / 'watchdog.log'}</string>\n"
        + f"  <key>StandardErrorPath</key>\n  <string>{LOGS / 'watchdog.log'}</string>\n"
        + FOOTER
    )

    # The scorecard is the one thing a disk failure cannot rebuild. Every day,
    # not just trading days: subscribers change on weekends too.
    backup_entries = [{"Weekday": d, "Hour": 19, "Minute": 45} for d in range(7)]
    plists["com.tradingskills.backup"] = (
        _base("com.tradingskills.backup", "backup.py", [])
        + _calendar(backup_entries)
        + f"  <key>StandardOutPath</key>\n  <string>{LOGS / 'backup.log'}</string>\n"
        + f"  <key>StandardErrorPath</key>\n  <string>{LOGS / 'backup.log'}</string>\n"
        + FOOTER
    )

    plists["com.tradingskills.alerts"] = (
        _base("com.tradingskills.alerts", "alert_monitor.py", [])
        + "  <key>StartInterval</key>\n  <integer>300</integer>\n"
        + f"  <key>StandardOutPath</key>\n  <string>{LOGS / 'alerts.log'}</string>\n"
        + f"  <key>StandardErrorPath</key>\n  <string>{LOGS / 'alerts.log'}</string>\n"
        + FOOTER
    )

    # Candle cache: 08:50 before the morning plan, hourly at :00 so the :15
    # bulletin scans today's bars, 15:40 once the close is in (the 16:00 review
    # needs the full session), and 19:30 as a backstop.
    cache_entries = ([{"Weekday": d, "Hour": 8, "Minute": 50} for d in WEEKDAYS]
                     + [{"Weekday": d, "Hour": 9, "Minute": 10} for d in WEEKDAYS]
                     + [{"Weekday": d, "Hour": h, "Minute": 0} for d in WEEKDAYS
                        for h in range(10, 16)]
                     + [{"Weekday": d, "Hour": 15, "Minute": 40} for d in WEEKDAYS]
                     + [{"Weekday": d, "Hour": 19, "Minute": 30} for d in WEEKDAYS])
    plists["com.tradingskills.cache"] = (
        _base("com.tradingskills.cache", "build_cache.py", [])
        + _calendar(cache_entries)
        + f"  <key>StandardOutPath</key>\n  <string>{LOGS / 'cache.log'}</string>\n"
        + f"  <key>StandardErrorPath</key>\n  <string>{LOGS / 'cache.log'}</string>\n"
        + FOOTER
    )
    return plists


def run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def status() -> None:
    code, out = run(["launchctl", "list"])
    for line in out.splitlines():
        if "tradingskills" in line:
            print(line)
    if "tradingskills" not in out:
        print("No trading-skills jobs loaded.")


def uninstall(plists: dict[str, str]) -> None:
    for label in plists:
        path = LAUNCH_AGENTS / f"{label}.plist"
        if path.exists():
            run(["launchctl", "bootout", DOMAIN, str(path)])
            print(f"booted out {label}")
        # legacy label
    legacy = LAUNCH_AGENTS / "com.tradingskills.bulletin.plist"
    if legacy.exists():
        run(["launchctl", "bootout", DOMAIN, str(legacy)])
        legacy.unlink()
        print("removed legacy com.tradingskills.bulletin.plist")


def install(plists: dict[str, str]) -> int:
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    uninstall(plists)

    failures = 0
    for label, xml in plists.items():
        path = LAUNCH_AGENTS / f"{label}.plist"
        path.write_text(xml)
        code, out = run(["launchctl", "bootstrap", DOMAIN, str(path)])
        if code != 0 and "already bootstrapped" not in out:
            print(f"[fail] {label}: {out}", file=sys.stderr)
            failures += 1
        else:
            print(f"[ok] {label} -> {path.name}")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    plists = build_plists()

    if args.status:
        status()
        return 0
    if args.uninstall:
        uninstall(plists)
        return 0

    failures = install(plists)
    print()
    status()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
