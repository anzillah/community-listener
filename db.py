"""
SQLite-backed deduplication and Reddit thread-to-conversation mapping.

Tables:
  processed_items   — deduplication for all sources
  thread_map        — reddit thread ID → Help Scout conversation ID
"""
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

import config

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(config.DB_PATH, check_same_thread=False)


def init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_items (
                id        TEXT NOT NULL,
                source    TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                PRIMARY KEY (id, source)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thread_map (
                thread_id  TEXT PRIMARY KEY,
                conv_id    TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS poll_state (
                source     TEXT PRIMARY KEY,
                last_poll  TEXT NOT NULL
            )
        """)
        conn.commit()


def is_processed(item_id: str, source: str) -> bool:
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_items WHERE id = ? AND source = ?",
                (item_id, source),
            ).fetchone()
            return row is not None


def mark_processed(item_id: str, source: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with _lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_items (id, source, timestamp) VALUES (?, ?, ?)",
                (item_id, source, ts),
            )
            conn.commit()


# ── Poll state (last-seen timestamps) ────────────────────────────────────────

def get_last_poll(source: str) -> Optional[str]:
    """Return the ISO-8601 timestamp of the last successful poll for *source*, or None."""
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT last_poll FROM poll_state WHERE source = ?", (source,)
            ).fetchone()
            return row[0] if row else None


def set_last_poll(source: str, timestamp: str) -> None:
    """Record that *source* was successfully polled at *timestamp* (ISO-8601)."""
    with _lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO poll_state (source, last_poll) VALUES (?, ?)",
                (source, timestamp),
            )
            conn.commit()


# ── Reddit thread → Help Scout conversation mapping ───────────────────────────

def get_thread_conv_id(thread_id: str) -> Optional[str]:
    """Return the Help Scout conversation ID for a Reddit thread, or None."""
    with _lock:
        with _connect() as conn:
            row = conn.execute(
                "SELECT conv_id FROM thread_map WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            return row[0] if row else None


def set_thread_conv_id(thread_id: str, conv_id: str) -> None:
    """Persist a Reddit thread ID → Help Scout conversation ID mapping."""
    ts = datetime.now(timezone.utc).isoformat()
    with _lock:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO thread_map (thread_id, conv_id, created_at) "
                "VALUES (?, ?, ?)",
                (thread_id, conv_id, ts),
            )
            conn.commit()


def clear_thread_conv_id(thread_id: str) -> None:
    """Remove a stale Reddit thread → Help Scout conversation mapping.

    Called when a 404 response tells us the conversation was deleted in
    Help Scout. Removing the entry lets the next poll cycle re-create it.
    """
    with _lock:
        with _connect() as conn:
            conn.execute("DELETE FROM thread_map WHERE thread_id = ?", (thread_id,))
            conn.commit()
