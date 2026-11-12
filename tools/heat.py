#!/usr/bin/env python3
"""Total risk on the book, not risk per trade.

Every plan in this system is sized to 1% of capital in isolation, which is the
right answer to "how big should this trade be" and the wrong answer to "how
much am I risking today". Fifteen positions each sized to 1% is a 15% account
drawdown if the market gaps against the whole book, and the per-trade rule
never notices because no single trade broke it.

Heat is the sum, in rupees, of what is actually at risk right now:

    at risk     filled and open        (fill - stop) x qty
    committed   pending, if all fill   (entry - stop) x qty

The second number matters because the triggers were designed to fill together
in a rising market — exactly when the book is already fat.

Two things get reported alongside it:

    sector clusters   six energy names is one 6% loss wearing six costumes
    headroom          how much of the heat cap is left for new positions

The bulletin uses headroom to decide how many new plans to publish, so a hot
book produces fewer ideas rather than the usual six.

    python3 tools/heat.py
    python3 tools/heat.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))


def _sector_map() -> dict:
    try:
        import universe
        return universe.sector_map()
    except Exception:
        return {}


def positions() -> list[dict]:
    """The book, with the rupee risk each row carries."""
    import recs
    out = []
    seen: set[tuple] = set()
    for r in recs.load()["records"]:
        status = r.get("status")
        if status in ("stopped", "t2_hit", "no_fill", "expired"):
            continue
        qty = r.get("qty") or 0
        stop = r.get("stop") or 0
        if status in ("filled", "t1_hit"):
            base = r.get("fill_price") or r.get("entry") or 0
            kind = "open"
        else:
            base = r.get("entry") or 0
            kind = "pending"
        if not qty or not stop or base <= stop:
            continue
        # Records written before duplicate suppression existed can hold the
        # same trigger twice. Counting it twice would double the heat.
        key = (r["symbol"], round(base, 2), round(stop, 2))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "symbol": r["symbol"],
            "status": status,
            "kind": kind,
            "qty": qty,
            "entry": base,
            "stop": stop,
            "risk": round((base - stop) * qty, 0),
            "sector": r.get("sector") or "",
            "date": r.get("live_from") or r.get("date"),
        })
    return out


def compute(capital: float | None = None, risk_pct: float | None = None,
            cap_pct: float | None = None) -> dict:
    import settings
    capital = capital or settings.capital()
    risk_pct = risk_pct if risk_pct is not None else settings.risk_pct()
    cap_pct = cap_pct if cap_pct is not None else settings.max_heat_pct()

    rows = positions()
    sectors = _sector_map()
    for r in rows:
        if not r["sector"]:
            r["sector"] = sectors.get(r["symbol"], "") or "Other"

    at_risk = sum(r["risk"] for r in rows if r["kind"] == "open")
    committed = sum(r["risk"] for r in rows if r["kind"] == "pending")
    per_trade = capital * risk_pct / 100.0
    cap_rs = capital * cap_pct / 100.0

    by_sector: dict[str, dict] = {}
    for r in rows:
        s = by_sector.setdefault(r["sector"], {"sector": r["sector"], "count": 0,
                                               "risk": 0.0, "symbols": []})
        s["count"] += 1
        s["risk"] += r["risk"]
        s["symbols"].append(r["symbol"])

    sector_rows = sorted(by_sector.values(), key=lambda s: -s["risk"])
    for s in sector_rows:
        s["risk"] = round(s["risk"])
        s["pct"] = round(100 * s["risk"] / capital, 2) if capital else 0

    used_pct = round(100 * at_risk / capital, 2) if capital else 0
    committed_pct = round(100 * (at_risk + committed) / capital, 2) if capital else 0
    headroom = round(cap_pct - used_pct, 2)

    # What is actually left to deploy: the smaller of remaining money and
    # remaining heat, expressed in whole 1%-risk positions.
    by_heat = max(0, int(headroom // risk_pct)) if risk_pct > 0 else 0
    by_money = max(0, int((cap_rs - at_risk) // per_trade)) if per_trade > 0 else 0
    slots = min(by_heat, by_money)

    if used_pct >= cap_pct:
        status = "hot"
    elif used_pct >= cap_pct * 0.75:
        status = "warm"
    else:
        status = "ok"

    # "Other" is the bucket for symbols the universe file does not classify,
    # not a sector. Three unclassified names are not correlated risk.
    clusters = [s for s in sector_rows
                if s["count"] >= 3 and s["sector"] not in ("Other", "", None)]

    return {
        "capital": round(capital),
        "risk_budget": round(per_trade),
        "cap_pct": cap_pct,
        "cap_rupees": round(cap_rs),
        "open_count": sum(1 for r in rows if r["kind"] == "open"),
        "pending_count": sum(1 for r in rows if r["kind"] == "pending"),
        "at_risk": round(at_risk),
        "at_risk_pct": used_pct,
        "committed": round(committed),
        "committed_pct": committed_pct,
        "headroom_pct": headroom,
        "headroom_rupees": round(max(0, cap_rs - at_risk)),
        "slots": slots,
        "status": status,
        "sectors": sector_rows,
        "clusters": clusters,
        "rows": rows,
    }


def headline(h: dict) -> list[str]:
    """Plain sentences, worst fact first."""
    out = []
    out.append(f"{h['open_count']} open and {h['pending_count']} waiting: "
               f"₹{h['at_risk']:,.0f} at risk ({h['at_risk_pct']:.1f}% of capital), "
               f"₹{h['committed']:,.0f} more if every trigger fills "
               f"({h['committed_pct']:.1f}%).")
    if h["status"] == "hot":
        out.append(f"That is at or past the {h['cap_pct']:g}% heat cap. "
                   f"No new positions — the book is already the trade.")
    elif h["status"] == "warm":
        out.append(f"That is most of the {h['cap_pct']:g}% heat cap. "
                   f"At most {h['slots']} new position(s) fit.")
    else:
        out.append(f"Room for {h['slots']} more position(s) inside the "
                   f"{h['cap_pct']:g}% cap.")
    if h["committed_pct"] > h["cap_pct"]:
        out.append(f"If every pending trigger fills you would be at "
                   f"{h['committed_pct']:.1f}%, above the {h['cap_pct']:g}% cap — "
                   f"place the best few, not all of them.")
    for s in h["clusters"]:
        out.append(f"{s['sector']} carries {s['count']} names "
                   f"({', '.join(s['symbols'])}) — ₹{s['risk']:,.0f} of correlated risk.")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float)
    ap.add_argument("--risk-pct", type=float)
    ap.add_argument("--cap-pct", type=float)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    h = compute(args.capital, args.risk_pct, args.cap_pct)
    if args.json:
        print(json.dumps({k: v for k, v in h.items() if k != "rows"}, indent=1))
        return 0
    for line in headline(h):
        print(line)
    print()
    for r in sorted(h["rows"], key=lambda x: -x["risk"]):
        print(f"  {r['symbol']:12} {r['kind']:8} {r['qty']:>6} sh  "
              f"₹{r['risk']:>9,.0f}  {r['sector']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
