from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TaskInsertResult:
    state: str
    task: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


@dataclass(slots=True)
class CallbackAttempt:
    callback_id: int
    issuer: str
    task_id: str
    url: str
    timeout_ms: int
    attempt: int


class SQLiteTaskStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cursor = self.connection.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
              issuer TEXT NOT NULL,
              task_id TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              expected_output_schema_json TEXT NOT NULL,
              status TEXT NOT NULL,
              accepted_at TEXT NOT NULL,
              completed_at TEXT,
              callback_url TEXT,
              callback_timeout_ms INTEGER,
              callback_state TEXT,
              PRIMARY KEY (issuer, task_id)
            );

            CREATE TABLE IF NOT EXISTS results (
              issuer TEXT NOT NULL,
              task_id TEXT NOT NULL,
              result_json TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (issuer, task_id)
            );

            CREATE TABLE IF NOT EXISTS nonces (
              issuer TEXT NOT NULL,
              nonce TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY (issuer, nonce)
            );

            CREATE TABLE IF NOT EXISTS callback_attempts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              issuer TEXT NOT NULL,
              task_id TEXT NOT NULL,
              callback_url TEXT NOT NULL,
              callback_timeout_ms INTEGER NOT NULL,
              attempt INTEGER NOT NULL,
              scheduled_at TEXT NOT NULL,
              delivered_at TEXT,
              last_error TEXT
            );
            """
        )
        self.connection.commit()

    def reserve_nonce(self, issuer: str, nonce: str, payload_sha256: str) -> str:
        cursor = self.connection.cursor()
        row = cursor.execute(
            "SELECT payload_sha256 FROM nonces WHERE issuer = ? AND nonce = ?",
            (issuer, nonce),
        ).fetchone()
        if row:
            return "duplicate" if row["payload_sha256"] == payload_sha256 else "conflict"
        cursor.execute(
            "INSERT INTO nonces (issuer, nonce, payload_sha256, created_at) VALUES (?, ?, ?, ?)",
            (issuer, nonce, payload_sha256, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()
        return "new"

    def create_task(self, issuer: str, task_id: str, payload_sha256: str, task_payload: dict[str, Any]) -> TaskInsertResult:
        cursor = self.connection.cursor()
        existing = cursor.execute(
            "SELECT * FROM tasks WHERE issuer = ? AND task_id = ?",
            (issuer, task_id),
        ).fetchone()
        if existing:
            if existing["payload_sha256"] != payload_sha256:
                return TaskInsertResult(state="conflict")
            result_row = cursor.execute(
                "SELECT result_json FROM results WHERE issuer = ? AND task_id = ?",
                (issuer, task_id),
            ).fetchone()
            return TaskInsertResult(
                state="duplicate",
                task=json.loads(existing["payload_json"]),
                result=json.loads(result_row["result_json"]) if result_row else None,
            )
        callback_url = None
        callback_timeout_ms = None
        callback_state = None
        response_mode = task_payload["response_mode"]
        if response_mode["mode"] == "callback":
            callback_url = response_mode["callback"]["url"]
            callback_timeout_ms = response_mode["callback"]["timeout_ms"]
            callback_state = "pending"
        cursor.execute(
            """
            INSERT INTO tasks (
              issuer, task_id, payload_sha256, payload_json, expected_output_schema_json,
              status, accepted_at, callback_url, callback_timeout_ms, callback_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issuer,
                task_id,
                payload_sha256,
                json.dumps(task_payload),
                json.dumps(task_payload["expected_output_schema"]),
                "accepted",
                datetime.now(UTC).isoformat(),
                callback_url,
                callback_timeout_ms,
                callback_state,
            ),
        )
        self.connection.commit()
        return TaskInsertResult(state="created")

    def get_task(self, issuer: str, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload_json, status, accepted_at, completed_at, callback_url, callback_timeout_ms, callback_state FROM tasks WHERE issuer = ? AND task_id = ?",
            (issuer, task_id),
        ).fetchone()
        if not row:
            return None
        task = json.loads(row["payload_json"])
        task["_store"] = {
            "status": row["status"],
            "accepted_at": row["accepted_at"],
            "completed_at": row["completed_at"],
            "callback_url": row["callback_url"],
            "callback_timeout_ms": row["callback_timeout_ms"],
            "callback_state": row["callback_state"],
        }
        return task

    def save_result(self, issuer: str, task_id: str, result: dict[str, Any]) -> None:
        now = datetime.now(UTC).isoformat()
        cursor = self.connection.cursor()
        cursor.execute(
            """
            INSERT INTO results (issuer, task_id, result_json, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(issuer, task_id) DO UPDATE SET result_json = excluded.result_json, status = excluded.status
            """,
            (issuer, task_id, json.dumps(result), result["status"], now),
        )
        cursor.execute(
            "UPDATE tasks SET status = ?, completed_at = ? WHERE issuer = ? AND task_id = ?",
            (result["status"], now, issuer, task_id),
        )
        self.connection.commit()

    def get_result(self, issuer: str, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT result_json FROM results WHERE issuer = ? AND task_id = ?",
            (issuer, task_id),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["result_json"])

    def status_snapshot(self, issuer: str, task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return self.get_task(issuer, task_id), self.get_result(issuer, task_id)

    def enqueue_callback(self, issuer: str, task_id: str, url: str, timeout_ms: int, *, delay_seconds: int, attempt: int) -> None:
        scheduled_at = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
        self.connection.execute(
            """
            INSERT INTO callback_attempts (issuer, task_id, callback_url, callback_timeout_ms, attempt, scheduled_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (issuer, task_id, url, timeout_ms, attempt, scheduled_at),
        )
        self.connection.commit()

    def due_callbacks(self, *, limit: int = 10) -> list[CallbackAttempt]:
        now = datetime.now(UTC).isoformat()
        rows = self.connection.execute(
            """
            SELECT id, issuer, task_id, callback_url, callback_timeout_ms, attempt
            FROM callback_attempts
            WHERE delivered_at IS NULL AND scheduled_at <= ?
            ORDER BY scheduled_at ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        return [
            CallbackAttempt(
                callback_id=row["id"],
                issuer=row["issuer"],
                task_id=row["task_id"],
                url=row["callback_url"],
                timeout_ms=row["callback_timeout_ms"],
                attempt=row["attempt"],
            )
            for row in rows
        ]

    def mark_callback_delivered(self, callback_id: int) -> None:
        self.connection.execute(
            "UPDATE callback_attempts SET delivered_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), callback_id),
        )
        self.connection.commit()

    def reschedule_callback(self, callback_id: int, *, delay_seconds: int, error: str) -> None:
        scheduled_at = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
        self.connection.execute(
            "UPDATE callback_attempts SET scheduled_at = ?, last_error = ? WHERE id = ?",
            (scheduled_at, error, callback_id),
        )
        self.connection.commit()

    def callback_result(self, issuer: str, task_id: str) -> tuple[str, int] | None:
        row = self.connection.execute(
            "SELECT callback_url, callback_timeout_ms FROM tasks WHERE issuer = ? AND task_id = ?",
            (issuer, task_id),
        ).fetchone()
        if not row or not row["callback_url"]:
            return None
        return row["callback_url"], int(row["callback_timeout_ms"])

