"""
Database layer for Ash-orders.
Uses SQLite with WAL mode for better concurrency.
All order + ledger + idempotency writes happen in a single transaction.
"""

import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = "orders.db"


class Database:
    """Thread-safe SQLite wrapper with connection-per-thread."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._local = threading.local()

    # ── Connection management ────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    # ── Schema initialization ────────────────────────────────────────

    def initialize(self):
        conn = self._get_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id        TEXT PRIMARY KEY,
                customer_id     TEXT NOT NULL,
                item_id         TEXT NOT NULL,
                quantity         INTEGER NOT NULL,
                status          TEXT NOT NULL DEFAULT 'created',
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ledger (
                ledger_id       TEXT PRIMARY KEY,
                order_id        TEXT NOT NULL,
                customer_id     TEXT NOT NULL,
                amount          REAL NOT NULL,
                type            TEXT NOT NULL DEFAULT 'charge',
                created_at      TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            );

            CREATE TABLE IF NOT EXISTS idempotency_records (
                idempotency_key     TEXT PRIMARY KEY,
                request_fingerprint TEXT NOT NULL,
                response_body       TEXT NOT NULL,
                response_status_code INTEGER NOT NULL,
                created_at          TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_orders_customer
                ON orders(customer_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_order
                ON ledger(order_id);
            """
        )
        conn.commit()

    # ── Atomic order creation ────────────────────────────────────────

    def create_order_atomic(
        self,
        order_id: str,
        customer_id: str,
        item_id: str,
        quantity: int,
        ledger_id: str,
        amount: float,
        idempotency_key: str,
        fingerprint: str,
        response_body: str,
        response_status_code: int,
    ):
        """
        Insert into orders, ledger, and idempotency_records in ONE
        transaction.  If anything fails the entire thing rolls back.
        """
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute("BEGIN IMMEDIATE")

            conn.execute(
                """
                INSERT INTO orders (order_id, customer_id, item_id, quantity, status, created_at)
                VALUES (?, ?, ?, ?, 'created', ?)
                """,
                (order_id, customer_id, item_id, quantity, now),
            )

            conn.execute(
                """
                INSERT INTO ledger (ledger_id, order_id, customer_id, amount, type, created_at)
                VALUES (?, ?, ?, ?, 'charge', ?)
                """,
                (ledger_id, order_id, customer_id, amount, now),
            )

            conn.execute(
                """
                INSERT INTO idempotency_records
                    (idempotency_key, request_fingerprint, response_body, response_status_code, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (idempotency_key, fingerprint, response_body, response_status_code, now),
            )

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    #Queries

    def get_idempotency_record(self, key: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM idempotency_records WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def get_order(self, order_id: str) -> dict | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_orders(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def list_ledger(self) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM ledger ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


    def reset(self):
        conn = self._get_conn()
        conn.executescript(
            """
            DELETE FROM ledger;
            DELETE FROM orders;
            DELETE FROM idempotency_records;
            """
        )
        conn.commit()
