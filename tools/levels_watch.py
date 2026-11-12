#!/usr/bin/env python3
"""Watch the positions we told you to take, and say when a level breaks.

Every other alert in this system is about a stock you may not own. This one is
about money already at risk: the plans recorded in data/recommendations.json
that have filled and are still open.

What it watches, in priority order
    STOP    last price at or below the stop        -> get out, thesis is wrong
    T2      last price at or above the second target
    T1      last price at or above the first target
    ENTRY   an unfilled trigger being cleared      -> the trade is now live

Why it matters: the bulletin runs hourly, so without this a stop breach at
11:20 reaches you at 12:15. By then the stop is not a decision, it is a story.

Prices come from 5-minute bars, so "last" is the latest trade rather than
yesterday's close. If a symbol cannot be quoted it is skipped and named in the
log — a stop is never checked against stale data.

Once a level has fired it stays quiet for that symbol until the next session.

Usage:
    python3 tools/levels_watch.py --dry-run
    python3 tools/levels_watch.py             # send
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_calendar as cal
import recs

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / "logs" / "levels_state.json"

MAX_PER_RUN = 4          # a strong day must not turn into fourteen messages
OPEN_STATES = ("filled", "t1_hit")


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"date": None, "fired": {}}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    s["date"] = date.today().isoformat()
    # Keep only today's firings: the file exists to stop repeats, not to
    # become a history (logs/alerts.jsonl is the history).
    STATE.write_text(json.dumps(s, indent=1))


def _fresh(state: dict) -> dict:
    today = date.today().isoformat()
    if state.get("date") != today:
        return {"date": today, "fired": {}}
    state.setdefault("fired", {})
    return state


# ---------------------------------------------------------------------------
# prices
# ---------------------------------------------------------------------------
def live_prices(symbols: list[str]) -> dict[str, float]:
    """Latest traded price per symbol, from 5-minute bars."""
    if not symbols:
        return {}
    try:
        import yfinance as yf
    except Exception:
        return {}

    tickers = [f"{s}.NS" for s in symbols]
    try:
        df = yf.download(tickers, period="2d", interval="5m", progress=False,
                         auto_adjust=False, threads=True, group_by="ticker")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] intraday download failed: {exc}", file=sys.stderr)
        return {}
    if df is None or getattr(df, "empty", True):
        return {}

    cols = df.columns
    multi = hasattr(cols, "nlevels") and cols.nlevels > 1
    ticker_level = list(cols.get_level_values(0)) if multi else []

    out: dict[str, float] = {}
    for sym, t in zip(symbols, tickers):
        try:
            block = df[t] if (multi and t in ticker_level) else df
            close = block["Close"]
            if hasattr(close, "columns"):        # still 2-D: take the only column
                close = close.iloc[:, 0]
            close = close.dropna()
            if len(close):
                out[sym] = float(close.iloc[-1])
        except Exception:
            continue
    missing = [s for s in symbols if s not in out]
    if missing:
        print(f"[warn] no intraday quote for {', '.join(missing)} — levels unchecked",
              file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# level logic
# ---------------------------------------------------------------------------
def open_positions() -> list[dict]:
    """Records that deserve watching today: filled, or waiting on a trigger."""
    today = date.today().isoformat()
    out = []
    for r in recs.load()["records"]:
        if r.get("status") in ("stopped", "t2_hit", "no_fill", "expired"):
            continue
        if (r.get("live_from") or r.get("date") or "") > today:
            continue
        if r.get("status") not in OPEN_STATES + ("pending_fill",):
            continue
        out.append(r)
    return out


def _r_now(rec: dict, last: float) -> float | None:
    fill, stop = rec.get("fill_price") or rec.get("entry"), rec.get("stop")
    if not fill or not stop or fill <= stop:
        return None
    return (last - fill) / (fill - stop)


def _candidate(rec: dict, last: float) -> tuple[str, str, str] | None:
    """(level, headline, why) for the most important level this price breaks."""
    sym = rec["symbol"]
    stop, t1, t2 = rec.get("stop"), rec.get("t1"), rec.get("t2")
    entry = rec.get("entry")
    status = rec.get("status")
    r_now = _r_now(rec, last)
    r_txt = f"{r_now:+.1f}R" if r_now is not None else "n/a"

    if status in OPEN_STATES and stop and last <= stop:
        return ("stop",
                f"🛑 <b>{sym} · below the stop</b>",
                f"Last <b>{last:,.2f}</b> · stop {stop:,.2f} · position is "
                f"{r_txt}. The stop is where the thesis was wrong — take it, "
                f"do not average down.")
    if status in OPEN_STATES and t2 and last >= t2:
        return ("t2",
                f"🏁 <b>{sym} · second target reached</b>",
                f"Last <b>{last:,.2f}</b> · T2 {t2:,.2f} · {r_txt}. Both targets "
                f"are done; the trade is finished, so trail the rest or book it.")
    if status in OPEN_STATES and t1 and last >= t1:
        return ("t1",
                f"🎯 <b>{sym} · first target reached</b>",
                f"Last <b>{last:,.2f}</b> · T1 {t1:,.2f} · {r_txt}. Bank the first "
                f"leg and move the stop to entry — the rest is free carry.")
    if status == "pending_fill" and entry and last >= entry:
        return ("entry",
                f"✅ <b>{sym} · trigger cleared</b>",
                f"Last <b>{last:,.2f}</b> · trigger {entry:,.2f}. The plan is "
                f"live now. Stop {stop:,.2f} is the only level that matters.")
    return None


def scan(dry: bool = False) -> list[str]:
    if not cal.is_market_open():
        return []

    state = _fresh(load_state())
    fired = state["fired"]
    positions = open_positions()
    if not positions:
        return []

    prices = live_prices([r["symbol"] for r in positions])
    stamp = datetime.now().strftime("%d %b %Y, %H:%M IST")

    # Stop breaches first: those look at price, so gather then sort by severity.
    hits = []
    for rec in positions:
        last = prices.get(rec["symbol"])
        if last is None:
            continue
        cand = _candidate(rec, last)
        if not cand:
            continue
        level, head, why = cand
        key = f"{rec['symbol']}:{level}"
        if key in fired:
            continue
        hits.append((level, rec, last, head, why, key))

    order = {"stop": 0, "t2": 1, "t1": 2, "entry": 3}
    hits.sort(key=lambda h: order.get(h[0], 9))

    sent = []
    for level, rec, last, head, why, key in hits:
        if len(sent) >= MAX_PER_RUN:
            print(f"  … {len(hits) - len(sent)} more level alert(s) held for "
                  f"the next run")
            break
        body = (f"{head}\n<i>{stamp}</i>\n───────────────\n{why}\n\n"
                f"<i>Educational only — not advice.</i>")
        if dry:
            print("\n----- DRY RUN -----")
            print(body.replace("<b>", "").replace("</b>", "")
                      .replace("<i>", "").replace("</i>", ""))
            sent.append(key)
            continue
        import notify
        import buttons
        try:
            res = notify.send(body, kind="level",
                              reply_markup=buttons.levels(rec["symbol"]))
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] send failed: {exc}", file=sys.stderr)
            break
        if res.get("sent"):
            fired[key] = datetime.now().isoformat(timespec="seconds")
            sent.append(key)
            print(f"  → {rec['symbol']} · {level} @ {last:,.2f}")

    if not dry:
        save_state(state)
    return sent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not cal.is_market_open() and not args.dry_run:
        print("market closed — level watch idle")
        return 0
    sent = scan(dry=args.dry_run)
    print(f"{len(sent)} level alert(s)" if sent else "no levels broken")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
