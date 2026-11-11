#!/usr/bin/env python3
"""Ad-hoc market alert monitor for India (NSE/BSE).

Runs every few minutes. Two independent scanners:

  NEWS  (24x7)   multi-feed RSS scan, keyword-tiered, deduped by headline.
                 CRITICAL items are sent immediately with no cooldown.
  PRICE (market  Nifty threshold crossings, India VIX regime changes, and
         hours)  single-stock shocks on a liquid universe.

Every alert is its own Telegram message so it lands instantly, with a plain
English "why it matters" note. No fixed-width tables, no spam: per-key
cooldowns and a global rate limit.

Usage:
    python3 tools/alert_monitor.py               # normal run (scheduler)
    python3 tools/alert_monitor.py --news-only
    python3 tools/alert_monitor.py --prices-only
    python3 tools/alert_monitor.py --dry-run     # print, never send
    python3 tools/alert_monitor.py --force       # send a test alert
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_calendar as cal

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / "logs" / "alert_state.json"
PYTHON = sys.executable

# ---------------------------------------------------------------------------
# news configuration
# ---------------------------------------------------------------------------
FEEDS = [
    ("ET Stocks", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
    ("ET Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("ET Economy", "https://economictimes.indiatimes.com/economy/rssfeeds/1373380680.cms"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Mint Markets", "https://www.livemint.com/rss/markets"),
]

NOISE = (
    "share price highlights",
    "stock price history",
    "stocks to watch",
    "market wrap",
    "top gainers and losers",
    "white marubozu",
    "closing above vwap",
    "rsi trending",
    "rose above their 200 dma",
    "candlestick",
)

# (regex, short label, why-it-matters)
CRITICAL = [
    (r"circuit breaker|trading halt|markets? (?:shut|halted)|suspend(?:ed|sion) of trading",
     "trading halt",
     "Exchange-level halt. Liquidity disappears and stop orders can fill far worse than planned."),
    (r"\bmarket crash\b|\bsensex crashes\b|\bnifty crashes\b|\bbloodbath\b|"
     r"worst (?:day|fall|crash) (?:for|in) (?:the )?(?:sensex|nifty|indian stocks|dalal street)",
     "market crash",
     "Broad risk-off. Correlations go to 1 — diversify by cash, not by stock."),
    (r"emergency (?:rate|policy)|off-cycle (?:rate|policy)|unscheduled (?:mpc|policy)",
     "emergency RBI action",
     "Off-cycle policy moves reprice banks, NBFCs and rate-sensitives instantly."),
    (r"\bdefault(?:s|ed)?\b|insolvency|bank run|collapse of|\bNPA fraud",
     "credit event",
     "Credit stress hits lenders and the whole funding chain, not just the company named."),
    (r"declares? war|war with|military strike|border (?:strike|escalation)|attack on",
     "geopolitical shock",
     "Geopolitical shocks hit crude first, then the rupee, then the whole index."),
    (r"SEBI (?:ban|bars|restrains)|F&O ban|trade-for-trade|GSM|ASM stage",
     "regulatory action",
     "Surveillance or bans cut liquidity and can force position exits."),
    (r"\bCBI\b|\bED\b raid|income tax raid|enforcement directorate|\bSFIO\b",
     "enforcement action",
     "Raids on promoters usually mean governance risk and a sharp de-rating."),
]

HIGH = [
    (r"\bRBI\b|repo rate|CRR|SLR|monetary policy|\bMPC\b",
     "RBI / policy",
     "Rate news moves banks, NBFCs, autos and realty first."),
    (r"\bFed\b|federal reserve|fomc|rate (?:cut|hike)|treasury yield",
     "US Fed / rates",
     "Global risk appetite. IT and metals react most; a strong dollar pressures FII flows."),
    (r"crude|brent|opec|oil price",
     "crude oil",
     "Oil hurts importers (paints, airlines, tyres) and helps upstream (ONGC, Oil India)."),
    (r"tariff|trade deal|export ban|import duty|anti-dumping",
     "trade policy",
     "Tariffs reprice exporters and import-heavy sectors within a session."),
    (r"inflation|\bCPI\b|\bWPI\b|GDP|IIP|fiscal deficit|current account",
     "macro data",
     "Macro prints move the index and the rate-sensitive basket together."),
    (r"\bFII\b|\bDII\b|block deal|bulk deal|stake sale|promoter (?:sell|buy)",
     "flows / deals",
     "Supply-demand shift in a single stock; block deals often print at a discount."),
    (r"\bQ[1-4]\b results|earnings|profit (?:jumps|falls|surges|plunges)|revenue (?:up|down)|guidance cut",
     "results",
     "Results gaps of 5–15% jump any stop. Check whether your position reports today."),
    (r"merger|acquisition|open offer|delisting|demerger|buyback",
     "corporate action",
     "Corporate actions reprice the stock and sometimes its peers in the same sector."),
    (r"\bIPO\b|listing (?:gain|day)|anchor (?:investor|book)|grey market",
     "primary market",
     "IPO news pulls liquidity and attention away from listed small caps."),
    (r"downgrade|upgrade|target price|brokerage (?:cut|raise)",
     "analyst action",
     "Rating changes move institutional flows over the following sessions."),
]

WHY_FALLBACK = "Material headline for Indian equities — check whether you hold the name."

# An alert must be about India. Global-only items (US futures, European shares,
# dollar/sterling moves) are noisy and mostly irrelevant to an NSE trader.
INDIA_SIGNALS = re.compile(
    r"\bnifty\b|\bsensex\b|\bbse\b|\bnse\b|\brbi\b|\bsebi\b|\brupee\b|\bindia\b|"
    r"\bindian\b|\bcrore\b|\blakh\b|dalal street|\bmcap\b|gift nifty|\bfii\b|\bdii\b|"
    r"\bipo\b|\bqip\b|\bmutual fund\b|\bsip\b|\bdemat\b",
    re.I,
)

COMPANY_HINTS = re.compile(
    r"reliance|hdfc|icici|tcs|infosys|infy|sbi|state bank|bharti|airtel|itc|"
    r"larsen|ltim|kotak|axis bank|bajaj|maruti|mahindra|sun pharma|titan|"
    r"ultratech|asian paints|hindustan unilever|nestle|tata|adani|jsw|hindalco|"
    r"dr reddy|cipla|divis|tech mahindra|hcl|wipro|power grid|ntpc|ongc|"
    r"coal india|bpcl|iocl|indian oil|bel|bharat electronics|hal|irctc|zomato|"
    r"swiggy|paytm|nykaa|pnb|bank of baroda|idfc|yes bank|vedanta|dlf|godrej|"
    r"lupin|aurobindo|muthoot|cholamandalam|shriram|lic|gail|sail|nmdc|"
    r"jubilant|page industries|polycab|havells|siemens|abb|bosch|cummins",
    re.I,
)

# Liquid universe for single-stock shock alerts.
ALERT_UNIVERSE = (
    "RELIANCE,HDFCBANK,ICICIBANK,TCS,INFY,SBIN,BHARTIARTL,ITC,LT,KOTAKBANK,"
    "AXISBANK,BAJFINANCE,MARUTI,M&M,SUNPHARMA,TITAN,ULTRACEMCO,ASIANPAINT,"
    "HINDUNILVR,NESTLEIND,TMPV,TATASTEEL,JSWSTEEL,HINDALCO,ADANIENT,"
    "ADANIPORTS,BAJAJFINSV,BAJAJ-AUTO,HEROMOTOCO,EICHERMOT,DRREDDY,CIPLA,"
    "DIVISLAB,TECHM,HCLTECH,WIPRO,POWERGRID,NTPC,ONGC,COALINDIA,BPCL,IOC,"
    "BEL,HAL,IRCTC,ETERNAL,DMART,PNB,BANKBARODA,IDFCFIRSTB,DLF"
)

STOCK_MOVE_PCT = 4.5
NIFTY_STEP_PCT = 1.0
VIX_LEVELS = (18, 20, 25, 30)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def load_env() -> None:
    env = REPO / ".env"
    if not env.exists():
        return
    import os
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"seen_news": {}, "last_by_key": {}, "nifty_bucket": 0, "vix_bucket": 0,
            "sent_log": []}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    # keep the state bounded
    seen = s.get("seen_news", {})
    if len(seen) > 2000:
        keep = sorted(seen.items(), key=lambda kv: kv[1])[-2000:]
        s["seen_news"] = dict(keep)
    s["sent_log"] = (s.get("sent_log") or [])[-200:]
    STATE.write_text(json.dumps(s, indent=2, default=str))


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _hash(title: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def _cooled(state: dict, key: str, minutes: int) -> bool:
    """True when this key is still inside its cooldown."""
    last = state.get("last_by_key", {}).get(key)
    if not last:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(last) < timedelta(minutes=minutes)
    except Exception:
        return False


def _mark(state: dict, key: str) -> None:
    state.setdefault("last_by_key", {})[key] = datetime.now().isoformat(timespec="seconds")


def _global_rate_ok(state: dict, max_per_30min: int = 8) -> bool:
    cutoff = datetime.now() - timedelta(minutes=30)
    recent = 0
    for ts in (state.get("sent_log") or [])[-60:]:
        try:
            if datetime.fromisoformat(ts) > cutoff:
                recent += 1
        except Exception:
            continue
    return recent < max_per_30min


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(text: str, dry: bool = False) -> bool:
    if dry:
        print("\n----- DRY RUN MESSAGE -----")
        print(re.sub(r"<[^>]+>", "", text))
        return True
    import os
    import telegram_bulletin as tb
    tb.load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Missing Telegram credentials", file=sys.stderr)
        return False
    tb.send_message(token, chat_id, text)
    return True


# ---------------------------------------------------------------------------
# news scanner
# ---------------------------------------------------------------------------
def _parse_feed(name: str, url: str):
    try:
        r = requests.get(url, timeout=12,
                         headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
        if r.status_code != 200:
            return []
    except Exception:
        return []

    items = []
    # ET-style feeds use CDATA; others put plain text in <title>.
    for m in re.finditer(
        r"<item>(.*?)</item>", r.text, re.S | re.I
    ):
        block = m.group(1)
        tm = re.search(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", block, re.S | re.I)
        lm = re.search(r"<link>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</link>", block, re.S | re.I)
        if not tm:
            continue
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", tm.group(1))).strip()
        link = (lm.group(1).strip() if lm else "")
        if not title:
            continue
        items.append({"title": title, "link": link, "source": name})
    return items


def _classify(title: str):
    low = title.lower()
    for table, tier in ((CRITICAL, "CRITICAL"), (HIGH, "HIGH")):
        for pattern, label, why in table:
            if re.search(pattern, low, re.I):
                return tier, label, why
    return None, None, None


def scan_news(state: dict, dry: bool) -> list[str]:
    seen = state.setdefault("seen_news", {})
    now_iso = datetime.now().isoformat(timespec="seconds")
    # First ever run: learn the current headlines instead of dumping them all.
    warm_up = not seen
    found = []
    for name, url in FEEDS:
        for item in _parse_feed(name, url):
            low = item["title"].lower()
            if any(n in low for n in NOISE):
                continue
            h = _hash(item["title"])
            if h in seen:
                continue
            seen[h] = now_iso
            if warm_up:
                continue

            tier, label, why = _classify(item["title"])
            if not tier:
                continue
            # HIGH-tier items must actually be about India; CRITICAL always passes.
            if tier != "CRITICAL" and not (
                INDIA_SIGNALS.search(item["title"]) or COMPANY_HINTS.search(item["title"])
            ):
                continue
            if not _global_rate_ok(state):
                continue
            if tier != "CRITICAL" and _cooled(state, f"news:{label}", 45):
                # already alerted this category recently; still record as seen
                continue

            icon = "🚨" if tier == "CRITICAL" else "📰"
            stamp = datetime.now().strftime("%d %b %Y, %H:%M IST")
            body = (
                f"{icon} <b>{tier} · {esc(label)}</b>\n"
                f"<i>{stamp}</i>\n"
                f"───────────────\n"
                f"<b>{esc(item['title'])}</b>\n"
                f"<i>{esc(item['source'])}</i>\n\n"
                f"<i>Why it matters: {esc(why)}</i>"
            )
            if item["link"]:
                body += f"\n<a href=\"{esc(item['link'])}\">Read the story</a>"
            body += "\n\n<i>Educational only — not advice.</i>"

            if send_telegram(body, dry=dry):
                found.append(item["title"])
                state.setdefault("sent_log", []).append(now_iso)
                _mark(state, f"news:{label}")
            if len(found) >= 4:      # cap a single run so we never spam
                return found
    return found


# ---------------------------------------------------------------------------
# price scanner
# ---------------------------------------------------------------------------
def _nifty_and_vix():
    out = {}
    try:
        import yfinance as yf
    except Exception:
        return out
    try:
        df = yf.download(["^NSEI", "^INDIAVIX"], period="5d", interval="1d",
                         progress=False, auto_adjust=False, threads=True)
        closes = df["Close"]
        for col, name in (("^NSEI", "nifty"), ("^INDIAVIX", "vix")):
            if col not in closes:
                continue
            series = closes[col].dropna()
            if len(series) >= 2:
                last, prev = float(series.iloc[-1]), float(series.iloc[-2])
                out[name] = {"close": last, "change_pct": (last / prev - 1) * 100}
    except Exception:
        pass
    return out


def scan_prices(state: dict, dry: bool) -> list[str]:
    sent = []
    data = _nifty_and_vix()
    nifty = data.get("nifty")
    vix = data.get("vix")

    if nifty:
        chg = nifty["change_pct"]
        bucket = int(abs(chg) // NIFTY_STEP_PCT)
        if bucket > state.get("nifty_bucket", 0):
            state["nifty_bucket"] = bucket
            direction = "up" if chg > 0 else "down"
            icon = "🚀" if chg > 0 else "🔻"
            body = (
                f"{icon} <b>Nifty 50 {direction} {chg:+.2f}%</b>\n"
                f"<i>{datetime.now().strftime('%d %b %Y, %H:%M IST')}</i>\n"
                f"───────────────\n"
                f"Index at <b>{nifty['close']:,.2f}</b>.\n\n"
                f"<i>Why it matters: a move of this size usually means a macro or global "
                f"catalyst, not stock-specific news. Check breadth before adding risk.</i>"
            )
            if send_telegram(body, dry=dry):
                sent.append("nifty")
                state.setdefault("sent_log", []).append(datetime.now().isoformat(timespec="seconds"))
    if nifty and abs(nifty["change_pct"]) < NIFTY_STEP_PCT * 0.5:
        state["nifty_bucket"] = 0

    if vix:
        level = vix["close"]
        bucket = max([b for b in VIX_LEVELS if level >= b], default=0)
        if bucket > state.get("vix_bucket", 0):
            state["vix_bucket"] = bucket
            body = (
                f"⚠️ <b>India VIX through {bucket}</b>\n"
                f"<i>{datetime.now().strftime('%d %b %Y, %H:%M IST')}</i>\n"
                f"───────────────\n"
                f"VIX at <b>{level:.2f}</b> ({vix['change_pct']:+.1f}%).\n\n"
                f"<i>Why it matters: implied volatility is repricing. Option premiums get "
                f"expensive and stop distances need widening — or size down.</i>"
            )
            if send_telegram(body, dry=dry):
                sent.append("vix")
                state.setdefault("sent_log", []).append(datetime.now().isoformat(timespec="seconds"))
        elif level < VIX_LEVELS[0] * 0.8:
            state["vix_bucket"] = 0

    # single-stock shocks
    try:
        import yfinance as yf
        symbols = [s for s in ALERT_UNIVERSE.split(",") if s]
        tickers = [f"{s}.NS" for s in symbols]
        df = yf.download(tickers, period="5d", interval="1d",
                         progress=False, auto_adjust=False, threads=True)
        closes = df["Close"]
        hits = []
        for sym, ticker in zip(symbols, tickers):
            if ticker not in closes:
                continue
            series = closes[ticker].dropna()
            if len(series) < 2:
                continue
            last, prev = float(series.iloc[-1]), float(series.iloc[-2])
            chg = (last / prev - 1) * 100
            if abs(chg) >= STOCK_MOVE_PCT:
                hits.append((sym, last, chg))
        hits.sort(key=lambda x: -abs(x[2]))
        for sym, last, chg in hits[:3]:
            if _cooled(state, f"stock:{sym}", 90):
                continue
            icon = "📈" if chg > 0 else "📉"
            body = (
                f"{icon} <b>{esc(sym)} {chg:+.2f}%</b>\n"
                f"<i>{datetime.now().strftime('%d %b %Y, %H:%M IST')}</i>\n"
                f"───────────────\n"
                f"Last <b>{last:,.2f}</b>.\n\n"
                f"<i>Why it matters: a one-stock move this large is usually news or a block "
                f"deal. Check for results, orders or a stake sale before acting.</i>"
            )
            if send_telegram(body, dry=dry):
                sent.append(sym)
                _mark(state, f"stock:{sym}")
                state.setdefault("sent_log", []).append(datetime.now().isoformat(timespec="seconds"))
    except Exception as e:
        print(f"[price scan skipped: {e}]", file=sys.stderr)

    return sent


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    force = "--force" in sys.argv
    dry = "--dry-run" in sys.argv
    news_only = "--news-only" in sys.argv
    prices_only = "--prices-only" in sys.argv

    load_env()
    state = load_state()

    if force:
        send_telegram(
            "🔔 <b>Test alert</b>\n"
            f"<i>{datetime.now().strftime('%d %b %Y, %H:%M IST')}</i>\n\n"
            "Automation is alive. News alerts run 24×7, price alerts during market hours.",
            dry=dry,
        )
        save_state(state)
        return 0

    sent = []
    if not prices_only:
        sent += scan_news(state, dry)

    if not news_only and cal.is_market_open():
        sent += scan_prices(state, dry)
    elif not news_only:
        print(f"Market closed ({datetime.now():%H:%M}) — news only.")

    if not dry:
        save_state(state)
    print(f"{len(sent)} alert(s) sent." if sent else "No alerts triggered.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
