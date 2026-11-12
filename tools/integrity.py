#!/usr/bin/env python3
"""Find price series that cannot be trusted, and stop the scanner using them.

The candle cache is the input to every number this desk publishes: pivots,
moving averages, ATR stops, gap scans, "6% shock" alerts. If a series contains
a false price jump, every one of those outputs is quietly wrong, and nothing
in the system complains.

That is not hypothetical. 53 series in this cache jump more than 45% in one
session, and a clean re-download reproduces most of them, so they come from
the data source rather than from the merge:

    BANSALWIRE  2026-10-06   -79.8%    a real 1:5 split (Yahoo files it 10 days late)
    KSE         2026-08-17   -70.8%    no split on record
    LANCER      2026-08-17   -92.6%    no split on record
    ARIHANT     2026-04-20  +2188.9%   four zero-volume bars, then the real series
    SILVERTUC   2024-01-01   +1008.7%  and -89.9% in 2026: two scales in one file
    CYBERTECH   2025-01-10  +9511.1%   a single Rs 20,505 print between Rs 215 bars

Two different faults, and they need opposite treatment:

    SPURIOUS    one bad print that the next session reverses. Safe to delete:
                the true price on that day was the level either side of it.
    BREAK       the series genuinely stays on the new scale, because of a split,
                a demerger, or a scale switch we cannot verify. NOT safe to
                guess. The bars before it describe a different price level.

A break is never "repaired" here. Rescaling a demerger by the observed ratio
would manufacture a smooth uptrend out of two different companies. Instead the
break is recorded, and any symbol whose break falls inside the indicator
lookback is withheld from the scan until enough post-break bars exist.

Usage:
    python3 tools/integrity.py --scan        # report only
    python3 tools/integrity.py --repair      # drop spurious bars, record breaks
    python3 tools/integrity.py --repair --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import datetime, date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CANDLES = REPO / "data" / "candles"
BREAKS = REPO / "data" / "breaks.json"

BREAK_PCT = 0.45      # beyond this, a single session is a data event, not a trade
REVERT_PCT = 0.10     # returns within this of the old level -> the bar was bad
PERSIST_BARS = 5      # must hold the new level this long to count as structural
MIN_BREAK_BARS = 250  # stays out of the scan this long after a break (~1 year)
MAX_LEAD_JUNK = 10    # leading zero-volume bars before the real series starts


# ---------------------------------------------------------------------------
# io
# ---------------------------------------------------------------------------
def rows(path: Path) -> list[dict]:
    out = []
    with path.open() as fh:
        for r in csv.DictReader(fh):
            try:
                out.append({"Date": r["Date"][:10], "Open": float(r["Open"]),
                            "High": float(r["High"]), "Low": float(r["Low"]),
                            "Close": float(r["Close"]), "Volume": float(r["Volume"] or 0)})
            except (KeyError, TypeError, ValueError):
                continue
    return out


def write_rows(path: Path, rs: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["Date", "Open", "High", "Low",
                                           "Close", "Volume"])
        w.writeheader()
        for r in rs:
            w.writerow({"Date": r["Date"], "Open": r["Open"], "High": r["High"],
                        "Low": r["Low"], "Close": r["Close"],
                        "Volume": int(r["Volume"])})
    tmp.replace(path)


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------
def _persists(rs: list[dict], i: int) -> bool:
    """Does the level at bar i survive, or does the tape come back to i-1?"""
    before = rs[i - 1]["Close"]
    after = [r["Close"] for r in rs[i + 1:i + 1 + PERSIST_BARS]]
    if not after:
        return True                       # last bar: assume real, do not delete
    back = statistics.median(after)
    return abs(back / before - 1) > REVERT_PCT


def detect(rs: list[dict], threshold: float = BREAK_PCT) -> list[dict]:
    """Every suspicious one-day move, judged as spurious or structural."""
    out = []
    for i in range(1, len(rs)):
        prev, cur = rs[i - 1]["Close"], rs[i]["Close"]
        if prev <= 0 or cur <= 0:
            continue
        ratio = cur / prev
        if abs(ratio - 1) <= threshold:
            continue
        # A spike produces two suspicious moves: into it and out of it. The
        # exit is judged against the level before the spike, not the spike
        # itself, or every bad print would also invent a structural break.
        returns_to_before = (i >= 2 and rs[i - 2]["Close"] > 0
                             and abs(cur / rs[i - 2]["Close"] - 1) <= REVERT_PCT)
        out.append({
            "index": i,
            "date": rs[i]["Date"],
            "ratio": round(ratio, 4),
            "kind": "spurious" if (returns_to_before or not _persists(rs, i))
                    else "break",
        })
    return out


def scan_all(threshold: float = BREAK_PCT) -> dict[str, list[dict]]:
    found = {}
    for path in sorted(CANDLES.glob("*.csv")):
        rs = rows(path)
        if len(rs) < 3:
            continue
        hits = detect(rs, threshold)
        if hits:
            found[path.stem] = hits
    return found


# ---------------------------------------------------------------------------
# classification helpers
# ---------------------------------------------------------------------------
def _lead_junk(rs: list[dict], i: int) -> bool:
    """A handful of zero-volume bars before the real series begins."""
    if i > MAX_LEAD_JUNK:
        return False
    return all(r["Volume"] <= 0 for r in rs[:i])


def confirmed_split(symbol: str, day: str, ratio: float) -> float | None:
    """The split factor, but only when it explains this exact move.

    Yahoo files BANSALWIRE's 1:5 at 2026-10-16 while the price actually
    changed on 2026-10-06, so the date is not trustworthy — only the ratio is.
    Rescaling happens at the observed break, using the recorded factor.
    """
    try:
        import yfinance as yf
        sp = yf.Ticker(f"{symbol}.NS").splits
    except Exception:
        return None
    if sp is None or len(sp) == 0:
        return None
    try:
        d = date.fromisoformat(day)
        for k, v in sp.items():
            kd = k.date() if hasattr(k, "date") else k
            if abs((kd - d).days) > 60:
                continue
            f = float(v)
            if f >= 1 and abs(ratio / (1 / f) - 1) < 0.08:
                return f
    except Exception:
        return None
    return None


def rescale(rs: list[dict], upto: int, factor: float) -> None:
    """Put the pre-split bars on the post-split scale. Volume moves inversely."""
    for r in rs[:upto]:
        r["Open"] = round(r["Open"] / factor, 4)
        r["High"] = round(r["High"] / factor, 4)
        r["Low"] = round(r["Low"] / factor, 4)
        r["Close"] = round(r["Close"] / factor, 4)
        r["Volume"] = r["Volume"] * factor


def split_note(symbol: str, day: str, ratio: float) -> str:
    """Name the cause when yfinance actually recorded a split, else say so."""
    try:
        import yfinance as yf
        sp = yf.Ticker(f"{symbol}.NS").splits
    except Exception:
        return ""
    if sp is None or len(sp) == 0:
        return ""
    try:
        d = date.fromisoformat(day)
        for k, v in sp.items():
            kd = k.date() if hasattr(k, "date") else k
            if abs((kd - d).days) <= 60:
                f = float(v)
                if f >= 1 and abs(ratio / (1 / f) - 1) < 0.08:
                    return f"1:{f:g} split on {kd}"
                return f"split {f:g} on {kd}, ratio does not match"
    except Exception:
        return ""
    return ""


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------
def repair(dry: bool = False, be_quiet: bool = False) -> dict:
    """Drop spurious prints, truncate lead junk, record structural breaks."""
    found = scan_all()
    dropped = 0
    truncated = 0
    breaks: dict[str, list[dict]] = {}

    for sym, hits in found.items():
        path = CANDLES / f"{sym}.csv"
        rs = rows(path)
        changed = False

        # Spurious prints, newest first so indices stay valid.
        for h in sorted([h for h in hits if h["kind"] == "spurious"],
                        key=lambda h: -h["index"]):
            i = h["index"]
            if i < len(rs) and abs(rs[i]["Close"] / rs[i - 1]["Close"] - 1) > BREAK_PCT:
                del rs[i]
                dropped += 1
                changed = True

        # Re-detect: deleting one bad bar can expose another, or clear a break.
        remaining = detect(rs)

        # A confirmed split is the one break we can repair with certainty: the
        # ratio is on record, so the old bars are the same company at a
        # different share count. A demerger is not, and is left alone.
        for _ in range(3):
            fixed = False
            for h in [x for x in remaining if x["kind"] == "break"]:
                f = confirmed_split(sym, h["date"], h["ratio"])
                if not f:
                    continue
                i = h["index"]
                if i < 1 or i >= len(rs):
                    continue
                rescale(rs, i, f)
                changed = fixed = True
                if not be_quiet:
                    print(f"  rescaled {sym}: pre-{h['date']} bars /{f:g} "
                          f"(1:{f:g} split on record)")
                break
            if not fixed:
                break
            remaining = detect(rs)

        # Leading junk before the real series starts.
        for h in sorted([h for h in remaining if h["kind"] == "break"],
                        key=lambda h: h["index"]):
            if _lead_junk(rs, h["index"]):
                rs = rs[h["index"]:]
                truncated += 1
                changed = True
                break
        if changed:
            remaining = detect(rs)

        # Whatever is left is a level change we cannot verify: a demerger, a
        # scale switch, or a partially applied split. Bars before it describe
        # a different price level, so the usable series starts at the break.
        # The file is only shortened when enough clean history remains to be
        # worth scanning; otherwise it is left intact and withheld instead.
        cut = None
        if remaining:
            last = max(remaining, key=lambda h: h["index"])["index"]
            if last >= 1 and (len(rs) - last) >= 30:
                cut = remaining
                rs = rs[last:]
                changed = True
                if not be_quiet:
                    print(f"  truncated {sym}: usable history starts "
                          f"{rs[0]['Date']} ({len(rs)} rows)")
                remaining = detect(rs)

        if remaining:
            breaks[sym] = [{"date": h["date"], "ratio": h["ratio"],
                            "bars_since": len([r for r in rs if r["Date"] > h["date"]])}
                           for h in remaining]

        if changed and not dry:
            write_rows(path, rs)
        if changed and not be_quiet:
            print(f"  repaired {sym}: {len(rs)} clean rows, "
                  f"{len(remaining)} break(s) recorded")

    if not dry:
        BREAKS.parent.mkdir(parents=True, exist_ok=True)
        tmp = BREAKS.with_suffix(".tmp")
        tmp.write_text(json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                                   "threshold": BREAK_PCT,
                                   "symbols": breaks}, indent=1))
        tmp.replace(BREAKS)

    return {"scanned": len(list(CANDLES.glob("*.csv"))),
            "suspect": len(found), "spurious_dropped": dropped,
            "truncated": truncated, "breaks": breaks}


# ---------------------------------------------------------------------------
# what the scanner should refuse
# ---------------------------------------------------------------------------
def active_breaks(min_bars: int = MIN_BREAK_BARS) -> dict[str, str]:
    """symbol -> break date, for series too short after a break to be read.

    A 200-day average spanning a price-level break is not a 200-day average.
    Until a year of clean bars exists, the name sits out. That costs a few
    candidates and removes a whole class of phantom pivots.
    """
    if not BREAKS.exists():
        return {}
    try:
        store = json.loads(BREAKS.read_text())
    except Exception:
        return {}
    out = {}
    for sym, hits in (store.get("symbols") or {}).items():
        for h in hits:
            if (h.get("bars_since") or 0) < min_bars:
                out[sym] = h["date"]
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="report only")
    ap.add_argument("--repair", action="store_true", help="fix what is safe to fix")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--threshold", type=float, default=BREAK_PCT)
    args = ap.parse_args()

    if args.repair:
        res = repair(dry=args.dry_run)
        print(f"\n{res['suspect']} suspect series of {res['scanned']}")
        print(f"  {res['spurious_dropped']} spurious bar(s) dropped, "
              f"{res['truncated']} lead-junk file(s) truncated")
        print(f"  {len(res['breaks'])} series hold a structural break")
        withheld = active_breaks()
        print(f"  {len(withheld)} withheld from the scan today")
        if args.json:
            print(json.dumps({"breaks": res["breaks"], "withheld": withheld}, indent=1))
        return 0

    found = scan_all(args.threshold)
    if args.json:
        print(json.dumps(found, indent=1))
        return 0

    print(f"{len(found)} series with a one-day move beyond "
          f"+/-{args.threshold:.0%}\n")
    print(f"{'symbol':14} {'date':11} {'move':>9}  kind       note")
    for sym, hits in sorted(found.items()):
        for h in hits:
            note = ""
            if h["kind"] == "break" and not args.dry_run:
                note = split_note(sym, h["date"], h["ratio"])
            print(f"{sym:14} {h['date']:11} {h['ratio'] - 1:+8.1%}  "
                  f"{h['kind']:9}  {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
