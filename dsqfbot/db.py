from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .utils import now_iso


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT NOT NULL UNIQUE,
                    phone TEXT NOT NULL,
                    session_file TEXT NOT NULL UNIQUE,
                    is_premium INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_state (
                    user_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    peer_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    username TEXT,
                    link TEXT,
                    join_status TEXT NOT NULL DEFAULT 'joined',
                    speak_status TEXT NOT NULL DEFAULT 'unknown',
                    last_error TEXT,
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_id, peer_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );

                CREATE TABLE IF NOT EXISTS join_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER,
                    session_id INTEGER NOT NULL,
                    link TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'balanced',
                    status TEXT NOT NULL DEFAULT 'pending',
                    scheduled_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    group_id INTEGER,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(batch_id) REFERENCES join_batches(id),
                    FOREIGN KEY(session_id) REFERENCES sessions(id),
                    FOREIGN KEY(group_id) REFERENCES groups(id)
                );

                CREATE TABLE IF NOT EXISTS join_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL DEFAULT 'balanced',
                    interval_seconds INTEGER NOT NULL,
                    total_jobs INTEGER NOT NULL DEFAULT 0,
                    notify_chat_id INTEGER,
                    notify_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    group_id INTEGER NOT NULL,
                    message_text TEXT NOT NULL,
                    schedule_at TEXT NOT NULL,
                    repeat_mode TEXT NOT NULL DEFAULT 'once',
                    next_run_at TEXT,
                    last_scheduled_for TEXT,
                    last_telegram_message_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'scheduled',
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id),
                    FOREIGN KEY(group_id) REFERENCES groups(id)
                );
                """
            )
            self._ensure_column(conn, "join_jobs", "batch_id", "INTEGER")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_sql: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_sql}")

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def set_user_state(self, user_id: int, state: str, payload: dict[str, Any]) -> None:
        now = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO user_state (user_id, state, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    state=excluded.state,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (user_id, state, json.dumps(payload, ensure_ascii=False), now),
            )

    def get_user_state(self, user_id: int) -> tuple[str | None, dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT state, payload FROM user_state WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return None, {}
        return row["state"], json.loads(row["payload"] or "{}")

    def clear_user_state(self, user_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM user_state WHERE user_id = ?", (user_id,))

    def create_session(self, label: str, phone: str, session_file: str, is_premium: bool, status: str = "online") -> int:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (label, phone, session_file, is_premium, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (label, phone, session_file, int(is_premium), status, now, now),
            )
            return int(cur.lastrowid)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM sessions ORDER BY id ASC").fetchall()
        return [dict(row) for row in rows]

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row_to_dict(row)

    def update_session(self, session_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        keys = ", ".join(f"{key}=?" for key in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE sessions SET {keys} WHERE id = ?", (*fields.values(), session_id))

    def delete_session(self, session_id: int) -> None:
        with self.connect() as conn:
            group_ids = [int(row["id"]) for row in conn.execute("SELECT id FROM groups WHERE session_id = ?", (session_id,)).fetchall()]
            if group_ids:
                placeholders = ", ".join("?" for _ in group_ids)
                conn.execute(f"DELETE FROM tasks WHERE group_id IN ({placeholders})", group_ids)
                conn.execute(f"DELETE FROM join_jobs WHERE group_id IN ({placeholders})", group_ids)
            conn.execute("DELETE FROM tasks WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM join_jobs WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM groups WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def upsert_group(
        self,
        session_id: int,
        peer_id: int,
        title: str,
        username: str | None,
        link: str | None,
        join_status: str = "joined",
        speak_status: str = "unknown",
        last_error: str | None = None,
    ) -> int:
        now = now_iso()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM groups WHERE session_id = ? AND peer_id = ?",
                (session_id, peer_id),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE groups
                    SET title=?, username=?, link=?, join_status=?, speak_status=?, last_error=?, last_checked_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (title, username, link, join_status, speak_status, last_error, now, now, row["id"]),
                )
                return int(row["id"])
            cur = conn.execute(
                """
                INSERT INTO groups (
                    session_id, peer_id, title, username, link, join_status, speak_status, last_error, last_checked_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, peer_id, title, username, link, join_status, speak_status, last_error, now, now, now),
            )
            return int(cur.lastrowid)

    def list_groups(self, session_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM groups WHERE session_id = ? ORDER BY updated_at DESC, id DESC",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_group(self, group_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        return self._row_to_dict(row)

    def update_group(self, group_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        keys = ", ".join(f"{key}=?" for key in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE groups SET {keys} WHERE id = ?", (*fields.values(), group_id))

    def create_join_batch(
        self,
        mode: str,
        interval_seconds: int,
        total_jobs: int,
        notify_chat_id: int | None = None,
        notify_message_id: int | None = None,
    ) -> int:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO join_batches (
                    mode, interval_seconds, total_jobs, notify_chat_id, notify_message_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (mode, interval_seconds, total_jobs, notify_chat_id, notify_message_id, now, now),
            )
            return int(cur.lastrowid)

    def update_join_batch(self, batch_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        keys = ", ".join(f"{key}=?" for key in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE join_batches SET {keys} WHERE id = ?", (*fields.values(), batch_id))

    def get_join_batch(self, batch_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM join_batches WHERE id = ?", (batch_id,)).fetchone()
        return self._row_to_dict(row)

    def join_batch_stats(self, batch_id: int) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status = 'retry' THEN 1 ELSE 0 END) AS retry,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN status = 'joined' THEN 1 ELSE 0 END) AS joined,
                    SUM(CASE WHEN status = 'awaiting_approval' THEN 1 ELSE 0 END) AS awaiting_approval,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM join_jobs
                WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
        if not row:
            return {
                "total": 0,
                "pending": 0,
                "retry": 0,
                "running": 0,
                "joined": 0,
                "awaiting_approval": 0,
                "failed": 0,
            }
        return {key: int(row[key] or 0) for key in row.keys()}

    def join_job_stats(self) -> dict[str, int]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status = 'retry' THEN 1 ELSE 0 END) AS retry,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN status = 'joined' THEN 1 ELSE 0 END) AS joined,
                    SUM(CASE WHEN status = 'awaiting_approval' THEN 1 ELSE 0 END) AS awaiting_approval,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM join_jobs
                """
            ).fetchone()
        if not row:
            return {
                "total": 0,
                "pending": 0,
                "retry": 0,
                "running": 0,
                "joined": 0,
                "awaiting_approval": 0,
                "failed": 0,
            }
        return {key: int(row[key] or 0) for key in row.keys()}

    def create_join_job(self, session_id: int, link: str, mode: str, scheduled_at: str, batch_id: int | None = None) -> int:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO join_jobs (batch_id, session_id, link, mode, scheduled_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (batch_id, session_id, link, mode, scheduled_at, now, now),
            )
            return int(cur.lastrowid)

    def list_join_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM join_jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_due_join_job(self) -> dict[str, Any] | None:
        now = now_iso()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM join_jobs
                WHERE status IN ('pending', 'retry') AND scheduled_at <= ?
                ORDER BY scheduled_at ASC, id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE join_jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id = ?",
                (now, row["id"]),
            )
            row = conn.execute("SELECT * FROM join_jobs WHERE id = ?", (row["id"],)).fetchone()
        return self._row_to_dict(row)

    def finish_join_job(self, job_id: int, status: str, group_id: int | None = None, last_error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE join_jobs SET status=?, group_id=?, last_error=?, updated_at=? WHERE id=?",
                (status, group_id, last_error, now_iso(), job_id),
            )

    def retry_join_job(self, job_id: int, scheduled_at: str, last_error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE join_jobs SET status='retry', scheduled_at=?, last_error=?, updated_at=? WHERE id=?",
                (scheduled_at, last_error, now_iso(), job_id),
            )

    def create_task(
        self,
        session_id: int,
        group_id: int,
        message_text: str,
        schedule_at: str,
        repeat_mode: str,
        next_run_at: str | None,
        last_scheduled_for: str | None,
        last_telegram_message_id: int | None,
        status: str,
        last_error: str | None = None,
    ) -> int:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tasks (
                    session_id, group_id, message_text, schedule_at, repeat_mode,
                    next_run_at, last_scheduled_for, last_telegram_message_id,
                    status, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    group_id,
                    message_text,
                    schedule_at,
                    repeat_mode,
                    next_run_at,
                    last_scheduled_for,
                    last_telegram_message_id,
                    status,
                    last_error,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def count_tasks(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM tasks").fetchone()
        return int(row["total"] if row else 0)

    def list_tasks(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT tasks.*, groups.title AS group_title, sessions.label AS session_label
                FROM tasks
                JOIN groups ON groups.id = tasks.group_id
                JOIN sessions ON sessions.id = tasks.session_id
                ORDER BY tasks.id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT tasks.*, groups.title AS group_title, sessions.label AS session_label
                FROM tasks
                JOIN groups ON groups.id = tasks.group_id
                JOIN sessions ON sessions.id = tasks.session_id
                WHERE tasks.id = ?
                """,
                (task_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def update_task(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        keys = ", ".join(f"{key}=?" for key in fields)
        with self.connect() as conn:
            conn.execute(f"UPDATE tasks SET {keys} WHERE id = ?", (*fields.values(), task_id))

    def delete_completed_once_tasks(self, cutoff_iso: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM tasks
                WHERE repeat_mode='once' AND status='scheduled' AND schedule_at <= ?
                """,
                (cutoff_iso,),
            )
            return int(cur.rowcount or 0)

    def list_due_repeat_tasks(self, cutoff_iso: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE repeat_mode='daily' AND status='scheduled' AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at ASC, id ASC
                """,
                (cutoff_iso,),
            ).fetchall()
        return [dict(row) for row in rows]
