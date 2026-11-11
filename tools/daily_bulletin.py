#!/usr/bin/env python3
"""Generate the India market bulletin and push it to Telegram.

Pipeline:
    bulletin_data.collect()  → structured dict
    bulletin_render          → .md and .html
    telegram_bulletin        → chat messages + HTML attachment

Modes:
    --mode intraday   hourly bulletin during the session (default)
    --mode daily      pre-market brief, every day
    --mode eod        closing wrap after the bell

Intraday and eod runs skip exchange holidays. Daily runs always fire.

Usage:
    python3 tools/daily_bulletin.py
    python3 tools/daily_bulletin.py --mode daily
    python3 tools/daily_bulletin.py --mode eod --no-push
    python3 tools/daily_bulletin.py --capital 200000 --risk-pct 1
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bulletin_data
import bulletin_render
import market_calendar as cal

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "reports"
PYTHON = sys.executable


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["intraday", "daily", "eod"], default="intraday")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--capital", type=float, default=500_000)
    ap.add_argument("--risk-pct", type=float, default=1.0)
    ap.add_argument("--candidates", type=int, default=6)
    ap.add_argument("--force", action="store_true",
                    help="run even on a market holiday")
    args = ap.parse_args()

    if args.mode in ("intraday", "eod") and not args.force:
        if not cal.is_trading_day():
            print(f"{datetime.now():%Y-%m-%d} is not an NSE trading day — skipping {args.mode} run.")
            return 0

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    # Mode in the filename so two runs in the same minute never overwrite each other.
    stem = f"bulletin_{args.mode}_{stamp}"

    data = bulletin_data.collect(
        capital=args.capital,
        risk_pct=args.risk_pct,
        candidates=args.candidates,
    )
    data["mode"] = args.mode

    md_path = REPORTS / f"{stem}.md"
    html_path = REPORTS / f"{stem}.html"
    json_path = REPORTS / f"{stem}.json"

    md_path.write_text(bulletin_render.render_md(data), encoding="utf-8")
    html_path.write_text(
        bulletin_render.render_html(data, f"{args.mode} {stamp}"), encoding="utf-8"
    )
    json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    print(f"Wrote {md_path}")
    print(f"Wrote {html_path}")
    print(f"Wrote {json_path}")

    if not args.no_push:
        r = subprocess.run(
            [PYTHON, str(REPO / "tools" / "telegram_bulletin.py"), "--file", str(md_path)],
            cwd=REPO,
        )
        if r.returncode:
            print("[warn] telegram push failed", file=sys.stderr)
            return r.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
