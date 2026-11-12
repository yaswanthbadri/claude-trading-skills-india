#!/usr/bin/env python3
"""Back up the only state that cannot be rebuilt.

The candle cache is disposable (100 seconds to rebuild). These are not:

    data/recommendations.json   the entire track record
    data/subscribers.json       who gets the reports
    data/universe.csv           the 2,396-symbol scan list
    data/results_dates.json     results calendar cache
    logs/*_state.json           what has already run / been sent

A disk failure today would erase the scorecard, and the scorecard is the only
thing that makes this system better than a screener.

Two copies: a dated tarball on disk (kept 14 days) and a Telegram document to
the owner, which is genuinely off-machine with no extra infrastructure.

    python3 tools/backup.py            # snapshot + send to the owner
    python3 tools/backup.py --local    # snapshot only
    python3 tools/backup.py --list
"""
from __future__ import annotations

import argparse
import os
import sys
import tarfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "data" / "backups"
KEEP = 14

FILES = [
    "data/recommendations.json",
    "data/subscribers.json",
    "data/universe.csv",
    "data/results_dates.json",
    "logs/scheduler_state.json",
    "logs/alert_state.json",
    "logs/levels_state.json",
]


def snapshot() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    path = OUT / f"desk_{stamp}.tgz"

    present = [f for f in FILES if (REPO / f).exists()]
    if not present:
        raise SystemExit("nothing to back up — is this the right directory?")

    with tarfile.open(path, "w:gz") as tar:
        for rel in present:
            tar.add(REPO / rel, arcname=rel)

    size = path.stat().st_size
    print(f"wrote {path.relative_to(REPO)} ({size / 1024:.0f} KB, "
          f"{len(present)} file(s))")
    missing = [f for f in FILES if f not in present]
    if missing:
        print(f"  (absent, skipped: {', '.join(missing)})")
    return path


def prune(keep: int = KEEP) -> int:
    snapshots = sorted(OUT.glob("desk_*.tgz"))
    old = snapshots[:-keep] if len(snapshots) > keep else []
    for p in old:
        p.unlink(missing_ok=True)
    if old:
        print(f"pruned {len(old)} old snapshot(s), keeping {keep}")
    return len(old)


def send(path: Path) -> bool:
    """Off-machine copy. A backup on the same laptop is not a backup."""
    try:
        import notify
        notify.load_env()
        chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        if not chat:
            print("no TELEGRAM_CHAT_ID — kept local only", file=sys.stderr)
            return False
        ok = notify.send_document(
            notify.token(), chat, path,
            f"Desk backup {datetime.now():%d %b %Y} · scorecard + subscribers")
        print("sent to Telegram" if ok else "Telegram send failed")
        return ok
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not send backup: {exc}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="no Telegram copy")
    ap.add_argument("--list", action="store_true", help="show existing snapshots")
    args = ap.parse_args()

    if args.list:
        rows = sorted(OUT.glob("desk_*.tgz")) if OUT.exists() else []
        for p in rows:
            print(f"{p.name:34} {p.stat().st_size / 1024:8.0f} KB")
        print(f"{len(rows)} snapshot(s) in {OUT.relative_to(REPO)}")
        return 0

    path = snapshot()
    prune()
    if not args.local:
        send(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
