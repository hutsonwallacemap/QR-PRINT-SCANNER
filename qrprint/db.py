"""SQLite storage for orders, so jobs, tokens and reports survive restarts."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    access_key      TEXT NOT NULL,
    token           INTEGER NOT NULL,
    day             TEXT NOT NULL,
    customer_name   TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    pay_method      TEXT NOT NULL,          -- razorpay | upi | counter | free
    paid            INTEGER NOT NULL DEFAULT 0,
    amount_paise    INTEGER NOT NULL,
    rzp_order_id    TEXT,
    rzp_payment_id  TEXT,
    payment_ref     TEXT NOT NULL DEFAULT '',
    customer_claimed_paid INTEGER NOT NULL DEFAULT 0,
    printer_name    TEXT NOT NULL DEFAULT '',
    message         TEXT NOT NULL DEFAULT '',
    files_deleted   INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    paid_at         REAL,
    completed_at    REAL,
    last_checked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_day ON orders(day);

CREATE TABLE IF NOT EXISTS items (
    id              TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES orders(id),
    position        INTEGER NOT NULL,
    filename        TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    print_path      TEXT NOT NULL,
    total_pages     INTEGER NOT NULL,
    page_range      TEXT NOT NULL,
    selected_pages  INTEGER NOT NULL,
    color_mode      TEXT NOT NULL,
    sides           TEXT NOT NULL,
    copies          INTEGER NOT NULL,
    orientation     TEXT NOT NULL,
    paper_size      TEXT NOT NULL,
    sheets          INTEGER NOT NULL,
    amount_paise    INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    pages_printed   INTEGER NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_items_order ON items(order_id);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

ORDER_STATUSES = {
    "awaiting_payment",   # waiting for customer payment / shopkeeper confirmation
    "waiting_approval",   # paid (or print-first) but auto-print is off
    "queued",
    "printing",
    "printed",
    "failed",
    "cancelled",
    "expired",
}

FIRST_TOKEN = 101


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self.revision = 0  # bumps on every change; admin page polls cheaply

    # ── low level ──
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    # Reads fetch while holding the lock: the connection is shared by the web
    # threads and the print worker, and an unlocked fetch can be interleaved.
    def _one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def _all(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _bump(self) -> None:
        self.revision += 1

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── tokens ──
    def next_token(self) -> int:
        """Daily token numbers: #101, #102 ... reset every morning."""
        key = f"token:{today()}"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT value FROM counters WHERE name=?", (key,)).fetchone()
                value = (row["value"] + 1) if row else FIRST_TOKEN
                self._conn.execute(
                    "INSERT INTO counters(name, value) VALUES(?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return value

    # ── orders ──
    def insert_order(self, order: Dict[str, Any], items: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cols = ",".join(order)
                self._conn.execute(
                    f"INSERT INTO orders({cols}) VALUES({','.join('?' * len(order))})",
                    tuple(order.values()),
                )
                for it in items:
                    icols = ",".join(it)
                    self._conn.execute(
                        f"INSERT INTO items({icols}) VALUES({','.join('?' * len(it))})",
                        tuple(it.values()),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._bump()

    def update_order(self, order_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._exec(f"UPDATE orders SET {sets} WHERE id=?", [*fields.values(), order_id])
            self._bump()

    def transition(self, order_id: str, from_statuses: Iterable[str], **fields: Any) -> bool:
        """Atomically update an order only if it is currently in one of from_statuses.

        Returns True if the update happened. Prevents double-printing when two
        events (e.g. payment webhook + customer callback) race each other.
        """
        froms = list(from_statuses)
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            cur = self._exec(
                f"UPDATE orders SET {sets} WHERE id=? AND status IN ({','.join('?' * len(froms))})",
                [*fields.values(), order_id, *froms],
            )
            changed = cur.rowcount > 0
            if changed:
                self._bump()
            return changed

    def update_item(self, item_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._exec(f"UPDATE items SET {sets} WHERE id=?", [*fields.values(), item_id])
            self._bump()

    def reset_items(self, order_id: str) -> None:
        with self._lock:
            self._exec(
                "UPDATE items SET status='pending', pages_printed=0, error='' WHERE order_id=?",
                [order_id],
            )
            self._bump()

    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM items WHERE id=?", [item_id])
        return dict(row) if row else None

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM orders WHERE id=?", [order_id])
        if not row:
            return None
        order = dict(row)
        order["items"] = self.get_items(order_id)
        return order

    def get_items(self, order_id: str) -> List[Dict[str, Any]]:
        rows = self._all("SELECT * FROM items WHERE order_id=? ORDER BY position", [order_id])
        return [dict(r) for r in rows]

    def list_orders(self, limit: int = 150, day: Optional[str] = None) -> List[Dict[str, Any]]:
        if day:
            rows = self._all(
                "SELECT * FROM orders WHERE day=? ORDER BY created_at DESC LIMIT ?", [day, limit]
            )
        else:
            rows = self._all("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", [limit])
        orders = [dict(r) for r in rows]
        if orders:
            ids = [o["id"] for o in orders]
            items = self._all(
                f"SELECT * FROM items WHERE order_id IN ({','.join('?' * len(ids))}) ORDER BY position",
                ids,
            )
            by_order: Dict[str, List[Dict[str, Any]]] = {}
            for it in items:
                by_order.setdefault(it["order_id"], []).append(dict(it))
            for o in orders:
                o["items"] = by_order.get(o["id"], [])
        return orders

    def orders_with_status(self, statuses: Iterable[str]) -> List[Dict[str, Any]]:
        sts = list(statuses)
        rows = self._all(
            f"SELECT * FROM orders WHERE status IN ({','.join('?' * len(sts))}) ORDER BY COALESCE(paid_at, created_at)",
            sts,
        )
        return [dict(r) for r in rows]

    def next_queued(self) -> Optional[Dict[str, Any]]:
        row = self._one(
            "SELECT id FROM orders WHERE status='queued' ORDER BY COALESCE(paid_at, created_at) LIMIT 1"
        )
        return self.get_order(row["id"]) if row else None

    def orders_needing_file_cleanup(self, older_than: float) -> List[Dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM orders WHERE files_deleted=0 AND status IN ('printed','cancelled','expired','failed') "
            "AND COALESCE(completed_at, created_at) < ?",
            [older_than],
        )
        return [dict(r) for r in rows]

    def day_summary(self, day: str) -> Dict[str, Any]:
        row = self._one(
            """
            SELECT
              COUNT(*) AS orders,
              SUM(CASE WHEN status='printed' THEN 1 ELSE 0 END) AS printed,
              SUM(CASE WHEN status IN ('awaiting_payment','waiting_approval','queued','printing') THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
              SUM(CASE WHEN paid=1 THEN amount_paise ELSE 0 END) AS collected_paise,
              SUM(CASE WHEN paid=1 AND pay_method='razorpay' THEN amount_paise ELSE 0 END) AS online_paise,
              SUM(CASE WHEN status='printed' AND paid=0 THEN amount_paise ELSE 0 END) AS unpaid_printed_paise
            FROM orders WHERE day=?
            """,
            [day],
        )
        pages = self._one(
            """
            SELECT
              SUM(CASE WHEN i.color_mode='bw' THEN i.selected_pages*i.copies ELSE 0 END) AS bw_pages,
              SUM(CASE WHEN i.color_mode='color' THEN i.selected_pages*i.copies ELSE 0 END) AS color_pages,
              SUM(i.sheets) AS sheets
            FROM items i JOIN orders o ON o.id=i.order_id
            WHERE o.day=? AND o.status='printed'
            """,
            [day],
        )
        out = {k: (row[k] or 0) for k in row.keys()}
        out.update({k: (pages[k] or 0) for k in pages.keys()})
        return out

    @staticmethod
    def now() -> float:
        return time.time()
