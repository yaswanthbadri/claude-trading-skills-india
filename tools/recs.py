#!/usr/bin/env python3
"""Record every recommendation we publish, then grade it against the tape.

This is the accountability layer. Each morning the bulletin publishes plans
(entry above a trigger, a stop, T1, T2). This module stores them, and every
evening replays the actual candles to answer one question:

    did the trade we told you to take actually work?

States a record moves through:

    pending_fill -> no_fill      never traded through the trigger
                 -> filled       triggered; now being tracked
                 -> t1_hit       first target reached (partial banked)
                 -> t2_hit       second target reached
                 -> stopped      stop hit before the second target
                 -> expired      still unfilled after N sessions

Rules that keep the scorecard honest:

* A gap above the chase limit counts as NO FILL, not as a winner. You cannot
  buy a stock that opens 6% above your trigger at your trigger.
* If a bar touches both the stop and a target, the stop is assumed first.
  Intraday sequencing is unknowable from daily bars, so we take the bad case.
* Only filled trades count toward the win rate. Fill rate is reported
  separately, because "condition never triggered" is not a loss.

Usage:
    python3 tools/recs.py --record reports/bulletin_daily_x.json
    python3 tools/recs.py --evaluate
    python3 tools/recs.py --show
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
STORE = DATA / "recommendations.json"
CANDLES = DATA / "candles"

EXPIRY_SESSIONS = 5      # an unfilled trigger goes stale after a week


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def load() -> dict:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text())
        except Exception:
            pass
    return {"version": 1, "records": [], "updated": None}


def save(store: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    store["updated"] = datetime.now().isoformat(timespec="seconds")
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1, default=str))
    tmp.replace(STORE)


def _rid(symbol: str, day: str) -> str:
    return f"{day}:{symbol}"


def live_from(published_at: str | None) -> str:
    """First session in which a published trigger can actually fill.

    A plan published before the close can fill that same session. One published
    after the close (the 16:00 review) cannot — grading it against the very bar
    it was derived from is look-ahead, and it produced a fake stop-out on the
    day of publication.
    """
    if not published_at:
        return date.today().isoformat()
    try:
        ts = datetime.fromisoformat(published_at)
    except (TypeError, ValueError):
        return str(published_at)[:10]

    minutes = ts.hour * 60 + ts.minute
    if minutes <= 15 * 60 + 30:
        return ts.date().isoformat()

    nxt = ts.date() + timedelta(days=1)
    try:
        import market_calendar as cal
        for _ in range(10):
            if cal.is_trading_day(nxt):
                break
            nxt += timedelta(days=1)
    except Exception:
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
    return nxt.isoformat()


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------
def record(data: dict, source: str | None = None) -> int:
    """Store the plans and deep candidates from a generated bulletin."""
    store = load()
    by_id = {r["id"]: r for r in store["records"]}
    published = data.get("generated_at") or datetime.now().isoformat(timespec="seconds")
    day = published[:10]
    first_session = live_from(published)
    added = 0

    def add(symbol, plan, src, structure, score=None):
        nonlocal added
        if not symbol or not plan or not plan.get("entry"):
            return
        # Key on the session the plan is FOR, not the evening it was published.
        # An after-close plan for Thursday and Thursday's 09:00 plan are the same
        # slot, so the morning version simply supersedes it.
        rid = _rid(symbol, first_session)
        existing = by_id.get(rid)
        rec = {
            "id": rid, "symbol": symbol, "date": first_session, "source": src,
            "structure": structure,
            "entry": _f(plan.get("entry")), "limit": _f(plan.get("limit")),
            "stop": _f(plan.get("stop")), "t1": _f(plan.get("t1")),
            "t2": _f(plan.get("t2")), "hundred": _f(plan.get("hundred")),
            "qty": plan.get("qty"), "score": score,
            "published_at": published, "live_from": first_session,
            "status": "pending_fill", "fill_date": None, "fill_price": None,
            "exit_date": None, "exit_price": None, "exit_reason": None,
            "r_realized": None, "max_high": None, "min_low": None,
            "sessions": 0, "notes": [],
        }
        if existing:
            # Never rewrite a trade that already filled.
            if existing.get("status") != "pending_fill":
                return
            existing.update({k: v for k, v in rec.items() if k != "id"})
            return
        by_id[rid] = rec
        store["records"].append(rec)
        added += 1

    for p in data.get("plans") or []:
        # bulletin plans carry trigger/stop/targets rather than entry/t1
        targets = p.get("targets") or []
        plan = {
            "entry": p.get("trigger"), "limit": p.get("limit"), "stop": p.get("stop"),
            "t1": targets[0]["price"] if len(targets) > 0 else None,
            "t2": targets[1]["price"] if len(targets) > 1 else None,
            "qty": p.get("qty"),
        }
        add(p.get("symbol"), plan, "plan", p.get("base_state") or "breakout")

    for c in (data.get("deep") or {}).get("candidates") or []:
        p = c.get("plan") or {}
        add(c.get("symbol"), p, "deep", c.get("structure"),
            score=c.get("score"))

    save(store)
    return added


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
def _bars(symbol: str):
    path = CANDLES / f"{symbol}.csv"
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for row in csv.DictReader(f):
            try:
                out.append({
                    "date": row["Date"][:10],
                    "open": float(row["Open"]), "high": float(row["High"]),
                    "low": float(row["Low"]), "close": float(row["Close"]),
                })
            except (KeyError, ValueError, TypeError):
                continue
    return out


def evaluate() -> dict:
    """Replay candles and update every open record."""
    store = load()
    changed = 0
    today = date.today().isoformat()

    # A verdict needs a finished candle. Until the 15:30 close, today's bar is
    # still moving, so grading it would call a stop-out that may not happen.
    now = datetime.now()
    session_done = (now.hour, now.minute) >= (15, 35)

    for rec in store["records"]:
        if rec["status"] in ("t2_hit", "stopped", "no_fill", "expired"):
            continue

        bars = [b for b in _bars(rec["symbol"])
                if b["date"] >= (rec.get("live_from") or rec["date"])
                and (b["date"] != today or session_done)]
        if not bars:
            continue

        entry, stop = rec["entry"], rec["stop"]
        limit = rec["limit"] or entry * 1.03
        t1, t2 = rec.get("t1"), rec.get("t2")
        sessions = 0

        for b in bars:
            sessions += 1
            if rec["status"] == "pending_fill":
                if b["open"] > limit:
                    rec.update(status="no_fill", exit_date=b["date"],
                               exit_reason="gapped past the chase limit",
                               sessions=sessions)
                    changed += 1
                    break
                if b["high"] >= entry:
                    rec.update(status="filled", fill_date=b["date"],
                               fill_price=max(b["open"], entry))
                    changed += 1
                elif sessions >= EXPIRY_SESSIONS:
                    rec.update(status="expired", exit_date=b["date"],
                               exit_reason=f"no trigger within {EXPIRY_SESSIONS} sessions",
                               sessions=sessions)
                    changed += 1
                    break
                else:
                    continue

            if rec["status"] in ("filled", "t1_hit"):
                fill = rec["fill_price"] or entry
                rec["max_high"] = max(rec.get("max_high") or b["high"], b["high"])
                rec["min_low"] = min(rec.get("min_low") or b["low"], b["low"])

                # Bad case first: same-bar stop wins.
                if b["low"] <= stop:
                    rec.update(status="stopped", exit_date=b["date"],
                               exit_price=stop, exit_reason="stop hit",
                               r_realized=round((stop - fill) / (fill - stop), 2)
                               if fill > stop else -1.0,
                               sessions=sessions)
                    changed += 1
                    break
                if t2 and b["high"] >= t2:
                    rec.update(status="t2_hit", exit_date=b["date"],
                               exit_price=t2, exit_reason="target 2 reached",
                               r_realized=round((t2 - fill) / (fill - stop), 2)
                               if fill > stop else None,
                               sessions=sessions)
                    changed += 1
                    break
                if t1 and b["high"] >= t1 and rec["status"] == "filled":
                    rec.update(status="t1_hit")
                    rec.setdefault("notes", []).append(
                        f"T1 {t1} hit on {b['date']}")
                    changed += 1
        else:
            rec["sessions"] = sessions

    save(store)
    return {"changed": changed, "as_of": today}


# ---------------------------------------------------------------------------
# scorecard
# ---------------------------------------------------------------------------
def _r_for(rec: dict, last_close: float | None) -> float | None:
    entry, stop = rec.get("fill_price") or rec.get("entry"), rec.get("stop")
    if not entry or not stop or entry <= stop:
        return None
    if rec.get("r_realized") is not None:
        return rec["r_realized"]
    if last_close:
        return round((last_close - entry) / (entry - stop), 2)
    return None


def scorecard(sessions: int = 20) -> dict:
    """Aggregate stats plus today's outcomes and tomorrow's focus list."""
    store = load()
    recs = store["records"]
    today = date.today().isoformat()

    closed_today = [r for r in recs if r.get("exit_date") == today
                    and r["status"] in ("t1_hit", "t2_hit", "stopped", "no_fill", "expired")]
    new_today = [r for r in recs if r["date"] == today]

    open_recs = [r for r in recs if r["status"] in ("filled", "t1_hit")]
    pending = [r for r in recs if r["status"] == "pending_fill"]

    # rolling window by publication date
    cutoff = (date.today() - timedelta(days=sessions * 2)).isoformat()
    window = [r for r in recs if r["date"] >= cutoff]

    decided = [r for r in window if r["status"] in ("t1_hit", "t2_hit", "stopped")]
    wins = [r for r in decided if r["status"] in ("t1_hit", "t2_hit")]

    # A runner that banked T1 has no exit price yet. Dropping it would quietly
    # remove winners from the average, so mark it to the latest close instead.
    rs = []
    for r in decided:
        if r.get("r_realized") is not None:
            rs.append(r["r_realized"])
            continue
        bars = _bars(r["symbol"])
        v = _r_for(r, bars[-1]["close"] if bars else None)
        if v is not None:
            rs.append(v)

    attempted = [r for r in window if r["status"] != "pending_fill"]
    fills = [r for r in window if r["status"] in ("filled", "t1_hit", "t2_hit", "stopped")]

    rows = []
    for r in sorted(closed_today + open_recs, key=lambda x: x["date"]):
        last_close = None
        if r in open_recs:
            bars = _bars(r["symbol"])
            last_close = bars[-1]["close"] if bars else None
        rows.append({
            "symbol": r["symbol"], "date": r["date"], "source": r["source"],
            "status": r["status"],
            "entry": r["entry"], "fill_price": r.get("fill_price"),
            "stop": r["stop"], "t1": r.get("t1"), "t2": r.get("t2"),
            "exit_price": r.get("exit_price"), "exit_reason": r.get("exit_reason"),
            "r": _r_for(r, last_close), "sessions": r.get("sessions"),
            "last_close": last_close, "notes": r.get("notes") or [],
        })

    return {
        "as_of": today,
        "new_today": len(new_today),
        "closed_today": len(closed_today),
        "open_count": len(open_recs),
        "pending_count": len(pending),
        "sessions_window": sessions,
        "attempted": len(attempted),
        "fills": len(fills),
        "fill_rate_pct": round(100 * len(fills) / len(attempted), 0) if attempted else None,
        "decided": len(decided),
        "wins": len(wins),
        "hit_rate_pct": round(100 * len(wins) / len(decided), 0) if decided else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "total_r": round(sum(rs), 1) if rs else None,
        "best": max(decided, key=lambda x: x.get("r_realized") or -9, default=None),
        "worst": min(decided, key=lambda x: x.get("r_realized") or 9, default=None),
        "rows": rows,
        "open_rows": [x for x in rows if x["status"] in ("filled", "t1_hit")],
        "closed_rows": [x for x in rows if x["status"] not in ("filled", "t1_hit")],
    }


def focus_list(data: dict, card: dict) -> list[str]:
    """Plain-English 'what to do tomorrow' derived from the scorecard."""
    out = []
    if card["open_count"]:
        names = ", ".join(r["symbol"] for r in card["open_rows"][:5])
        out.append(f"Manage {card['open_count']} open position(s): {names}. "
                   "Trail stops, do not add to a loser.")
    if card["pending_count"]:
        out.append(f"{card['pending_count']} trigger(s) still pending — re-check the levels "
                   "before the open; a stale trigger gets cancelled.")
    stopped = [r for r in card["closed_rows"] if r["status"] == "stopped"]
    if stopped:
        out.append(f"{len(stopped)} stopped out. Re-entry needs a fresh base, not hope.")
    nofill = [r for r in card["closed_rows"] if r["status"] == "no_fill"]
    if nofill:
        out.append(f"{len(nofill)} gapped past the chase limit — chasing those is how "
                   "accounts bleed; wait for the next pivot.")
    reg = (data.get("regime") or {})
    if reg.get("label"):
        out.append(f"Regime into tomorrow: {reg['label']}, exposure ceiling "
                   f"{reg.get('exposure_pct')}%.")
    near = [s for s in (data.get("breakouts") or {}).get("setups") or []
            if (s.get("from_pivot_pct") is not None and -3 <= s["from_pivot_pct"] <= 1
                and s.get("stage2"))][:5]
    if near:
        out.append("Closest to a pivot: " + ", ".join(s["symbol"] for s in near) + ".")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", type=Path, help="bulletin JSON to store")
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    if args.record:
        data = json.loads(args.record.read_text())
        print(f"recorded {record(data)} new recommendation(s)")
    if args.evaluate:
        print(evaluate())
    if args.show:
        card = scorecard()
        def _pct(v):
            return f"{v:.0f}%" if v is not None else "—"
        def _num(v, suffix=""):
            return f"{v:+.1f}{suffix}" if v is not None else "—"
        print(f"\n{card['as_of']}  fills {card['fills']}/{card['attempted']} "
              f"({_pct(card['fill_rate_pct'])})  hit rate {_pct(card['hit_rate_pct'])}  "
              f"avg R {_num(card['avg_r'])}  total {_num(card['total_r'], 'R')}  "
              f"pending {card['pending_count']}")
        for r in card["rows"]:
            print(f"  {r['symbol']:12} {r['date']} {r['status']:9} "
                  f"R {r['r'] if r['r'] is not None else '—'}")
    if not (args.record or args.evaluate or args.show):
        print(f"{len(load()['records'])} records in {STORE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
