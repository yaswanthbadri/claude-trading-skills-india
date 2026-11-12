#!/usr/bin/env python3
"""Download and maintain a local OHLCV cache for the whole universe.

The scanner reads CSVs, not the network, so a scan of 2,400 symbols costs disk
reads instead of 2,400 HTTP calls. This script is the only thing that touches
yfinance, and it is incremental: it asks only for the bars it does not have.

Layout
    data/candles/SYMBOL.csv     Date,Open,High,Low,Close,Volume
    data/nifty.csv              benchmark series (NIFTY 50)
    data/cache_state.json       last successful run per symbol

Usage:
    python3 tools/build_cache.py                  # update everything
    python3 tools/build_cache.py --full           # rebuild from scratch
    python3 tools/build_cache.py --limit 200      # smoke test
    python3 tools/build_cache.py --years 5
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
CANDLES = DATA / "candles"
BENCH = DATA / "nifty.csv"
STATE = DATA / "cache_state.json"

CHUNK = 70          # tickers per yfinance request
COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"symbols": {}, "last_run": None}


def _save_state(s: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(STATE)


def _ticker(sym: str, exchange: str) -> str:
    if sym.endswith(".BO") or sym.startswith("^"):
        return sym
    return f"{sym}.NS"


def _last_date(path: Path):
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400))
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
        if not tail:
            return None
        return datetime.strptime(tail[-1].split(",")[0], "%Y-%m-%d").date()
    except Exception:
        return None


def _write_atomic(path: Path, frame) -> None:
    tmp = path.with_suffix(".tmp")
    frame.to_csv(tmp, index=False, date_format="%Y-%m-%d")
    tmp.replace(path)


def _merge(path: Path, new_frame) -> int:
    """Append/replace rows in an existing CSV. Returns total rows."""
    import pandas as pd

    new_frame = new_frame.copy()
    new_frame["Date"] = pd.to_datetime(new_frame["Date"]).dt.strftime("%Y-%m-%d")
    if path.exists():
        old = pd.read_csv(path)
        old["Date"] = pd.to_datetime(old["Date"]).dt.strftime("%Y-%m-%d")
        merged = (pd.concat([old, new_frame])
                  .drop_duplicates(subset="Date", keep="last")
                  .sort_values("Date"))
    else:
        merged = new_frame.sort_values("Date")
    merged = merged[COLUMNS]
    _write_atomic(path, merged)
    return len(merged)


def _download(chunk: list[dict], years: int, full: bool):
    """Return {symbol: DataFrame} for one chunk."""
    import yfinance as yf

    tickers = [_ticker(r["symbol"], r["exchange"]) for r in chunk]
    kwargs = dict(interval="1d", progress=False, auto_adjust=False,
                  threads=True, group_by="ticker")

    if not full:
        # Ask only for what is missing: the earliest gap in the chunk.
        starts = []
        for r in chunk:
            ld = _last_date(CANDLES / f"{r['symbol']}.csv")
            starts.append(ld or (datetime.now().date() - timedelta(days=365 * years)))
        start = min(starts)
        if start >= datetime.now().date():
            return {}
        kwargs["start"] = start.isoformat()
    else:
        kwargs["period"] = f"{years}y"

    df = yf.download(tickers, **kwargs)
    out = {}
    for r, t in zip(chunk, tickers):
        try:
            sub = df[t] if len(tickers) > 1 else df
            sub = sub[COLUMNS[1:]].dropna(how="all")
            sub = sub.dropna(subset=["Close"])
            if len(sub):
                sub = sub.reset_index()
                sub.columns = COLUMNS[:len(sub.columns)]
                out[r["symbol"]] = sub
        except Exception:
            continue
    return out


def update(years: int = 3, full: bool = False, limit: int | None = None,
           sleep: float = 0.25) -> dict:
    warnings.filterwarnings("ignore")
    import universe

    CANDLES.mkdir(parents=True, exist_ok=True)
    rows = universe.load()
    if limit:
        rows = rows[:limit]

    state = _load_state()
    t0 = time.time()
    written = skipped = failed = up_to_date = 0

    for i in range(0, len(rows), CHUNK):
        chunk = rows[i:i + CHUNK]
        try:
            frames = _download(chunk, years, full)
        except Exception as e:
            print(f"[chunk {i}-{i+len(chunk)}] download failed: {e}", file=sys.stderr)
            failed += len(chunk)
            continue

        for r in chunk:
            sym = r["symbol"]
            frame = frames.get(sym)
            path = CANDLES / f"{sym}.csv"
            if frame is None or len(frame) == 0:
                # Existing file that is already current is not a failure.
                ld = _last_date(path)
                if ld and (datetime.now().date() - ld).days <= 4:
                    up_to_date += 1
                else:
                    skipped += 1
                continue
            try:
                n = _merge(path, frame)
                state["symbols"][sym] = {
                    "rows": n,
                    "last_date": _last_date(path).isoformat() if _last_date(path) else None,
                    "updated": datetime.now().isoformat(timespec="seconds"),
                }
                written += 1
            except Exception as e:
                failed += 1
                print(f"[{sym}] write failed: {e}", file=sys.stderr)

        done = min(i + CHUNK, len(rows))
        rate = done / max(time.time() - t0, 0.1)
        print(f"  {done}/{len(rows)}  ({rate:.0f}/s)  new {written}  "
              f"current {up_to_date}  no-data {skipped}  failed {failed}", flush=True)
        time.sleep(sleep)

    _save_benchmark(years)

    # A bad price jump corrupts every indicator downstream and nothing
    # complains, so the cache is checked after every build: spurious prints
    # dropped, confirmed splits rescaled, unverifiable breaks truncated.
    try:
        import integrity
        rep = integrity.repair(be_quiet=True)
        if rep["spurious_dropped"] or rep["truncated"] or rep["breaks"]:
            print(f"  integrity: {rep['spurious_dropped']} bad bar(s) dropped, "
                  f"{rep['truncated']} series truncated, "
                  f"{len(rep['breaks'])} break(s) left")
    except Exception as e:  # never fail the build over the check
        print(f"[integrity] check failed: {e}", file=sys.stderr)

    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    _save_state(state)

    return {"written": written, "up_to_date": up_to_date, "skipped": skipped,
            "failed": failed, "seconds": round(time.time() - t0, 1),
            "total": len(rows)}


def _save_benchmark(years: int = 3) -> None:
    try:
        import pandas as pd
        import yfinance as yf

        df = yf.download("^NSEI", period=f"{years}y", interval="1d",
                         progress=False, auto_adjust=False, group_by="ticker")
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.droplevel(0)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
        _write_atomic(BENCH, df.reset_index().assign(
            Date=lambda d: pd.to_datetime(d["Date"]).dt.strftime("%Y-%m-%d")))
    except Exception as e:
        print(f"[benchmark] failed: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--full", action="store_true", help="rebuild from scratch")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if args.full and CANDLES.exists():
        for f in CANDLES.glob("*.csv"):
            f.unlink()

    r = update(years=args.years, full=args.full, limit=args.limit)
    print(f"\ncache: {r['written']} updated, {r['up_to_date']} already current, "
          f"{r['skipped']} no-data, {r['failed']} failed, {r['seconds']}s "
          f"for {r['total']} symbols")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
