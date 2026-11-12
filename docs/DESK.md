# The desk — operator's guide

An automated market-intelligence desk for NSE/BSE: a plan at 09:00, a review at 16:00, hourly
briefings during the session, instant alerts on stock-moving events, and a Telegram bot you can
actually talk to.

Everything here is **educational analysis, not SEBI-registered advice**. Every plan carries an
entry, a stop and two targets; every claim about what happened next is replayed from stored
candles, not remembered.

---

## 1. The schedule (IST)

| When | What | Job |
|---|---|---|
| 08:50 | refresh the candle cache before the open | `build_cache.py` |
| 09:00 | the day's plan — regime, setups, entry / stop / targets | `daily_bulletin.py --mode daily` |
| hourly :15 | what changed since the last card | `daily_bulletin.py --mode intraday` |
| 09:15–15:15 | stop / T1 / T2 breaches on the open book | `levels_watch.py` (inside the alerts job) |
| every 5 min | news events, price shocks, watchlist pings | `alert_monitor.py` |
| 15:40 | close-of-day cache | `build_cache.py` |
| 16:00 | how today's calls actually did | `daily_bulletin.py --mode eod` |
| 19:45 | nightly snapshot to the owner | `backup.py` |

On the Mac these run through launchd (`tools/install_schedule.py`). In the cloud they run through
`tools/scheduler.py`, which is one always-on loop — no cron, no launchd.

Weekends and holidays are handled in code (`tools/market_calendar.py`), not by the scheduler.

## 2. The bot

Ask it things. It answers in a card, not a paragraph.

```
/check CENTUM      one stock: trend, base, pivot, plan, verdict
/book              open positions with live R and the next target
/heat              what the whole book is risking, and what still fits
/watch CENTUM      ping me when it clears its pivot
/unwatch CENTUM    stop
/watchlist         what you are stalking, and how far away it is
/mode              all | quiet | alerts | none
/status            how fresh the last report is
/report            resend the latest full report
/start [code]      join
/stop              leave
```

Owner only: `/who`, `/run daily|intraday|eod|alerts|backup`, `/errors`, `/invite`.

Alerts arrive with inline buttons — **Book**, **Check**, **Snooze 2h** — so acting on one is a tap.

### Invites

Set `TELEGRAM_INVITE_CODE` in `.env`. A new person sends `/start DESK-MCXNU`. Rotate the code by
changing it in `.env` and restarting the listener; existing subscribers are unaffected. Leave it
unset and anyone who finds the bot can join.

### Per-person modes

Silence should be a setting, not an unsubscribe.

| Mode | You get |
|---|---|
| `all` (default) | everything: plan, hourly cards, review, alerts, level breaches |
| `quiet` | the 09:00 plan and the 16:00 review only |
| `alerts` | alerts and level breaches only — no scheduled reports |
| `none` | nothing until you change it back |

A snooze (the button, or `/mode` after 2 hours of quiet) mutes alerts and level breaches but
never the plan.

### Watchlists

`/watch SYMBOL` pins a level **at the moment you add it** — the top of its base, or its 52-week
high if it has no base, or the next 2% up if it is already through the obvious level. You get one
message when it clears that level, once a day, addressed to you and nobody else.

## 3. How a call is graded

The desk publishes, stores, and then replays. A plan is keyed on the session it is *for*, not the
day it was published.

- Decide on the close, fill at the next open.
- A gap through the chase limit = **no fill** — the trade never happened.
- A bar that touches both the stop and the target = **stop first**.
- Verdicts come only from **finished** candles (after 15:35).
- Only `daily` and `eod` runs record new plans; intraday grades the book and never rewrites it.
- Net R is after Indian charges, not before.

## 4. Where things live

| File | Job |
|---|---|
| `tools/daily_bulletin.py` | builds the report (md + json + html) |
| `tools/bulletin_data.py` | collects candidates, heat cap, delivery, tradability |
| `tools/bulletin_render.py` | renders cards and the styled HTML — one source of truth for verdicts |
| `tools/stock_check.py` | the single-symbol pipeline behind `/check` |
| `tools/alert_monitor.py` | news events, price shocks, watchlist pings |
| `tools/levels_watch.py` | stop / T1 / T2 breaches on the open book |
| `tools/recs.py` | the book: record, replay, scorecard, attribution |
| `tools/heat.py` | total risk, sector clusters, remaining slots |
| `tools/integrity.py` | detects and repairs impossible one-day price jumps |
| `tools/tradability.py` | price band, series, F&O ban |
| `tools/delivery.py` | delivery % against the stock's own 20-session average |
| `tools/subscribers.py` | who gets what: modes, snooze, watchlists |
| `tools/bot_listener.py` | the bot: command dispatch and inline buttons |
| `tools/scheduler.py` | the always-on loop (cloud) |
| `tools/watchdog.py` | notices a missed report and repairs it |

State lives in `data/` (gitignored): `recommendations.json`, `subscribers.json`, `candles/`,
`breaks.json`, `tradability.json`, `delivery/`, `backups/`. Logs in `logs/`, reports in `reports/`.

## 5. Settings

All optional, all in `.env` (see `.env.example`):

| Key | Default | Meaning |
|---|---|---|
| `TRADING_CAPITAL` | 500000 | capital used for sizing and heat |
| `RISK_PCT` | 1 | percent of capital risked per trade |
| `MAX_HEAT_PCT` | 6 | total risk, all positions, that the book may carry |
| `BROKER` | zerodha | whose charges apply |
| `TELEGRAM_CHAT_ID` | — | the owner's chat; seeded into the subscriber list |
| `TELEGRAM_BOT_TOKEN` | — | required |
| `TELEGRAM_INVITE_CODE` | — | if set, `/start` needs it |

## 6. Tests

```bash
/opt/homebrew/bin/python3 -m unittest discover -s tests -p 'test_desk*.py'
```

Every bug found while building this was a small pure function that failed silently — a reversed
tuple unpack, a dropped column level, a truthiness test on `"none"`. They are all pinned here.
