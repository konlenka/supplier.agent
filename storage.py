import json
import os
import sqlite3
from datetime import date, datetime, timezone

from config import DB_PATH, STOCK_TARGETS
from models import Approval, JobRun, OrderLine, StockLevel

# Where an order held for approval has got to — the status column of order_approvals.
APPROVAL_PENDING = "pending"        # the approver has been asked; nothing sent
APPROVAL_APPROVED = "approved"      # they said yes; the supplier send was attempted
APPROVAL_REJECTED = "rejected"      # they said no
APPROVAL_EXPIRED = "expired"        # no answer before the wait ended
APPROVAL_SUPERSEDED = "superseded"  # still unanswered when a later order was held
APPROVAL_CANCELLED = "cancelled"    # the request never reached the approver


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
            CREATE TABLE IF NOT EXISTS job_runs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date   TEXT NOT NULL,
                outcome    TEXT NOT NULL,
                detail     TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_approvals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                order_date   TEXT NOT NULL,
                order_lines  TEXT NOT NULL,
                counts       TEXT NOT NULL,
                status       TEXT NOT NULL,
                reply        TEXT NOT NULL DEFAULT '',
                requested_at TIMESTAMP NOT NULL,
                decided_at   TIMESTAMP
            );
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


def log_run(run_date: date, outcome: str, detail: str = "") -> None:
    """Record one run of the weekly order job: when, what it decided, what it did."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO job_runs (run_date, outcome, detail, created_at) VALUES (?, ?, ?, ?)",
            (run_date.isoformat(), outcome, detail, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_last_run(outcomes: tuple[str, ...]) -> JobRun | None:
    """Most recent run with one of the given outcomes."""
    placeholders = ",".join("?" for _ in outcomes)
    conn = _get_connection()
    try:
        row = conn.execute(
            f"SELECT run_date, outcome, created_at FROM job_runs WHERE outcome IN ({placeholders}) "
            "ORDER BY id DESC LIMIT 1",
            outcomes,
        ).fetchone()
        if row:
            return JobRun(
                run_date=date.fromisoformat(row["run_date"]),
                outcome=row["outcome"],
                at=datetime.fromisoformat(row["created_at"]),
            )
        return None
    finally:
        conn.close()


def save_approval_request(
    order_date: date, order_lines: list[OrderLine], counts: dict[str, StockLevel]
) -> Approval:
    """Hold an order for the approver's answer. Stores the order as worked out now, so a yes
    releases what they were shown and not whatever the counts say by then. Only one order
    can be waiting: a request still open from before is superseded."""
    now = datetime.now(timezone.utc)
    lines_json = [{"item": ol.item, "label": ol.label, "quantity": ol.quantity} for ol in order_lines]
    counts_json = {k: {"quantity": v.quantity, "unit": v.unit} for k, v in counts.items()}
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE order_approvals SET status = ?, decided_at = ? WHERE status = ?",
            (APPROVAL_SUPERSEDED, now.isoformat(), APPROVAL_PENDING),
        )
        cursor = conn.execute(
            "INSERT INTO order_approvals (order_date, order_lines, counts, status, requested_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (order_date.isoformat(), json.dumps(lines_json), json.dumps(counts_json), APPROVAL_PENDING, now.isoformat()),
        )
        conn.commit()
        return Approval(
            id=cursor.lastrowid, order_date=order_date, order_lines=order_lines, counts=counts, requested_at=now
        )
    finally:
        conn.close()


def get_pending_approval() -> Approval | None:
    """The order waiting for the approver's answer, if there is one."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT id, order_date, order_lines, counts, requested_at FROM order_approvals "
            "WHERE status = ? ORDER BY id DESC LIMIT 1",
            (APPROVAL_PENDING,),
        ).fetchone()
        if row is None:
            return None
        return Approval(
            id=row["id"],
            order_date=date.fromisoformat(row["order_date"]),
            order_lines=[OrderLine(**line) for line in json.loads(row["order_lines"])],
            counts={k: StockLevel(k, v["quantity"], v["unit"]) for k, v in json.loads(row["counts"]).items()},
            requested_at=datetime.fromisoformat(row["requested_at"]),
        )
    finally:
        conn.close()


def decide_approval(approval_id: int, status: str, reply: str = "") -> bool:
    """Close a waiting approval with its outcome and the approver's own words. Returns False
    if it was no longer waiting — the caller must then send nothing, because someone else
    (a second yes, the closing job) got there first."""
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "UPDATE order_approvals SET status = ?, reply = ?, decided_at = ? WHERE id = ? AND status = ?",
            (status, reply, datetime.now(timezone.utc).isoformat(), approval_id, APPROVAL_PENDING),
        )
        conn.commit()
        return cursor.rowcount == 1
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
