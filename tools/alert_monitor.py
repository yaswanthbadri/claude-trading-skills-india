#!/usr/bin/env python3
"""Stock-event alerts: only when something moves a *particular* stock.

What this replaced, and why
    The first version pushed index moves, VIX levels and generic macro
    headlines. That is noise: "Nifty +1%" tells you nothing you can trade.
    This version asks one question of every headline:

        which single NSE stock does this move, and by how much is it moving?

    If it cannot name the stock, it does not send anything.

Three scanners
    LEVELS  the positions we already told you to take: stop, T1, T2 and
            unfilled triggers. This is the only alert about money you have
            at risk, so it runs first.
    EVENTS  A headline is matched to one listed company, the event type must be
            one that reprices a stock (results, order win, regulatory action,
            rating change, deal, fraud, guidance), and the card carries the
            live reaction: last price, % move, volume vs its own average.
    SHOCKS  A liquid stock moves 6%+ on 1.8x+ its normal volume with no
            headline needed. The tape is the news.

Never sent: index levels, VIX milestones, "stocks to watch", market wraps,
policy commentary that names no company. Educational only, not advice.

Usage:
    python3 tools/alert_monitor.py               # normal run (scheduler)
    python3 tools/alert_monitor.py --news-only
    python3 tools/alert_monitor.py --prices-only
    python3 tools/alert_monitor.py --dry-run
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

import buttons
import market_calendar as cal

REPO = Path(__file__).resolve().parents[1]
STATE = REPO / "logs" / "alert_state.json"
# Every alert ever sent, with the headline that caused it. Without this, a
# false positive is invisible after the fact: the state file only remembers
# which headlines were seen, not what was actually delivered.
AUDIT = REPO / "logs" / "alerts.jsonl"
PYTHON = sys.executable

# ---------------------------------------------------------------------------
# news configuration
# ---------------------------------------------------------------------------
FEEDS = [
    ("ET Stocks", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
    ("ET Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("ET Industry", "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Mint Markets", "https://www.livemint.com/rss/markets"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/business.xml"),
]

# Headline shapes that never carry a tradable stock event.
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
    "technical analysis",
    "mutual fund",
    "sip",
    "gold rate",
    "rupee",
)

# Only events that reprice one company. Pattern, label, plain-English reason.
EVENTS = [
    (r"\bq[1-4]\b.*(?:results|profit|revenue|earnings)|(?:net profit|revenue|ebitda|margin)"
     r"\s*(?:jumps?|falls?|rises?|slumps?|surges?|plunges?|beats?|misses?)|"
     r"results (?:today|tomorrow)|profit warning",
     "results",
     "Results gaps of 5-15% jump any stop. If you hold it, the stop is now the open, "
     "not yesterday's close."),
    (r"(?:wins?|bags?|secures?|receives?|clinches?|awarded|awards?|sanctioned)"
     r"\s+[^;\n]{0,45}?(?:order|contract|deal|mandate|loa)\b|"
     r"(?:order|contract) worth|letter of award",
     "order win",
     "A large order changes forward earnings. Check the size against annual revenue, "
     "not against the share price."),
    (r"\bsebi\b.*(?:ban|bar|restrain|penalt|fine)|f&o ban|trade[- ]for[- ]trade|"
     r"\bgsm\b|\basm\b|surveillance measure|suspended from trading",
     "regulatory action",
     "Surveillance cuts liquidity and can force exits. Positions get harder to leave, "
     "not cheaper to hold."),
    (r"rating (?:cut|downgrade|upgrade)|downgrades? .* to (?:sell|reduce)|"
     r"upgrades? .* to (?:buy|add)|target price (?:cut|raised|hiked|slashed)",
     "analyst action",
     "Ratings move institutional flows over the next few sessions, not minutes. "
     "Useful for direction, useless for timing."),
    (r"block deal|bulk deal|stake (?:sale|buy|purchase)|promoter (?:sells?|buys?|pledges?)|"
     r"open offer|delisting|merger|acquisition|demerger|stake sale|qip|"
     r"fund[- ]?rais\w*|raises? (?:rs\.?|₹)?\s?[\d,]+ crore",
     "deal / ownership",
     "Supply and control change hands. Block deals often print at a discount to the "
     "prevailing price, which caps the stock that day."),
    (r"fraud|raid|probe|investigation|default|insolvency|\bnclt\b|recall|"
     r"accounting irregularit|whistleblower",
     "risk event",
     "Governance and solvency events de-rate a stock for months, not days. "
     "This is an exit signal, not a dip."),
    (r"(?:cuts?|raises?|lifts?|lowers?|upgrades?|downgrades?|trims?)\s+"
     r"(?:its\s+)?(?:fy\d{2}\s+)?(?:guidance|outlook)|guidance (?:cut|raised|lifted)|"
     r"capex plan|new plant|capacity expansion|commission(?:s|ed) .* plant|"
     r"joint venture|\bjv\b",
     "capex / guidance",
     "Forward-looking capex and guidance move the multiple. Verify against cash flow, "
     "not the press release."),
    (r"(?:reports?|posts?|registers?|logs?)\s+[^;\n]{0,30}?\b(?:sales|dispatches|"
     r"production|volumes)\b",
     "monthly numbers",
     "Volume disclosures are the monthly heartbeat of autos and FMCG. A miss here "
     "usually moves the stock more than the quarterly print did."),
    (r"buyback|bonus issue|stock split|rights issue|dividend",
     "capital action",
     "Mechanical price change or cash return. Adjust your levels before the ex-date "
     "or your stop will trigger on nothing."),
]

# Kept deliberately tiny: a trading halt or a market-wide crash is the only
# non-stock-specific event worth waking someone for.
SYSTEMIC = [
    (r"circuit breaker|trading halt|markets? (?:shut|halted)|suspension of trading",
     "exchange halt",
     "Trading stops. Stops and targets cannot fill until it reopens, and gaps do the "
     "pricing when it does."),
    (r"\bmarket crash\b|\bsensex crashes\b|\bnifty crashes\b|worst (?:day|fall|crash)",
     "market-wide fall",
     "Everything correlates in a crash. The only real diversifier today is cash."),
]

WHY_FALLBACK = "Material event for this company — check whether you hold it."

# Event types that routinely appear without moving the stock. For these, the
# tape has to agree: at least a 1% move or 1.5x volume. A rating note on a flat
# stock is a newsletter, not an alert. Results, risk and regulatory events skip
# the gate — they matter before the reaction shows up.
REACTION_GATED = {"analyst action", "capital action", "monthly numbers",
                  "capex / guidance"}
MIN_REACTION_PCT = 1.0
MIN_REACTION_VOL = 1.5

# Names a headline uses that the registered company name does not contain.
ALIASES = {
    "hul": "HINDUNILVR", "hindustan unilever": "HINDUNILVR",
    "l&t": "LT", "larsen": "LT", "larsen & toubro": "LT",
    "m&m": "M&M", "mahindra & mahindra": "M&M",
    "sbi": "SBIN", "state bank": "SBIN", "state bank of india": "SBIN",
    "infy": "INFY", "tcs": "TCS", "wipro": "WIPRO", "hcl": "HCLTECH",
    "techm": "TECHM", "ltim": "LTIM", "mindtree": "LTIM",
    "bajaj finance": "BAJFINANCE", "bajaj finserv": "BAJAJFINSV",
    "kotak": "KOTAKBANK", "axis": "AXISBANK", "icici": "ICICIBANK",
    "hdfc bank": "HDFCBANK", "hdfc life": "HDFCLIFE", "hdfc amc": "HDFCAMC",
    "airtel": "BHARTIARTL", "bharti airtel": "BHARTIARTL",
    "maruti": "MARUTI", "maruti suzuki": "MARUTI",
    "tata motors": "TATAMOTORS", "tata steel": "TATASTEEL",
    "tata power": "TATAPOWER", "tata chemicals": "TATACHEM",
    "tata consumer": "TATACONSUM", "tata elxsi": "TATAELXSI",
    "titan": "TITAN", "asian paints": "ASIANPAINT",
    "ultratech": "ULTRACEMCO", "nestle": "NESTLEIND",
    "sun pharma": "SUNPHARMA", "dr reddy": "DRREDDY", "cipla": "CIPLA",
    "divis": "DIVISLAB", "apollo hospitals": "APOLLOHOSP",
    "power grid": "POWERGRID", "ntpc": "NTPC", "ongc": "ONGC",
    "coal india": "COALINDIA", "bpcl": "BPCL", "iocl": "IOC",
    "indian oil": "IOC", "gail": "GAIL", "sail": "SAIL",
    "vedanta": "VEDANTA", "adani enterprises": "ADANIENT",
    "adani ports": "ADANIPORTS", "adani power": "ADANIPOWER",
    "adani green": "ADANIGREEN", "adani total gas": "ATGL",
    "jsw steel": "JSWSTEEL", "hindalco": "HINDALCO",
    "dlf": "DLF", "godrej properties": "GODREJPROP",
    "zomato": "ETERNAL", "eternal": "ETERNAL", "swiggy": "SWIGGY",
    "paytm": "PAYTM", "nykaa": "NYKAA", "dmart": "DMART",
    "avenue supermarts": "DMART", "irctc": "IRCTC",
    "bel": "BEL", "bharat electronics": "BEL", "hal": "HAL",
    "hindustan aeronautics": "HAL", "lic": "LICI",
    "pnb": "PNB", "punjab national bank": "PNB",
    "bank of baroda": "BANKBARODA", "canara bank": "CANBK",
    "yes bank": "YESBANK", "idfc first": "IDFCFIRSTB",
    "muthoot": "MUTHOOTFIN", "cholamandalam": "CHOLAFIN",
    "shriram finance": "SHRIRAMFIN", "bajaj auto": "BAJAJ-AUTO",
    "eicher": "EICHERMOT", "royal enfield": "EICHERMOT",
    "hero motocorp": "HEROMOTOCO", "tvs motor": "TVSMOTOR",
    "bosch": "BOSCHLTD", "siemens": "SIEMENS", "abb": "ABB",
    "cummins": "CUMMINSIND", "polycab": "POLYCAB", "havells": "HAVELLS",
    "page industries": "PAGEIND", "jubilant": "JUBLFOOD",
    "lupin": "LUPIN", "aurobindo": "AUROPHARMA", "alkem": "ALKEM",
    "torrent pharma": "TORNTPHARM", "cipla": "CIPLA",
    "indigo": "INDIGO", "interglobe": "INDIGO",
    "spicejet": "SPICEJET", "air india": "AIRINDIA",
    "irfc": "IRFC", "rvnl": "RVNL", "ircon": "IRCON",
    "bhel": "BHEL", "beml": "BEML", "mazagon": "MAZDOCK",
    "cochin shipyard": "COCHINSHIP", "garden reach": "GRSE",
    "hudco": "HUDCO", "rec": "RECLTD", "power finance": "PFC",
    "suzlon": "SUZLON", "inox wind": "INOXWIND",
    "vodafone idea": "IDEA",
    "yes bank": "YESBANK", "federal bank": "FEDERALBNK",
    "indusind": "INDUSINDBK", "bandhan": "BANDHANBNK",
    "au small finance": "AUBANK", "idbi": "IDBI",
    "central bank": "CENTRALBK", "union bank": "UNIONBANK",
    "iob": "IOB", "bank of maharashtra": "MAHABANK",
}

# Legal-form words that add nothing to a company's identity.
STRIP_WORDS = {
    "limited", "ltd", "ltd.", "inc", "incorporated", "corp", "corporation",
    "company", "co", "co.", "pvt", "private", "plc", "the",
}
# Brands too generic to identify one company on their own.
GENERIC = {"tata", "adani", "bajaj", "godrej", "mahindra", "jindal", "reliance",
           "hinduja", "essar", "jaypee", "steel", "power", "finance", "capital",
           "industries", "enterprises", "holdings", "group", "india", "bank"}

SHOCK_PCT = 6.0
SHOCK_VOLUME_X = 1.8


def shock_symbols() -> list[str]:
    """The watch list, minus anything NSE no longer trades under that symbol."""
    wanted = [s for s in SHOCK_UNIVERSE.split(",") if s]
    try:
        import universe
        live = {r["symbol"].upper() for r in universe.load() if r.get("symbol")}
    except Exception:
        return wanted
    keep = [s for s in wanted if s in live]
    dropped = [s for s in wanted if s not in live]
    if dropped:
        print(f"[warn] shock list: unknown NSE symbols skipped: {', '.join(dropped)}",
              file=sys.stderr)
    return keep or wanted

# Liquid names whose quotes are reliable every five minutes. A 6% move in an
# illiquid microcap is usually a data artefact, not news.
SHOCK_UNIVERSE = (
    "RELIANCE,HDFCBANK,ICICIBANK,TCS,INFY,SBIN,BHARTIARTL,ITC,LT,KOTAKBANK,"
    "AXISBANK,BAJFINANCE,MARUTI,M&M,SUNPHARMA,TITAN,ULTRACEMCO,ASIANPAINT,"
    "HINDUNILVR,NESTLEIND,TMPV,TATASTEEL,JSWSTEEL,HINDALCO,ADANIENT,"
    "ADANIPORTS,BAJAJFINSV,BAJAJ-AUTO,HEROMOTOCO,EICHERMOT,DRREDDY,CIPLA,"
    "DIVISLAB,TECHM,HCLTECH,WIPRO,POWERGRID,NTPC,ONGC,COALINDIA,BPCL,IOC,"
    "BEL,HAL,IRCTC,ETERNAL,DMART,PNB,BANKBARODA,IDFCFIRSTB,DLF,GAIL,SAIL,"
    "VEDL,ADANIPOWER,ADANIGREEN,ATGL,JSWENERGY,TATAPOWER,TMCV,TMPV,"
    "TATACONSUM,TATACHEM,TVSMOTOR,INDIGO,HAVELLS,POLYCAB,SIEMENS,ABB,"
    "CUMMINSIND,BOSCHLTD,LUPIN,AUROPHARMA,ALKEM,TORNTPHARM,APOLLOHOSP,"
    "MUTHOOTFIN,CHOLAFIN,SHRIRAMFIN,CANBK,UNIONBANK,FEDERALBNK,INDUSINDBK,"
    "AUBANK,BANDHANBNK,LICI,RECLTD,PFC,IRFC,RVNL,BHEL,MAZDOCK,COCHINSHIP,"
    "SUZLON,INOXWIND,PAYTM,NYKAA,IDEA"
)


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
    return {"seen_news": {}, "last_by_key": {}, "sent_log": []}


def save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    seen = s.get("seen_news", {})
    if len(seen) > 3000:
        keep = sorted(seen.items(), key=lambda kv: kv[1])[-3000:]
        s["seen_news"] = dict(keep)
    s["sent_log"] = (s.get("sent_log") or [])[-300:]
    STATE.write_text(json.dumps(s, indent=2, default=str))


def _audit(symbol: str | None, event: str, title: str, source: str,
           link: str) -> None:
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "symbol": symbol, "event": event, "title": title,
                "source": source, "link": link,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _hash(title: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def _cooled(state: dict, key: str, minutes: int) -> bool:
    last = state.get("last_by_key", {}).get(key)
    if not last:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(last) < timedelta(minutes=minutes)
    except Exception:
        return False


def _mark(state: dict, key: str) -> None:
    state.setdefault("last_by_key", {})[key] = datetime.now().isoformat(timespec="seconds")


def _global_rate_ok(state: dict, max_per_30min: int = 4) -> bool:
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
def send_telegram(text: str, dry: bool = False,
                  reply_markup: dict | None = None) -> bool:
    if dry:
        print("\n----- DRY RUN MESSAGE -----")
        print(re.sub(r"<[^>]+>", "", text))
        return True
    import notify
    try:
        res = notify.send(text, kind="alert", reply_markup=reply_markup)
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram credentials missing or send failed: {exc}", file=sys.stderr)
        return False
    if not res["sent"]:
        print(f"No subscriber could be reached: {res}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# headline -> one listed company
# ---------------------------------------------------------------------------
_NAME_INDEX: list[tuple[str, str, bool]] | None = None

# Words that are a company's first name but also ordinary headline vocabulary.
# "Mumbai metro" is not Metro Brands, and "West Bengal" is not West Coast Paper.
# A full company name or an alias still resolves these; only the lazy
# first-word shortcut is withheld.
WEAK_BLOCK = {
    "west", "east", "north", "south", "central", "national", "indian", "india",
    "global", "united", "general", "standard", "metro", "new", "first", "prime",
    "universal", "oriental", "dynamic", "future", "advance", "super", "sun",
    "star", "shree", "shri", "sri", "jai", "om", "sanghvi", "paras", "vibrant",
    "perfect", "popular", "royal", "modern", "smart", "true", "green", "blue",
    # states, regions and metros
    "bengal", "assam", "punjab", "gujarat", "kerala", "karnataka", "tamil",
    "maharashtra", "rajasthan", "madhya", "uttar", "andhra", "telangana",
    "bihar", "odisha", "orissa", "haryana", "goa", "delhi", "mumbai", "chennai",
    "kolkata", "hyderabad", "pune", "nagpur", "surat", "indore", "jaipur",
}

# Qualifiers that turn the next word into a place, not a brand: 'West Bengal',
# 'New Delhi', 'South Mumbai'.
GEO_QUALIFIER = r"(?:west|east|north|south|new|old|greater|upper|lower|central)"


def _name_index() -> list[tuple[str, str, bool]]:
    """(phrase, symbol, strong) — strongest matches first.

    strong=True  an alias or a full registered name: match anywhere, any case.
    strong=False a first name only ('Metro' from 'Metro Brands'): matched only
                 when it is capitalised, because 'metro' in a headline is far
                 more often the Mumbai metro than the shoe retailer.

    Order matters twice over: strong entries all beat weak ones, and within
    each group the longest phrase wins, so 'Tata Steel' never loses to 'Tata'.
    """
    global _NAME_INDEX
    if _NAME_INDEX is not None:
        return _NAME_INDEX

    strong: dict[str, str] = {}
    weak: dict[str, str] = {}

    def add(key: str, sym: str, is_strong: bool) -> None:
        k = re.sub(r"\s+", " ", key.lower()).strip(" .,")
        if len(k) < 3 or k in GENERIC:
            return
        if not re.search(r"[a-z]", k):
            return
        (strong if is_strong else weak).setdefault(k, sym)

    for alias, sym in ALIASES.items():
        add(alias, sym, True)

    try:
        import universe
        for row in universe.load():
            sym = (row.get("symbol") or "").upper()
            name = (row.get("name") or "")
            if not sym or not name:
                continue
            words = [w for w in re.split(r"[\s]+", name)
                     if w.lower().strip(".,") not in STRIP_WORDS]
            add(" ".join(words), sym, True)
            # 'Reliance Industries' -> 'Reliance' is useful; 'Adani' alone is not.
            first = words[0].lower().strip(".,") if words else ""
            if len(words) > 1 and first not in GENERIC and first not in WEAK_BLOCK:
                add(words[0], sym, False)
            # A two-word pair is far safer than one word and rescues names whose
            # first word is blocked: 'Sun TV Network' still answers to 'Sun TV'.
            if len(words) > 2:
                two = " ".join(words[:2]).lower().strip(" .,")
                if len(two) >= 6:
                    add(" ".join(words[:2]), sym, False)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] name index unavailable: {e}", file=sys.stderr)

    _NAME_INDEX = (
        [(k, v, True) for k, v in sorted(strong.items(), key=lambda kv: -len(kv[0]))]
        + [(k, v, False) for k, v in sorted(weak.items(), key=lambda kv: -len(kv[0]))]
    )
    return _NAME_INDEX


def match_symbol(text: str) -> tuple[str, str] | None:
    """Return (symbol, matched phrase) for the company the headline is about."""
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "

    # An explicit ticker in brackets is the strongest signal there is.
    for phrase, sym, _strong in _name_index():
        if f"({phrase})" in low or f"[{phrase}]" in low:
            return sym, phrase

    for phrase, sym, strong in _name_index():
        if strong:
            if re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", low):
                return sym, phrase
        else:
            # Capitalised in the headline only: a proper noun, not a word.
            pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])", re.I)
            for m in pat.finditer(text):
                if not m.group()[0].isupper():
                    continue
                head = text[max(0, m.start() - 14):m.start()]
                if re.search(rf"(?<![A-Za-z]){GEO_QUALIFIER}\s+$", head):
                    continue      # 'West Bengal' is a state, not Bengal & Assam
                return sym, phrase
    return None


def classify(title: str) -> tuple[str, str, str] | None:
    """(kind, label, why) — kind is EVENT or SYSTEMIC."""
    low = title.lower()
    for pattern, label, why in SYSTEMIC:
        if re.search(pattern, low, re.I):
            return "SYSTEMIC", label, why
    for pattern, label, why in EVENTS:
        if re.search(pattern, low, re.I):
            return "EVENT", label, why
    return None


# ---------------------------------------------------------------------------
# live quote for the reaction line
# ---------------------------------------------------------------------------
def quote(symbol: str) -> dict | None:
    """Last price, day move and volume versus its own 20-day average."""
    try:
        import yfinance as yf
        df = yf.download(f"{symbol}.NS", period="1mo", interval="1d",
                         progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        # One ticker still comes back with a two-level column index
        # (field, ticker). Dropping the wrong level left the columns named
        # after the ticker, so every lookup raised and the reaction line was
        # silently blank. Keep whichever level actually names the fields.
        cols = df.columns
        if hasattr(cols, "levels"):
            for i in range(cols.nlevels):
                if "Close" in cols.get_level_values(i):
                    df.columns = cols.get_level_values(i)
                    break
        close = df["Close"].dropna()
        vol = df["Volume"].dropna()
        if len(close) < 2:
            return None
        last, prev = float(close.iloc[-1]), float(close.iloc[-2])
        avg_vol = float(vol.iloc[-21:-1].mean()) if len(vol) > 5 else 0.0
        day_vol = float(vol.iloc[-1])
        return {
            "last": last,
            "change_pct": (last / prev - 1) * 100 if prev else 0.0,
            "volume_x": (day_vol / avg_vol) if avg_vol > 0 else None,
        }
    except Exception:
        return None


def _reaction_line(q: dict | None) -> str:
    if not q:
        return ""
    bits = [f"<b>{q['last']:,.2f}</b> ({q['change_pct']:+.2f}%)"]
    if q.get("volume_x"):
        bits.append(f"volume {q['volume_x']:.1f}x its 20-day average")
    return "\nNow: " + " · ".join(bits)


# ---------------------------------------------------------------------------
# event scanner
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
    for m in re.finditer(r"<item>(.*?)</item>", r.text, re.S | re.I):
        block = m.group(1)
        tm = re.search(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", block, re.S | re.I)
        lm = re.search(r"<link>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</link>", block, re.S | re.I)
        if not tm:
            continue
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", tm.group(1))).strip()
        link = (lm.group(1).strip() if lm else "")
        if title:
            items.append({"title": title, "link": link, "source": name})
    return items


def scan_events(state: dict, dry: bool) -> list[str]:
    seen = state.setdefault("seen_news", {})
    now_iso = datetime.now().isoformat(timespec="seconds")
    warm_up = not seen          # first run learns the backlog, sends nothing
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

            kind = classify(item["title"])
            if not kind:
                continue
            kind, label, why = kind

            if kind == "SYSTEMIC":
                sym, reaction = None, None
            else:
                hit = match_symbol(item["title"])
                if not hit:
                    continue          # no company named, no alert
                sym, _phrase = hit
                if _cooled(state, f"stock:{sym}", 240):
                    continue          # one alert per stock per four hours
                reaction = quote(sym)
                if label in REACTION_GATED and reaction is not None:
                    moved = abs(reaction.get("change_pct") or 0) >= MIN_REACTION_PCT
                    busy = (reaction.get("volume_x") or 0) >= MIN_REACTION_VOL
                    if not (moved or busy):
                        print(f"  · {sym} {label}: no price reaction "
                              f"({reaction.get('change_pct', 0):+.2f}%, "
                              f"{(reaction.get('volume_x') or 0):.1f}x volume) — held back")
                        continue

            if not _global_rate_ok(state):
                continue

            stamp = datetime.now().strftime("%d %b %Y, %H:%M IST")
            head = (f"🚨 <b>{esc(sym)} · {esc(label)}</b>" if sym
                    else f"🚨 <b>{esc(label)}</b>")
            body = (
                f"{head}\n"
                f"<i>{stamp}</i>\n"
                f"───────────────\n"
                f"<b>{esc(item['title'])}</b>\n"
                f"<i>{esc(item['source'])}</i>"
                f"{_reaction_line(reaction)}\n\n"
                f"<i>Why it matters: {esc(why)}</i>"
            )
            if item["link"]:
                body += f"\n<a href=\"{esc(item['link'])}\">Read the story</a>"
            body += "\n\n<i>Educational only — not advice.</i>"

            if send_telegram(body, dry=dry,
                             reply_markup=buttons.alert(sym) if sym else None):
                if not dry:
                    _audit(sym, label, item["title"], item["source"], item["link"])
                print(f"  → {sym or 'market'} · {label}: {item['title'][:80]}")
                found.append(f"{sym or 'market'}: {item['title'][:60]}")
                state.setdefault("sent_log", []).append(now_iso)
                if sym:
                    _mark(state, f"stock:{sym}")
            if len(found) >= 3:
                return found
    return found


# ---------------------------------------------------------------------------
# shock scanner
# ---------------------------------------------------------------------------
def scan_shocks(state: dict, dry: bool) -> list[str]:
    sent = []
    try:
        import yfinance as yf
        symbols = shock_symbols()
        tickers = [f"{s}.NS" for s in symbols]
        df = yf.download(tickers, period="3mo", interval="1d",
                         progress=False, auto_adjust=False, threads=True)
        closes, vols = df["Close"], df["Volume"]
        hits = []
        for sym, t in zip(symbols, tickers):
            if t not in closes:
                continue
            c = closes[t].dropna()
            v = vols[t].dropna() if t in vols else None
            if len(c) < 2:
                continue
            last, prev = float(c.iloc[-1]), float(c.iloc[-2])
            chg = (last / prev - 1) * 100
            if abs(chg) < SHOCK_PCT:
                continue
            avg = float(v.iloc[-21:-1].mean()) if v is not None and len(v) > 5 else 0.0
            today = float(v.iloc[-1]) if v is not None and len(v) else 0.0
            volx = (today / avg) if avg > 0 else None
            # A big move on ordinary volume is usually stale data or a block
            # print; require participation before calling it news.
            if volx is not None and volx < SHOCK_VOLUME_X:
                continue
            hits.append((sym, last, chg, volx))

        hits.sort(key=lambda x: -abs(x[2]))
        for sym, last, chg, volx in hits[:3]:
            if _cooled(state, f"shock:{sym}", 240):
                continue
            icon = "📈" if chg > 0 else "📉"
            body = (
                f"{icon} <b>{esc(sym)} {chg:+.2f}%</b>\n"
                f"<i>{datetime.now().strftime('%d %b %Y, %H:%M IST')}</i>\n"
                f"───────────────\n"
                f"Last <b>{last:,.2f}</b>"
                + (f" · volume {volx:.1f}x its 20-day average" if volx else "")
                + f"\n\n<i>Why it matters: a {abs(chg):.0f}% move on this volume is money "
                  f"changing its mind, not drift. Look for results, an order or a block "
                  f"deal before acting.</i>\n\n<i>Educational only — not advice.</i>"
            )
            if send_telegram(body, dry=dry, reply_markup=buttons.alert(sym)):
                if not dry:
                    _audit(sym, "price shock", f"{chg:+.2f}% on {volx or 0:.1f}x volume",
                           "price", "")
                print(f"  → {sym} · shock {chg:+.2f}%")
                sent.append(sym)
                _mark(state, f"shock:{sym}")
                state.setdefault("sent_log", []).append(
                    datetime.now().isoformat(timespec="seconds"))
    except Exception as e:  # noqa: BLE001
        print(f"[shock scan skipped: {e}]", file=sys.stderr)
    return sent


# ---------------------------------------------------------------------------
# results ahead
# ---------------------------------------------------------------------------
def scan_results(state: dict, dry: bool = False, within: int = 5) -> list[str]:
    """Tell the desk which company reports next, and whether it is ready.

    The alert goes out before the print, while there is still a decision to
    make. After the number the only information left is the gap.
    """
    sent = []
    try:
        import pre_results
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] pre_results unavailable: {exc}", file=sys.stderr)
        return sent

    try:
        rows = pre_results.scan(within=within, with_setup=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] results scan failed: {exc}", file=sys.stderr)
        return sent

    market = pre_results.market_reaction()
    for r in rows:
        sym = r["symbol"]
        key = f"results:{sym}:{datetime.now().strftime('%Y-%m-%d')}"
        if _cooled(state, key, 60 * 24):
            continue
        s2 = r.get("setup") or {}
        ready = pre_results.ready(r)

        # Only the ones with a chart worth trading get a message. A print
        # from a stock in a downtrend is information, not an opportunity.
        if not ready:
            continue

        body = [f"📅 <b>{esc(sym)}</b> reports "
                f"{esc(r['date'])} — {r['sessions']} session(s) away, "
                f"{'confirmed' if r.get('confirmed') else 'estimated'}", ""]
        body.append(f"<i>Chart: Stage 2 ✓, {esc(s2.get('base_type') or '')} "
                    f"grade {esc(s2.get('base_grade') or '?')}"
                    + (f", pivot {s2['pivot']:,.2f} "
                       f"({s2.get('from_pivot_pct') or 0:+.1f}% away)"
                       if s2.get("pivot") else "") + "</i>")
        react = r.get("reaction") or {}
        if react.get("n"):
            g = (react.get("gap") or {}).get("median", 0)
            h5 = (react.get("h5") or {}).get("median", 0)
            body.append(f"<i>Its last {react['n']} print(s): median overnight "
                        f"gap {g:+.1f}%, five sessions later {h5:+.1f}%</i>")
        if (market.get("h5") or {}).get("n"):
            h5 = market["h5"]
            g = (market.get("gap") or {}).get("median", 0)
            body.append("")
            body.append(f"<i>Across {market['n']:,} prints: median gap "
                        f"{g:+.1f}%, five sessions later "
                        f"{h5.get('median', 0):+.1f}% (positive "
                        f"{h5.get('hit', 0):.0f}%). A stop does not protect "
                        "you through the gap — the exchange is shut. Size this "
                        "for a gap, not for a normal day.</i>")
        body.append("")
        body.append("<i>Buy the setup before the number, not the gap after "
                    "it. Check the plan with /check " + sym + ". "
                    "Educational only — not advice.</i>")
        text = "\n".join(body)

        if send_telegram(text, dry=dry, reply_markup=buttons.alert(sym)):
            if not dry:
                _audit(sym, "results ahead", f"reports {r['date']}",
                       "results calendar", "")
                _mark(state, key)
            sent.append(sym)
            print(f"  → {sym} reports in {r['sessions']} session(s)")
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
            "Event alerts are live: one message when a named NSE stock is being "
            "moved by news, plus 6% shocks on volume.",
            dry=dry,
        )
        save_state(state)
        return 0

    sent = []
    if not news_only:
        # Levels before headlines: a stop breach is more urgent than any story.
        try:
            import levels_watch
            sent += [f"level:{s}" for s in levels_watch.scan(dry=dry)]
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] level watch failed: {exc}", file=sys.stderr)

    if not prices_only:
        sent += scan_events(state, dry)

    # Personal watchlists: addressed to one chat, never broadcast.
    if not news_only:
        try:
            import watch_scan
            sent += [f"watch:{w}" for w in watch_scan.scan(dry=dry)]
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] watch scan failed: {exc}", file=sys.stderr)

    # Results: the one alert that has to arrive before the event.
    if not news_only:
        sent += [f"results:{r}" for r in scan_results(state, dry=dry)]

    if not news_only and cal.is_market_open():
        sent += scan_shocks(state, dry)
    elif not news_only:
        print(f"Market closed ({datetime.now():%H:%M}) — event scan only.")

    if not dry:
        save_state(state)
    if not sent:
        print("No alerts triggered.")
    elif dry:
        print(f"{len(sent)} alert(s) would be sent.")
    else:
        print(f"{len(sent)} alert(s) sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
