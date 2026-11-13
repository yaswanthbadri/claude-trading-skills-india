#!/usr/bin/env python3
"""Is this suggestion real, or is it a level the market left behind?

A suggestion is only useful if a person could act on it today. Three things
make a suggestion useless, and all three have happened here:

    unreachable   the trigger is 12% above the price. Nobody is filling it,
                  and the stop printed underneath it is above the market, so
                  the "risk" is fiction.
    stale         the pivot was cleared a week ago. The card still says "buy
                  above X" for a stock trading 17% higher.
    failed        the trigger was touched on the 6th, the stock fell back
                  below it, and the card still presents it as a setup.

The old code ranked candidates and published the best of them. Ranking is not
filtering: a grade-C stock in a downtrend 12% below its pivot can outrank
nothing and still get published. This module is the filter, and it is used by
the report, the /check card and the alert path alike — one rule, so a stock
cannot be BUY-READY in a chat and WATCH in the report.

    python3 tools/plan_gate.py CENTUM
    python3 tools/plan_gate.py --json CENTUM
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

CHASE_PCT = 3.0            # the most you pay over the pivot to still call it a setup
MAX_FROM_PIVOT_PCT = 1.5   # above this the breakout is chased, not bought
GOOD_GRADES = ("A", "B")
LOOKBACK_SESSIONS = 25     # how far back to look for a failed trigger
RECENT_SESSIONS = 5        # a failure inside this window is fresh news


def _rows(symbol: str) -> list[dict]:
    try:
        import tech_detail
        return tech_detail.load_candles(symbol)
    except Exception:
        return []


def failed_trigger(symbol: str, trigger: float, rows: list[dict] | None = None) -> dict:
    """Did the trigger already get touched and fail?

    A day whose high reached the trigger but whose close fell back below it is
    a failed breakout. That is not a setup any more — it is a memory of one.
    """
    rows = rows if rows is not None else _rows(symbol)
    if not trigger or len(rows) < 3:
        return {}
    window = rows[-LOOKBACK_SESSIONS:-1]      # yesterday and back, not today
    for r in window:
        if r["high"] >= trigger and r["close"] < trigger:
            return {"date": r["date"], "high": round(r["high"], 2),
                    "close": round(r["close"], 2),
                    "text": (f"the trigger at {trigger:,.2f} was touched on "
                             f"{r['date']} and the stock closed back below it "
                             f"at {r['close']:,.2f}")}
    return {}


def passed_trigger(symbol: str, trigger: float, rows: list[dict] | None = None) -> dict:
    """Did the trigger get cleared and hold? Then the setup ran without us.

    The first close above the trigger is the breakout day, not the last one.
    """
    rows = rows if rows is not None else _rows(symbol)
    if not trigger or len(rows) < 3:
        return {}
    for r in rows[-LOOKBACK_SESSIONS:-1]:
        if r["close"] > trigger:
            return {"date": r["date"], "close": round(r["close"], 2),
                    "text": (f"it cleared {trigger:,.2f} on {r['date']} and "
                             "closed above it — that trade is over")}
    return {}


def last_clear_and_fail(rows: list[dict], trigger: float) -> dict:
    """The most recent time the trigger was cleared, and the most recent
    failure, so the gate can tell a fresh failure from an old scar."""
    cleared = None
    failed = None
    cleared_i = failed_i = None
    window = rows[-LOOKBACK_SESSIONS:]
    # Today counts. A stock that opened above the trigger this morning and
    # closed back below it has just failed, and the reader needs to know.
    # Backwards, so what we keep is the most recent of each.
    for i in range(len(window) - 1, -1, -1):
        r = window[i]
        if cleared is None and r["close"] > trigger:
            cleared, cleared_i = r, i
        if failed is None and r["high"] >= trigger and r["close"] < trigger:
            failed, failed_i = r, i
    last = len(window) - 1
    return {"cleared": cleared, "failed": failed,
            "cleared_age": (last - cleared_i) if cleared_i is not None else None,
            "failed_age": (last - failed_i) if failed_i is not None else None}


BREAKOUT_KINDS = ("breakout", "continuation")
PULLBACK_KINDS = ("pullback", "retest")
MAX_PULLBACK_BELOW_PCT = 15.0   # further than this is not a pullback, it is a collapse


def gate(plan: dict, symbol: str, close: float | None = None,
         stage2: bool | None = None, base_grade: str | None = None,
         from_pivot_pct: float | None = None,
         rows: list[dict] | None = None, kind: str | None = None) -> dict:
    """Decide whether a plan may be published, and say why if not.

    `plan` needs: trigger, limit, stop. The optional arguments come from the
    chart read; when they are missing the gate checks only what it can.

    The kind matters. A breakout trigger is supposed to sit just above the
    price; a pullback entry is supposed to sit below it. Applying the
    breakout rule to a pullback would reject every pullback there is.
    """
    reasons: list[str] = []
    notes: list[str] = []
    if not plan or not plan.get("trigger"):
        return {"ok": False, "verdict": "NO TRADE", "reasons": ["no plan"],
                "notes": [], "publish": False}

    trigger = float(plan.get("trigger") or 0)
    stop = float(plan.get("stop") or 0)
    limit = float(plan.get("limit") or trigger)
    kind = (kind or plan.get("kind") or "breakout").lower()
    if close is None:
        close = plan.get("close")
    close = float(close) if close else None

    rows = rows if rows is not None else _rows(symbol)

    # 1. The entry has to be reachable and sane for its kind.
    if close:
        if kind in BREAKOUT_KINDS:
            if trigger > close * (1 + CHASE_PCT / 100):
                reasons.append(
                    f"the trigger at {trigger:,.2f} is "
                    f"{(trigger / close - 1) * 100:.1f}% above the price — "
                    "nobody is getting filled")
            elif trigger < close * 0.995:
                reasons.append(
                    f"the trigger at {trigger:,.2f} is below the price — the "
                    "breakout already happened")
            if stop and stop >= close:
                reasons.append(
                    f"the stop at {stop:,.2f} is above the price — the risk "
                    "figure would be nonsense")
        else:   # a pullback entry
            if trigger >= close:
                reasons.append(
                    f"the entry at {trigger:,.2f} is above the price — that is "
                    "not a pullback entry")
            elif (1 - trigger / close) * 100 > MAX_PULLBACK_BELOW_PCT:
                reasons.append(
                    f"the entry at {trigger:,.2f} is "
                    f"{(1 - trigger / close) * 100:.1f}% below the price — "
                    "that is not a pullback, that is a collapse")
            if stop and stop >= trigger:
                reasons.append(
                    f"the stop at {stop:,.2f} is above the entry — the risk "
                    "figure would be nonsense")
            if stop and stop >= close:
                reasons.append(
                    f"the stop at {stop:,.2f} is above today's price — the "
                    "trade would open already stopped")

    # 2. The chart has to qualify.
    if stage2 is False:
        reasons.append("it is not a Stage-2 uptrend")
    if base_grade is not None and str(base_grade).upper() not in GOOD_GRADES:
        reasons.append(f"the base is grade {base_grade}, not A or B")
    if (kind in BREAKOUT_KINDS and from_pivot_pct is not None
            and from_pivot_pct > MAX_FROM_PIVOT_PCT):
        reasons.append(
            f"it is {from_pivot_pct:.1f}% above the pivot — that is chasing, "
            "not buying")

    # 3. And the setup must not already be dead. Only a breakout can fail
    #    this way: a pullback level is meant to be touched again.
    if kind in BREAKOUT_KINDS:
        hist = last_clear_and_fail(rows, trigger)
        # Above the trigger now: the trade is running, not waiting.
        if close and close > trigger:
            reasons.append(
                f"it is already {((close / trigger - 1) * 100):.1f}% above the "
                f"{trigger:,.2f} trigger — that trade is running, not waiting "
                "for you")
            return {"ok": False, "verdict": "ALREADY RAN", "reasons": reasons,
                    "notes": notes, "publish": False}
        # A failure inside the last few sessions is fresh: the market just
        # told you this level does not work.
        _fage = hist["failed_age"]
        if hist["failed"] is not None and (_fage if _fage is not None else 99) <= RECENT_SESSIONS:
            f = hist["failed"]
            reasons.append(
                f"the trigger at {trigger:,.2f} was touched on {f['date']} and "
                f"the stock closed back below it at {f['close']:,.2f} — that is "
                "a fresh failure, not a setup")
            return {"ok": False, "verdict": "SETUP FAILED", "reasons": reasons,
                    "notes": notes, "publish": False,
                    "failed": {"date": f["date"]}}
        # An old failure is a scar, not a death: the base has reformed and the
        # stock is back at the level. It is worth saying so, not worth
        # hiding the stock.
        if hist["failed"] is not None:
            notes.append(
                f"this level failed before, on {hist['failed']['date']} — a "
                "second attempt is a weaker setup, so size it down")
        if hist["cleared"] is not None:
            notes.append(
                f"it cleared {trigger:,.2f} on {hist['cleared']['date']} and "
                "gave it back")

    ok = not reasons
    return {
        "ok": ok,
        "verdict": "BUY-READY" if ok else "WATCH — not actionable today",
        "reasons": reasons,
        "notes": notes,
        "publish": ok,
        "kind": kind,
        "trigger": trigger, "limit": limit, "stop": stop,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    import stock_check
    r = stock_check.check(args.symbol)
    if r.get("error"):
        print(r["error"])
        return 1
    plans = r.get("plans") or []
    if not plans:
        print("no plan to check")
        return 0
    p = plans[0]
    res = gate(
        {"trigger": p.get("entry"), "limit": (p.get("entry_zone") or [None, None])[1],
         "stop": p.get("stop"), "close": r.get("close")},
        r["symbol"], close=r.get("close"),
        stage2=(r.get("trend") or {}).get("stage2"),
        base_grade=(r.get("base") or {}).get("grade"),
        from_pivot_pct=(r.get("base") or {}).get("from_pivot_pct"),
    )
    if args.json:
        print(json.dumps(res, indent=1))
    else:
        print(f"{r['symbol']}: {res['verdict']}")
        for x in res["reasons"]:
            print(f"  - {x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
