#!/usr/bin/env python3
"""Ping one person when a stock on *their* watchlist clears its pivot.

The broadcast knows about six names a day. A watchlist is the other question:
"tell me when the thing I am stalking actually goes." That answer is personal,
so it is addressed to one chat, never to the audience.

The pivot is captured at /watch time, not looked up here. If the level moves
the alert means nothing — you asked to be told about 4,230, and 4,198 clearing
later is not the same event.

Once per person per symbol per day. A stock that gaps 4% over its pivot at
09:20 does not need to be re-announced at 09:25, 09:30 and 09:35.

    python3 tools/watch_scan.py            # send
    python3 tools/watch_scan.py --dry-run  # print, send nothing
    python3 tools/watch_scan.py --all-hours
"""
from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import market_calendar as cal  # noqa: E402
import subscribers as sub  # noqa: E402


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _already_today(entry: dict) -> bool:
    when = (entry or {}).get("notified") or ""
    return str(when).startswith(_today())


def live_prices(symbols: list[str]) -> dict[str, float]:
    """Last traded price for a handful of symbols in one download.

    yfinance returns a MultiIndex column frame for several tickers and a
    single-level one for a single ticker; both shapes are handled, because the
    alternative is a silent KeyError on the one-person watchlist.
    """
    if not symbols:
        return {}
    out: dict[str, float] = {}
    try:
        import yfinance as yf
        tickers = [f"{s}.NS" for s in symbols]
        df = yf.download(tickers, period="5d", interval="1d", progress=False,
                         auto_adjust=False, threads=True, group_by="ticker")
        if df is None or df.empty:
            return {}
        for sym, tk in zip(symbols, tickers):
            try:
                col = df[tk]["Close"].dropna() if len(symbols) > 1 else df["Close"].dropna()
                if len(col):
                    out[sym] = float(col.iloc[-1])
            except Exception:
                continue
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] price download failed: {exc}", file=sys.stderr)

    # Anything the batch missed gets one more try on its own. A single name
    # failing should not lose the whole scan.
    for sym in symbols:
        if sym in out:
            continue
        try:
            import yfinance as yf
            df = yf.download(f"{sym}.NS", period="5d", interval="1d",
                             progress=False, auto_adjust=False)
            if df is not None and not df.empty:
                cols = df.columns
                if hasattr(cols, "levels"):
                    for i in range(cols.nlevels):
                        if "Close" in cols.get_level_values(i):
                            df.columns = cols.get_level_values(i)
                            break
                col = df["Close"].dropna()
                if len(col):
                    out[sym] = float(col.iloc[-1])
        except Exception:
            continue
    return out


def _message(sym: str, level: float, last: float, kind: str) -> str:
    pct = (last / level - 1) * 100 if level else 0.0
    head = {
        "pivot": "🔔 <b>{0} cleared your pivot</b>",
        "high": "🔔 <b>{0} made a new high</b>",
    }.get(kind, "🔔 <b>{0} cleared your level</b>").format(_esc(sym))
    return (
        f"{head}\n"
        f"<i>{datetime.now().strftime('%d %b %Y, %H:%M IST')}</i>\n"
        f"───────────────\n"
        f"Level <b>{level:,.2f}</b> · now <b>{last:,.2f}</b> ({pct:+.1f}%)\n"
        f"<i>You asked to be told when this one went. It has.</i>\n\n"
        f"<i>Educational only — not advice.</i>"
    )


def scan(dry: bool = False, all_hours: bool = False,
         only: str | None = None) -> list[str]:
    """Return a list of 'chat:symbol' that fired."""
    watchers = sub.all_watchers()
    if not watchers:
        print("No watchlists yet.")
        return []

    if not all_hours:
        if not cal.is_trading_day():
            print("Not a trading day — watch scan skipped.")
            return []
        if not (cal.is_market_open() or dry):
            print("Market closed — watch scan skipped.")
            return []

    rows = [w for w in watchers if (only is None or w[0] == str(only))]
    rows = [w for w in rows if not _already_today(w[2])]
    if not rows:
        print("Every watch already notified today.")
        return []

    # A snoozed or fully-silenced person does not want this either — even
    # though they asked for it, they asked not to be disturbed more recently.
    live: list[tuple[str, str, dict]] = []
    for cid, sym, entry in rows:
        if sub.snoozed(cid):
            continue
        if sub.mode_of(cid) == "none":
            continue
        live.append((cid, sym, entry))
    if not live:
        print("All watchers are snoozed or silenced.")
        return []

    prices = live_prices(sorted({sym for _c, sym, _e in live}))
    if not prices:
        print("No prices available — nothing to compare.")
        return []

    fired: list[str] = []
    for cid, sym, entry in live:
        last = prices.get(sym)
        level = entry.get("pivot")
        if last is None or not level:
            continue
        kind = entry.get("kind") or "pivot"
        if last < float(level):
            continue
        body = _message(sym, float(level), last, kind)
        if dry:
            print("\n----- DRY RUN (to one chat) -----")
            print(f"chat {cid}:")
            print(body.replace("<b>", "").replace("</b>", "")
                      .replace("<i>", "").replace("</i>", ""))
            fired.append(f"{cid}:{sym}")
            continue
        import buttons
        import notify
        try:
            res = notify.send(body, chat_id=cid, kind="level",
                              reply_markup=buttons.watch(sym))
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] watch alert to {cid} failed: {exc}", file=sys.stderr)
            continue
        if res.get("sent"):
            sub.mark_notified(cid, sym)
            fired.append(f"{cid}:{sym}")
            print(f"  → {sym} {float(level):,.2f} → {last:,.2f} for chat {cid}")
    return fired


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all-hours", action="store_true")
    ap.add_argument("--chat", default=None, help="only this chat id")
    args = ap.parse_args()

    try:
        import notify
        notify.load_env()
    except Exception:
        pass

    fired = scan(dry=args.dry_run, all_hours=args.all_hours, only=args.chat)
    if not fired:
        print("No watch alerts.")
    elif args.dry_run:
        print(f"{len(fired)} watch alert(s) would be sent.")
    else:
        print(f"{len(fired)} watch alert(s) sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
