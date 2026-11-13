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

### Stale orders

If a published trigger stops making sense — the price has walked 3% or more
away from the level you were told to buy above — the desk tells you that
same day, in the alerts feed, in `/book` and in the closing review. A GTT
sitting on a level nobody is defending is money held out of your risk
budget for nothing, and nothing else will cancel it for you.

### Watchlists

`/watch SYMBOL` pins a level **at the moment you add it** — the top of its base, or its 52-week
high if it has no base, or the next 2% up if it is already through the obvious level. You get one
message when it clears that level, once a day, addressed to you and nobody else.

### The gate: nothing is published that cannot be bought

Every suggestion — in the report, in `/check`, in an alert — goes through one
gate (`tools/plan_gate.py`). A suggestion is published only if all of these
hold:

| Rule | Why |
|---|---|
| the trigger is at most 3% above the price | a level 10% higher is not filled, and the "risk" is fiction |
| the trigger is not already below the price | the breakout happened; buying now is chasing |
| the stop is below the price | a long stop above the market is nonsense |
| Stage 2 and a grade A or B base | a grade C base in a downtrend is not a setup |
| the price is within 1.5% of the pivot | beyond that it is chasing, not buying |
| the trigger was not touched and failed in the last 5 sessions | that is a fresh failure, not a setup |

A rejected plan is never printed as an order. The card says what is wrong
instead: *"the trigger at 1,295.70 is 10.0% above the price — nobody is
getting filled"*. A level you cannot buy is not a plan.

An old failure is a warning, not a rejection: a level that failed three weeks
ago and has been rebuilt since is published with *"this level failed before —
a second attempt is a weaker setup, so size it down"*.

### Base rates

`/check` measures what followed the same situation before, over every name in
the local cache: a +5% day on 1.5x volume in an uptrend, a close above the
20-day range high, a pullback into the 20-day average that held, a 15% run in
10 sessions, a stock already 15% above its range high.

For each, the forward return at 5, 10 and 20 sessions is kept — how often it
worked, the median outcome, and the spread. The same measurement is run for
the single stock in front of you, because a stock's own record is more
relevant than the market's average.

These are base rates, not forecasts. The market figure counts stock-days, not
independent events, and the card says so.

## 5. Event risk — what else lands while you hold it

A swing trade is held for weeks, and something always happens in those
weeks: an expiry, an RBI decision, a budget, the day results season ends and
half the market gaps. `tools/event_risk.py` reads the event calendar (the
`event-calendar` skill) and asks one narrow question about the window
between the entry and the second target.

Every published plan carries the answer on its card:

> 📅 Inside your holding window: 23 Nov: NSE monthly expiry; 26 Nov: BSE
> monthly expiry; 04 Dec: RBI MPC policy decision. Size the trade knowing
> these are there.

Weekly expiries are dropped once the monthly one is in the list — a month of
expiry dates is a calendar, not risk.

## 6. Results ahead — buy the setup, not the gap

Being told "TCS reported and rose 6%" is worth nothing: the move has already
happened. So the desk works the other way round.

`tools/results_calendar.py` pulls Yahoo's earnings history for the liquid
names — it carries the **next scheduled date** (confirmed, not inferred) and
the surprise for every past print. `tools/pre_results.py` then says which
companies report inside the next week, what the chart looks like now, and
what the market has done after this stock's last few prints.

The plan for a results trade is different from a breakout plan, and the card
says so: **a stop order does not protect you through the gap**, because the
exchange is shut. The number behind it is measured, not guessed — across
~900 prints in this market the median overnight gap is +0.4% and the median
five-session move is +0.3%, positive 53% of the time. That is not an edge.
It is a small bet on a big day, sized accordingly, and the card says that.

A results alert goes out **before** the print, only for names whose chart is
ready, and once per day per name.

## 7. How a call is graded

The desk publishes, stores, and then replays. A plan is keyed on the session it is *for*, not the
day it was published.

- Decide on the close, fill at the next open.
- A gap through the chase limit = **no fill** — the trade never happened.
- A bar that touches both the stop and the target = **stop first**.
- Verdicts come only from **finished** candles (after 15:35).
- Only `daily` and `eod` runs record new plans; intraday grades the book and never rewrites it.
- Net R is after Indian charges, not before.

## 8. Where things live

| File | Job |
|---|---|
| `tools/daily_bulletin.py` | builds the report (md + json + html) |
| `tools/bulletin_data.py` | collects candidates, heat cap, delivery, tradability |
| `tools/bulletin_render.py` | renders cards and the styled HTML — one source of truth for verdicts |
| `tools/stock_check.py` | the single-symbol pipeline behind `/check` |
| `tools/forward_plan.py` | what to do with a stock in its current state |
| `tools/tech_detail.py` | RSI, ATR, ADX, moving averages, volume, extension |
| `tools/base_rates.py` | what happened after the same setup, market-wide and per stock |
| `tools/plan_gate.py` | the one gate every suggestion has to pass |
| `tools/results_calendar.py` | who reports, when — confirmed dates and history |
| `tools/pre_results.py` | the results playbook: the setup, the plan, the base rate |
| `tools/event_risk.py` | what lands inside a plan's holding window |
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

## 9. Settings

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

## 10. Tests

```bash
/opt/homebrew/bin/python3 -m unittest discover -s tests -p 'test_desk*.py'
```

Every bug found while building this was a small pure function that failed silently — a reversed
tuple unpack, a dropped column level, a truthiness test on `"none"`. They are all pinned here.
