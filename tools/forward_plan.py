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
CHASE_PCT = 3.0             # the most you pay over the pivot
HANDLE_MAX_RANGE_PCT = 15.0  # a handle deeper than this is not a handle
HANDLE_MIN_BARS = 5
HANDLE_MAX_BARS = 20


def _rows(symbol: str) -> list[dict]:
    from tech_detail import load_candles
    return load_candles(symbol)


def find_handle(rows: list[dict], close: float) -> dict | None:
    """The most recent tight shelf the market has been holding.

    Windows ending yesterday, from 4 to 20 bars. The widest one whose depth
    is still shallow is the handle; its high is the pivot the stock has just
    cleared and its low is where the shelf failed.
    """
    if len(rows) < HANDLE_MIN_BARS + 5:
        return None
    best = None
    end = len(rows) - 1                      # yesterday is the last full bar
    for w in range(HANDLE_MIN_BARS, HANDLE_MAX_BARS + 1):
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
        # a pivot the price has already left far behind is not this stock's
        # pivot any more
        if close and (hi / close - 1) * 100 > 25:
            continue
        cand = {"pivot": hi, "low": lo, "bars": w, "depth_pct": round(depth, 1),
                "start": window[0]["date"], "end": window[-1]["date"]}
        # The tightest shelf nearest the price: a stop belongs under the most
        # recent support, not under the bottom of a two-week range.
        if best is None or lo > best["low"] or (lo == best["low"] and w > best["bars"]):
            best = cand
    return best


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
            classic: dict | None = None) -> dict:
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

    # ---- a fresh handle and a volume breakout through it -------------------
    breakout_today = False
    if handle and close > handle["pivot"]:
        if vol_x >= 1.5:
            breakout_today = True
        else:
            why.append(f"Price cleared the {handle['bars']}-bar handle at "
                       f"{handle['pivot']:.0f} but on only {vol_x:.1f}x volume — "
                       "the breakout is not confirmed.")

    if breakout_today and handle:
        state = "breakout_today"
        state_text = (f"Breakout today above a {handle['bars']}-bar handle "
                      f"({handle['pivot']:.2f}), on {vol_x:.1f}x volume")
        why.append(f"The shelf ran {handle['start']} to {handle['end']} between "
                   f"{handle['low']:.0f} and {handle['pivot']:.0f} "
                   f"({handle['depth_pct']}% deep). Today's close {close:.2f} is "
                   f"above it for the first time.")
        # (A) the retest — buy the shelf, not the spike
        retest_entry = handle["pivot"]
        retest = _plan(
            "retest", retest_entry, handle["low"], capital, risk_pct,
            "Buy the retest (preferred)",
            note=(f"Wait for a pullback into {handle['pivot']:.0f}–"
                  f"{handle['pivot'] * 1.03:.0f}. The stop sits under the shelf "
                  f"low {handle['low']:.0f}. Better risk than chasing."),
            extra={"entry_zone": [round(handle["pivot"], 2),
                                  round(handle["pivot"] * 1.03, 2)]})
        if retest:
            plans.append(retest)
        # (B) the continuation — only if it keeps running
        cont_stop = max(last["low"], handle["pivot"] * 0.98)
        cont = _plan(
            "continuation", last["high"], cont_stop, capital, risk_pct,
            "Buy above today's high (aggressive)",
            note=("Only if it keeps running. The stop is "
                  f"{last['high'] - cont_stop:.0f} points away on a stock with a "
                  f"{detail.get('avg_range_pct') or 0:.1f}% average daily range, so "
                  "the R is poor."),
            extra={"entry_zone": [round(last["high"], 2),
                                  round(last["high"] * 1.03, 2)]})
        if cont:
            plans.append(cont)

    # ---- already extended with no fresh base ------------------------------
    if state == "no_base" and old_pivot and from_pivot is not None:
        if from_pivot > CHASE_PCT:
            state = "extended"
            state_text = (f"Extended — {from_pivot:.1f}% past the {old_pivot:.2f} "
                          "pivot, no new base")
            why.append(f"The pivot at {old_pivot:.2f} was cleared long ago. "
                       f"Buying at {old_pivot:.0f} today is not a plan; the level "
                       "is behind the market.")
            pull = _pullback_plan(rows, detail, capital, risk_pct)
            if pull:
                plans.append(pull)
        elif from_pivot >= -15:
            state = "at_pivot"
            state_text = f"At the pivot ({old_pivot:.2f})"
            cp = _from_classic(classic or {}, capital, risk_pct)
            if cp:
                plans.append(cp)
            else:
                why.append("No usable base here, so there is no level to buy "
                           "above yet.")
        else:
            state = "pre_breakout"
            state_text = f"Inside the base, below the pivot ({old_pivot:.2f})"
            cp = _from_classic(classic or {}, capital, risk_pct)
            if cp:
                cp["note"] = ("Not a buy yet — the price is still below the pivot. "
                              "The order goes in when it clears it.")
                plans.append(cp)

    if state == "no_base":
        state = "no_base"
        state_text = "No base to trade"

    return {
        "state": state,
        "state_text": state_text,
        "handle": handle,
        "old_pivot": old_pivot,
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
