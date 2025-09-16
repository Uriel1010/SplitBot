import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import time

DB_PATH = Path('data') / 'splitbot.db'

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY,
        currency TEXT NOT NULL,
        virtual_seq INTEGER NOT NULL DEFAULT -1,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        is_virtual INTEGER NOT NULL DEFAULT 0,
        weight REAL NOT NULL DEFAULT 1.0,
        PRIMARY KEY (chat_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        payer_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        description TEXT NOT NULL,
        category TEXT NOT NULL,
        ts INTEGER NOT NULL,
        original_amount REAL,
        original_currency TEXT,
        fx_rate REAL,
        fx_fallback INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS expense_participants (
        expense_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        weight REAL NOT NULL DEFAULT 1.0,
        PRIMARY KEY (expense_id, user_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_expenses_chat_ts ON expenses(chat_id, ts DESC)
    """,
]


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db(default_currency: str = 'USD'):
    DB_PATH.parent.mkdir(exist_ok=True, parents=True)
    with get_conn() as conn:
        cur = conn.cursor()
        for stmt in SCHEMA:
            cur.execute(stmt)
        # Migrations: add weight columns if older schema
        try:
            cur.execute("PRAGMA table_info(users)")
            cols = [r[1] for r in cur.fetchall()]
            if 'weight' not in cols:
                cur.execute("ALTER TABLE users ADD COLUMN weight REAL NOT NULL DEFAULT 1.0")
        except Exception:
            pass
        try:
            cur.execute("PRAGMA table_info(expense_participants)")
            cols = [r[1] for r in cur.fetchall()]
            if 'weight' not in cols:
                cur.execute("ALTER TABLE expense_participants ADD COLUMN weight REAL NOT NULL DEFAULT 1.0")
        except Exception:
            pass
        conn.commit()


def ensure_chat(chat_id: int, default_currency: str) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO chats(chat_id, currency, virtual_seq, created_at) VALUES (?,?,?,?)",
                (chat_id, default_currency, -1, int(time.time()))
            )
            conn.commit()


def get_chat(chat_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT chat_id, currency, virtual_seq FROM chats WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {"chat_id": row[0], "currency": row[1], "virtual_seq": row[2]}


def set_chat_currency(chat_id: int, currency: str):
    with get_conn() as conn:
        conn.execute("UPDATE chats SET currency=? WHERE chat_id=?", (currency, chat_id))
        conn.commit()


def get_currency(chat_id: int, default_currency: str) -> str:
    chat = get_chat(chat_id)
    if chat:
        return chat['currency']
    ensure_chat(chat_id, default_currency)
    return default_currency


def ensure_user(chat_id: int, user_id: int, name: str) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM users WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO users(chat_id, user_id, name, is_virtual) VALUES (?,?,?,0)", (chat_id, user_id, name))
        else:
            # Optionally update name if changed
            if row[0] != name:
                cur.execute("UPDATE users SET name=? WHERE chat_id=? AND user_id=?", (name, chat_id, user_id))
        conn.commit()


def list_users(chat_id: int) -> Dict[int, str]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, name FROM users WHERE chat_id=? ORDER BY user_id", (chat_id,))
        return {row[0]: row[1] for row in cur.fetchall()}


def add_virtual_user(chat_id: int, name: str) -> Optional[int]:
    if not name.strip():
        return None
    with get_conn() as conn:
        cur = conn.cursor()
        # Check duplicate
        cur.execute("SELECT 1 FROM users WHERE chat_id=? AND lower(name)=lower(?)", (chat_id, name.strip()))
        if cur.fetchone() is not None:
            return None
        # Get current seq
        cur.execute("SELECT virtual_seq FROM chats WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        if not row:
            return None
        seq = row[0]
        new_id = seq
        next_seq = seq - 1
        cur.execute("UPDATE chats SET virtual_seq=? WHERE chat_id=?", (next_seq, chat_id))
        cur.execute("INSERT INTO users(chat_id, user_id, name, is_virtual) VALUES (?,?,?,1)", (chat_id, new_id, name.strip()))
        conn.commit()
        return new_id


def insert_expense(chat_id: int, payer_id: int, amount: float, description: str, category: str, ts: int,
                   participants: List[int], original_amount: Optional[float], original_currency: Optional[str],
                   fx_rate: Optional[float], fx_fallback: bool) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO expenses(chat_id, payer_id, amount, description, category, ts, original_amount, original_currency, fx_rate, fx_fallback) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (chat_id, payer_id, amount, description, category, ts, original_amount, original_currency, fx_rate, 1 if fx_fallback else 0)
        )
        exp_id = cur.lastrowid
        # Fetch current user weights
        if participants:
            qmarks = ",".join(["?"] * len(participants))
            cur.execute(f"SELECT user_id, weight FROM users WHERE chat_id=? AND user_id IN ({qmarks})", (chat_id, *participants))
            weights_map = {row[0]: row[1] for row in cur.fetchall()}
        else:
            weights_map = {}
        cur.executemany(
            "INSERT OR IGNORE INTO expense_participants(expense_id, user_id, weight) VALUES (?,?,?)",
            [(exp_id, uid, float(weights_map.get(uid, 1.0))) for uid in participants]
        )
        conn.commit()
        return exp_id


def get_next_expense_id(chat_id: int) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(id),0)+1 FROM expenses WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row else 1


def list_expenses(chat_id: int, limit: int, offset: int) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, payer_id, amount, description, category, ts, original_amount, original_currency, fx_rate, fx_fallback "
            "FROM expenses WHERE chat_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (chat_id, limit, offset)
        )
        rows = cur.fetchall()
        result = []
        for r in rows:
            result.append({
                'id': r[0], 'payer': r[1], 'amount': r[2], 'description': r[3], 'category': r[4], 'ts': r[5],
                'original_amount': r[6], 'original_currency': r[7], 'fx_rate': r[8], 'fx_fallback': bool(r[9])
            })
        return result


def count_expenses(chat_id: int) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) FROM expenses WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row else 0


def compute_balances(chat_id: int) -> Dict[int, float]:
    # Weighted approach: each participant has a weight snapshot per expense.
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, payer_id, amount FROM expenses WHERE chat_id=?", (chat_id,))
        expenses = cur.fetchall()
        if not expenses:
            return {}
        cur2 = conn.cursor()
        balances: Dict[int, float] = {}
        for exp_id, payer_id, amount in expenses:
            cur2.execute("SELECT user_id, weight FROM expense_participants WHERE expense_id=?", (exp_id,))
            rows = cur2.fetchall()
            if not rows:
                continue
            total_weight = sum(r[1] or 1.0 for r in rows)
            if total_weight <= 0:
                continue
            # Payer initially pays full amount, owes their proportional share
            balances[payer_id] = balances.get(payer_id, 0.0) + amount
            for uid, w in rows:
                share = amount * (float(w or 1.0) / total_weight)
                balances[uid] = balances.get(uid, 0.0) - share
        for k in list(balances.keys()):
            balances[k] = round(balances[k], 2)
        return balances


def list_settlements(balances: Dict[int, float]) -> List[Dict[str, Any]]:
    creditors = []
    debtors = []
    for uid, amt in balances.items():
        if amt > 0.01:
            creditors.append([uid, amt])
        elif amt < -0.01:
            debtors.append([uid, amt])
    creditors.sort(key=lambda x: x[1], reverse=True)
    debtors.sort(key=lambda x: x[1])
    settlements = []
    ci = 0; di = 0
    while ci < len(creditors) and di < len(debtors):
        c_uid, c_amt = creditors[ci]
        d_uid, d_amt = debtors[di]
        pay = min(c_amt, -d_amt)
        settlements.append({'from': d_uid, 'to': c_uid, 'amount': round(pay,2)})
        c_amt -= pay; d_amt += pay
        creditors[ci][1] = c_amt; debtors[di][1] = d_amt
        if c_amt <= 0.01: ci += 1
        if d_amt >= -0.01: di += 1
    return settlements

def category_totals(chat_id: int) -> List[Tuple[str, float]]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT category, SUM(amount) FROM expenses WHERE chat_id=? GROUP BY category ORDER BY SUM(amount) DESC", (chat_id,))
        return [(row[0], row[1]) for row in cur.fetchall()]


def export_expenses(chat_id: int) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT e.id, e.payer_id, e.amount, e.description, e.category, e.ts, e.original_amount, e.original_currency, e.fx_rate, e.fx_fallback "
            "FROM expenses e WHERE e.chat_id=? ORDER BY e.id",
            (chat_id,)
        )
        rows = cur.fetchall()
        result = []
        pcur = conn.cursor()
        for r in rows:
            pcur.execute("SELECT user_id FROM expense_participants WHERE expense_id=? ORDER BY user_id", (r[0],))
            participants = [pr[0] for pr in pcur.fetchall()]
            result.append({
                'id': r[0], 'payer': r[1], 'amount': r[2], 'description': r[3], 'category': r[4], 'ts': r[5],
                'original_amount': r[6], 'original_currency': r[7], 'fx_rate': r[8], 'fx_fallback': bool(r[9]),
                'participants': participants,
            })
        return result
