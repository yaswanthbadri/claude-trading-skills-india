# Cloud deployment — the desk runs without your Mac

The problem this solves: launchd only fires while the Mac is awake and logged
in. A shut lid at 09:00 means no plan; a sleeping Mac at 16:00 means no review.
Everything below runs on a machine that never sleeps.

## Option 0 — stay on this Mac for now (current setup)

Nothing below is required yet. The Mac runs the same jobs through launchd, and
two safety nets were added for the case you described:

1. **Watchdog** (`tools/watchdog.py`) runs at :25 past 09–12 and 16–18. If the
   morning plan or the closing review is missing — closed lid, crash, forced
   sleep — it rebuilds it once, but only while that is still useful: a plan
   before 13:00, a review before 19:00. Outside those windows it logs a gap
   instead of publishing something stale as if it were live.
2. **Cache check** in the same run. If the candle cache missed a session it
   refreshes it, so the next report cannot quietly show last week's prices.

Keep it awake for exact timing:

```bash
caffeinate -dimsu          # run in a terminal; Ctrl-C to stop
```
Plus System Settings → Lock Screen → **Prevent automatic sleeping when the
display is off**. Closing the lid still sleeps the machine, watchdog or not —
that is the one thing software cannot fix.

Everything below is the move to a machine that never sleeps. It is ready when
you are; no code changes needed.

## What you need

| Item | Cost | Notes |
|---|---|---|
| Ubuntu VPS, 1 vCPU / 1 GB RAM / 20 GB disk | ₹300–500/month, or free on Oracle Cloud's always-free ARM tier | Hetzner CX22, DigitalOcean basic, Vultr, Contabo all fine |
| Telegram bot token | free | @BotFather |

1 GB RAM is comfortable: the whole pipeline is CPU-light and stdlib-heavy. The
candle cache is ~2,400 small CSVs (about 120 MB) and grows slowly.

## Deploy

```bash
# 1. from this folder, on your Mac
deploy/sync.sh root@YOUR_SERVER_IP

# 2. on the server: installs Docker, sets the firewall, asks for secrets
ssh root@YOUR_SERVER_IP 'bash /opt/market-desk/deploy/vps_setup.sh'
```

The setup script installs Docker, allows only SSH through the firewall, writes
`.env` with mode 600, builds the image and starts one container. Nothing
listens on a public port — Telegram is polled, not webhooked, so there is no
inbound surface to attack.

## What happens on a trading day

All times IST, enforced inside the container regardless of server timezone:

| 08:50 | cache refresh |
| 09:00 | **the day's plan** |
| 09:15 → 15:15 | hourly bulletin |
| 15:40 | cache refresh after the close |
| 16:00 | **closing review + scorecard of our own calls** |
| every 5 min, 24×7 | news and index alerts |
| 19:30 | cache backstop |

Weekends and NSE holidays are skipped in code (`tools/market_calendar.py`).

## Operations

```bash
docker compose ps                       # is it up
docker compose logs -f --tail=50        # what it is doing
docker compose restart                  # bounce
docker compose up -d --build            # apply code changes
docker compose exec desk python tools/subscribers.py   # who gets reports
docker compose exec desk python tools/recs.py --show   # the scorecard
cat logs/health.json                    # last run of each job today
```

A missed run is not replayed hours later as if it were live: each job has a
grace window (10 minutes for intraday, 3–4 hours for the plan and the review),
after which it is logged as missed. That is deliberate — a 14:00 "morning plan"
is worse than an honest gap.

## Backups

Only three small files actually matter:

```bash
tar czf desk-state.tgz data/recommendations.json data/subscribers.json \
                        data/universe.csv logs/scheduler_state.json
```

The candle cache is disposable (100 seconds to rebuild); the scorecard is not.

## Without Docker

A plain systemd unit is in `deploy/systemd/market-desk.service`. It runs the
same `tools/scheduler.py`, so behaviour is identical.

## Two servers, one bot

Do not run this on the Mac and the VPS at the same time. Both would poll the
same bot, and Telegram gives each update to only one of them — commands would
be answered at random. Pick one home for the bot and stop the other:

```bash
# on the Mac, once the cloud desk is live
launchctl unload ~/Library/LaunchAgents/com.tradingskills.*.plist
```
