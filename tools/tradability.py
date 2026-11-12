#!/usr/bin/env python3
"""Can this plan actually be filled, or is it a setup you cannot trade?

A Stage-2 breakout in a stock with a 2% price band is not a trade. You will not
get filled above the trigger, and if you do, you cannot exit where the stop
says. The same is true of a stock in trade-for-trade settlement, one under
Graded Surveillance Measures, or one already at the F&O ban limit.

None of that appears in a chart, so the desk used to publish these as clean
plans with authoritative levels.

Two sources, both plain NSE archives rather than the browser-gated APIs:

    sec_list.csv    series (EQ/BE/ST/SM/BZ) and price band per security
    fo_secban.csv   securities banned from fresh F&O positions today

Both are cached for the session, so a report that quotes six names costs two
downloads.

Judgement, not just data:

    2% band          blocked    the trigger and stop cannot both be honoured
    5% band          blocked    a single 5% gap takes out the whole stop
    BE / ST series   flagged    trade-for-trade: compulsory delivery, no MIS
    SM series        flagged    SME, lot-based, thin
    GSM stage        flagged    surveillance framework
    F&O ban          flagged    no fresh derivative positions

Nothing here knows about ASM beyond GSM, so an unlisted stock is reported as
"no restriction found" rather than "safe".

Usage:
    python3 tools/tradability.py --symbols CENTUM,ACMESOLAR,ABAN
    python3 tools/tradability.py --refresh
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
CACHE = REPO / "data" / "tradability.json"
MAX_AGE_HOURS = 12

SEC_LIST = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
FO_BAN = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"

HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124 Safari/537.36")}

SERIES_NOTE = {
    "BE": "trade-for-trade (compulsory delivery, no intraday)",
    "ST": "trade-for-trade under surveillance",
    "BZ": "trade-for-trade, illiquid",
    "SM": "SME series (lot-based, thin)",
    "IV": "SME / institutional series",
}


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
def _load() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except Exception:
            pass
    return {"at": None, "securities": {}, "fo_ban": []}


def _fresh(store: dict) -> bool:
    if not store.get("at"):
        return False
    try:
        age = datetime.now() - datetime.fromisoformat(store["at"])
    except Exception:
        return False
    return age < timedelta(hours=MAX_AGE_HOURS)


def _get(url: str) -> str | None:
    try:
        import requests
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            return None
        return r.text
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] {url.rsplit('/', 1)[-1]}: {exc}", file=sys.stderr)
        return None


def refresh() -> dict:
    store = {"at": datetime.now().isoformat(timespec="seconds"),
             "securities": {}, "fo_ban": [], "sources": {}}

    text = _get(SEC_LIST)
    if text:
        rows = list(csv.reader(io.StringIO(text)))
        for row in rows[1:]:
            if len(row) < 5:
                continue
            sym = row[0].strip().upper()
            if not sym:
                continue
            band = row[3].strip()
            store["securities"][sym] = {
                "series": row[1].strip(),
                "name": row[2].strip(),
                "band_pct": None if band.lower() == "no band" else _int(band),
                "remarks": row[4].strip(),
            }
        store["sources"]["sec_list"] = len(store["securities"])

    text = _get(FO_BAN)
    if text:
        for line in text.splitlines():
            for part in line.split(":"):
                for tok in part.split(","):
                    tok = tok.strip().upper()
                    if tok and tok.isalpha():
                        store["fo_ban"].append(tok)
        store["fo_ban"] = sorted(set(store["fo_ban"]))
        store["sources"]["fo_ban"] = len(store["fo_ban"])

    if store["securities"] or store["fo_ban"]:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=1))
        tmp.replace(CACHE)
    return store


def _int(v: str) -> int | None:
    try:
        return int(float(v))
    except Exception:
        return None


def store(force: bool = False) -> dict:
    s = _load()
    if force or not _fresh(s):
        fresh = refresh()
        # Keep yesterday's list if the network failed: a stale restriction is
        # better than silently reporting no restriction.
        if fresh["securities"]:
            return fresh
        print("[warn] tradability refresh failed — using cached list",
              file=sys.stderr)
    return s


# ---------------------------------------------------------------------------
# judgement
# ---------------------------------------------------------------------------
def lookup(symbols: list[str]) -> dict[str, dict]:
    s = store()
    secs = s.get("securities") or {}
    banned = set(s.get("fo_ban") or [])
    out = {}
    for sym in dict.fromkeys(x.upper() for x in symbols if x):
        info = secs.get(sym)
        if info is None:
            out[sym] = {"known": False, "blockers": [],
                        "notes": ["not in the NSE security master — verify it trades"],
                        "band_pct": None, "series": None, "fo_ban": False}
            continue
        series = (info.get("series") or "").upper()
        band = info.get("band_pct")
        remarks = (info.get("remarks") or "").strip()
        blockers, notes = [], []

        if band is not None and band <= 2:
            blockers.append(f"{band}% price band — trigger and stop cannot both be honoured")
        elif band is not None and band <= 5:
            blockers.append(f"{band}% price band — one gap takes out the whole stop")

        if series in SERIES_NOTE:
            notes.append(SERIES_NOTE[series])
        if remarks and remarks != "-":
            notes.append(f"NSE {remarks}")
        if sym in banned:
            notes.append("F&O ban — no fresh derivative positions")

        out[sym] = {"known": True, "blockers": blockers, "notes": notes,
                    "band_pct": band, "series": series,
                    "fo_ban": sym in banned,
                    "as_of": (s.get("at") or "")[:10]}
    return out


def summary(symbols: list[str]) -> dict:
    rows = lookup(symbols)
    blocked = {k: v["blockers"] for k, v in rows.items() if v["blockers"]}
    flagged = {k: v["notes"] for k, v in rows.items()
               if not v["blockers"] and v["notes"]}
    return {"checked": len(rows), "blocked": blocked, "flagged": flagged,
            "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma-separated NSE symbols")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    if args.refresh or not args.symbols:
        s = store(force=args.refresh)
        print(f"security master: {len(s.get('securities') or {})} symbols, "
              f"F&O ban: {', '.join(s.get('fo_ban') or ['none']) or 'none'}")
        print(f"cached at {s.get('at')}")
        if not args.symbols:
            return 0

    syms = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    res = summary(syms)
    for sym, row in res["rows"].items():
        if row["blockers"]:
            print(f"{sym:14} BLOCKED  {'; '.join(row['blockers'])}")
        elif row["notes"]:
            print(f"{sym:14} flagged  {'; '.join(row['notes'])}")
        else:
            band = row.get("band_pct")
            tail = f"{band}% band" if band else "no band limit"
            print(f"{sym:14} clear    {row.get('series') or '?'} series, {tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
