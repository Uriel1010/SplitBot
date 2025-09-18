import os
import json
import time
import threading
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
import asyncio
import re
import json as _json
from db import init_db, ensure_chat, get_currency, ensure_user as db_ensure_user, add_virtual_user as db_add_virtual_user, get_next_expense_id, insert_expense, list_expenses as db_list_expenses, count_expenses as db_count_expenses, list_users as db_list_users, compute_balances as db_compute_balances, list_settlements as db_list_settlements, export_expenses as db_export_expenses, set_chat_currency, get_chat, category_totals

try:
    import yfinance as yf  # type: ignore
except ImportError:  # pragma: no cover
    yf = None

try:
    import google.generativeai as genai  # type: ignore
except ImportError:
    genai = None

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("splitbot")
log_currency = logging.getLogger("splitbot.currency")
log_ai = logging.getLogger("splitbot.ai")

# Core constants (must appear before translation dict which references them)
EPS = 0.01
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "USD").upper()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()

# Ollama provider configuration
AI_PROVIDER = (os.getenv("AI_PROVIDER") or ("GEMINI" if GEMINI_API_KEY else "" )).upper().strip()
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b").strip()

# Unconditionally rewrite localhost/127.* when using OLLAMA to improve container ↔ host connectivity.
if AI_PROVIDER == "OLLAMA" and ("localhost" in OLLAMA_BASE_URL or "127.0.0.1" in OLLAMA_BASE_URL):
    _orig = OLLAMA_BASE_URL
    OLLAMA_BASE_URL = OLLAMA_BASE_URL.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")
    log_ai.info("Rewrote OLLAMA_BASE_URL %s -> %s", _orig, OLLAMA_BASE_URL)

AI_ENABLED = False
AI_PROVIDER_ACTIVE = None  # 'GEMINI' or 'OLLAMA' or None
if AI_PROVIDER == "GEMINI" and GEMINI_API_KEY and genai is not None:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        AI_ENABLED = True
        AI_PROVIDER_ACTIVE = "GEMINI"
    except Exception as e:  # pragma: no cover
        logging.warning("Failed to configure Gemini: %s", e)
elif AI_PROVIDER == "OLLAMA":
    AI_ENABLED = True
    AI_PROVIDER_ACTIVE = "OLLAMA"
else:
    AI_ENABLED = False
    AI_PROVIDER_ACTIVE = None

# Simple health check cache for Ollama provider
_AI_HEALTH_LAST_CHECK = 0.0
_AI_HEALTH_OK = False
_AI_HEALTH_TTL = 300  # seconds
_AI_LAST_ERROR: Optional[str] = None

def _check_ollama_health(force: bool = False) -> bool:
    """Return True if current AI provider is healthy (or not Ollama).
    For Ollama: GET /api/tags (lightweight) every _AI_HEALTH_TTL seconds.
    """
    global _AI_HEALTH_LAST_CHECK, _AI_HEALTH_OK
    if AI_PROVIDER_ACTIVE != "OLLAMA":
        return AI_ENABLED and bool(AI_PROVIDER_ACTIVE)
    now = time.time()
    if not force and (now - _AI_HEALTH_LAST_CHECK) < _AI_HEALTH_TTL:
        return _AI_HEALTH_OK
    _AI_HEALTH_LAST_CHECK = now
    global OLLAMA_BASE_URL
    try:
        import http.client
        from urllib.parse import urlparse
        def _probe(base: str) -> Tuple[bool, Optional[str]]:
            try:
                u = urlparse(base)
                conn = http.client.HTTPConnection(u.hostname, u.port or (80 if u.scheme == 'http' else 443), timeout=5)
                conn.request("GET", "/api/tags")
                resp = conn.getresponse()
                if resp.status == 200:
                    return True, None
                return False, f"HTTP {resp.status}"
            except Exception as ex:  # pragma: no cover
                return False, f"{type(ex).__name__}: {ex}"
        ok, err = _probe(OLLAMA_BASE_URL)
        if not ok and err:
            log_ai.debug("Primary Ollama probe failed base=%s err=%s", OLLAMA_BASE_URL, err)
        if not ok:
            # second chance: ensure host.docker.internal variant tried (already rewritten earlier, but just in case custom URL given)
            alt = OLLAMA_BASE_URL
            if "host.docker.internal" not in alt:
                alt = alt.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")
            if alt != OLLAMA_BASE_URL:
                ok2, err2 = _probe(alt)
                if ok2:
                    log_ai.info("Switched OLLAMA_BASE_URL to %s after failed probe", alt)
                    OLLAMA_BASE_URL = alt
                    ok = True
                    err = None
                else:
                    log_ai.debug("Alt Ollama probe failed alt=%s err=%s", alt, err2)
                    if err is None:
                        err = err2
        _AI_HEALTH_OK = ok
        if not ok:
            _AI_LAST_ERROR = err or "unknown error"
            log_ai.warning("Ollama health unresolved base=%s error=%s", OLLAMA_BASE_URL, _AI_LAST_ERROR)
        else:
            _AI_LAST_ERROR = None
        return ok
    except Exception as e:  # pragma: no cover
        _AI_LAST_ERROR = f"{type(e).__name__}: {e}"
        log_ai.warning("Ollama health exception base=%s error=%s", OLLAMA_BASE_URL, _AI_LAST_ERROR)
        _AI_HEALTH_OK = False
        return False

# Categories & emojis
CATEGORIES = [
    "food", "groceries", "transport", "entertainment", "travel", "utilities", "health", "rent", "other"
]
CATEGORY_EMOJI = {
    "food": "🍽️",
    "groceries": "🛒",
    "transport": "🚕",
    "entertainment": "🎉",
    "travel": "✈️",
    "utilities": "💡",
    "health": "💊",
    "rent": "🏠",
    "other": "📦",
}
CATEGORY_SYNONYMS = {
    "meal": "food",
    "dinner": "food",
    "lunch": "food",
    "breakfast": "food",
    "uber": "transport",
    "taxi": "transport",
    "bus": "transport",
    "flight": "travel",
    "hotel": "travel",
    "movie": "entertainment",
    "cinema": "entertainment",
    "pharmacy": "health",
    "medicine": "health",
}

INLINE_CURRENCIES = ["ILS", "USD", "EUR", "GBP", "JPY", "CHF", "CAD"]
_notified_limited_mode = set()
PENDING_EXPENSES: Dict[int, Dict[str, Any]] = {}
PENDING_NAMES: Dict[int, int] = {}  # chat_id -> user_id awaiting name text
PAGE_SIZE = 10

# Hebrew localization flag (always true for now)
# Hebrew localization flag (legacy global, now overridden per chat via /lang)
HE_IL = True  # will be updated dynamically when /lang used

"# Translation dictionary (basic static mapping)"
T = {
    "start": "👋 היי! אני הבוט לפיצול הוצאות. כתוב /help כדי לראות פקודות.",
    "help": (
        "📘 פקודות קיימות:\n"
        "ℹ️ /help - עזרה זו.\n"
        "🚀 /start - הודעת פתיחה.\n"
        "� /setcurrency [ISO3] - קביעת מטבע לפני הוצאה ראשונה.\n"
        "💰 /currency - הצגת המטבע הנוכחי.\n"
        "➕ /add <סכום> [ISO3] <תיאור> - הוספת הוצאה.\n"
        "🧑‍🤝‍🧑 /adduser [שם] - הוספת משתתף וירטואלי או שמך.\n"
        "👥 /users - רשימת משתתפים.\n"
        "🧾 /list [עמוד] - הוצאות (דפדוף עם חצים).\n"
        "⚖️ /bal - מאזנים משוקללים.\n"
        "🤝 /settle - הצעות לסגירת חובות.\n"
        "🏷️ /categories - רשימת קטגוריות.\n"
        "📊 /stats - סיכום לפי קטגוריה.\n"
    "🤖 /ai - סטטוס ספק ה-AI.\n"
        "📤 /export - יצוא CSV.\n"
        "🌐 /lang - החלפת שפה (עברית/English).\n"
        "♻️ /reset - איפוס מוחק הכל.\n"
        "✍️ טקסט חופשי (למשל: '120 שח על מצות') יוצר הוצאה ממתינה לאישור.\n"
        "(מטבע נוכחי: {currency})"
    ),
    "choose_currency": "מטבע נוכחי: {cur}. בחר חדש (נחסם אחרי הוצאה ראשונה):",
    "usage_setcurrency": "שימוש: /setcurrency <ISO3>",
    "bad_currency": "מטבע חייב להיות קוד בן 3 אותיות (לדוגמה USD, EUR, ILS).",
    "currency_locked": "אי אפשר לשנות מטבע אחרי שיש הוצאות (עדיין {cur}).",
    "currency_already": "המטבע כבר מוגדר ל-{cur}.",
    "currency_changed": "מטבע עודכן: {old} -> {new}.",
    "current_currency": "המטבע הנוכחי: {cur}",
    "add_usage": "שימוש: /add <amount> [ISO3] <description>",
    "amount_positive": "הסכום חייב להיות מספר חיובי.",
    "currency_mismatch": "מזהה מטבע שונה מהמטבע הצ'אט ({cur}). שנה עם /setcurrency לפני או הסר קוד.",
    "expense_recorded": "נרשמה הוצאה #{id} {amt:.2f} {cur} חולק בין {n} משתתפים.",
    "expense_recorded_conv": "נרשמה הוצאה #{id} {amt:.2f} {cur} (הומר מ-{oamt:.2f} {ocur} בשער {rate:.4f}) חולקה בין {n} משתתפים.",
    "auto_added": "נוספה הוצאה אוטומטית #{id} {amt:.2f} {cur} [{cat}] - {desc}",
    "auto_added_conv": "נוספה הוצאה אוטומטית #{id} {amt:.2f} {cur} (מ-{oamt:.2f} {ocur} בשער {rate:.4f}) [{cat}] - {desc}",
    "no_expenses": "אין הוצאות עדיין.",
    "expenses_header": "🧾 הוצאות (15 אחרונות) [{cur}]:",
    "balances_zero": "הכל סגור: המאזנים אפס.",
    "balances_zero_one": "הכל סגור: רק משתתף אחד ולכן אין חובות.",
    "balances_header": "⚖️ מאזנים [{cur}]:",
    "settle_none": "אין מה לסגור.",
    "settle_header": "🤝 תשלומים מוצעים [{cur}]:",
    "ai_disabled": "ניתוח AI כבוי (אין GEMINI_API_KEY). משתמש במנתח בסיסי.",
    "amount_not_found": "לא הצלחתי לזהות סכום בהודעה.",
    "reset_warn": "אזהרה: פקודת איפוס תמחק את כל ההוצאות והמשתמשים בצ'אט הזה. הפעל /reset confirm כדי לאשר.",
    "reset_done": "בוצע איפוס. המטבע כעת {cur} והכל נוקה.",
    "conversion_fail": "(ניסיון המרה נכשל - משאיר את הסכום המקורי.)",
    "pending_missing": "אין הוצאה ממתינה.",
    "pending_saved": "✅ נשמרה הוצאה #{id}.",
    "pending_canceled": "בוטל.",
    "approx_rate": "(שער משוער)",
    "adduser_usage": "שימוש: /adduser <שם>",
    "user_exists": "המשתתף כבר קיים.",
    "user_added": "נוסף משתתף: {name}",
    "users_header": "משתתפים:",
    "no_other_users": "עדיין רק משתמש אחד. הוספת משתתף תאפשר חובות.",
    # New strings for inline reset + name capture
    "reset_inline_warn": "⚠️ אזהרה: פעולה זו תמחק את כל ההוצאות והמשתתפים בצ'אט הזה. להמשיך?",
    "reset_inline_done": "♻️ בוצע איפוס נתונים. השתמשו ב-/adduser כדי להוסיף משתתפים חדשים.",
    "reset_inline_canceled": "❌ האיפוס בוטל.",
    "prompt_adduser": "👥 אין משתתפים עדיין. השתמשו ב-/adduser כדי להוסיף את שמכם.",
    "ask_name": "✍️ מה השם שנציג עבורך? שלח הודעה אחת עם השם.",
    "name_saved": "✅ השם נשמר: {name}",
}

# Currency synonym / symbol detection (Hebrew + symbols)
COMMON_CURRENCIES = [
    "USD","EUR","GBP","ILS","JPY","CHF","CAD","AUD","NZD","SEK","NOK","DKK","ZAR","PLN","TRY","MXN","BRL","INR","RUB","CNY","HKD","SGD","AED","SAR","EGP"
]

# Map lowercase tokens / symbols -> ISO
"""Currency synonyms map.
NOTE: We add multiple variants for Hebrew shekel forms including different quotes and punctuation.
We intentionally keep shorter tokens like 'שח' but later when cleaning description we remove an
isolated trailing currency word so it does not appear twice.
"""
CURRENCY_SYNONYMS = {
    "₪": "ILS",
    "שח": "ILS",        # common no-quote form
    "ש״ח": "ILS",       # Hebrew double quote char U+05F4
    "ש""ח": "ILS",      # ASCII quotes variant appears in source escaping
    "ש""ח.": "ILS",
    "ש""ח,": "ILS",
    "ש""ח?": "ILS",
    "ש""ח!": "ILS",
    "ש""ח\n": "ILS",
    "ש""ח\r": "ILS",
    "שקל": "ILS",
    "שקלים": "ILS",
    "שקל חדש": "ILS",
    "nis": "ILS",
    "n.i.s": "ILS",
    "ils": "ILS",
    "usd$": "USD",  # improbable but just in case
    "$": "USD",
    "eur": "EUR",
    "€": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "aud": "AUD",
    "cad": "CAD",
    "fr": "CHF",  # sometimes mistakenly typed
    "chf": "CHF",
    "yen": "JPY",
    "jpy": "JPY",
    "inr": "INR",
    "rs": "INR",
    "₹": "INR",
    "brl": "BRL",
    "real": "BRL",
    "mxn": "MXN",
    "peso": "MXN",
    "zar": "ZAR",
    "rand": "ZAR",
    "rub": "RUB",
    "руб": "RUB",
    "cny": "CNY",
    "rmb": "CNY",
    "元": "CNY",
    "sgd": "SGD",
    "hkd": "HKD",
    "aed": "AED",
    "درهم": "AED",
    "sar": "SAR",
    "ريال": "SAR",
    "egp": "EGP",
    # Hebrew plain words
    "דולר": "USD",
    "דולרים": "USD",
    "דולר אמריקאי": "USD",
    "יורו": "EUR",
    "אירו": "EUR",
    "פאונד": "GBP",
    "לירה": "GBP",
    "לירה שטרלינג": "GBP",
    "רופי": "INR",
    "רופי הודי": "INR",
    "פסו": "MXN",
    "ריאל": "BRL",
    "יואן": "CNY",
    "דירהם": "AED",
    "ריאל סעודי": "SAR",
    "לירה טורקית": "TRY",
    "שקל": "ILS",
    "שקל חדש": "ILS",
}

FX_CACHE: Dict[str, Tuple[float, float]] = {}  # pair -> (rate, timestamp_epoch)
FX_TTL_SECONDS = 60 * 60 * 6  # 6 hours

# Static emergency fallback mid-market estimates (update occasionally)
STATIC_FX_RATES = {
    # base pairs stored as FROM->TO (approx)
    "USD->ILS": 3.70,
    "EUR->ILS": 4.00,
    "GBP->ILS": 4.70,
    "USD->EUR": 0.92,
    "EUR->USD": 1.09,
}


def fx_pair_symbol(from_cur: str, to_cur: str) -> str:
    return f"{from_cur}{to_cur}=X"


def get_fx_rate(from_cur: str, to_cur: str) -> Tuple[Optional[float], bool]:
    """Return (rate, fallback_used) for FX FROM->TO.
    Fallback flag True when we could not obtain a direct OR inverse quote and had to bridge or use static table.
    Strategies:
      1. Direct quote (not fallback)
      2. Inverse quote (not fallback)
      3. Bridge via USD (fallback=True)
      4. Static table (fallback=True)
    Cached for FX_TTL_SECONDS.
    """
    if from_cur == to_cur:
        return 1.0, False
    pair = f"{from_cur}->{to_cur}"
    now = time.time()
    if pair in FX_CACHE:
        rate, ts = FX_CACHE[pair]
        if now - ts < FX_TTL_SECONDS:
            log_currency.debug("fx cache hit pair=%s rate=%s age=%.1fs", pair, rate, now - ts)
            return rate, False  # cache retains original fallback semantics; simplification: treat cached as non-fallback
    # Helper to fetch a direct yahoo pair
    def _fetch_direct(a: str, b: str) -> Optional[float]:
        if yf is None:
            return None
        symbol = fx_pair_symbol(a, b)
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1d")
            if hist.empty:
                log_currency.debug("fx empty history symbol=%s", symbol)
                return None
            r = float(hist["Close"].iloc[-1])
            log_currency.debug("fx direct fetch symbol=%s rate=%s", symbol, r)
            return r
        except Exception as e:  # pragma: no cover
            log_currency.debug("fx direct exception symbol=%s error=%s", symbol, e)
            return None

    # Strategy 1: direct
    direct = _fetch_direct(from_cur, to_cur)
    if direct:
        FX_CACHE[pair] = (direct, now)
        return direct, False
    # Strategy 2: inverse
    inverse = _fetch_direct(to_cur, from_cur)
    if inverse:
        inv_rate = 1.0 / inverse if inverse else None
        if inv_rate:
            FX_CACHE[pair] = (inv_rate, now)
            log_currency.debug("fx inverse used pair=%s base_rate=%s inv=%s", pair, inverse, inv_rate)
            return inv_rate, False
    # Strategy 3: bridge USD
    if from_cur != "USD" and to_cur != "USD":
        a, a_fb = get_fx_rate(from_cur, "USD")
        b, b_fb = get_fx_rate("USD", to_cur)
        if a and b:
            bridged = round(a * b, 6)
            FX_CACHE[pair] = (bridged, now)
            log_currency.debug("fx bridged via USD pair=%s rate=%s (a=%s b=%s)", pair, bridged, a, b)
            return bridged, True
    # Strategy 4: static fallback
    if pair in STATIC_FX_RATES:
        rate = STATIC_FX_RATES[pair]
        FX_CACHE[pair] = (rate, now)
        log_currency.debug("fx static fallback pair=%s rate=%s", pair, rate)
        return rate, True
    log_currency.debug("fx all strategies failed pair=%s", pair)
    return None, True


def detect_currency_token(text: str) -> Optional[str]:
    """Attempt to detect a foreign currency token in free text.

    Heuristics order:
      1. Amount immediately followed or preceded by ISO3 (e.g. 120usd, usd120, 120 usd, USD 120)
      2. Symbols / synonyms mapped in CURRENCY_SYNONYMS
      3. Standalone ISO3 words from COMMON_CURRENCIES
      4. Embedded symbol after digits (e.g. 120₪)
    Returns first match (normalized uppercase) or None.
    """
    if not text:
        log_currency.debug("detect_currency_token: empty text")
        return None
    lower = text.lower()
    # Normalize Hebrew quotes variant for detection (convert U+05F4 to straight quotes pattern we keyed)
    lower = lower.replace("ש״ח", "ש""ח")
    # 1. number + code or code + number (allow punctuation) e.g. 120usd, usd120, 120 usd, usd 120
    num_code_pattern = re.compile(r"(?:(\d+[\.,]?\d*)\s*([a-z]{3}))|(([a-z]{3})\s*(\d+[\.,]?\d*))")
    for m in num_code_pattern.finditer(lower):
        groups = [g for g in m.groups() if g]
        for g in groups:
            g2 = g.strip().lower()
            if len(g2) == 3 and g2.isalpha():
                iso = g2.upper()
                if iso in COMMON_CURRENCIES:
                    log_currency.debug("pattern num+code match iso=%s text=%s", iso, text)
                    return iso
    # 2. digits immediately followed by ₪ (common user input like 30₪) before generic synonym scan
    if re.search(r"\d+\s*₪", text):
        log_currency.debug("digits+₪ immediate match text=%s", text)
        return "ILS"
    # 3. symbols / synonyms substring search (prefer longer keys first to avoid partial overshadow)
    for key in sorted(CURRENCY_SYNONYMS.keys(), key=len, reverse=True):
        iso = CURRENCY_SYNONYMS[key]
        if key and key in lower:
            log_currency.debug("symbol/synonym match key=%s iso=%s text=%s", key, iso, text)
            return iso
    # 4. standalone ISO3 tokens
    for iso in COMMON_CURRENCIES:
        if re.search(rf"\b{iso.lower()}\b", lower):
            log_currency.debug("standalone iso match iso=%s text=%s", iso, text)
            return iso
    # 5. fallback: already handled digits+₪ above
    log_currency.debug("no currency detected text=%s", text)
    return None


def load_chat(chat_id: int) -> Dict[str, Any]:
    fp = DATA_DIR / f"{chat_id}.json"
    if not fp.exists():
        return {
            "chat_id": chat_id,
            "currency": DEFAULT_CURRENCY,
            "users": {},
            "expenses": [],
            "next_expense_id": 1,
            "virtual_seq": -1,
        }
    with fp.open("r", encoding="utf-8") as f:
        data = json.load(f)
    # Backfill currency if missing from old file
    if "currency" not in data:
        data["currency"] = DEFAULT_CURRENCY
    if "virtual_seq" not in data:
        data["virtual_seq"] = -1
    if "language" not in data:
        data["language"] = "he"
    return data


def save_chat(data: Dict[str, Any]):
    fp = DATA_DIR / f"{data['chat_id']}.json"
    tmp = fp.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(fp)


def compute_balances(data: Dict[str, Any]) -> Dict[str, float]:
    balances: Dict[str, float] = {uid: 0.0 for uid in data["users"].keys()}
    for exp in data["expenses"]:
        participants = exp["participants"]
        if not participants:
            continue
        share = exp["amount"] / len(participants)
        payer = str(exp["payer"])
        balances.setdefault(payer, 0.0)
        balances[payer] += exp["amount"] - share
        for uid in participants:
            suid = str(uid)
            balances.setdefault(suid, 0.0)
            if suid != payer:
                balances[suid] -= share
    # Round to cents for stability
    for k, v in balances.items():
        balances[k] = round(v, 2)
    return balances


def greedy_settlement(balances: Dict[str, float]) -> List[Dict[str, Any]]:
    creditors = []  # (user_id, amount > 0)
    debtors = []    # (user_id, amount < 0)
    for uid, amt in balances.items():
        if amt > EPS:
            creditors.append([uid, amt])
        elif amt < -EPS:
            debtors.append([uid, amt])
    creditors.sort(key=lambda x: x[1], reverse=True)
    debtors.sort(key=lambda x: x[1])  # most negative first
    settlements = []
    ci = 0
    di = 0
    while ci < len(creditors) and di < len(debtors):
        c_uid, c_amt = creditors[ci]
        d_uid, d_amt = debtors[di]
        pay = min(c_amt, -d_amt)
        settlements.append({
            "from": d_uid,
            "to": c_uid,
            "amount": round(pay, 2)
        })
        c_amt -= pay
        d_amt += pay
        creditors[ci][1] = c_amt
        debtors[di][1] = d_amt
        if c_amt <= EPS:
            ci += 1
        if d_amt >= -EPS:
            di += 1
    return settlements


async def start(update, context):
    chat_id = update.message.chat.id
    data = load_chat(chat_id)
    lang = data.get("language", "he")
    he = (lang == "he")
    text = T["start"] if he else "👋 Hi! I'm the expense split bot. Type /help to see commands."
    if not data["users"]:
        text += "\n" + (T["prompt_adduser"] if he else "No participants yet. Use /adduser to add your name.")
    await update.message.reply_text(text, disable_notification=True)


async def help_cmd(update, context):
    chat_id = update.message.chat.id
    data = load_chat(chat_id)
    lang = data.get("language", "he")
    he = (lang == "he")
    currency = data.get("currency", DEFAULT_CURRENCY)
    if he:
        text = T["help"].replace("{currency}", currency)
        if AI_PROVIDER_ACTIVE == "GEMINI":
            text += "\n🤖 מצב AI: Gemini פעיל."
        elif AI_PROVIDER_ACTIVE == "OLLAMA":
            healthy = _check_ollama_health()
            text += f"\n🤖 מצב AI: Ollama ({OLLAMA_MODEL}) {'פעיל' if healthy else 'לא זמין – מעבר לניתוח בסיסי'}"
        else:
            text += "\n🤖 מצב AI: כבוי (Regex בלבד)."
    else:
        # Build English help dynamically to reflect AI status & currency
        text = (
            "📘 Commands:\n"
            "ℹ️ /help - this help.\n"
            "🚀 /start - welcome message.\n"
            "💱 /setcurrency [ISO3] - set base currency before first expense.\n"
            "💰 /currency - show current currency.\n"
            "➕ /add <amount> [ISO3] <description> - add expense.\n"
            "🧑‍🤝‍🧑 /adduser [name] - add virtual participant or set your name.\n"
            "👥 /users - list participants.\n"
            "🧾 /list [page] - list expenses (pagination arrows).\n"
            "⚖️ /bal - weighted balances.\n"
            "🤝 /settle - settlement suggestions.\n"
            "🏷️ /categories - list categories.\n"
            "📊 /stats - category totals.\n"
            "🤖 /ai - AI provider status.\n"
            "📤 /export - export CSV.\n"
            "🌐 /lang - toggle language (Hebrew/English).\n"
            "♻️ /reset - wipe all data (confirmation).\n"
            "✍️ Free text like '120 ils falafel' creates a pending expense for confirmation.\n"
            f"(Current currency: {currency})"
        )
        if AI_PROVIDER_ACTIVE == "GEMINI":
            text += f"\n🤖 AI: Gemini model={GEMINI_MODEL} (healthy)"
        elif AI_PROVIDER_ACTIVE == "OLLAMA":
            healthy = _check_ollama_health()
            text += f"\n🤖 AI: Ollama model={OLLAMA_MODEL} base={OLLAMA_BASE_URL} status={'healthy' if healthy else 'unreachable'}"
        else:
            text += "\n🤖 AI: disabled (regex fallback)."
    await update.message.reply_text(text, disable_notification=True)

async def ai_status_cmd(update, context):
    msg = update.message
    chat_id = msg.chat.id
    data = load_chat(chat_id)
    lang = data.get("language", "he")
    he = (lang == "he")
    if AI_PROVIDER_ACTIVE == "GEMINI" and AI_ENABLED:
        text = ("ספק AI: Gemini\nמודל: " + GEMINI_MODEL + "\nמצב: פעיל") if he else f"AI Provider: Gemini\nModel: {GEMINI_MODEL}\nStatus: active"
    elif AI_PROVIDER_ACTIVE == "OLLAMA" and AI_ENABLED:
        healthy = _check_ollama_health(force=True)
        if he:
            text = f"ספק AI: Ollama\nמודל: {OLLAMA_MODEL}\nכתובת: {OLLAMA_BASE_URL}\nמצב: {'פעיל' if healthy else 'לא זמין'}"
            if not healthy and _AI_LAST_ERROR:
                text += f"\nשגיאה: {_AI_LAST_ERROR}"
        else:
            text = f"AI Provider: Ollama\nModel: {OLLAMA_MODEL}\nBase URL: {OLLAMA_BASE_URL}\nStatus: {'healthy' if healthy else 'unreachable'}"
            if not healthy and _AI_LAST_ERROR:
                text += f"\nError: {_AI_LAST_ERROR}"
    else:
        text = "AI כבוי: שימוש במנתח בסיסי." if he else "AI disabled: using regex parser."
    await msg.reply_text(text, disable_notification=True)

async def lang_cmd(update, context):
    msg = update.message
    chat_id = msg.chat.id
    data = load_chat(chat_id)
    current = data.get("language", "he")
    new_lang = "en" if current == "he" else "he"
    data["language"] = new_lang
    save_chat(data)
    global HE_IL
    HE_IL = (new_lang == "he")  # legacy for code paths still using HE_IL
    if new_lang == "he":
        await msg.reply_text("✅ השפה הוחלפה לעברית.", disable_notification=True)
    else:
        await msg.reply_text("✅ Language switched to English.", disable_notification=True)
    # Show help in new language
    await help_cmd(update, context)


async def categories_cmd(update, context):
    listing = []
    for c in CATEGORIES:
        listing.append(f"{CATEGORY_EMOJI.get(c,'')} {c}")
    if HE_IL:
        await update.message.reply_text("קטגוריות:\n" + ", ".join(listing), disable_notification=True)
    else:
        await update.message.reply_text("Categories:\n" + ", ".join(listing), disable_notification=True)


def normalize_category(raw: str) -> str:
    if not raw:
        return "other"
    r = raw.lower().strip()
    if r in CATEGORIES:
        return r
    if r in CATEGORY_SYNONYMS:
        return CATEGORY_SYNONYMS[r]
    for k, v in CATEGORY_SYNONYMS.items():
        if k in r:
            return v
    return "other"


async def ai_parse_expense(text: str, chat_currency: str) -> Dict[str, Any]:
    def regex_fallback():
        m = re.search(r"(\d+(?:[.,]\d+)?)", text)
        if not m:
            return None
        amt = float(m.group(1).replace(",", "."))
        desc = text.replace(m.group(0), "").strip() or "(no description)"
        cat = normalize_category(desc.split()[0]) if desc else "other"
        return {"amount": round(amt, 2), "description": desc, "category": cat}
    if not AI_ENABLED or not AI_PROVIDER_ACTIVE:
        return regex_fallback() or {"amount": None, "description": text, "category": "other"}
    # Health gate for Ollama
    if AI_PROVIDER_ACTIVE == "OLLAMA" and not _check_ollama_health():
        return regex_fallback() or {"amount": None, "description": text, "category": "other"}

    prompt = (
        "You are an expense extraction assistant. Return ONLY a JSON object with keys: "
        "amount (number), description (string), category (one of food, groceries, transport, entertainment, travel, utilities, health, rent, other). "
        f"If unsure, pick 'other'. Currency context: {chat_currency}. Message: {text}"
    )
    if AI_PROVIDER_ACTIVE == "GEMINI":
        try:
            model = genai.GenerativeModel(GEMINI_MODEL)
            resp = await asyncio.to_thread(model.generate_content, prompt)
            raw = getattr(resp, 'text', '').strip()
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                data = _json.loads(match.group(0))
                amount = data.get("amount")
                desc = data.get("description") or "(no description)"
                cat = normalize_category(data.get("category", ""))
                if isinstance(amount, (int, float)) and amount > 0:
                    return {"amount": round(float(amount), 2), "description": desc, "category": cat}
        except Exception as e:  # pragma: no cover
            logging.warning("Gemini parse failed: %s", e)
    elif AI_PROVIDER_ACTIVE == "OLLAMA":
        # Ollama local API: POST /api/generate {model,prompt,stream:false}
        try:
            import http.client, urllib.parse, json as __json
            from urllib.parse import urlparse
            u = urlparse(OLLAMA_BASE_URL)
            path = "/api/generate"
            payload = __json.dumps({
                "model": OLLAMA_MODEL,
                "prompt": prompt + "\nReturn ONLY raw JSON.",
                "stream": False,
            })
            conn = http.client.HTTPConnection(u.hostname, u.port or (80 if u.scheme == 'http' else 443), timeout=15)
            conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status == 200:
                body = resp.read().decode("utf-8", errors="ignore")
                # Ollama returns JSON with 'response' field containing the model text
                try:
                    outer = __json.loads(body)
                    raw_out = outer.get("response", "")
                except Exception:
                    raw_out = body
                match = re.search(r"\{.*\}", raw_out, re.DOTALL)
                if match:
                    data = _json.loads(match.group(0))
                    amount = data.get("amount")
                    desc = data.get("description") or "(no description)"
                    cat = normalize_category(data.get("category", ""))
                    if isinstance(amount, (int, float)) and amount > 0:
                        return {"amount": round(float(amount), 2), "description": desc, "category": cat}
            else:
                logging.warning("Ollama HTTP %s", resp.status)
        except Exception as e:  # pragma: no cover
            logging.warning("Ollama parse failed: %s", e)
    return regex_fallback() or {"amount": None, "description": text, "category": "other"}


async def free_text_handler(update: Update, context):
    msg = update.message
    if not msg or not msg.text or msg.text.startswith('/'):
        return
    chat_id = msg.chat.id
    # Pending name capture overrides expense parsing
    if chat_id in PENDING_NAMES and PENDING_NAMES[chat_id] == msg.from_user.id:
        name = msg.text.strip()
        data = load_chat(chat_id)
        # Assign or update this user's display name (real user id stored positively)
        ensure_user(data, msg.from_user)
        data["users"][str(msg.from_user.id)] = name[:40]  # limit length
        save_chat(data)
        PENDING_NAMES.pop(chat_id, None)
        await msg.reply_text(T["name_saved"].format(name=name) if HE_IL else f"Saved name: {name}", disable_notification=True)
        return
    data = load_chat(chat_id)
    lang = data.get("language", "he")
    he = (lang == "he")
    if (not AI_ENABLED or (AI_PROVIDER_ACTIVE == "OLLAMA" and not _check_ollama_health())) and chat_id not in _notified_limited_mode:
        if AI_PROVIDER_ACTIVE == "OLLAMA" and AI_ENABLED:
            warn = "שרת Ollama לא זמין – מעבר לניתוח בסיסי." if he else "Ollama unreachable – falling back to basic parser."
        else:
            warn = T["ai_disabled"] if he else "AI parsing disabled. Using basic parser."
        await msg.reply_text(warn, disable_notification=True)
        _notified_limited_mode.add(chat_id)
    # Show a transient "thinking" indicator if AI provider active & healthy (Gemini or Ollama reachable)
    thinking_msg = None
    need_ai = AI_ENABLED and AI_PROVIDER_ACTIVE and not (AI_PROVIDER_ACTIVE == "OLLAMA" and not _check_ollama_health())
    if need_ai:
        try:
            thinking_msg = await msg.reply_text("🤖 חושב..." if he else "🤖 Thinking...", disable_notification=True)
        except Exception as _e:  # pragma: no cover
            thinking_msg = None
    parsed = await ai_parse_expense(msg.text, data.get("currency", DEFAULT_CURRENCY))
    log_ai.debug("free_text parsed amount=%s desc=%s cat=%s", parsed.get("amount"), parsed.get("description"), parsed.get("category"))
    amount = parsed.get("amount")
    if not amount:
        if thinking_msg:
            try:
                await thinking_msg.delete()
            except Exception:  # pragma: no cover
                pass
        await msg.reply_text(T["amount_not_found"] if he else "Couldn't detect an amount.", disable_notification=True)
        return
    ensure_user(data, msg.from_user)
    participants = list(map(int, data["users"].keys()))
    payer_id = msg.from_user.id
    if payer_id not in participants:
        participants.append(payer_id)
    # Currency detection & conversion for free text similar to /add
    # Detect currency FIRST on the original user message to avoid losing a trailing token like 'דולר'
    original_message_text = msg.text
    initial_detected = detect_currency_token(original_message_text)
    description = parsed.get("description", "(no description)")
    # Strip trailing currency word/symbol duplicates (Hebrew forms) so they don't pollute description output
    desc_tokens = description.strip().split()
    if desc_tokens:
        last = desc_tokens[-1].lower()
        last_norm = last.replace("ש״ח", "ש""ח")
        if last_norm in CURRENCY_SYNONYMS or last_norm in {c.lower() for c in COMMON_CURRENCIES}:
            desc_tokens = desc_tokens[:-1]
    # After removing a trailing currency token we may have a dangling Hebrew preposition 'ב' (meaning 'for/at') or other short connector.
    connectors = {"ב", "על", "עם", "for", "at", "on", "to"}
    while desc_tokens and desc_tokens[-1].lower() in connectors:
        desc_tokens.pop()
    if desc_tokens:
        description = " ".join(desc_tokens).strip()
    else:
        description = "(no description)"
    if initial_detected:
        detected_cur = initial_detected
    else:
        detected_cur = detect_currency_token(description) or data.get("currency", DEFAULT_CURRENCY)
    chat_cur = data.get("currency", DEFAULT_CURRENCY)
    original_amount = round(amount, 2)
    original_currency = detected_cur
    rate_used = None
    fx_fallback = False
    final_amount = original_amount
    if detected_cur != chat_cur:
        rate, fb = get_fx_rate(detected_cur, chat_cur)
        if rate:
            final_amount = round(final_amount * rate, 2)
            rate_used = rate
            fx_fallback = fb
        else:
            fx_fallback = True
    pending = {
        "payer": payer_id,
        "amount": final_amount,
        "description": description,
        "participants": participants,  # list[int]
        "ts": time.time(),
        "category": parsed.get("category", "other"),
        "original_amount": original_amount,
        "original_currency": original_currency,
        "fx_rate": rate_used,
        "fx_fallback": fx_fallback,
    }
    PENDING_EXPENSES[chat_id] = pending
    preview_id = get_next_expense_id(chat_id)
    if rate_used:
        # Use final_amount (converted) for primary amount display; always show base chat currency (chat_cur)
        preview = T["auto_added_conv"].format(
            id=preview_id, amt=final_amount, cur=chat_cur,
            oamt=original_amount, ocur=original_currency, rate=rate_used,
            cat=pending['category'], desc=pending['description']
        )
        # Add approximate marker if FX fallback strategy used (bridged/static)
        if fx_fallback:
            preview = preview.replace(f"{final_amount:.2f} {chat_cur}", f"{final_amount:.2f}~ {chat_cur}")
        preview = f"{CATEGORY_EMOJI.get(pending['category'],'')} " + preview
    else:
        # If detected currency differs but we lacked a rate, still show original for clarity
        if original_currency != chat_cur:
            if fx_fallback:
                preview = T["auto_added"].format(id=preview_id, amt=final_amount, cur=chat_cur,
                                                  cat=pending['category'], desc=pending['description']) + \
                          f" (מקור: {original_amount:.2f} {original_currency} {T['approx_rate']})"
                preview = f"{CATEGORY_EMOJI.get(pending['category'],'')} " + preview
            else:
                preview = T["auto_added"].format(id=preview_id, amt=final_amount, cur=chat_cur,
                                                  cat=pending['category'], desc=pending['description']) + \
                          f" (מקור: {original_amount:.2f} {original_currency} ללא המרה)"
                preview = f"{CATEGORY_EMOJI.get(pending['category'],'')} " + preview
        else:
            preview = T["auto_added"].format(id=preview_id, amt=final_amount, cur=chat_cur,
                                              cat=pending['category'], desc=pending['description'])
            preview = f"{CATEGORY_EMOJI.get(pending['category'],'')} " + preview
    preview += ("\nמאשר?" if he else "\nApprove?")
    # Remove thinking indicator before sending preview
    if thinking_msg:
        try:
            await thinking_msg.delete()
        except Exception:  # pragma: no cover
            pass
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ אישור" if he else "✅ Yes", callback_data="AIEXP:ACCEPT"), InlineKeyboardButton("❌ ביטול" if he else "❌ Cancel", callback_data="AIEXP:CANCEL")]])
    await msg.reply_text(preview, reply_markup=keyboard, disable_notification=True)


def _is_currency(token: str) -> bool:
    return len(token) == 3 and token.isalpha()


async def set_currency(update, context):
    msg = update.message
    chat_id = msg.chat.id
    data = load_chat(chat_id)
    parts = msg.text.strip().split()
    if len(parts) == 1:
        keyboard = [
            [InlineKeyboardButton(code, callback_data=f"CUR:{code}") for code in INLINE_CURRENCIES[:3]],
            [InlineKeyboardButton(code, callback_data=f"CUR:{code}") for code in INLINE_CURRENCIES[3:6]],
            [InlineKeyboardButton(INLINE_CURRENCIES[6], callback_data=f"CUR:{INLINE_CURRENCIES[6]}")],
        ]
        await msg.reply_text(
            (T["choose_currency"].format(cur=data['currency']) if HE_IL else f"Current currency: {data['currency']}. Choose new (disabled after first expense):"),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    if len(parts) != 2:
        await msg.reply_text(T["usage_setcurrency"] if HE_IL else "Usage: /setcurrency <ISO3>", disable_notification=True)
        return
    code = parts[1].upper()
    if not _is_currency(code):
        await msg.reply_text(T["bad_currency"] if HE_IL else "Currency must be a 3-letter ISO code.", disable_notification=True)
        return
    if data["expenses"] and code != data["currency"]:
        await msg.reply_text(T["currency_locked"].format(cur=data['currency']) if HE_IL else f"Cannot change currency after expenses exist (still {data['currency']}).", disable_notification=True)
        return
    if code == data["currency"]:
        await msg.reply_text(T["currency_already"].format(cur=code) if HE_IL else f"Currency already set to {code}.", disable_notification=True)
        return
    old = data["currency"]
    data["currency"] = code
    save_chat(data)
    logging.info("[setcurrency] chat=%s old=%s new=%s", chat_id, old, code)
    await msg.reply_text(T["currency_changed"].format(old=old, new=code) if HE_IL else f"Default currency changed: {old} -> {code}.", disable_notification=True)


async def show_currency(update, context):
    msg = update.message
    chat_id = msg.chat.id
    data = load_chat(chat_id)
    cur = data.get('currency', DEFAULT_CURRENCY)
    await msg.reply_text(T["current_currency"].format(cur=cur) if HE_IL else f"Current currency: {cur}", disable_notification=True)


def ensure_user(data: Dict[str, Any], user) -> None:
    uid = str(user.id)
    if uid not in data["users"]:
        data["users"][uid] = user.first_name or f"User{uid}"
    # DB persistence
    try:
        db_ensure_user(data['chat_id'], int(uid), data["users"][uid])
    except Exception as e:  # pragma: no cover
        log.warning("failed to ensure user in db: %s", e)

def add_virtual_user(data: Dict[str, Any], name: str) -> bool:
    # Returns True if added, False if duplicate
    norm = name.strip()
    if not norm:
        return False
    # Check duplicates case-insensitive
    existing_lower = {v.lower(): k for k, v in data["users"].items()}
    if norm.lower() in existing_lower:
        return False
    vid = data.get("virtual_seq", -1)
    data["users"][str(vid)] = norm
    data["virtual_seq"] = vid - 1
    # DB
    try:
        added_id = db_add_virtual_user(data['chat_id'], norm)
        if added_id is not None and str(added_id) != str(vid):
            # align json virtual_seq if mismatch
            data["virtual_seq"] = added_id - 1
    except Exception as e:  # pragma: no cover
        log.warning("failed to add virtual user in db: %s", e)
    return True

async def adduser_cmd(update, context):
    msg = update.message
    chat_id = msg.chat.id
    data = load_chat(chat_id)
    parts = msg.text.strip().split(maxsplit=1)
    # If user supplies a name directly, treat as virtual participant add (legacy behavior)
    if len(parts) == 2 and parts[1].strip():
        name = parts[1].strip()
        added = add_virtual_user(data, name)
        if not added:
            await msg.reply_text(T["user_exists"] if HE_IL else "User already exists.", disable_notification=True)
            return
        save_chat(data)
        await msg.reply_text(T["user_added"].format(name=name) if HE_IL else f"Added user: {name}", disable_notification=True)
        return
    # No name given: initiate personal name capture for the invoking Telegram user
    PENDING_NAMES[chat_id] = msg.from_user.id
    await msg.reply_text(T["ask_name"] if HE_IL else "Send your display name in one message.", disable_notification=True)

async def users_cmd(update, context):
    msg = update.message
    chat_id = msg.chat.id
    data = load_chat(chat_id)
    if not data["users"]:
        await msg.reply_text(T["no_other_users"] if HE_IL else "No participants yet.", disable_notification=True)
        return
    lines = []
    for uid, name in data["users"].items():
        marker = "(וירטואלי)" if uid.startswith('-') else ""
        if not HE_IL:
            marker = "(virtual)" if uid.startswith('-') else ""
        lines.append(f"{name} {marker}".strip())
    header = T["users_header"] if HE_IL else "Participants:"
    await msg.reply_text(header + "\n" + "\n".join(lines), disable_notification=True)

async def stats_cmd(update, context):
    msg = update.message
    chat_id = msg.chat.id
    total = db_count_expenses(chat_id)
    if total == 0:
        await msg.reply_text(T["no_expenses"] if HE_IL else "No expenses yet.")
        return
    rows = category_totals(chat_id)
    currency = load_chat(chat_id).get("currency", DEFAULT_CURRENCY)
    grand = sum(v for _, v in rows) or 1.0
    lines = []
    for cat, amt in rows:
        pct = (amt / grand) * 100.0
        lines.append(f"{CATEGORY_EMOJI.get(cat,'')} {cat}: {amt:.2f} {currency} ({pct:.1f}%)")
    header = "📊 קטגוריות:" if HE_IL else "Category totals:" 
    await msg.reply_text(header + "\n" + "\n".join(lines), disable_notification=True)


async def add_expense(update, context):
    msg = update.message
    chat_id = msg.chat.id
    data = load_chat(chat_id)
    ensure_user(data, msg.from_user)
    tokens = msg.text.split()
    if len(tokens) < 2:
        await msg.reply_text(T["add_usage"] if HE_IL else "Usage: /add <amount> [ISO3] <description>")
        return
    # tokens[0] = /add
    amount_part = tokens[1]
    currency = data["currency"]
    desc_start_index = 2
    provided_currency = None
    if len(tokens) >= 3 and _is_currency(tokens[2].upper()):
        provided_currency = tokens[2].upper()
        desc_start_index = 3
    if len(tokens) <= desc_start_index:
        description = "(no description)"
    else:
        description = " ".join(tokens[desc_start_index:])
    try:
        amount = float(amount_part)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await msg.reply_text(T["amount_positive"] if HE_IL else "Amount must be positive.")
        return
    # participants = all known users for now (including payer)
    participants = list(map(int, data["users"].keys()))
    payer_id = msg.from_user.id
    if payer_id not in participants:
        participants.append(payer_id)
    detected_cur = provided_currency or detect_currency_token(description) or data["currency"]
    log_currency.debug("/add detected_cur=%s provided=%s desc=%s", detected_cur, provided_currency, description)
    original_amount = round(amount, 2)
    original_currency = detected_cur
    final_amount = original_amount
    rate_used = None
    fx_fallback_flag = False
    if detected_cur != data["currency"]:
        rate, fb = get_fx_rate(detected_cur, data["currency"])
        if rate:
            final_amount = round(final_amount * rate, 2)
            rate_used = rate
            fx_fallback_flag = fb
            log_currency.debug("/add converted original=%s %s final=%s %s rate=%s fallback=%s", original_amount, detected_cur, final_amount, data["currency"], rate, fb)
        else:
            fx_fallback_flag = True
            log_currency.debug("/add conversion failed from=%s to=%s", detected_cur, data["currency"])
    exp_id = insert_expense(
        chat_id=chat_id,
        payer_id=payer_id,
        amount=final_amount,
        description=description.strip(),
        category="other",
        ts=int(time.time()),
        participants=participants,
        original_amount=original_amount,
        original_currency=original_currency,
        fx_rate=rate_used,
    fx_fallback=fx_fallback_flag,
    )
    exp = {"id": exp_id, "amount": final_amount, "original_amount": original_amount, "original_currency": original_currency, "fx_rate": rate_used}
    if rate_used:
        await msg.reply_text(
            (f"{CATEGORY_EMOJI.get('other','')} " + T["expense_recorded_conv"].format(id=exp_id, amt=exp['amount'], cur=currency,
                                               oamt=original_amount, ocur=original_currency, rate=rate_used, n=len(participants)))
            if HE_IL else f"Recorded expense #{exp['id']} {exp['amount']:.2f} {currency} (from {original_amount:.2f} {original_currency} @ {rate_used:.4f}) split among {len(participants)} participants.",
            disable_notification=True
        )
    else:
        await msg.reply_text(
            (f"{CATEGORY_EMOJI.get('other','')} " + T["expense_recorded"].format(id=exp_id, amt=exp['amount'], cur=currency, n=len(participants)))
            if HE_IL else f"Recorded expense #{exp['id']} {exp['amount']:.2f} {currency} split among {len(participants)} participants.",
            disable_notification=True
        )


async def list_expenses(update, context):
    msg = update.message
    chat_id = msg.chat.id
    data = load_chat(chat_id)
    users = data["users"]
    currency = data.get("currency", DEFAULT_CURRENCY)
    total = db_count_expenses(chat_id)
    if total == 0:
        await msg.reply_text(T["no_expenses"] if HE_IL else "No expenses yet.")
        return
    # Optional page argument: /list <page>
    page = 0
    parts = msg.text.split()
    if len(parts) == 2 and parts[1].isdigit():
        req = int(parts[1]) - 1
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if 0 <= req < pages:
            page = req
    expenses = db_list_expenses(chat_id, PAGE_SIZE, page * PAGE_SIZE)
    text = build_expense_page_text(expenses, users, currency, page, total)
    keyboard = build_pagination_keyboard(page, total)
    await msg.reply_text(text, reply_markup=keyboard, disable_notification=True)

def build_expense_page_text(expenses: List[Dict[str, Any]], users: Dict[str,str], currency: str, page: int, total: int) -> str:
    lines = []
    for exp in expenses:
        payer_name = users.get(str(exp["payer"]), str(exp["payer"]))
        cat = exp.get("category", "other")
        if exp.get("original_currency") and exp.get("original_currency") != currency:
            if exp.get("fx_rate"):
                approx = "~" if exp.get("fx_fallback") else ""
                lines.append(
                    (f"{CATEGORY_EMOJI.get(cat,'')} #{exp['id']} {payer_name} שילם {exp['amount']:.2f}{approx} {currency} (מ-{(exp.get('original_amount') or 0):.2f} {exp.get('original_currency')} @ {exp.get('fx_rate'):.4f}) [{cat}] - {exp['description']}") if HE_IL else
                    (f"{CATEGORY_EMOJI.get(cat,'')} #{exp['id']} {payer_name} paid {exp['amount']:.2f}{approx} {currency} (from {(exp.get('original_amount') or 0):.2f} {exp.get('original_currency')} @ {exp.get('fx_rate'):.4f}) [{cat}] - {exp['description']}")
                )
            else:
                approx = "~" if exp.get("fx_fallback") else ""
                lines.append(
                    f"{CATEGORY_EMOJI.get(cat,'')} #{exp['id']} {payer_name} שילם {exp['amount']:.2f}{approx} {currency} (מ-{(exp.get('original_amount') or 0):.2f} {exp.get('original_currency')}) [{cat}] - {exp['description']}" if HE_IL else
                    f"{CATEGORY_EMOJI.get(cat,'')} #{exp['id']} {payer_name} paid {exp['amount']:.2f}{approx} {currency} (from {(exp.get('original_amount') or 0):.2f} {exp.get('original_currency')}) [{cat}] - {exp['description']}"
                )
        else:
            lines.append(
                f"{CATEGORY_EMOJI.get(cat,'')} #{exp['id']} {payer_name} שילם {exp['amount']:.2f} {currency} [{cat}] - {exp['description']}" if HE_IL else
                f"{CATEGORY_EMOJI.get(cat,'')} #{exp['id']} {payer_name} paid {exp['amount']:.2f} {currency} [{cat}] - {exp['description']}"
            )
    header = (T["expenses_header"].format(cur=currency) if HE_IL else f"Expenses [{currency}]:")
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    footer = f"\n(Page {page+1}/{pages} • {total} total)"
    return header + "\n" + "\n".join(lines) + footer


def build_pagination_keyboard(page: int, total: int) -> InlineKeyboardMarkup:
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"LIST:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"LIST:{page+1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons) if buttons else None


async def show_balances(update, context):
    msg = update.message
    chat_id = msg.chat.id
    data = load_chat(chat_id)
    total = db_count_expenses(chat_id)
    if total == 0:
        await msg.reply_text(T["no_expenses"] if HE_IL else "No expenses yet.", disable_notification=True)
        return
    balances_map = db_compute_balances(chat_id)
    users = data["users"]
    lines = []
    currency = data.get("currency", DEFAULT_CURRENCY)
    for uid, amt in sorted(balances_map.items(), key=lambda x: x[0]):
        if abs(amt) < EPS:
            continue
        lines.append(f"{users.get(uid, uid)}: {amt:+.2f} {currency}")
    if not lines:
        if len(data["users"]) <= 1:
            await msg.reply_text(T.get("balances_zero_one", T["balances_zero"]) if HE_IL else "All settled (only one participant).", disable_notification=True)
        else:
            await msg.reply_text(T["balances_zero"] if HE_IL else "All settled: balances are zero.", disable_notification=True)
    else:
        header = T["balances_header"].format(cur=currency) if HE_IL else f"Balances [{currency}]:"
        await msg.reply_text(header + "\n" + "\n".join(lines), disable_notification=True)


async def settle(update, context):
    msg = update.message
    chat_id = msg.chat.id
    data = load_chat(chat_id)
    total = db_count_expenses(chat_id)
    if total == 0:
        await msg.reply_text(T["no_expenses"] if HE_IL else "No expenses yet.", disable_notification=True)
        return
    balances_map = db_compute_balances(chat_id)
    settlements = db_list_settlements(balances_map)
    if not settlements:
        await msg.reply_text(T["settle_none"] if HE_IL else "Nothing to settle.", disable_notification=True)
        return
    # Merge DB users (authoritative) with legacy JSON names as fallback
    db_users = db_list_users(chat_id)  # Dict[int,str]
    json_users = {int(k): v for k, v in data["users"].items()}
    merged_users = {**json_users, **db_users}
    lines = []
    currency = data.get("currency", DEFAULT_CURRENCY)
    for s in settlements:
        payer = merged_users.get(s["from"], s["from"])
        payee = merged_users.get(s["to"], s["to"])
        if HE_IL:
            lines.append(f"{payer} -> {payee}: {s['amount']:.2f} {currency}")
        else:
            lines.append(f"{payer} -> {payee}: {s['amount']:.2f} {currency}")
    header = T["settle_header"].format(cur=currency) if HE_IL else f"Suggested payments [{currency}]:"
    await msg.reply_text(header + "\n" + "\n".join(lines), disable_notification=True)

async def export_cmd(update, context):
    msg = update.message
    chat_id = msg.chat.id
    total = db_count_expenses(chat_id)
    if total == 0:
        await msg.reply_text(T["no_expenses"] if HE_IL else "No expenses yet.", disable_notification=True)
        return
    rows = db_export_expenses(chat_id)
    # Build CSV in-memory
    import io, csv, datetime
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id","payer","amount","currency","description","category","timestamp_iso","original_amount","original_currency","fx_rate","fx_fallback","participants"])
    # Need user names for payer mapping
    data = load_chat(chat_id)
    users = data['users']
    currency = data.get('currency', DEFAULT_CURRENCY)
    for r in rows:
        ts_iso = datetime.datetime.utcfromtimestamp(r['ts']).isoformat()
        participants = ";".join(str(p) for p in r['participants'])
        writer.writerow([
            r['id'], r['payer'], f"{r['amount']:.2f}", currency, r['description'], r['category'], ts_iso,
            ("" if r['original_amount'] is None else f"{r['original_amount']:.2f}"), r['original_currency'] or "", ("" if r['fx_rate'] is None else f"{r['fx_rate']:.6f}"), int(r.get('fx_fallback', False)), participants
        ])
    output.seek(0)
    csv_bytes = output.getvalue().encode('utf-8')
    from telegram import InputFile
    filename = f"expenses_{chat_id}.csv"
    await msg.reply_document(document=InputFile(csv_bytes, filename=filename), filename=filename, disable_notification=True)


async def reset_chat(update, context):
    msg = update.message
    chat_id = msg.chat.id
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ כן", callback_data="RESET:CONFIRM"), InlineKeyboardButton("❌ לא", callback_data="RESET:CANCEL")]
    ])
    await msg.reply_text(T["reset_inline_warn"] if HE_IL else "Warning: This will erase all data. Continue?", reply_markup=keyboard, disable_notification=True)


def _valid_token(token: str) -> bool:
    # Telegram bot tokens are of the form <digits>:<alphanumeric>
    if not token:
        return False
    if token in {"REPLACE_WITH_YOUR_TOKEN", "CHANGE_ME", ""}:
        return False
    return ":" in token and token.split(":", 1)[0].isdigit()


def main():
    if not _valid_token(BOT_TOKEN):
        raise SystemExit(
            "Invalid or missing TELEGRAM_BOT_TOKEN. Set it via environment variable. Example (PowerShell): \n"
            + "$env:TELEGRAM_BOT_TOKEN = '123456789:ABCDEF...'; docker compose up --build"
        )
    # Initialize database
    init_db(DEFAULT_CURRENCY)
    # Log AI provider selection
    if AI_PROVIDER_ACTIVE:
        if AI_PROVIDER_ACTIVE == "OLLAMA":
            log_ai.info("AI provider: %s model=%s base=%s", AI_PROVIDER_ACTIVE, OLLAMA_MODEL, OLLAMA_BASE_URL)
        elif AI_PROVIDER_ACTIVE == "GEMINI":
            log_ai.info("AI provider: %s model=%s", AI_PROVIDER_ACTIVE, GEMINI_MODEL)
    else:
        log_ai.info("AI disabled (provider not configured)")
    logging.info("[SplitBot] Starting polling bot (log level=%s)...", LOG_LEVEL)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setcurrency", set_currency))
    app.add_handler(CommandHandler("currency", show_currency))
    app.add_handler(CommandHandler("categories", categories_cmd))
    app.add_handler(CommandHandler("add", add_expense))
    app.add_handler(CommandHandler("reset", reset_chat))
    app.add_handler(CommandHandler("adduser", adduser_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("ai", ai_status_cmd))
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):  # pragma: no cover
        logging.exception("Unhandled exception: %s", context.error)
    app.add_error_handler(error_handler)
    # Inline currency callback
    async def currency_callback(update: Update, context):
        query = update.callback_query
        if not query or not query.data or not query.data.startswith("CUR:"):
            return
        await query.answer()
        code = query.data.split(":", 1)[1]
        chat_id = query.message.chat.id
        data = load_chat(chat_id)
        if data["expenses"] and code != data["currency"]:
            if HE_IL:
                await query.edit_message_text(T["currency_locked"].format(cur=data['currency']))
            else:
                await query.edit_message_text(f"Cannot change currency after expenses exist (still {data['currency']}).")
            return
        old = data["currency"]
        data["currency"] = code
        save_chat(data)
        if HE_IL:
            await query.edit_message_text(T["currency_changed"].format(old=old, new=code))
        else:
            await query.edit_message_text(f"Default currency changed: {old} -> {code}.")

    async def ai_expense_callback(update: Update, context):
        query = update.callback_query
        if not query or not query.data or not query.data.startswith("AIEXP:"):
            return
        await query.answer()
        action = query.data.split(":", 1)[1]
        chat_id = query.message.chat.id
        pending = PENDING_EXPENSES.get(chat_id)
        if action == "ACCEPT":
            if not pending:
                await query.edit_message_text(T["pending_missing"] if HE_IL else "No pending expense.")
                return
            # Insert into DB
            # participants expected as list[int] for equal split
            participants = list(pending["participants"])
            exp_id = insert_expense(
                chat_id=chat_id,
                payer_id=pending["payer"],
                amount=pending["amount"],
                description=pending["description"],
                category=pending.get("category", "other"),
                original_amount=pending.get("original_amount"),
                original_currency=pending.get("original_currency"),
                fx_rate=pending.get("fx_rate"),
                fx_fallback=pending.get("fx_fallback", False),
                participants=participants,
                ts=int(pending.get("ts", time.time())),
            )
            PENDING_EXPENSES.pop(chat_id, None)
            currency = get_currency(chat_id, DEFAULT_CURRENCY) or DEFAULT_CURRENCY
            rate = pending.get("fx_rate")
            if pending.get("original_currency") and pending.get("original_currency") != currency and rate:
                text = T["auto_added_conv"].format(id=exp_id, amt=pending['amount'], cur=currency,
                                                    oamt=pending['original_amount'], ocur=pending['original_currency'], rate=rate,
                                                    cat=pending['category'], desc=pending['description'])
            else:
                text = T["auto_added"].format(id=exp_id, amt=pending['amount'], cur=currency,
                                               cat=pending['category'], desc=pending['description'])
            text = f"{CATEGORY_EMOJI.get(pending['category'],'')} " + text
            if HE_IL:
                text += "\n" + T["pending_saved"].format(id=exp_id)
            else:
                text += f"\nSaved expense #{exp_id}"
            await query.edit_message_text(text)
        elif action == "CANCEL":
            if pending:
                PENDING_EXPENSES.pop(chat_id, None)
            base = query.message.text.split("\n")
            if HE_IL:
                base.append(T["pending_canceled"])
            else:
                base.append("Canceled.")
            await query.edit_message_text("\n".join(base))
        else:
            await query.edit_message_text("Unknown action.")

    async def reset_callback(update: Update, context):
        query = update.callback_query
        if not query or not query.data or not query.data.startswith("RESET:"):
            return
        await query.answer()
        action = query.data.split(":", 1)[1]
        chat_id = query.message.chat.id
        if action == "CONFIRM":
            fp = DATA_DIR / f"{chat_id}.json"
            if fp.exists():
                try:
                    fp.unlink()
                except Exception as e:  # pragma: no cover
                    logging.warning("Failed to delete chat file %s: %s", fp, e)
            data = load_chat(chat_id)
            text = T["reset_inline_done"] if HE_IL else "Reset complete. Use /adduser to add participants."
            await query.edit_message_text(text)
        elif action == "CANCEL":
            await query.edit_message_text(T["reset_inline_canceled"] if HE_IL else "Reset canceled.")
        else:
            await query.edit_message_text("Unknown reset action")

    async def list_pagination_callback(update: Update, context):
        query = update.callback_query
        if not query or not query.data or not query.data.startswith("LIST:"):
            return
        await query.answer()
        try:
            page = int(query.data.split(':',1)[1])
        except ValueError:
            return
        chat_id = query.message.chat.id
        data = load_chat(chat_id)
        users = data['users']
        currency = data.get('currency', DEFAULT_CURRENCY)
        total = db_count_expenses(chat_id)
        expenses = db_list_expenses(chat_id, PAGE_SIZE, page*PAGE_SIZE)
        text = build_expense_page_text(expenses, users, currency, page, total)
        keyboard = build_pagination_keyboard(page, total)
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            log.warning("pagination edit failed: %s", e)

    # Specific patterns to avoid overlap so AIEXP callbacks aren't swallowed by first handler
    app.add_handler(CallbackQueryHandler(currency_callback, pattern=r"^CUR:"))
    app.add_handler(CallbackQueryHandler(ai_expense_callback, pattern=r"^AIEXP:"))
    app.add_handler(CallbackQueryHandler(reset_callback, pattern=r"^RESET:"))
    app.add_handler(CallbackQueryHandler(list_pagination_callback, pattern=r"^LIST:"))
    # Free text handler last
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text_handler))
    app.add_handler(CommandHandler("list", list_expenses))
    app.add_handler(CommandHandler("bal", show_balances))
    app.add_handler(CommandHandler("settle", settle))
    try:
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        logging.exception("[SplitBot] Fatal error running bot: %s", e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
