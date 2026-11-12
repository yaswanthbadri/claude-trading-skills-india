#!/usr/bin/env python3
"""Delivery percentage: is the move being accumulated, or just churned?

A 4% move on 12% delivery is traders passing paper to each other and squaring
up by 3:20. The same move on 55% delivery means the shares changed hands and
stayed there. Volume alone cannot tell you which, and every breakout scan in
the system was reading volume alone.

NSE publishes it in the daily full bhavcopy, one column: DELIV_PER. Pulling it
also gives the traded value in rupees and the series code, so this doubles as
the liquidity check.

One file per session, kept for 30 sessions, so a symbol's delivery can be
compared with its own recent habit rather than a market-wide number:

    delivery 48%  ·  20-session average 24%  ·  2.01x

That multiple is the useful number. 48% delivery means nothing for a stock that
always does 45%, and 12% is normal for a large cap that never does more.

Usage:
    python3 tools/delivery.py --symbols CENTUM,ACMESOLAR
    python3 tools/delivery.py --date 2026-11-11
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "data" / "delivery"
KEEP = 30

BASE = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{}.csv"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124 Safari/537.36")}


def _path(day: str) -> Path:
    return OUT / f"{day}.csv"


def _get(url: str) -> str | None:
    try:
        import requests
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200 or "SYMBOL" not in r.text[:200].upper():
            return None
        return r.text
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] bhavcopy: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------
def fetch(day: str) -> bool:
    """Download one session. Returns True when the file now exists."""
    if _path(day).exists():
        return True
    text = _get(BASE.format(datetime.strptime(day, "%Y-%m-%d").strftime("%d%m%Y")))
    if not text:
        return False

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return False
    head = [h.strip().upper() for h in rows[0]]
    idx = {k: head.index(k) for k in
           ("SYMBOL", "SERIES", "CLOSE_PRICE", "TTL_TRD_QNTY", "TURNOVER_LACS")
           if k in head}
    dp = head.index("DELIV_PER") if "DELIV_PER" in head else None
    if "SYMBOL" not in idx or "SERIES" not in idx or dp is None:
        print("[warn] bhavcopy columns changed — delivery unavailable",
              file=sys.stderr)
        return False

    OUT.mkdir(parents=True, exist_ok=True)
    tmp = _path(day).with_suffix(".tmp")
    with tmp.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "series", "close", "turnover_cr", "deliv_pct"])
        for r in rows[1:]:
            if len(r) <= dp:
                continue
            sym = r[idx["SYMBOL"]].strip().upper()
            if not sym:
                continue
            try:
                turn = float(r[idx["TURNOVER_LACS"]].strip() or 0) / 100.0
            except Exception:
                turn = 0.0
            try:
                close = float(r[idx["CLOSE_PRICE"]].strip() or 0)
            except Exception:
                close = 0.0
            try:
                pct = float(r[dp].strip() or 0)
            except Exception:
                pct = 0.0
            w.writerow([sym, r[idx["SERIES"]].strip(), f"{close:.2f}",
                        f"{turn:.2f}", f"{pct:.2f}"])
    tmp.replace(_path(day))
    _prune()
    return True


def _prune(keep: int = KEEP) -> None:
    files = sorted(OUT.glob("*.csv"))
    for f in files[:-keep] if len(files) > keep else []:
        f.unlink(missing_ok=True)


def load_day(day: str) -> dict[str, dict]:
    p = _path(day)
    if not p.exists():
        return {}
    out = {}
    with p.open() as fh:
        for r in csv.DictReader(fh):
            try:
                out[r["symbol"]] = {"series": r["series"],
                                    "close": float(r["close"]),
                                    "turnover_cr": float(r["turnover_cr"]),
                                    "deliv_pct": float(r["deliv_pct"])}
            except Exception:
                continue
    return out


def sessions() -> list[str]:
    return sorted(p.stem for p in OUT.glob("*.csv")) if OUT.exists() else []


def latest(back: int = 7) -> str | None:
    """The newest session on disk, fetching backwards until one exists."""
    have = sessions()
    if have:
        return have[-1]
    d = date.today()
    for _ in range(back):
        if d.weekday() < 5 and fetch(d.isoformat()):
            return d.isoformat()
        d -= timedelta(days=1)
    return None


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------
def annotate(symbols: list[str], day: str | None = None) -> dict[str, dict]:
    """Delivery today against the symbol's own 20-session average."""
    day = day or latest()
    if not day:
        return {}
    today = load_day(day)
    if not today:
        today = {}
        if fetch(day):
            today = load_day(day)
    hist = [load_day(d) for d in sessions()[-21:-1]]
    hist = [h for h in hist if h]

    out = {}
    for sym in dict.fromkeys(s.upper() for s in symbols if s):
        row = today.get(sym)
        if not row:
            out[sym] = {}
            continue
        past = [h[sym]["deliv_pct"] for h in hist if sym in h]
        mean = sum(past) / len(past) if past else None
        out[sym] = {
            "day": day,
            "deliv_pct": row["deliv_pct"],
            "series": row["series"],
            "turnover_cr": row["turnover_cr"],
            "avg_pct": round(mean, 1) if mean else None,
            "multiple": round(row["deliv_pct"] / mean, 2) if mean else None,
            "sessions": len(past),
        }
    return out


def verdict(row: dict) -> str:
    """One phrase, in the language a trader actually uses."""
    if not row or row.get("deliv_pct") is None:
        return ""
    pct = row["deliv_pct"]
    mult = row.get("multiple")
    if mult is not None and mult >= 1.6:
        return f"{pct:.0f}% delivery, {mult:.1f}x its average — real accumulation"
    if mult is not None and mult >= 1.2:
        return f"{pct:.0f}% delivery, above its average — buying, not churn"
    if pct < 12:
        return f"{pct:.0f}% delivery — mostly intraday churn"
    return f"{pct:.0f}% delivery" + (f", {mult:.1f}x average" if mult else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma-separated NSE symbols")
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    day = args.date or latest()
    if args.refresh and day:
        _path(day).unlink(missing_ok=True)
        fetch(day)

    if not args.symbols:
        day = latest()
        print(f"latest session on disk: {day}")
        print(f"{len(sessions())} session(s) cached in {OUT.relative_to(REPO)}")
        for d in sessions()[-3:]:
            print(f"  {d}  {len(load_day(d))} symbols")
        return 0

    syms = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    rows = annotate(syms, day)
    for sym, row in rows.items():
        print(f"{sym:14} {verdict(row) or 'no delivery data'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
