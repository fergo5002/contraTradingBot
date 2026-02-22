"""
SQLite persistence layer.

Tables
──────
  posts          – every Reddit post seen, with filter outcome
  signals        – every trade signal extracted by the LLM
  trades         – every order submitted to Alpaca (open or closed)
  pending_orders – stock orders queued for next market open

All operations use thread-local connections so multiple threads can share
this module safely without connection contention.
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from logger import get_logger

logger = get_logger(__name__)

DB_PATH = Path("contra_bot.db")
_local = threading.local()


# ─── Connection management ────────────────────────────────────────────────────

def _get_connection() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return _local.conn


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = _get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ─── Schema initialisation ────────────────────────────────────────────────────

def init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS posts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                subreddit    TEXT    NOT NULL,
                post_id      TEXT    NOT NULL UNIQUE,
                title        TEXT,
                body         TEXT,
                author       TEXT,
                created_utc  REAL,
                upvotes      INTEGER DEFAULT 0,
                awards       INTEGER DEFAULT 0,
                processed_at TEXT,
                filter_passed INTEGER,
                filter_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS signals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id         TEXT NOT NULL,
                ticker          TEXT NOT NULL,
                asset_type      TEXT,
                raw_direction   TEXT,
                final_direction TEXT,
                confidence      REAL,
                reasoning       TEXT,
                created_at      TEXT,
                FOREIGN KEY (post_id) REFERENCES posts(post_id)
            );

            CREATE TABLE IF NOT EXISTS trades (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id        INTEGER,
                alpaca_order_id  TEXT,
                ticker           TEXT NOT NULL,
                direction        TEXT,
                asset_type       TEXT,
                qty              REAL,
                entry_price      REAL,
                current_price    REAL,
                status           TEXT DEFAULT 'open',
                opened_at        TEXT,
                closed_at        TEXT,
                pnl              REAL,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            );

            CREATE TABLE IF NOT EXISTS pending_orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id   INTEGER,
                ticker      TEXT NOT NULL,
                direction   TEXT,
                qty         REAL,
                asset_type  TEXT,
                created_at  TEXT,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            );

            CREATE INDEX IF NOT EXISTS idx_posts_post_id   ON posts(post_id);
            CREATE INDEX IF NOT EXISTS idx_signals_ticker  ON signals(ticker);
            CREATE INDEX IF NOT EXISTS idx_trades_ticker   ON trades(ticker);
            CREATE INDEX IF NOT EXISTS idx_trades_status   ON trades(status);
        """)
    logger.info("Database initialised at %s", DB_PATH)


# ─── Post operations ──────────────────────────────────────────────────────────

def is_post_processed(post_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,))
        return cur.fetchone() is not None


def save_post(
    subreddit: str,
    post_id: str,
    title: str,
    body: str,
    author: str,
    created_utc: float,
    upvotes: int,
    awards: int,
    filter_passed: bool,
    filter_reason: str,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO posts
                (subreddit, post_id, title, body, author, created_utc, upvotes, awards,
                 processed_at, filter_passed, filter_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subreddit, post_id, title, body, author, created_utc, upvotes, awards,
                datetime.utcnow().isoformat(), int(filter_passed), filter_reason,
            ),
        )


# ─── Signal operations ────────────────────────────────────────────────────────

def save_signal(
    post_id: str,
    ticker: str,
    asset_type: str,
    raw_direction: str,
    final_direction: str,
    confidence: float,
    reasoning: str,
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals
                (post_id, ticker, asset_type, raw_direction, final_direction,
                 confidence, reasoning, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id, ticker, asset_type, raw_direction, final_direction,
                confidence, reasoning, datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def has_recent_signal_for_ticker(ticker: str, hours: int = 24) -> bool:
    """True if we already generated a signal for this ticker in the last *hours* hours."""
    with get_db() as conn:
        cur = conn.execute(
            """
            SELECT 1 FROM signals
            WHERE ticker = ?
              AND created_at > datetime('now', ? || ' hours')
            """,
            (ticker, f"-{hours}"),
        )
        return cur.fetchone() is not None


# ─── Trade operations ─────────────────────────────────────────────────────────

def save_trade(
    signal_id: int,
    alpaca_order_id: str,
    ticker: str,
    direction: str,
    asset_type: str,
    qty: float,
    entry_price: float,
    status: str = "open",
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades
                (signal_id, alpaca_order_id, ticker, direction, asset_type,
                 qty, entry_price, status, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id, alpaca_order_id, ticker, direction, asset_type,
                qty, entry_price, status, datetime.utcnow().isoformat(),
            ),
        )
        return cur.lastrowid


def get_open_trades() -> List[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at DESC")
        return [dict(row) for row in cur.fetchall()]


def get_open_trade_for_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.execute(
            "SELECT * FROM trades WHERE ticker = ? AND status = 'open'", (ticker,)
        )
        row = cur.fetchone()
        return dict(row) if row else None


def update_trade_price(trade_id: int, current_price: float, pnl: float) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE trades SET current_price = ?, pnl = ? WHERE id = ?",
            (current_price, pnl, trade_id),
        )


def close_trade(trade_id: int, current_price: float, pnl: float) -> None:
    with get_db() as conn:
        conn.execute(
            """
            UPDATE trades
            SET status = 'closed', closed_at = ?, current_price = ?, pnl = ?
            WHERE id = ?
            """,
            (datetime.utcnow().isoformat(), current_price, pnl, trade_id),
        )


def get_total_pnl() -> float:
    with get_db() as conn:
        cur = conn.execute("SELECT COALESCE(SUM(pnl), 0.0) FROM trades WHERE status = 'closed'")
        return float(cur.fetchone()[0])


def count_open_positions() -> int:
    with get_db() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM trades WHERE status = 'open'")
        return int(cur.fetchone()[0])


# ─── Pending order operations ─────────────────────────────────────────────────

def save_pending_order(
    signal_id: int, ticker: str, direction: str, qty: float, asset_type: str
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO pending_orders (signal_id, ticker, direction, qty, asset_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (signal_id, ticker, direction, qty, asset_type, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def get_pending_orders() -> List[Dict[str, Any]]:
    with get_db() as conn:
        cur = conn.execute("SELECT * FROM pending_orders ORDER BY created_at ASC")
        return [dict(row) for row in cur.fetchall()]


def delete_pending_order(order_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM pending_orders WHERE id = ?", (order_id,))
