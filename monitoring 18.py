# =============================================================================
# KuCoin Multi-Symbol OHLC Scanner  ·  v11
# =============================================================================
# Pine Script source : "Hammer" indicator
# Signal source of truth : Pine Script — faithfully reproduced line-by-line
#
# Data source : KuCoin Spot API (public, no API key required)
#
# KuCoin API endpoints used:
#   REST klines  : GET  https://api.kucoin.com/api/v1/market/candles
#                        ?symbol=BTC-USDT&type=4hour&startAt=<s>&endAt=<s>
#                  Row  : [start_time_s, open, close, high, low, volume, amount]
#                  Note : returns newest-first; max 1500 rows per call
#
#   WS token     : POST https://api.kucoin.com/api/v1/bullet-public
#                  Returns dynamic WSS URL + token + pingInterval
#
#   WebSocket    : wss://<endpoint>?token=<token>&connectId=<id>
#                  Topic: /market/candles:<SYMBOL>_4hour
#                  Subject: trade.candles.update  (fires on every tick)
#                  Candle-close: detected when candles[0] (open_time) changes
#                  Heartbeat: {"type":"ping","id":"<id>"} every pingInterval ms
#
# All Pine Script strategy logic UNCHANGED:
#   ✅ pivothigh / pivotlow (length=21)
#   ✅ Pattern cascade: hammer → ihammer → bulleng → hanging → shooting → beareng
#   ✅ run_strategy() fires ONLY on closed candles
#   ✅ Rolling 3-signal buffer per symbol
#   ✅ Duplicate-signal guard
#   ✅ Telegram alerts
#
# Output always shows:
#   SYMBOL | SIGNAL TYPE (LONG/SHORT) | TIME of signal
# =============================================================================

import json
import asyncio
import logging
import os
import sys
import time
import uuid
import aiohttp
from collections import deque
from datetime    import datetime, timezone

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

# =============================================================================
# DEBUG SWITCH
# =============================================================================
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

logging.basicConfig(
    level   = logging.DEBUG if DEBUG else logging.CRITICAL,
    format  = "%(asctime)s [%(levelname)s] %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
    stream  = sys.stdout,
)
log = logging.getLogger("kucoin_scanner")

# =============================================================================
# CONFIGURATION
# =============================================================================

# KuCoin REST base
KC_REST_BASE        = "https://api.kucoin.com"
KC_KLINES_PATH      = "/api/v1/market/candles"
KC_BULLET_PATH      = "/api/v1/bullet-public"   # POST → WS token + URL

# Interval string (KuCoin candles endpoint)
KC_INTERVAL         = "4hour"   # valid: 1min 3min 5min 15min 30min 1hour 2hour
                                #        4hour 6hour 8hour 12hour 1day 1week

# Candle duration in seconds
INTERVAL_SECONDS    = 4 * 3600  # 14 400 s

# Max candles per REST request (KuCoin hard limit)
KC_MAX_PER_REQ      = 1500

# Total candles to seed on startup
HIST_TOTAL_CANDLES  = 1500      # one KuCoin request covers ~250 days of 4H

# Concurrency for historical fetches
HIST_SEMAPHORE      = 4

# REST polling fallback (when WS unavailable)
POLL_INTERVAL       = 60        # seconds between polls

# Dashboard refresh
DASHBOARD_REFRESH   = 1         # seconds

# Pine Script: length = input(21)
length = 21

# Telegram
TELEGRAM_BOT_TOKEN  = "8374221123:AAGgoW7GvyHY8qFMR4zMPimvXTXlV3K72M0"
TELEGRAM_CHAT_ID    = "-1003806017217"

# =============================================================================
# PINE SCRIPT PATTERN LABELS  (verbatim)
# =============================================================================
hammer_   = "Hammer pattern"
ihammer_  = "Inverted Hammer pattern"
bulleng_  = "Bullish Engulfing pattern"
hanging_  = "Hanging Man pattern"
shooting_ = "Shooting Star pattern"
beareng_  = "Bearish Engulfing pattern"

# =============================================================================
# SYMBOL LIST
# KuCoin symbol format: BTC-USDT  (dash-separated)
# =============================================================================
# Original 56 symbols
_SYMBOLS_ORIGINAL = [
    "BTC",  "ETH",  "XRP",  "SOL",  "ADA",  "DOGE", "AVAX",
    "LTC",  "LINK", "DOT",  "UNI",  "ATOM", "XLM",  "ALGO",
    "FIL",  "ICP",  "EOS",  "SAND", "MANA", "AAVE", "AXS",
    "XTZ",  "CHZ",  "ENJ",  "STX",  "SNX",  "CRV",  "COMP",
    "BAL",  "1INCH","AUDIO","OCEAN","BAND", "ANKR", "LPT",
    "BAT",  "ZRX",  "KNC",  "CELO", "ZIL",  "LRC",  "IOTX",
    "AMP",  "OGN",  "CHR",  "POWR", "BNT",  "CTSI", "GLM",
    "NMR",  "REQ",  "SNT",  "ANT",  "YFI",  "GRT",  "FET",
    "MKR",  "RNDR", "INJ",  "QNT",  "FTM",  "AR",   "EGLD",
    "NEAR", "VET",
]

# Added 44 symbols (active KuCoin USDT pairs, not in exclusion list)
_SYMBOLS_ADDED = [
    "TRX",  "SHIB", "TON",   "SUI",    "APT",   "OP",    "ARB",
    "PEPE", "WIF",  "BONK",  "FLOKI",  "JUP",   "STRK",  "MANTA",
    "ALT",  "PIXEL","PORTAL","DYM",    "ZETA",  "SEI",   "TIA",
    "PYTH", "JTO",  "MEME",  "ACE",    "NFP",   "XAI",   "ORDI",
    "LUNC", "LUNA", "CFX",   "MASK",   "ID",    "EDU",   "HIGH",
    "PENDLE","HOOK","MAGIC", "AGLD",   "W",     "BLUR",  "DYDX",
    "GMX",  "RDNT",
]

SYMBOLS = _SYMBOLS_ORIGINAL + _SYMBOLS_ADDED

# KuCoin pair strings
KC_PAIR  = {s: f"{s}-USDT" for s in SYMBOLS}

# WS topic per symbol: /market/candles:BTC-USDT_4hour
def kc_ws_topic(sym: str) -> str:
    return f"/market/candles:{KC_PAIR[sym]}_{KC_INTERVAL}"

# =============================================================================
# PER-SYMBOL STATE
# =============================================================================
ohlc_data         = {s: [] for s in SYMBOLS}
last_candle_time  = {s: None for s in SYMBOLS}    # open-time of last closed candle
pending_candle    = {s: None for s in SYMBOLS}    # in-progress candle from WS
candle_close_time = {s: None for s in SYMBOLS}    # expected close timestamp
signal_buffer     = {s: deque(maxlen=3) for s in SYMBOLS}
last_sent_key     = {s: None for s in SYMBOLS}

live_mode = "poll"   # "ws" or "poll"

# =============================================================================
# ADDED STATE — Features 3 / 4 / 7 / 8
# =============================================================================

# Feature 7 (updated): last accepted s3 identity key per symbol
# Stored as (direction, bar_time, price) of s3 — Option A dedup
# Signal is new only when s3's direction, bar_time, or price changes
last_sent_s3_key = {s: None for s in SYMBOLS}

# Feature 3/4/5: computed engine output per symbol
# Holds dict: {"symbol", "entry_price", "signal_type"} or None
engine_output = {s: None for s in SYMBOLS}

# =============================================================================
# ADDED STATE — Startup blast (new features)
# =============================================================================

# Guard: engine sends Telegram only after history replay is complete.
# During load_all_history() this is False — no Telegram sent.
# Set to True in main() after load_all_history() + startup blast completes.
_engine_active = False

# =============================================================================
# SECTION 1 — PINE SCRIPT PIVOT HELPERS  (UNCHANGED)
# =============================================================================

def pivothigh(highs: list, i: int, l: int):
    """
    ta.pivothigh(length, length): returns high price at (i-l) if it is
    a local maximum over l bars left and l bars right; else None.
    """
    pivot_idx = i - l
    if pivot_idx < l or pivot_idx + l >= len(highs):
        return None
    center = highs[pivot_idx]
    for x in range(pivot_idx - l, pivot_idx + l + 1):
        if highs[x] > center:
            return None
    return center


def pivotlow(lows: list, i: int, l: int):
    """
    ta.pivotlow(length, length): returns low price at (i-l) if it is
    a local minimum over l bars left and l bars right; else None.
    """
    pivot_idx = i - l
    if pivot_idx < l or pivot_idx + l >= len(lows):
        return None
    center = lows[pivot_idx]
    for x in range(pivot_idx - l, pivot_idx + l + 1):
        if lows[x] < center:
            return None
    return center

# =============================================================================
# SECTION 2 — PINE SCRIPT PATTERN ENGINE  (UNCHANGED)
# =============================================================================

def detect_pattern(opens, highs, lows, closes, i, l, ph, pl) -> str:
    """
    Evaluates all six pattern conditions exactly as Pine Script does.
    Returns the title of the first matching pattern, or "" if none match.
    """
    pivot_idx = i - l
    o  = opens [pivot_idx]
    h  = highs [pivot_idx]
    lv = lows  [pivot_idx]
    c  = closes[pivot_idx]
    d  = abs(c - o)

    if i < 1:
        c1 = o1 = None
    else:
        c1 = closes[i - 1]
        o1 = opens [i - 1]

    # Pattern conditions (verbatim Pine Script; beareng bug fixed)
    hammer   = pl is not None and (min(o, c) - lv > d) and (h - max(c, o) < d)
    ihammer  = pl is not None and (h - max(o, c) > d)  and (min(c, o) - lv < d)
    bulleng  = c1 is not None and c > o and c1 < o1 and c > o1 and o < c1
    hanging  = ph is not None and (min(c, o) - lv > d) and (h - max(c, o) < d)
    shooting = ph is not None and (h - max(o, c) > d)  and (min(c, o) - lv < d)
    beareng  = c1 is not None and c < o and c1 > o1 and c < o1 and o > c1

    if   hammer:   return hammer_
    elif ihammer:  return ihammer_
    elif bulleng:  return bulleng_
    elif hanging:  return hanging_
    elif shooting: return shooting_
    elif beareng:  return beareng_
    else:          return ""

# =============================================================================
# SECTION 3 — STRATEGY ENGINE  (UNCHANGED — fires only on closed candles)
# =============================================================================

def run_strategy(symbol: str) -> None:
    """
    Runs the Pine Script strategy on ohlc_data[symbol].
    Called exactly once per closed candle (historical replay + live).

    ORIGINAL LOGIC IS UNCHANGED.
    ADDED LAYERS (Features 3/4/5/6/7/8) are appended AFTER original logic.
    """
    try:
        data = ohlc_data[symbol]
        # Need at least 2*length + 2 = 44 candles for a full pivot window
        if len(data) < 2 * length + 2:
            return

        opens  = [c["open"]  for c in data]
        highs  = [c["high"]  for c in data]
        lows   = [c["low"]   for c in data]
        closes = [c["close"] for c in data]
        times  = [c["time"]  for c in data]

        i         = len(data) - 1
        pivot_idx = i - length

        ph = pivothigh(highs, i, length)
        pl = pivotlow (lows,  i, length)

        if ph is None and pl is None:
            return

        pattern_title = detect_pattern(opens, highs, lows, closes, i, length, ph, pl)

        bar_time  = times [pivot_idx]
        bar_open  = opens [pivot_idx]
        bar_high  = highs [pivot_idx]
        bar_low   = lows  [pivot_idx]
        bar_close = closes[pivot_idx]

        signals_to_emit = []

        if ph is not None:
            signals_to_emit.append({
                "direction": "SHORT", "price": ph,
                "pattern":   pattern_title, "bar_time": bar_time,
                "bar_index": pivot_idx,
                "open": bar_open, "high": bar_high,
                "low":  bar_low,  "close": bar_close,
            })

        if pl is not None:
            signals_to_emit.append({
                "direction": "LONG", "price": pl,
                "pattern":   pattern_title, "bar_time": bar_time,
                "bar_index": pivot_idx,
                "open": bar_open, "high": bar_high,
                "low":  bar_low,  "close": bar_close,
            })

        # ── ORIGINAL dedup + buffer append (UNCHANGED) ────────────────────────
        for sig in signals_to_emit:
            sig_key = (sig["direction"], sig["bar_time"], sig["price"])
            if last_sent_key[symbol] == sig_key:
                log.debug("[%s] Duplicate suppressed: %s", symbol, sig_key)
                continue

            last_sent_key[symbol] = sig_key
            signal_buffer[symbol].append(sig)

            log.info(
                "[%s] %s @ %.6g  pattern=%r  bar=%s",
                symbol, sig["direction"], sig["price"], sig["pattern"],
                datetime.utcfromtimestamp(sig["bar_time"]).strftime("%Y-%m-%d %H:%M UTC"),
            )

        # ── ADDED LAYER: Feature 3/4/5/7/8 — Signal Engine ───────────────────
        # Runs after every candle close (the hard-refresh point).
        # Operates exclusively on signal_buffer[symbol] — no new signals created.
        _run_signal_engine(symbol)

    except Exception as e:
        log.error("[%s] Strategy error: %s: %s", symbol, type(e).__name__, e)


# =============================================================================
# ADDED — Signal Engine  (Features 3 / 4 / 5 / 7 / 8)
#
# This function is called by run_strategy() after every candle close.
# It is a pure additive layer — it does NOT touch any existing logic.
#
# It reads signal_buffer[symbol] (already populated above) and:
#   Feature 3  — maps LONG/SHORT → +1/-1, multiplies s1*s2*s3
#   Feature 4  — derives entry_price from signal_product sign
#   Feature 5  — computes structured output {symbol, entry_price, signal_type}
#   Feature 6  — this is called on every candle close = hard refresh point
#   Feature 7  — deduplicates via signal-candle OHLC tuple, not just key
#   Feature 8  — sends Telegram in new clean format, once per unique signal
# =============================================================================

def _run_signal_engine(symbol: str) -> None:
    """
    Additive signal engine layer.  Reads signal_buffer[symbol] only.
    During history replay: updates state silently (no Telegram).
    After replay: sends Telegram only for new unique signals.
    """
    buf = list(signal_buffer[symbol])

    # Feature 3 / Q3: strict 3-signal system — skip if buffer not full
    if len(buf) != 3:
        return

    s1, s2, s3 = buf   # oldest → newest (deque insertion order)

    # ── Feature 3: signal mapping + multiplication ────────────────────────────
    _dir_to_num = {"LONG": +1, "SHORT": -1}

    n1 = _dir_to_num.get(s1["direction"], 0)
    n2 = _dir_to_num.get(s2["direction"], 0)
    n3 = _dir_to_num.get(s3["direction"], 0)

    if n1 == 0 or n2 == 0 or n3 == 0:
        return

    signal_product = n1 * n2 * n3   # +1 or -1

    # ── Feature 4: entry price from s3 (newest signal candle) ─────────────────
    if signal_product > 0:
        entry_price = s3["open"]    # POSITIVE product → use OPEN of s3
    else:
        entry_price = s3["close"]   # NEGATIVE product → use CLOSE of s3

    # ── Feature 5: signal type = direction of s3 (newest signal) ──────────────
    signal_type = s3["direction"]

    # ── Feature 7 (Option A): dedup based on s3 identity ─────────────────────
    s3_key = (s3["direction"], s3["bar_time"], s3["price"])

    # Always update engine_output for dashboard
    engine_output[symbol] = {
        "symbol":       KC_PAIR[symbol],
        "entry_price":  entry_price,
        "signal_type":  signal_type,
    }

    # During history replay — silently update s3_key, no Telegram
    if not _engine_active:
        last_sent_s3_key[symbol] = s3_key
        return

    # Live mode — check dedup before sending
    if last_sent_s3_key[symbol] == s3_key:
        log.debug("[ENGINE][%s] s3 unchanged — suppressed: %s", symbol, s3_key)
        return

    # New unique s3 — accept and send
    last_sent_s3_key[symbol] = s3_key

    log.info(
        "[ENGINE][%s] NEW SIGNAL  product=%+d  entry=%.6g  type=%s",
        symbol, signal_product, entry_price, signal_type,
    )

    # ── Feature 8: Telegram — formatted, 2s delay, once per unique signal ──────
    tg_msg = _format_signal_message(KC_PAIR[symbol], entry_price, signal_type)
    asyncio.get_event_loop().create_task(_send_engine_telegram_delayed(tg_msg))

# =============================================================================
# SECTION 4 — TELEGRAM SENDER  (UNCHANGED)
# =============================================================================

async def send_telegram(symbol: str, message: str) -> None:
    full_msg = f"[{KC_PAIR[symbol]}] {message}"
    url      = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload  = {"chat_id": TELEGRAM_CHAT_ID, "text": full_msg}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    log.info("[Telegram] ✓ %s", full_msg[:80])
                else:
                    body = await resp.text()
                    log.warning("[Telegram] HTTP %d: %s", resp.status, body[:120])
    except Exception as e:
        log.error("[Telegram] Error: %s: %s", type(e).__name__, e)


# =============================================================================
# ADDED — Engine Telegram Sender  (Feature 8)
#
# Sends the new clean format:
#   SYMBOL
#   ENTRY PRICE
#   SIGNAL TYPE
#
# This is separate from send_telegram() to keep original function untouched.
# Called only from _run_signal_engine() — once per unique OHLC-deduped signal.
# =============================================================================

async def _send_engine_telegram(message: str) -> None:
    """Send the new clean signal format to Telegram (Feature 8)."""
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    log.info("[ENGINE][Telegram] ✓ sent")
                else:
                    body = await resp.text()
                    log.warning("[ENGINE][Telegram] HTTP %d: %s", resp.status, body[:120])
    except Exception as e:
        log.error("[ENGINE][Telegram] Error: %s: %s", type(e).__name__, e)


async def _send_engine_telegram_delayed(message: str) -> None:
    """Wait 2 seconds then send — prevents Telegram rate-limit for 110 symbols."""
    await asyncio.sleep(2)
    await _send_engine_telegram(message)


# =============================================================================
# ADDED — Signal message formatter  (fixed English)
#
# Formats the signal output in the requested visual style.
# =============================================================================

def _format_signal_message(pair: str, entry_price: float, signal_type: str) -> str:
    """
    Returns the formatted Telegram signal message in English.
    """
    sep             = "_-_-_-_-_-_-_-_-_-"
    direction_label = "👆 LONG" if signal_type == "LONG" else "👇 SHORT"

    return (
        f"-❗🔥CRYPTO SIGNAL 🔥❗-\n\n"
        f"💡 Symbol:   {pair}\n\n"
        f"{sep}\n\n"
        f"{direction_label}\n\n"
        f"{sep}\n\n"
        f"🛍 Leverage : 10X\n\n"
        f"{sep}\n\n"
        f"🧲 Entry Price: {entry_price}\n\n"
        f"{sep}\n\n"
        f"⚡️ Always observe stop-loss and capital management\n\n\n"
        f"🔗 @AutoTrading_SIG"
    )




# =============================================================================
# SECTION 5 — TIMER HELPERS
# =============================================================================

def get_candle_close_time(open_time: int) -> int:
    return open_time + INTERVAL_SECONDS

def seconds_until_close(symbol: str) -> int:
    close_ts = candle_close_time[symbol]
    if close_ts is None:
        return 0
    now = int(datetime.now(timezone.utc).timestamp())
    return max(0, close_ts - now)

def format_countdown(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

# =============================================================================
# SECTION 6 — LIVE DASHBOARD
# Always shows: SYMBOL | SIGNAL TYPE | TIME of signal
# =============================================================================

async def dashboard_loop() -> None:
    ESC_HOME  = "\033[H"
    ESC_CLEAR = "\033[2J"

    C_RESET  = "\033[0m"
    C_WHITE  = "\033[97m"
    C_YELLOW = "\033[93m"
    C_GREEN  = "\033[92m"
    C_RED    = "\033[91m"
    C_GRAY   = "\033[90m"
    C_ORANGE = "\033[33m"
    C_CYAN   = "\033[96m"
    C_BLUE   = "\033[94m"

    print(ESC_CLEAR, end="")

    while True:
        try:
            lines   = []
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            lines.append(f"{'='*100}")
            lines.append(
                f"  {C_BLUE}KuCoin{C_RESET} 4H Scanner  v11  ·  {now_utc}"
                f"  ·  length={length}  ·  mode={live_mode.upper()}"
            )
            lines.append(f"  Symbols: {len(SYMBOLS)}   Interval: 4hour   Pivot: {length} bars")
            lines.append(f"{'='*100}")

            # ── Per-symbol summary table ──────────────────────────────────────
            lines.append(
                f"  {C_WHITE}"
                f"{'SYMBOL':<12} {'CANDLES':>7}  {'SIGNAL':<7} "
                f"{'PRICE':>14}  {'PATTERN':<26}  {'SIGNAL TIME (UTC)':<19}  {'NEXT CLOSE':>10}"
                f"{C_RESET}"
            )
            lines.append(f"  {'-'*98}")

            for sym in SYMBOLS:
                buf   = list(signal_buffer[sym])
                n_can = len(ohlc_data[sym])
                pair  = KC_PAIR[sym]

                if buf:
                    last = buf[-1]
                    sig  = last["direction"]
                    prc  = f"{last['price']:.6g}"
                    pat  = last["pattern"][:24] if last["pattern"] else "-"
                    dt   = datetime.utcfromtimestamp(last["bar_time"])
                    dstr = dt.strftime("%Y-%m-%d %H:%M")
                else:
                    sig  = "NONE"
                    prc  = "-"
                    pat  = "-"
                    dstr = "-"

                sig_col = (
                    f"{C_GREEN}{sig:<7}{C_RESET}" if sig == "LONG"  else
                    f"{C_RED}{sig:<7}{C_RESET}"   if sig == "SHORT" else
                    f"{C_GRAY}{sig:<7}{C_RESET}"
                )

                secs      = seconds_until_close(sym)
                countdown = format_countdown(secs) if candle_close_time[sym] else "awaiting..."

                lines.append(
                    f"  {C_YELLOW}{pair:<12}{C_RESET} "
                    f"{n_can:>7}  "
                    f"{sig_col} "
                    f"{prc:>14}  "
                    f"{C_ORANGE}{pat:<26}{C_RESET}  "
                    f"{dstr:<19}  "
                    f"{countdown:>10}"
                )

            lines.append(f"{'='*100}")

            # ── Latest signals detail panel ───────────────────────────────────
            active = [(s, list(signal_buffer[s])) for s in SYMBOLS if signal_buffer[s]]
            if active:
                lines.append(f"\n  {C_CYAN}── LATEST SIGNALS PER SYMBOL {'─'*62}{C_RESET}")
                for sym, buf in active:
                    lines.append(f"\n  {C_YELLOW}{KC_PAIR[sym]}{C_RESET}")
                    for rank, sig in enumerate(reversed(buf), start=1):
                        direction = sig["direction"]
                        col = C_GREEN if direction == "LONG" else C_RED
                        dt  = datetime.utcfromtimestamp(sig["bar_time"])
                        lines.append(
                            f"    [{rank}] {col}{direction:<6}{C_RESET}"
                            f"  price={sig['price']:.6g}"
                            f"  O={sig['open']:.6g} H={sig['high']:.6g}"
                            f" L={sig['low']:.6g} C={sig['close']:.6g}"
                            f"  pattern={sig['pattern'] or '-'}"
                            f"  {dt.strftime('%Y-%m-%d %H:%M UTC')}"
                        )
                lines.append(f"\n  {'='*100}")

            # ── ADDED: Feature 5 — Signal Engine Output panel ─────────────────
            # Displays computed engine output for all symbols that have a full
            # 3-signal buffer and a unique (OHLC-deduped) signal ready.
            # Format: SYMBOL / ENTRY PRICE / SIGNAL TYPE
            engine_active = [(s, engine_output[s]) for s in SYMBOLS if engine_output[s]]
            if engine_active:
                lines.append(
                    f"\n  {C_CYAN}── SIGNAL ENGINE OUTPUT (3-Signal System) "
                    f"{'─'*53}{C_RESET}"
                )
                lines.append(
                    f"  {C_WHITE}"
                    f"{'SYMBOL':<14}  {'ENTRY PRICE':>16}  {'SIGNAL TYPE':<10}"
                    f"{C_RESET}"
                )
                lines.append(f"  {'-'*44}")
                for sym, out in engine_active:
                    sig_col = (
                        f"{C_GREEN}{out['signal_type']:<10}{C_RESET}"
                        if out["signal_type"] == "LONG" else
                        f"{C_RED}{out['signal_type']:<10}{C_RESET}"
                    )
                    lines.append(
                        f"  {C_YELLOW}{out['symbol']:<14}{C_RESET}"
                        f"  {out['entry_price']:>16.6g}"
                        f"  {sig_col}"
                    )
                lines.append(f"\n  {'='*100}")
            # ── END: Signal Engine Output panel ───────────────────────────────

            sys.stdout.write(ESC_HOME + "\n".join(lines) + "\n")
            sys.stdout.flush()

        except Exception:
            pass

        await asyncio.sleep(DASHBOARD_REFRESH)

# =============================================================================
# SECTION 7 — KUCOIN REST KLINES FETCHER
#
# KuCoin candle row format (IMPORTANT — different field order from most APIs):
#   [0] start_time_s  (Unix seconds)
#   [1] open
#   [2] close         ← NOTE: close is at index 2, NOT index 4
#   [3] high
#   [4] low
#   [5] volume        (transaction volume)
#   [6] amount        (transaction amount in quote currency)
#
# KuCoin returns rows newest-first; we sort oldest-first before use.
# Maximum 1500 rows per request; paginate with startAt/endAt for more.
# Exclude the still-open candle (candle whose close-time > now).
# =============================================================================

def _parse_kc_row(row: list) -> dict | None:
    """Parse one raw KuCoin kline row into a unified candle dict."""
    try:
        ts = int(row[0])   # already in seconds
        return {
            "time":   ts,
            "open":   float(row[1]),
            "close":  float(row[2]),   # ← KuCoin index 2 = close
            "high":   float(row[3]),   # ← KuCoin index 3 = high
            "low":    float(row[4]),   # ← KuCoin index 4 = low
            "volume": float(row[5]),
        }
    except (IndexError, ValueError, TypeError):
        return None


async def fetch_kucoin_klines(
    session: aiohttp.ClientSession,
    symbol:  str,
    start_at: int | None = None,
    end_at:   int | None = None,
) -> list[dict]:
    """
    Fetch closed 4H klines from KuCoin REST API for one symbol.

    Parameters
    ----------
    start_at : Unix timestamp (seconds) — start of window (inclusive)
    end_at   : Unix timestamp (seconds) — end of window (inclusive)

    Returns a list of closed candle dicts sorted oldest-first.
    """
    pair = KC_PAIR[symbol]
    url  = f"{KC_REST_BASE}{KC_KLINES_PATH}"

    params: dict = {"symbol": pair, "type": KC_INTERVAL}
    if start_at is not None:
        params["startAt"] = str(start_at)
    if end_at is not None:
        params["endAt"] = str(end_at)

    try:
        async with session.get(
            url, params=params,
            timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            if resp.status != 200:
                log.warning("[REST] HTTP %d for %s", resp.status, symbol)
                return []

            body = await resp.json(content_type=None)
            if body.get("code") != "200000":
                log.warning("[REST] API error %s: %s", symbol, body.get("msg", body))
                return []

            rows = body.get("data", [])
            if not rows:
                return []

            candles = [c for row in rows if (c := _parse_kc_row(row)) is not None]

            # Sort oldest-first (KuCoin returns newest-first)
            candles.sort(key=lambda c: c["time"])

            # Exclude the still-open candle
            now_s   = int(datetime.now(timezone.utc).timestamp())
            closed  = [c for c in candles if get_candle_close_time(c["time"]) <= now_s]

            return closed

    except aiohttp.ClientConnectionError as e:
        log.error("[REST] Connection error %s: %s", symbol, e)
    except asyncio.TimeoutError:
        log.error("[REST] Timeout %s", symbol)
    except Exception as e:
        log.error("[REST] Error %s: %s: %s", symbol, type(e).__name__, e)

    return []


async def fetch_history_for_symbol(
    session: aiohttp.ClientSession,
    symbol:  str,
    total:   int = HIST_TOTAL_CANDLES,
) -> list[dict]:
    """
    Fetch `total` closed 4H candles for one symbol using backward pagination
    (endAt sliding backwards by one candle per page).

    KuCoin returns up to 1500 per request — one call is usually enough
    for HIST_TOTAL_CANDLES=1500.  Falls back to pagination if needed.
    """
    all_candles: list[dict] = []
    now_s = int(datetime.now(timezone.utc).timestamp())
    end_at = now_s

    for page in range(1, 20):   # safety cap
        batch = await fetch_kucoin_klines(session, symbol, end_at=end_at)
        if not batch:
            break

        # Merge, dedup
        existing = {c["time"] for c in all_candles}
        new = [c for c in batch if c["time"] not in existing]
        all_candles.extend(new)
        all_candles.sort(key=lambda c: c["time"])

        log.debug("[HIST] %s page %d: +%d → %d total", symbol, page, len(new), len(all_candles))

        if len(all_candles) >= total:
            break

        # Move end_at back to just before the oldest candle we have
        end_at = all_candles[0]["time"] - 1
        if end_at <= 0:
            break

        await asyncio.sleep(0.2)

    # Keep newest `total` candles
    return all_candles[-total:]


async def load_all_history() -> None:
    """
    Concurrently load historical candles for all symbols (max HIST_SEMAPHORE
    parallel requests).  Replays through run_strategy() to seed signal_buffer.
    """
    print(f"  [HIST] Fetching up to {HIST_TOTAL_CANDLES} closed 4H candles/symbol "
          f"from KuCoin ...")

    semaphore = asyncio.Semaphore(HIST_SEMAPHORE)

    async def load_one(sess: aiohttp.ClientSession, sym: str) -> int:
        async with semaphore:
            candles = await fetch_history_for_symbol(sess, sym)
            if not candles:
                log.warning("[HIST] %s: no candles loaded.", sym)
                return 0

            ohlc_data[sym]         = candles
            last_candle_time[sym]  = candles[-1]["time"]
            candle_close_time[sym] = get_candle_close_time(candles[-1]["time"])

            log.info(
                "[HIST] %s: %d candles | %s → %s",
                sym, len(candles),
                datetime.utcfromtimestamp(candles[0]["time"]).strftime("%Y-%m-%d %H:%M UTC"),
                datetime.utcfromtimestamp(candles[-1]["time"]).strftime("%Y-%m-%d %H:%M UTC"),
            )

            # Historical replay — same candle-close trigger as live
            full = candles[:]
            for n in range(len(full)):
                ohlc_data[sym] = full[: n + 1]
                run_strategy(sym)
            ohlc_data[sym] = full
            return len(candles)

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[load_one(session, s) for s in SYMBOLS],
            return_exceptions=True,
        )

    seeded = sum(1 for r in results if isinstance(r, int) and r > 0)
    print(f"  [HIST] Complete: {seeded}/{len(SYMBOLS)} symbols seeded.")

# =============================================================================
# SECTION 8A — KUCOIN WEBSOCKET LIVE FEED
#
# KuCoin WS protocol:
#   1. POST /api/v1/bullet-public  → get token + dynamic WS endpoint URL
#                                    + pingInterval (ms)
#   2. Connect: wss://<endpoint>?token=<token>&connectId=<uuid>
#   3. Wait for {"type":"welcome"} message
#   4. Subscribe: {"type":"subscribe","topic":"<topics>","response":true,"id":"<id>"}
#      Batch all symbols into one subscription (comma-separated topics per message,
#      but KuCoin recommends ≤50 per subscription message)
#   5. Handle: {"type":"message","topic":"/market/candles:BTC-USDT_4hour",
#               "subject":"trade.candles.update","data":{...}}
#   6. Ping: {"type":"ping","id":"<id>"} every pingInterval ms;
#      server responds with {"type":"pong","id":"<id>"}
#
# Candle-close detection:
#   KuCoin kline WS fires on every trade (not just candle close).
#   The candle is closed when we receive a NEW open_time (data.candles[0])
#   that is DIFFERENT from the previously seen open_time for that symbol.
#   The OLD candle (saved in pending_candle[sym]) is then committed and
#   run_strategy() is fired.
# =============================================================================

async def _get_ws_token(session: aiohttp.ClientSession) -> tuple[str, str, int] | None:
    """
    POST /api/v1/bullet-public and return (token, ws_endpoint, ping_interval_ms).
    Returns None on failure.
    """
    url = f"{KC_REST_BASE}{KC_BULLET_PATH}"
    try:
        async with session.post(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                log.warning("[WS] bullet-public HTTP %d", resp.status)
                return None
            body = await resp.json(content_type=None)
            if body.get("code") != "200000":
                log.warning("[WS] bullet-public error: %s", body)
                return None
            data     = body["data"]
            token    = data["token"]
            server   = data["instanceServers"][0]
            endpoint = server["endpoint"]
            ping_ms  = int(server.get("pingInterval", 18000))
            return token, endpoint, ping_ms
    except Exception as e:
        log.error("[WS] token fetch error: %s: %s", type(e).__name__, e)
        return None


def _parse_kc_ws_candle(msg: dict) -> tuple[str | None, dict | None]:
    """
    Parse a KuCoin kline WS message.
    Returns (symbol_key, candle_dict) or (None, None).
    """
    try:
        if msg.get("type") != "message":
            return None, None
        if msg.get("subject") not in ("trade.candles.update", "trade.candles.add"):
            return None, None

        topic = msg.get("topic", "")
        # topic = "/market/candles:BTC-USDT_4hour"
        pair_part = topic.split(":")[-1].split("_")[0]   # "BTC-USDT"

        sym = None
        for s in SYMBOLS:
            if KC_PAIR[s] == pair_part:
                sym = s
                break
        if sym is None:
            return None, None

        data    = msg["data"]
        row     = data["candles"]   # same format as REST kline row
        candle  = _parse_kc_row(row)
        if candle is None:
            return None, None

        return sym, candle

    except (KeyError, ValueError, TypeError, IndexError):
        return None, None


async def kucoin_ws() -> bool:
    """
    Connect to KuCoin WebSocket and process candle events.
    Returns False if connection fails before receiving any data
    (triggers polling fallback).
    """
    global live_mode

    if not HAS_WEBSOCKETS:
        log.warning("[WS] 'websockets' library not installed — falling back to polling.")
        return False

    got_any_data = False
    retry_delay  = 5

    while True:
        async with aiohttp.ClientSession() as session:
            token_info = await _get_ws_token(session)

        if token_info is None:
            if not got_any_data:
                log.warning("[WS] Could not obtain WS token — falling back to polling.")
                return False
            log.warning("[WS] Token refresh failed. Retry in %ds.", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
            continue

        token, endpoint, ping_ms = token_info
        connect_id = uuid.uuid4().hex
        ws_url     = f"{endpoint}?token={token}&connectId={connect_id}"

        log.info("[WS] Connecting to %s ...", endpoint)

        try:
            async with websockets.connect(
                ws_url,
                ping_interval=None,   # we handle pings manually (KuCoin protocol)
                open_timeout=20,
            ) as ws:

                # ── Wait for welcome ──────────────────────────────────────────
                welcome_raw = await asyncio.wait_for(ws.recv(), timeout=15)
                welcome     = json.loads(welcome_raw)
                if welcome.get("type") != "welcome":
                    log.warning("[WS] Expected welcome, got: %s", welcome)
                    # Try to continue anyway

                log.info("[WS] Connected. pingInterval=%dms", ping_ms)
                live_mode = "ws"

                # ── Subscribe in batches of 50 topics ─────────────────────────
                topics = [kc_ws_topic(s) for s in SYMBOLS]
                batch_size = 50
                for i in range(0, len(topics), batch_size):
                    batch = topics[i: i + batch_size]
                    sub_msg = {
                        "id":       uuid.uuid4().hex,
                        "type":     "subscribe",
                        "topic":    ",".join(batch),
                        "response": True,
                    }
                    await ws.send(json.dumps(sub_msg))
                    log.info("[WS] Subscribed batch %d–%d (%d topics)",
                             i + 1, i + len(batch), len(batch))
                    await asyncio.sleep(0.1)

                # ── Receive loop with background ping task ────────────────────
                ping_interval_s = ping_ms / 1000.0

                async def ping_loop():
                    while True:
                        await asyncio.sleep(ping_interval_s * 0.9)
                        try:
                            pid = uuid.uuid4().hex
                            await ws.send(json.dumps({"id": pid, "type": "ping"}))
                            log.debug("[WS] ping sent id=%s", pid)
                        except Exception:
                            break

                ping_task = asyncio.get_event_loop().create_task(ping_loop())

                try:
                    async for raw_msg in ws:
                        try:
                            msg = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            continue

                        msg_type = msg.get("type", "")

                        # Skip ack / pong / welcome
                        if msg_type in ("ack", "pong", "welcome", "notice"):
                            continue

                        if msg_type == "error":
                            log.warning("[WS] Server error: %s", msg)
                            continue

                        sym, candle = _parse_kc_ws_candle(msg)
                        if sym is None:
                            continue

                        got_any_data = True

                        # Update close-time countdown
                        candle_close_time[sym] = get_candle_close_time(candle["time"])

                        prev_open_time = last_candle_time[sym]

                        if prev_open_time is None:
                            # First tick for this symbol
                            last_candle_time[sym] = candle["time"]
                            pending_candle[sym]   = candle

                        elif candle["time"] != prev_open_time:
                            # ── CANDLE CLOSED ─────────────────────────────────
                            # The open-time changed → prev candle is now closed.
                            closed = pending_candle[sym]
                            if closed is not None:
                                already = (
                                    ohlc_data[sym]
                                    and ohlc_data[sym][-1]["time"] == closed["time"]
                                )
                                if not already:
                                    ohlc_data[sym].append(closed)

                                log.info(
                                    "[%s] WS candle closed %s | O:%.6g C:%.6g",
                                    sym,
                                    datetime.utcfromtimestamp(closed["time"])
                                          .strftime("%Y-%m-%d %H:%M UTC"),
                                    closed["open"], closed["close"],
                                )
                                run_strategy(sym)

                                # ── ADDED Feature 6: WS hard-refresh layer ────
                                # signal_buffer is already updated inside run_strategy.
                                # _run_signal_engine() is already called inside run_strategy.
                                # No extra fetch needed in WS mode — WS data IS the hard feed.
                                # The hard-refresh here means: signal_buffer state is freshly
                                # computed from the just-closed candle, not carried from cache.
                                # (run_strategy above already performs this by design.)
                                # ── END hard-refresh marker ───────────────────

                            last_candle_time[sym] = candle["time"]
                            pending_candle[sym]   = candle

                        else:
                            # Intra-candle tick — update pending only, no strategy
                            pending_candle[sym] = candle

                finally:
                    ping_task.cancel()

            retry_delay = 5   # reset on clean disconnect

        except websockets.exceptions.ConnectionClosedError as e:
            log.warning("[WS] Connection closed: %s. Retry in %ds.", e, retry_delay)
        except websockets.exceptions.WebSocketException as e:
            log.warning("[WS] WS error: %s. Retry in %ds.", e, retry_delay)
        except OSError as e:
            log.warning("[WS] Network error: %s. Retry in %ds.", e, retry_delay)
        except Exception as e:
            if not got_any_data:
                log.warning("[WS] Failed before any data: %s. Falling back to polling.", e)
                return False
            log.error("[WS] Unexpected: %s: %s. Retry in %ds.", type(e).__name__, e, retry_delay)

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)

# =============================================================================
# SECTION 8B — REST POLLING FALLBACK
#
# When WebSocket is unavailable, polls KuCoin REST every POLL_INTERVAL seconds.
# On each poll, fetches the last 50 closed candles per symbol and appends any
# that are not already in ohlc_data[sym], then fires run_strategy().
# =============================================================================

async def rest_poll_loop() -> None:
    global live_mode
    live_mode = "poll"

    print(f"  [POLL] Using REST polling fallback — fetching only at candle close ...")
    await asyncio.sleep(5)   # stagger first polls

    semaphore = asyncio.Semaphore(HIST_SEMAPHORE)

    async def poll_one(sess: aiohttp.ClientSession, sym: str) -> None:
        async with semaphore:
            try:
                # ── ADDED Feature 6: Hard refresh — re-fetch candle data ───────
                # Discard cached candles, reload fresh from REST, replay strategy.
                new_candles = await fetch_kucoin_klines(sess, sym)
                if not new_candles:
                    return

                candle_close_time[sym] = get_candle_close_time(new_candles[-1]["time"])

                existing_times = {c["time"] for c in ohlc_data[sym]}
                fresh = sorted(
                    [c for c in new_candles if c["time"] not in existing_times],
                    key=lambda c: c["time"],
                )

                for c in fresh:
                    ohlc_data[sym].append(c)
                    last_candle_time[sym] = c["time"]
                    log.info(
                        "[%s] POLL new candle %s | O:%.6g C:%.6g",
                        sym,
                        datetime.utcfromtimestamp(c["time"]).strftime("%Y-%m-%d %H:%M UTC"),
                        c["open"], c["close"],
                    )
                    run_strategy(sym)

            except Exception as e:
                log.error("[POLL] %s error: %s: %s", sym, type(e).__name__, e)

    # ── ADDED Feature 6/Q7: Poll only at candle close, not on fixed interval ──
    # Sleep until the next candle close time, then hard-fetch all symbols.
    # Uses candle_close_time of the first seeded symbol as reference.
    # Falls back to POLL_INTERVAL if no close time is known yet.
    while True:
        # Find the next candle close time across all symbols
        now_s = int(datetime.now(timezone.utc).timestamp())

        # Find minimum positive seconds-to-close across symbols that have data
        known_closes = [
            candle_close_time[s] for s in SYMBOLS
            if candle_close_time[s] is not None
        ]

        if known_closes:
            # All symbols share the same 4H grid — use the nearest close
            next_close = min(known_closes)
            wait_secs  = max(1, next_close - now_s)
        else:
            # No candle close time known yet — fallback to POLL_INTERVAL
            wait_secs = POLL_INTERVAL

        log.info("[POLL] Next candle close in %ds", wait_secs)
        await asyncio.sleep(wait_secs)

        # ── Candle close arrived — hard refresh all symbols ───────────────────
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(*[poll_one(session, s) for s in SYMBOLS])

# =============================================================================
# SECTION 9 — MAIN ENTRY POINT
# =============================================================================

async def main() -> None:
    print("\033[2J\033[H", end="")
    print("=" * 68)
    print("  KuCoin Multi-Symbol OHLC Scanner  v11")
    print(f"  Strategy      : Pine Script 'Hammer' indicator")
    print(f"  Exchange      : KuCoin Spot  (api.kucoin.com)")
    print(f"  Interval      : 4hour  ({INTERVAL_SECONDS // 3600}H)")
    print(f"  Symbols       : {len(SYMBOLS)}")
    print(f"  Pivot length  : {length}")
    print(f"  History       : up to {HIST_TOTAL_CANDLES} candles/symbol")
    print(f"  WS protocol   : POST bullet-public → dynamic token URL")
    print(f"  Signal output : SYMBOL | SIGNAL TYPE | TIME")
    print("=" * 68)

    # Step 1 — load historical candles and replay through strategy
    await load_all_history()

    # ADDED: one-time startup blast — send current signal of each symbol (once)
    # At this point last_sent_s3_key is already set for all symbols from replay.
    # We reset it so the current state gets sent exactly once per symbol.
    global _engine_active
    sent = 0
    for sym in SYMBOLS:
        out = engine_output[sym]
        if out is None:
            continue
        # Reset key so this signal is treated as new (sends once)
        last_sent_s3_key[sym] = None
        buf = list(signal_buffer[sym])
        if len(buf) == 3:
            s3 = buf[-1]
            s3_key = (s3["direction"], s3["bar_time"], s3["price"])
            last_sent_s3_key[sym] = s3_key
            tg_msg = _format_signal_message(out["symbol"], out["entry_price"], out["signal_type"])
            await _send_engine_telegram(tg_msg)
            await asyncio.sleep(2)
            sent += 1
    print(f"  [BLAST] Startup signals sent: {sent}")

    # Activate engine — from now on, only NEW live signals go to Telegram
    _engine_active = True
    log.info("[ENGINE] Active — live signals will now be sent to Telegram.")

    # Step 2 — start live dashboard in background
    asyncio.get_event_loop().create_task(dashboard_loop())

    # Step 4 — start KuCoin WebSocket; fall back to REST polling if needed
    ws_ok = await kucoin_ws()

    if not ws_ok:
        await rest_poll_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Stopped by user.")