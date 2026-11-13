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
import shutil
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


def save(store: dict, allow_shrink: bool = False) -> bool:
    """Write the book. Refuse to make it smaller without saying so.

    This file is the entire track record: every verdict the desk has ever
    published and every trade it has graded comes from here. A bug that
    quietly drops half of it would not announce itself — the reports would
    simply stop mentioning trades the reader remembers being told about.

    So a shrink is refused, the previous file is kept as a backup, and the
    refusal is loud.
    """
    DATA.mkdir(parents=True, exist_ok=True)
    current = 0
    if STORE.exists():
        try:
            current = len(json.loads(STORE.read_text()).get("records") or [])
        except Exception:
            current = 0
    new = len(store.get("records") or [])
    if current and new < current and not allow_shrink:
        backup = STORE.with_suffix(".bak.json")
        try:
            shutil.copy2(STORE, backup)
        except Exception:
            pass
        print(f"[warn] refused to shrink the book: {current} records -> {new}. "
              f"Previous file kept at {backup.name}.", file=sys.stderr)
        return False
    store["updated"] = datetime.now().isoformat(timespec="seconds")
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1, default=str))
    tmp.replace(STORE)
    return True


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

    regime = ((data.get("regime") or {}).get("label") or "")

    # A plan that is still live was already a position you were told to take.
    # Publishing it again tomorrow is fine; booking it twice is not, because
    # the risk engine would count the same rupees twice and the desk would
    # "recommend" a stock you already hold.
    #
    # The key is the SYMBOL, not the entry. The trigger moves with the price:
    # CENTUM's plan was 4,238.50 on one session and 4,274.60 on the next, and
    # keying on the entry booked the same stock twice — two positions, twice
    # the risk, and a book that listed it twice.
    live_by_symbol: dict[str, dict] = {}
    for r in store["records"]:
        if r.get("status") in ("pending_fill", "filled", "t1_hit"):
            live_by_symbol.setdefault(r["symbol"], r)

    def add(symbol, plan, src, structure, score=None, base_type=None,
            base_grade=None, sector=None):
        nonlocal added
        if not symbol or not plan or not plan.get("entry"):
            return

        live = live_by_symbol.get(symbol)
        if live is not None and live.get("id") != _rid(symbol, first_session):
            if live.get("status") in ("filled", "t1_hit"):
                # Already in the book. One position per stock: a second entry
                # is a bigger bet, not a new idea.
                return
            # Still a live order. The trigger moves with the price, so the
            # levels are refreshed in place — one record, not two.
            for k in ("entry", "limit", "stop", "t1", "t2", "qty"):
                if plan.get(k) is not None:
                    live[k] = plan.get(k)
            live["published_at"] = published
            live["notes"] = list(live.get("notes") or []) + [
                f"levels refreshed {published[:10]}"]
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
            "base_type": base_type, "base_grade": base_grade,
            "sector": sector, "regime": regime,
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
        live_by_symbol[symbol] = rec
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
        add(p.get("symbol"), plan, "plan", p.get("base_state") or "breakout",
            base_type=p.get("base_type"), base_grade=p.get("base_grade"),
            sector=p.get("sector"))

    for c in (data.get("deep") or {}).get("candidates") or []:
        p = c.get("plan") or {}
        add(c.get("symbol"), p, "deep", c.get("structure"),
            score=c.get("score"), base_type=(c.get("base") or {}).get("type"),
            base_grade=(c.get("base") or {}).get("grade"),
            sector=c.get("sector"))

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
CHARGES_SCRIPT = REPO / "skills" / "trading-costs" / "scripts" / "charges.py"


def _round_trip_charges(buy: float, sell: float, qty: float) -> float | None:
    """Real Indian delivery charges for the trade, in rupees.

    Uses the same calculator the reports quote, so the scorecard and the plan
    card cannot disagree about what a trade costs.
    """
    if not buy or not sell or not qty or qty <= 0:
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("charges", CHARGES_SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        res = mod.calculate("delivery", buy=buy, sell=sell, qty=qty, broker="zerodha")
        return float(res["total_charges"])
    except Exception:
        return None


def net_r(rec: dict) -> float | None:
    """R after costs. Gross R flatters a small account and hides a thin edge.

    On a 5% stop, a round trip costs roughly 0.3-0.5% of turnover, which is
    about 0.08R. That is the difference between an edge and a hobby.
    """
    r = rec.get("r_realized")
    fill = rec.get("fill_price") or rec.get("entry")
    stop = rec.get("stop")
    exit_px = rec.get("exit_price")
    qty = rec.get("qty")
    if r is None or not fill or not stop or fill <= stop or not exit_px:
        return None
    q = float(qty) if qty else 100.0
    risk = (fill - stop) * q
    if risk <= 0:
        return None
    fees = _round_trip_charges(fill, exit_px, q)
    if fees is None:
        return round(float(r), 2)
    return round(float(r) - fees / risk, 2)


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

    # Net R is the honest number: gross R ignores what the broker and the
    # government take. Both are reported so the gap is visible.
    net_rs = [x for x in (net_r(r) for r in decided) if x is not None]

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
        "avg_net_r": round(sum(net_rs) / len(net_rs), 2) if net_rs else None,
        "attribution": attribution(decided, window),
        "best": max(decided, key=lambda x: x.get("r_realized") or -9, default=None),
        "worst": min(decided, key=lambda x: x.get("r_realized") or 9, default=None),
        "rows": rows,
        "open_rows": [x for x in rows if x["status"] in ("filled", "t1_hit")],
        "closed_rows": [x for x in rows if x["status"] not in ("filled", "t1_hit")],
        "stale_pending": _stale_pending(pending),
    }


def _stale_pending(pending: list[dict]) -> list[dict]:
    """Pending triggers the market has walked away from.

    A trigger published two sessions ago at 13,421 is not a commitment when
    the stock is now 8% below it. It is a note from a session that has passed,
    and it should be cancelled rather than counted as risk.
    """
    out = []
    seen = set()
    for r in pending:
        try:
            if r["symbol"] in seen:
                continue          # the same order, published for two sessions
            entry = r.get("entry")
            bars = _bars(r["symbol"])
            if not entry or not bars:
                continue
            last = float(bars[-1]["close"])
            below = (entry / last - 1) * 100 if last else None
            if below is not None and below > 3.0:
                seen.add(r["symbol"])
                out.append({"symbol": r["symbol"], "entry": entry,
                            "last_close": round(last, 2),
                            "below_trigger_pct": round(below, 2),
                            "date": r.get("live_from") or r.get("date")})
        except Exception:
            continue
    return out


def _bucket(rows: list[dict]) -> dict:
    """n, wins, avg gross R and avg net R for one slice of the book."""
    rs = [r["r_realized"] for r in rows if r.get("r_realized") is not None]
    nets = [x for x in (net_r(r) for r in rows) if x is not None]
    wins = [r for r in rows if r["status"] in ("t1_hit", "t2_hit")]
    return {
        "n": len(rows),
        "wins": len(wins),
        "hit_rate_pct": round(100 * len(wins) / len(rows)) if rows else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "avg_net_r": round(sum(nets) / len(nets), 2) if nets else None,
    }


def attribution(decided: list[dict], window: list[dict],
                min_n: int = 3) -> dict:
    """Which setups actually pay: base type, base grade, regime, sector.

    One aggregate R cannot tell you to stop publishing loose bases in a
    weakening market. Buckets only appear once they hold `min_n` decided
    trades, so nothing is inferred from a single lucky outcome.
    """
    if not decided:
        return {"buckets": [], "note": "no decided trades yet"}

    dims = [("base_type", "Base"), ("base_grade", "Grade"),
            ("regime", "Regime"), ("sector", "Sector")]
    buckets = []
    for key, label in dims:
        groups: dict[str, list[dict]] = {}
        for r in decided:
            v = (r.get(key) or "").strip()
            if not v:
                continue
            groups.setdefault(v, []).append(r)
        for v, rows in groups.items():
            if len(rows) < min_n:
                continue
            b = _bucket(rows)
            b.update({"dimension": label, "value": v})
            buckets.append(b)

    buckets.sort(key=lambda b: (b["dimension"], -(b["avg_net_r"] or -9)))
    return {"buckets": buckets,
            "decided": len(decided),
            "sessions": len({r["date"] for r in window}),
            "note": "" if buckets else f"no bucket has {min_n} decided trades yet"}


def _results_soon(rows: list[dict], within: int = 7) -> list[str]:
    """'CENTUM reports 18 Nov (3 sessions)' for open positions, if known."""
    if not rows:
        return []
    try:
        import results_dates as rd
        cal = rd.annotate([r["symbol"] for r in rows])
    except Exception:
        return []
    out = []
    for r in rows:
        row = cal.get(r["symbol"]) or {}
        n = row.get("sessions")
        if n is None or n > within:
            continue
        label = "confirmed" if row.get("source") == "confirmed" else "estimated"
        out.append(f"{r['symbol']} on {row['date']} ({n} sessions, {label})")
    return out


def focus_list(data: dict, card: dict) -> list[str]:
    """Plain-English 'what to do tomorrow' derived from the scorecard."""
    out = []
    if card["open_count"]:
        names = ", ".join(r["symbol"] for r in card["open_rows"][:5])
        out.append(f"Manage {card['open_count']} open position(s): {names}. "
                   "Trail stops, do not add to a loser.")
    if card["pending_count"]:
        stale = card.get("stale_pending") or []
        live = card["pending_count"] - len(stale)
        if stale:
            names = ", ".join(r["symbol"] for r in stale[:6])
            out.append(f"{len(stale)} of your {card['pending_count']} trigger(s) "
                       f"are stale — the price has moved away from them ({names}"
                       + (", …" if len(stale) > 6 else "") +
                       "). Cancel those orders; they are not going to fill on "
                       "levels the market has left.")
        else:
            out.append(f"{card['pending_count']} trigger(s) still pending — re-check the levels "
                       "before the open; a stale trigger gets cancelled.")
    stopped = [r for r in card["closed_rows"] if r["status"] == "stopped"]
    if stopped:
        out.append(f"{len(stopped)} stopped out. Re-entry needs a fresh base, not hope.")
    nofill = [r for r in card["closed_rows"] if r["status"] == "no_fill"]
    if nofill:
        out.append(f"{len(nofill)} gapped past the chase limit — chasing those is how "
                   "accounts bleed; wait for the next pivot.")
    # Results inside a holding window are a scheduled gap risk, not a surprise.
    # This is the one bullet that can save a full R, so it goes near the top.
    soon = _results_soon(card.get("open_rows") or [], within=7)
    if soon:
        out.append("Results inside the holding window: " + "; ".join(soon)
                   + ". A stop does not fill through a gap — decide before the "
                     "print, not after it.")

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
