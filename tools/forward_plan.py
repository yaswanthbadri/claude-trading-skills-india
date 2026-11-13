#!/usr/bin/env python3
"""What you can actually do with this stock today.

The old card answered the wrong question. It found a base, took its pivot,
and printed "buy above ₹329" for a stock trading at ₹384 — a level the
market cleared three weeks ago. The plan was arithmetically correct and
practically useless, and it was worse than useless because it looked
actionable.

This module asks the question the reader actually has: given where the price
is now, what is the trade?

    breakout_today   price closed above a fresh, tight handle on volume.
                     Two entries: the retest (better R, more likely) and the
                     continuation (buy above today's high, worse R).
    extended         it has already run past the old pivot and no new base
                     has formed. No chase entry. The only trade is a
                     pullback into support, with the stop under the swing low.
    at_pivot         price sits at a valid pivot within the chase limit —
                     the classic breakout entry.
    pre_breakout     price is inside the base, below the pivot. Wait.
    failed           price undercut the base low. The setup died.

Sizing follows the same rules as the report: risk a fixed percentage of
capital, cap the stop distance, round down to whole shares, and charge the
real Indian costs. Every plan carries entry, stop and two targets.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"


def _sibling(skill: str, script: str):
    name = f"_forward_plan_dep_{script}"
    if name in sys.modules:
        return sys.modules[name]
    path = SKILLS / skill / "scripts" / f"{script}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


risk = _sibling("position-sizing", "risk")

MAX_STOP_PCT = 8.0          # a stop further than this is not a stop
MAX_PIVOT_GAP_PCT = 1.5     # below the pivot by more than this: not a setup
CHASE_PCT = 3.0             # the most you pay over the pivot
HANDLE_MAX_RANGE_PCT = 15.0  # a handle deeper than this is not a handle
HANDLE_MIN_BARS = 5
HANDLE_MAX_BARS = 20


def _rows(symbol: str) -> list[dict]:
    from tech_detail import load_candles
    return load_candles(symbol)


def find_handle(rows: list[dict], close: float) -> dict | None:
    """The shelf the market has been holding, and the level it broke.

    Two ways to find it, in order of usefulness:

    1. The breakout bar tells you where the shelf was. Find the last bar that
       closed above the prior 20-day range high on real volume, then look at
       the tight run of bars right before it. That is the base the stock
       actually broke out of, not the one it broke out of three months ago.

    2. If nothing has broken out, the shelf is the tightest run of bars the
       market is holding right now, and its high is the level to buy above.

    The depth cap matters: without it the "shelf" becomes the breakout bar
    itself, and the level to buy becomes yesterday's high.
    """
    if len(rows) < HANDLE_MIN_BARS + 25:
        return None

    # ---- 1. the breakout bar, and the shelf in front of it -----------------
    bo = _breakout_bar(rows)
    if bo is not None:
        shelf = _tight_window(rows[:bo], HANDLE_MIN_BARS, HANDLE_MAX_BARS)
        if shelf:
            shelf["breakout_date"] = rows[bo]["date"]
            shelf["breakout_close"] = rows[bo]["close"]
            shelf["breakout_high"] = rows[bo]["high"]
            shelf["breakout_vol_x"] = _vol_x(rows, bo)
            return shelf

    # ---- 2. no breakout: the shelf being held right now --------------------
    shelf = _tight_window(rows[:-1], HANDLE_MIN_BARS, HANDLE_MAX_BARS)
    if shelf:
        shelf["breakout_date"] = None
    return shelf


def _vol_x(rows: list[dict], i: int) -> float | None:
    if i < 50:
        return None
    avg = sum(r["volume"] for r in rows[i - 50:i]) / 50
    return (rows[i]["volume"] / avg) if avg else None


def _breakout_bar(rows: list[dict], lookback: int = 30) -> int | None:
    """The last bar that CLOSED above the prior 20-day range high.

    The close, not the high. A stock that spikes intraday and gives it back
    has not broken out, and treating its high as the level to buy is how the
    "shelf" ends up being yesterday's spike.
    """
    for i in range(len(rows) - 1, max(20, len(rows) - lookback) - 1, -1):
        prior_hi = max(r["high"] for r in rows[i - 20:i])
        if rows[i]["close"] > prior_hi:
            # Volume decides whether the breakout is *good*, not whether it
            # happened. A breakout on light volume is still the structure the
            # market is working from.
            vx = _vol_x(rows, i)
            if vx is None or vx >= 0.8:
                return i
    return None


def _tight_window(rows: list[dict], min_bars: int, max_bars: int) -> dict | None:
    """The tightest shelf nearest the price: the one with the highest low."""
    best = None
    end = len(rows)
    for w in range(min_bars, max_bars + 1):
        if end - w < 0:
            break
        window = rows[end - w:end]
        hi = max(r["high"] for r in window)
        lo = min(r["low"] for r in window)
        if hi <= 0:
            continue
        depth = (hi - lo) / hi * 100
        if depth > HANDLE_MAX_RANGE_PCT:
            continue
        cand = {"pivot": hi, "low": lo, "bars": w, "depth_pct": round(depth, 1),
                "start": window[0]["date"], "end": window[-1]["date"]}
        # The tightest shelf nearest the price: a stop belongs under the most
        # recent support, not under the bottom of a two-week range.
        if best is None or lo > best["low"] or (lo == best["low"] and w > best["bars"]):
            best = cand
    return best


def _cleared_days_ago(rows: list[dict], pivot: float, since: str) -> int | None:
    """Sessions since the price first closed above this level.

    The FIRST close above it is the breakout day. The most recent one just
    tells you it is still above, which the caller already knows.
    """
    if not pivot:
        return None
    for i, r in enumerate(rows):
        if r["date"] < since:
            continue
        if r["close"] > pivot:
            return len(rows) - 1 - i
    return None


def _size(capital: float, risk_pct: float, entry: float, stop: float) -> dict:
    try:
        return risk.position_size(capital, risk_pct, entry, stop,
                                  cost_pct=0.25, max_alloc_pct=20)
    except Exception:
        return {"units": 0, "risk_amount": 0, "position_value": 0,
                "allocation_pct": 0, "notes": ["could not size this position"]}


def _plan(kind: str, entry: float, stop: float, capital: float, risk_pct: float,
          label: str, note: str = "", targets_r: tuple = (2.0, 3.0),
          extra: dict | None = None) -> dict:
    """One entry with a stop, two targets and a size."""
    if not entry or not stop or stop >= entry:
        return {}
    raw_pct = (entry - stop) / entry * 100
    structural_stop = stop
    capped = False
    if raw_pct > MAX_STOP_PCT:
        stop = entry * (1 - MAX_STOP_PCT / 100)
        capped = True
    sz = _size(capital, risk_pct, entry, stop)
    r_unit = entry - stop
    targets = []
    for r in targets_r:
        price = entry + r * r_unit
        targets.append({"r": r, "price": round(price, 2),
                        "pct": round((price / entry - 1) * 100, 1)})
    # A stop that had to be pulled in to fit the 8% rule is no longer under
    # the structure. Saying so is the difference between a plan and a hope.
    if not capped:
        quality = "structure"
    elif structural_stop < stop:
        quality = "capped inside the base"
    else:
        quality = "capped"
    out = {
        "kind": kind, "label": label, "entry": round(entry, 2),
        "stop": round(stop, 2),
        "stop_pct": round((entry - stop) / entry * 100, 2),
        "raw_stop_pct": round(raw_pct, 2),
        "structural_stop": round(structural_stop, 2),
        "stop_capped": capped, "stop_quality": quality,
        "qty": int(sz.get("units", 0) or 0),
        "position_value": round(sz.get("position_value", 0), 0),
        "allocation_pct": round(sz.get("allocation_pct", 0) or 0, 1),
        "risk": round(sz.get("risk_amount", 0), 0),
        "risk_budget": round(capital * risk_pct / 100, 0),
        "targets": targets,
        "note": note,
        "sizing_notes": sz.get("notes") or [],
    }
    if extra:
        out.update(extra)
    return out


def _from_classic(classic: dict, capital: float, risk_pct: float) -> dict:
    """Turn the breakout planner's pivot plan into a plan the card can show."""
    if not classic or not classic.get("trigger"):
        return {}
    targets = []
    for t in (classic.get("targets") or [])[:2]:
        targets.append({"r": t.get("R"), "price": t.get("price"),
                        "pct": t.get("pct_from_entry")})
    raw = classic.get("raw_stop_loss_pct")
    capped = bool(classic.get("stop_capped"))
    entry = classic.get("trigger")
    stop = classic.get("stop")
    quality = "structure"
    if capped:
        quality = "capped inside the base"
    return {
        "kind": "breakout", "label": "Buy above the pivot",
        "entry": round(entry, 2),
        "entry_zone": [round(entry, 2), round(classic.get("limit") or entry, 2)],
        "stop": round(stop, 2),
        "stop_pct": round((entry - stop) / entry * 100, 2),
        "raw_stop_pct": round(raw, 2) if raw else None,
        "stop_pct_from_limit": round(classic.get("loss_pct_from_limit") or 0, 2),
        "stop_capped": capped, "stop_quality": quality,
        "qty": classic.get("qty") or 0,
        "position_value": round(classic.get("position_value") or 0, 0),
        "allocation_pct": round(classic.get("allocation_pct") or 0, 1),
        "risk": round(classic.get("risk_if_stopped") or 0, 0),
        "risk_budget": round(capital * risk_pct / 100, 0),
        "targets": targets,
        "note": (f"A GTT at {entry:.2f} with a limit at "
                 f"{(classic.get('limit') or entry):.2f} fills only if the stock "
                 "comes to it — a gap above the limit goes unfilled, which is the "
                 "chase rule working."),
        "sizing_notes": [],
    }


def analyse(symbol: str, detail: dict, base: dict | None = None,
            capital: float = 500_000.0, risk_pct: float = 1.0,
            classic: dict | None = None, stage2: bool | None = None,
            base_grade: str | None = None) -> dict:
    """The state of the stock and the trade that belongs to it."""
    rows = _rows(symbol)
    if len(rows) < 60:
        return {"state": "no_data", "state_text": "Not enough history.",
                "plans": [], "why": []}

    last = rows[-1]
    close = last["close"]
    prev = rows[-2]["close"] if len(rows) > 1 else close
    vol_x = detail.get("vol_x50") or 0
    handle = find_handle(rows, close)

    base = base or {}
    old_pivot = base.get("pivot")
    from_pivot = detail.get("from_pivot_pct")
    if from_pivot is None and old_pivot and close:
        from_pivot = (close / old_pivot - 1) * 100

    why: list[str] = []
    plans: list[dict] = []
    state = "no_base"
    state_text = ""

    # ---- the live pivot, and how long ago it was cleared ------------------
    # A stock that broke out of a base in July and is now basing again has a
    # new pivot. Using the July one is how a card ends up offering to buy a
    # level the market left a month ago.
    pivot = old_pivot
    pivot_source = "the base"
    cleared_age = None
    shelf_breakout = bool(handle and handle.get("breakout_date"))
    if shelf_breakout:
        # The stock just broke out of this shelf: the shelf is the structure.
        pivot = handle["pivot"]
        pivot_source = f"a {handle['bars']}-bar shelf"
        cleared_age = _cleared_days_ago(rows, handle["pivot"], handle["end"])
    elif old_pivot:
        cleared_age = _cleared_days_ago(rows, old_pivot, base.get("since") or "")

    from_pivot = None
    if close and pivot:
        from_pivot = (close / pivot - 1) * 100

    # ---- where the price is relative to the level that matters ------------
    if handle and close < handle["low"]:
        state = "failed"
        state_text = (f"The base broke — {close:.2f} is under the "
                      f"{handle['low']:.2f} low")
        why.append(f"The base was holding between {handle['low']:.0f} and "
                   f"{handle['pivot']:.0f}. It has fallen through the bottom of "
                   "it, so the setup is dead. Wait for a new base to form "
                   "rather than buy into a broken one.")
    elif pivot and close > pivot:
        if cleared_age is not None and cleared_age == 0 and vol_x >= 1.5:
            state = "breakout_today"
            state_text = (f"Breakout today above {pivot_source} ({pivot:.2f}), "
                          f"on {vol_x:.1f}x volume")
            why.append(f"The shelf ran {handle['start']} to {handle['end']} "
                       f"between {handle['low']:.0f} and {handle['pivot']:.0f} "
                       f"({handle['depth_pct']}% deep). Today's close {close:.2f} "
                       "is above it for the first time.")
        elif cleared_age is not None and cleared_age <= 8:
            state = "breakout_running"
            state_text = (f"Breakout {cleared_age} session(s) old, now "
                          f"{(from_pivot or 0):.1f}% above {pivot_source} at "
                          f"{pivot:.2f}")
            why.append(f"It cleared {pivot:.2f} {cleared_age} session(s) ago "
                       f"and is {(from_pivot or 0):.1f}% above it. "
                       f"{pivot:.0f} is the level to buy on a pullback, not "
                       "the price you are looking at now.")
        else:
            state = "extended"
            state_text = (f"Extended — {(from_pivot or 0):.1f}% past the "
                          f"{pivot:.2f} {pivot_source}, no new base")
            why.append(f"The level at {pivot:.2f} was cleared long ago. "
                       f"Buying at {pivot:.0f} today is not a plan; the level "
                       "is behind the market.")
            pull = _pullback_plan(rows, detail, capital, risk_pct)
            if pull:
                plans.append(pull)
    else:
        state = "pre_breakout"
        state_text = (f"Inside the base, below its high ({pivot:.2f})"
                      if pivot else "Inside the base")
        if pivot:
            why.append(f"The price is {abs(from_pivot or 0):.1f}% below the "
                       f"{pivot:.2f} level. There is nothing to buy until it "
                       "comes back to it.")
        cp = _from_classic(classic or {}, capital, risk_pct)
        if cp:
            cp["note"] = ("Not a buy yet — the price is still below the "
                          "pivot. The order goes in when it clears it.")
            plans.append(cp)

    # The retest is the trade for a breakout that has just happened, whether
    # it happened today or a few sessions ago. Buy the shelf, not the spike.
    if state in ("breakout_today", "breakout_running") and handle and pivot:
        # Buy the level that was broken, not the level above it. A shelf high
        # the market has already rejected is not an entry.
        retest = _plan(
            "retest", pivot, handle["low"], capital, risk_pct,
            "Buy the retest (preferred)",
            note=(f"Wait for a pullback into {pivot:.0f}–"
                  f"{pivot * 1.03:.0f}. The stop sits under the "
                  f"shelf low {handle['low']:.0f}. Better risk than chasing."),
            extra={"entry_zone": [round(pivot, 2),
                                  round(pivot * 1.03, 2)]})
        if retest:
            plans.append(retest)
        cont_stop = max(last["low"], pivot * 0.98)
        cont = _plan(
            "continuation", last["high"], cont_stop, capital, risk_pct,
            "Buy above today's high (aggressive)",
            note=("Only if it keeps running. The stop is "
                  f"{last['high'] - cont_stop:.0f} points away on a stock with "
                  f"a {detail.get('avg_range_pct') or 0:.1f}% average daily "
                  "range, so the R is poor."),
            extra={"entry_zone": [round(last["high"], 2),
                                  round(last["high"] * 1.03, 2)]})
        if cont:
            plans.append(cont)

    # A reversal day: closed well off the high on heavy volume. The breakout
    # may still be intact, but somebody sold into it.
    rev = detail.get("reversal")
    if rev and rev.get("flag"):
        why.append(f"⚠️ {rev['text']}")

    if state == "no_base":
        state = "no_base"
        state_text = "No base to trade"

    # Every plan goes through the one gate. A plan that cannot be acted on
    # today keeps its numbers for the reader but is marked unpublishable, and
    # the verdict says so.
    try:
        import plan_gate
        for pl in plans:
            g = plan_gate.gate(
                {"trigger": pl.get("entry"), "limit": (pl.get("entry_zone") or [None, None])[1],
                 "stop": pl.get("stop"), "close": close},
                symbol, close=close, stage2=stage2, base_grade=base_grade,
                from_pivot_pct=from_pivot, rows=rows, kind=pl.get("kind"))
            pl["gate"] = g
            pl["publish"] = g.get("ok")
    except Exception:
        for pl in plans:
            pl.setdefault("gate", {"ok": True, "reasons": []})
            pl.setdefault("publish", True)

    return {
        "state": state,
        "state_text": state_text,
        "handle": handle,
        "old_pivot": old_pivot,
        "pivot": pivot,
        "pivot_source": pivot_source,
        "cleared_age": cleared_age,
        "from_pivot_pct": from_pivot,
        "vol_x50": vol_x,
        "plans": plans,
        "why": why,
        "last": last,
        "prev": prev,
    }


def _pullback_plan(rows: list[dict], detail: dict, capital: float,
                    risk_pct: float) -> dict:
    """When the move is over: buy the pullback into support, or nothing.

    Support is the 20-day average and the most recent shelf the market
    defended. The stop goes under the swing low that defines that shelf.
    """
    sma20 = detail.get("sma20")
    if not sma20:
        return {}
    lows = [r["low"] for r in rows[-21:-1]]
    swing_low = min(lows) if lows else None
    entry = sma20
    stop = swing_low
    if not stop or stop >= entry:
        return {}
    plan = _plan("pullback", entry, stop, capital, risk_pct,
                 "Buy a pullback into the 20-day average",
                 note=("No chase entry. If it comes back here and holds, buy "
                       "there. If it never comes back, you did not have a trade "
                       "— which is the point."),
                 extra={"entry_zone": [round(entry * 0.995, 2),
                                       round(entry * 1.01, 2)]})
    if plan:
        plan["note"] = (f"No chase entry. If it comes back to {entry:.0f} and "
                        f"holds, buy there with the stop at {plan['stop']:.0f}. "
                        "If it never comes back, you did not have a trade — "
                        "which is the point.")
    return plan
