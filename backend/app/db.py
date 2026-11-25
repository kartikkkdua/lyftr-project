"""SQLite storage for sessions and their message history."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    system_prompt TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    metadata    TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    """Small wrapper around one shared sqlite3 connection.

    Every call is short, so a single connection behind a lock is simpler and
    safer than a pool. WAL mode keeps reads from blocking on writes.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT id, system_prompt FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()

    def get_history(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Messages of a session, oldest first. `limit` keeps only the newest N."""
        with self._lock:
            if limit:
                rows = self._conn.execute(
                    "SELECT * FROM (SELECT id, role, content, metadata, created_at FROM messages "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, role, content, metadata, created_at FROM messages "
                    "WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
        result = []
        for row in rows:
            item: dict[str, Any] = {
                "role": row["role"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            if row["metadata"]:
                item["metadata"] = json.loads(row["metadata"])
            result.append(item)
        return result

    def save_exchange(
        self,
        session_id: str,
        system_prompt: str | None,
        user_message: str,
        assistant_reply: str,
        metadata: dict[str, Any],
    ) -> None:
        """Atomically store one successful user/assistant turn (and create the session)."""
        ts = now_iso()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO sessions (id, system_prompt, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "system_prompt = COALESCE(excluded.system_prompt, sessions.system_prompt), "
                    "updated_at = excluded.updated_at",
                    (session_id, system_prompt, ts, ts),
                )
                self._conn.execute(
                    "INSERT INTO messages (session_id, role, content, metadata, created_at) "
                    "VALUES (?, 'user', ?, NULL, ?)",
                    (session_id, user_message, ts),
                )
                self._conn.execute(
                    "INSERT INTO messages (session_id, role, content, metadata, created_at) "
                    "VALUES (?, 'assistant', ?, ?, ?)",
                    (session_id, assistant_reply, json.dumps(metadata), now_iso()),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
