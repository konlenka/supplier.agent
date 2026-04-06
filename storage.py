import json
import os
import sqlite3
from datetime import date, datetime, timezone

from config import DB_PATH, STOCK_TARGETS
from models import OrderLine, StockLevel


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_connection()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stock_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reported_by TEXT NOT NULL,
                raw_message TEXT NOT NULL,
                parsed_data TEXT NOT NULL,
                reported_at TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS current_stock (
                item TEXT PRIMARY KEY,
                quantity REAL NOT NULL,
                unit TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                order_date TEXT NOT NULL,
                item       TEXT NOT NULL,
                quantity   INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_order_history_date ON order_history(order_date);
        """)
        conn.commit()
    finally:
        conn.close()


def save_stock_report(phone: str, raw_message: str, parsed: dict[str, StockLevel]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    parsed_json = {
        k: {"quantity": v.quantity, "unit": v.unit} for k, v in parsed.items()
    }

    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO stock_reports (reported_by, raw_message, parsed_data, reported_at) VALUES (?, ?, ?, ?)",
            (phone, raw_message, json.dumps(parsed_json), now),
        )
        for item, level in parsed.items():
            conn.execute(
                """INSERT INTO current_stock (item, quantity, unit, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(item) DO UPDATE SET quantity=?, unit=?, updated_at=?""",
                (item, level.quantity, level.unit, now, level.quantity, level.unit, now),
            )
        conn.commit()
    finally:
        conn.close()


def get_current_stock() -> dict[str, StockLevel]:
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT item, quantity, unit, updated_at FROM current_stock").fetchall()
        result = {}
        for row in rows:
            result[row["item"]] = StockLevel(
                item=row["item"],
                quantity=row["quantity"],
                unit=row["unit"],
                reported_at=datetime.fromisoformat(row["updated_at"]),
            )
        return result
    finally:
        conn.close()


def get_latest_report_time() -> datetime | None:
    conn = _get_connection()
    try:
        row = conn.execute("SELECT MAX(reported_at) as latest FROM stock_reports").fetchone()
        if row and row["latest"]:
            return datetime.fromisoformat(row["latest"])
        return None
    finally:
        conn.close()


def save_order(order_date: date, order_lines: list[OrderLine]) -> None:
    """Persist a completed order to history. Idempotent — replaces any existing rows for that date."""
    date_str = order_date.isoformat()
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM order_history WHERE order_date = ?", (date_str,))
        for ol in order_lines:
            if ol.quantity > 0:
                conn.execute(
                    "INSERT INTO order_history (order_date, item, quantity, created_at) VALUES (?, ?, ?, ?)",
                    (date_str, ol.item, ol.quantity, now),
                )
        conn.commit()
    finally:
        conn.close()


def get_order_history(weeks: int = 8) -> list[dict]:
    """Return the last `weeks` order dates with per-item quantities (all 5 items, 0 if not ordered)."""
    weeks = min(weeks, 52)
    conn = _get_connection()
    try:
        dates = [
            row["order_date"]
            for row in conn.execute(
                "SELECT DISTINCT order_date FROM order_history ORDER BY order_date DESC LIMIT ?",
                (weeks,),
            ).fetchall()
        ]
        result = []
        for order_date in dates:
            rows = conn.execute(
                "SELECT item, quantity FROM order_history WHERE order_date = ?",
                (order_date,),
            ).fetchall()
            items = {key: 0 for key in STOCK_TARGETS}
            for row in rows:
                if row["item"] in items:
                    items[row["item"]] = row["quantity"]
            result.append({"order_date": order_date, "items": items})
        return result
    finally:
        conn.close()
