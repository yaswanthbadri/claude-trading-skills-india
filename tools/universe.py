#!/usr/bin/env python3
"""Build the tradable universe: every NSE mainboard symbol, plus optional BSE.

Why a file instead of a hard-coded list: NSE adds and delists names every
month, and a stale watchlist silently drops the one stock that matters.

NSE  https://archives.nseindia.com/content/equities/EQUITY_L.csv  (series EQ)
BSE  the exchange's list API sits behind Akamai bot protection and returns 403,
     so BSE-only names are only covered when you supply a CSV yourself with
     columns: scrip_code, scrip_name (see --bse-csv). Dual-listed companies are
     already in the NSE list and are scanned under their NSE symbol.

Usage:
    python3 tools/universe.py                # rebuild and print a summary
    python3 tools/universe.py --bse-csv ~/Downloads/bse_list.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
OUT = DATA / "universe.csv"

NSE_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
# Industry labels for the liquid names. Not every symbol is in the index, so
# anything missing simply carries no sector rather than a guessed one.
IND_URL = "https://niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# Symbols that are not ordinary equity or break yfinance tickers.
SKIP_SERIES = {"BE", "BZ", "EQ_SME", "ST", "SM", "IV"}
# '&' used to be excluded here, which quietly dropped M&M (a Nifty 50 name),
# M&MFIN, ARE&M, GMRP&UI and others from a "full universe" scan. Both yfinance
# and this filesystem handle it, so only characters that break a path or a URL
# are rejected now.
BAD_CHARS = set("/ .")


def fetch_nse() -> list[dict]:
    req = urllib.request.Request(NSE_URL, headers=UA)
    raw = urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace")
    rows = []
    for raw_row in csv.DictReader(io.StringIO(raw)):
        r = {k.strip(): v for k, v in raw_row.items() if k}
        sym = (r.get("SYMBOL") or "").strip().upper()
        series = (r.get("SERIES") or "").strip().upper()
        name = (r.get("NAME OF COMPANY") or "").strip()
        isin = (r.get("ISIN NUMBER") or "").strip()
        if not sym or series != "EQ":
            continue
        if sym in SKIP_SERIES or any(c in sym for c in BAD_CHARS):
            continue
        rows.append({"symbol": sym, "name": name, "isin": isin,
                     "series": series, "exchange": "NSE"})
    return rows


def fetch_industries() -> dict[str, str]:
    """symbol -> industry, from the NSE total-market index constituents."""
    try:
        req = urllib.request.Request(IND_URL, headers=UA)
        raw = urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] industry list unavailable ({e}); sectors will be blank",
              file=sys.stderr)
        return {}
    out = {}
    for raw_row in csv.DictReader(io.StringIO(raw)):
        r = {k.strip(): (v or "").strip() for k, v in raw_row.items() if k}
        sym = r.get("Symbol", "").upper()
        ind = r.get("Industry", "")
        if sym and ind:
            out[sym] = ind
    return out


def fetch_bse(path: Path) -> list[dict]:
    """Optional: a BSE scrip list you download yourself."""
    out = []
    if not path or not path.exists():
        return out
    for r in csv.DictReader(path.open()):
        code = (r.get("scrip_code") or r.get("Security Code") or "").strip()
        name = (r.get("scrip_name") or r.get("Security Name") or "").strip()
        if not code.isdigit():
            continue
        out.append({"symbol": f"{code}.BO", "name": name, "isin": "",
                    "series": "BSE", "exchange": "BSE"})
    return out


def build(bse_csv: Path | None = None) -> list[dict]:
    rows = fetch_nse()
    industry = fetch_industries()
    for r in rows:
        r["industry"] = industry.get(r["symbol"].upper(), "")
    if bse_csv:
        rows += fetch_bse(bse_csv)
    DATA.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=["symbol", "name", "industry", "isin", "series", "exchange"],
            extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return rows


def load() -> list[dict]:
    """Load the cached universe, building it once if it is missing."""
    if not OUT.exists():
        return build()
    with OUT.open() as f:
        return list(csv.DictReader(f))


def symbols() -> list[str]:
    return [r["symbol"] for r in load()]


def sector_map() -> dict[str, str]:
    """symbol -> industry, tolerant of an older universe file with no column."""
    return {r["symbol"]: (r.get("industry") or "")
            for r in load() if r.get("symbol")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bse-csv", type=Path, default=None)
    args = ap.parse_args()
    rows = build(args.bse_csv)
    nse = sum(1 for r in rows if r["exchange"] == "NSE")
    bse = len(rows) - nse
    print(f"{len(rows)} symbols ({nse} NSE, {bse} BSE) -> {OUT}")
    print("first five:", ", ".join(r["symbol"] for r in rows[:5]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
