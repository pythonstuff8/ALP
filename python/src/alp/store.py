from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import asyncpg

MIGRATION_LOCK_ID = 870421
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "sql" / "migrations"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def decode_json(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


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


class TaskStore(Protocol):
    async def start(self) -> None:
        ...

    async def close(self) -> None:
        ...

    async def is_ready(self) -> bool:
        ...

    async def reserve_nonce(self, issuer: str, nonce: str, payload_sha256: str) -> str:
        ...

    async def create_task(
        self,
        issuer: str,
        task_id: str,
        payload_sha256: str,
        task_payload: dict[str, Any],
    ) -> TaskInsertResult:
        ...

    async def get_task(self, issuer: str, task_id: str) -> dict[str, Any] | None:
        ...

    async def save_result(self, issuer: str, task_id: str, result: dict[str, Any]) -> None:
        ...

    async def get_result(self, issuer: str, task_id: str) -> dict[str, Any] | None:
        ...

    async def status_snapshot(self, issuer: str, task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        ...

    async def enqueue_callback(
        self,
        issuer: str,
        task_id: str,
        url: str,
        timeout_ms: int,
        *,
        delay_seconds: int,
        attempt: int,
    ) -> None:
        ...

    async def due_callbacks(self, *, limit: int = 10) -> list[CallbackAttempt]:
        ...

    async def mark_callback_delivered(self, callback_id: int) -> None:
        ...

    async def reschedule_callback(self, callback_id: int, *, delay_seconds: int, error: str) -> None:
        ...

    async def fail_callback(self, callback_id: int, *, error: str) -> None:
        ...


class SQLiteTaskStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._ready = False
        self._init_schema()

    async def start(self) -> None:
        self._ready = True

    async def close(self) -> None:
        self.connection.close()
        self._ready = False

    async def is_ready(self) -> bool:
        return self._ready

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

    async def reserve_nonce(self, issuer: str, nonce: str, payload_sha256: str) -> str:
        cursor = self.connection.cursor()
        row = cursor.execute(
            "SELECT payload_sha256 FROM nonces WHERE issuer = ? AND nonce = ?",
            (issuer, nonce),
        ).fetchone()
        if row:
            return "duplicate" if row["payload_sha256"] == payload_sha256 else "conflict"
        cursor.execute(
            "INSERT INTO nonces (issuer, nonce, payload_sha256, created_at) VALUES (?, ?, ?, ?)",
            (issuer, nonce, payload_sha256, utc_now_iso()),
        )
        self.connection.commit()
        return "new"

    async def create_task(
        self,
        issuer: str,
        task_id: str,
        payload_sha256: str,
        task_payload: dict[str, Any],
    ) -> TaskInsertResult:
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
                utc_now_iso(),
                callback_url,
                callback_timeout_ms,
                callback_state,
            ),
        )
        self.connection.commit()
        return TaskInsertResult(state="created")

    async def get_task(self, issuer: str, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT payload_json, status, accepted_at, completed_at, callback_url, callback_timeout_ms, callback_state
            FROM tasks
            WHERE issuer = ? AND task_id = ?
            """,
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

    async def save_result(self, issuer: str, task_id: str, result: dict[str, Any]) -> None:
        now = utc_now_iso()
        cursor = self.connection.cursor()
        cursor.execute(
            """
            INSERT INTO results (issuer, task_id, result_json, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(issuer, task_id) DO UPDATE SET
              result_json = excluded.result_json,
              status = excluded.status
            """,
            (issuer, task_id, json.dumps(result), result["status"], now),
        )
        cursor.execute(
            "UPDATE tasks SET status = ?, completed_at = ? WHERE issuer = ? AND task_id = ?",
            (result["status"], now, issuer, task_id),
        )
        self.connection.commit()

    async def get_result(self, issuer: str, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT result_json FROM results WHERE issuer = ? AND task_id = ?",
            (issuer, task_id),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["result_json"])

    async def status_snapshot(self, issuer: str, task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return await self.get_task(issuer, task_id), await self.get_result(issuer, task_id)

    async def enqueue_callback(
        self,
        issuer: str,
        task_id: str,
        url: str,
        timeout_ms: int,
        *,
        delay_seconds: int,
        attempt: int,
    ) -> None:
        scheduled_at = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
        cursor = self.connection.cursor()
        cursor.execute(
            """
            INSERT INTO callback_attempts (issuer, task_id, callback_url, callback_timeout_ms, attempt, scheduled_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (issuer, task_id, url, timeout_ms, attempt, scheduled_at),
        )
        cursor.execute(
            "UPDATE tasks SET callback_state = ? WHERE issuer = ? AND task_id = ?",
            ("pending", issuer, task_id),
        )
        self.connection.commit()

    async def due_callbacks(self, *, limit: int = 10) -> list[CallbackAttempt]:
        rows = self.connection.execute(
            """
            SELECT id, issuer, task_id, callback_url, callback_timeout_ms, attempt
            FROM callback_attempts
            WHERE delivered_at IS NULL AND scheduled_at <= ?
            ORDER BY scheduled_at ASC
            LIMIT ?
            """,
            (utc_now_iso(), limit),
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

    async def mark_callback_delivered(self, callback_id: int) -> None:
        cursor = self.connection.cursor()
        row = cursor.execute(
            "SELECT issuer, task_id FROM callback_attempts WHERE id = ?",
            (callback_id,),
        ).fetchone()
        cursor.execute(
            "UPDATE callback_attempts SET delivered_at = ? WHERE id = ?",
            (utc_now_iso(), callback_id),
        )
        if row:
            cursor.execute(
                "UPDATE tasks SET callback_state = ? WHERE issuer = ? AND task_id = ?",
                ("delivered", row["issuer"], row["task_id"]),
            )
        self.connection.commit()

    async def reschedule_callback(self, callback_id: int, *, delay_seconds: int, error: str) -> None:
        scheduled_at = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
        cursor = self.connection.cursor()
        cursor.execute(
            """
            UPDATE callback_attempts
            SET scheduled_at = ?, last_error = ?, attempt = attempt + 1
            WHERE id = ?
            """,
            (scheduled_at, error, callback_id),
        )
        self.connection.commit()

    async def fail_callback(self, callback_id: int, *, error: str) -> None:
        cursor = self.connection.cursor()
        row = cursor.execute(
            "SELECT issuer, task_id FROM callback_attempts WHERE id = ?",
            (callback_id,),
        ).fetchone()
        cursor.execute(
            "UPDATE callback_attempts SET delivered_at = ?, last_error = ? WHERE id = ?",
            (utc_now_iso(), error, callback_id),
        )
        if row:
            cursor.execute(
                "UPDATE tasks SET callback_state = ? WHERE issuer = ? AND task_id = ?",
                ("failed", row["issuer"], row["task_id"]),
            )
        self.connection.commit()


class PostgresTaskStore:
    def __init__(self, database_url: str, *, migrations_dir: str | Path | None = None) -> None:
        self.database_url = database_url
        self.migrations_dir = Path(migrations_dir) if migrations_dir else DEFAULT_MIGRATIONS_DIR
        self.pool: asyncpg.Pool | None = None
        self._ready = False

    async def start(self) -> None:
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=10, init=self._init_connection)
        await self._apply_migrations()
        self._ready = True

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None
        self._ready = False

    async def is_ready(self) -> bool:
        if not self._ready or not self.pool:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("SELECT 1")
        except Exception:
            return False
        return True

    async def reserve_nonce(self, issuer: str, nonce: str, payload_sha256: str) -> str:
        pool = self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT payload_sha256 FROM nonces WHERE issuer = $1 AND nonce = $2",
                issuer,
                nonce,
            )
            if row:
                return "duplicate" if row["payload_sha256"] == payload_sha256 else "conflict"
            await conn.execute(
                """
                INSERT INTO nonces (issuer, nonce, payload_sha256, created_at)
                VALUES ($1, $2, $3, NOW())
                """,
                issuer,
                nonce,
                payload_sha256,
            )
            return "new"

    async def create_task(
        self,
        issuer: str,
        task_id: str,
        payload_sha256: str,
        task_payload: dict[str, Any],
    ) -> TaskInsertResult:
        pool = self._pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT payload_sha256, payload_json, accepted_at, completed_at, callback_url, callback_timeout_ms, callback_state, status
                FROM tasks
                WHERE issuer = $1 AND task_id = $2
                """,
                issuer,
                task_id,
            )
            if existing:
                if existing["payload_sha256"] != payload_sha256:
                    return TaskInsertResult(state="conflict")
                result_row = await conn.fetchrow(
                    "SELECT result_json FROM results WHERE issuer = $1 AND task_id = $2",
                    issuer,
                    task_id,
                )
                task = decode_json(existing["payload_json"])
                task["_store"] = {
                    "status": existing["status"],
                    "accepted_at": existing["accepted_at"].isoformat(),
                    "completed_at": existing["completed_at"].isoformat() if existing["completed_at"] else None,
                    "callback_url": existing["callback_url"],
                    "callback_timeout_ms": existing["callback_timeout_ms"],
                    "callback_state": existing["callback_state"],
                }
                return TaskInsertResult(
                    state="duplicate",
                    task=task,
                    result=decode_json(result_row["result_json"]) if result_row else None,
                )

            response_mode = task_payload["response_mode"]
            callback_url = None
            callback_timeout_ms = None
            callback_state = None
            if response_mode["mode"] == "callback":
                callback_url = response_mode["callback"]["url"]
                callback_timeout_ms = response_mode["callback"]["timeout_ms"]
                callback_state = "pending"

            await conn.execute(
                """
                INSERT INTO tasks (
                  issuer, task_id, task_type, recipient, payload_sha256, payload_json, expected_output_schema,
                  response_mode, status, accepted_at, callback_url, callback_timeout_ms, callback_state
                ) VALUES (
                  $1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, 'accepted', NOW(), $9, $10, $11
                )
                """,
                issuer,
                task_id,
                task_payload["task_type"],
                task_payload["recipient"],
                payload_sha256,
                json.dumps(task_payload),
                json.dumps(task_payload["expected_output_schema"]),
                json.dumps(task_payload["response_mode"]),
                callback_url,
                callback_timeout_ms,
                callback_state,
            )
            return TaskInsertResult(state="created")

    async def get_task(self, issuer: str, task_id: str) -> dict[str, Any] | None:
        pool = self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT payload_json, status, accepted_at, completed_at, callback_url, callback_timeout_ms, callback_state
                FROM tasks
                WHERE issuer = $1 AND task_id = $2
                """,
                issuer,
                task_id,
            )
            if not row:
                return None
            task = decode_json(row["payload_json"])
            task["_store"] = {
                "status": row["status"],
                "accepted_at": row["accepted_at"].isoformat(),
                "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
                "callback_url": row["callback_url"],
                "callback_timeout_ms": row["callback_timeout_ms"],
                "callback_state": row["callback_state"],
            }
            return task

    async def save_result(self, issuer: str, task_id: str, result: dict[str, Any]) -> None:
        pool = self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO results (issuer, task_id, result_json, status, created_at)
                    VALUES ($1, $2, $3::jsonb, $4, NOW())
                    ON CONFLICT (issuer, task_id) DO UPDATE SET
                      result_json = EXCLUDED.result_json,
                      status = EXCLUDED.status,
                      created_at = NOW()
                    """,
                    issuer,
                    task_id,
                    json.dumps(result),
                    result["status"],
                )
                await conn.execute(
                    """
                    UPDATE tasks
                    SET status = $1, completed_at = NOW()
                    WHERE issuer = $2 AND task_id = $3
                    """,
                    result["status"],
                    issuer,
                    task_id,
                )

    async def get_result(self, issuer: str, task_id: str) -> dict[str, Any] | None:
        pool = self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT result_json FROM results WHERE issuer = $1 AND task_id = $2",
                issuer,
                task_id,
            )
            if not row:
                return None
            return decode_json(row["result_json"])

    async def status_snapshot(self, issuer: str, task_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return await self.get_task(issuer, task_id), await self.get_result(issuer, task_id)

    async def enqueue_callback(
        self,
        issuer: str,
        task_id: str,
        url: str,
        timeout_ms: int,
        *,
        delay_seconds: int,
        attempt: int,
    ) -> None:
        pool = self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO callback_attempts (
                      issuer, task_id, callback_url, callback_timeout_ms, attempt, scheduled_at
                    ) VALUES ($1, $2, $3, $4, $5, NOW() + ($6::text || ' seconds')::interval)
                    """,
                    issuer,
                    task_id,
                    url,
                    timeout_ms,
                    attempt,
                    delay_seconds,
                )
                await conn.execute(
                    """
                    UPDATE tasks
                    SET callback_state = 'pending'
                    WHERE issuer = $1 AND task_id = $2
                    """,
                    issuer,
                    task_id,
                )

    async def due_callbacks(self, *, limit: int = 10) -> list[CallbackAttempt]:
        pool = self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, issuer, task_id, callback_url, callback_timeout_ms, attempt
                FROM callback_attempts
                WHERE delivered_at IS NULL AND scheduled_at <= NOW()
                ORDER BY scheduled_at ASC
                LIMIT $1
                """,
                limit,
            )
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

    async def mark_callback_delivered(self, callback_id: int) -> None:
        pool = self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT issuer, task_id FROM callback_attempts WHERE id = $1",
                    callback_id,
                )
                await conn.execute(
                    "UPDATE callback_attempts SET delivered_at = NOW() WHERE id = $1",
                    callback_id,
                )
                if row:
                    await conn.execute(
                        """
                        UPDATE tasks
                        SET callback_state = 'delivered'
                        WHERE issuer = $1 AND task_id = $2
                        """,
                        row["issuer"],
                        row["task_id"],
                    )

    async def reschedule_callback(self, callback_id: int, *, delay_seconds: int, error: str) -> None:
        pool = self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE callback_attempts
                SET scheduled_at = NOW() + ($1::text || ' seconds')::interval,
                    last_error = $2,
                    attempt = attempt + 1
                WHERE id = $3
                """,
                delay_seconds,
                error,
                callback_id,
            )

    async def fail_callback(self, callback_id: int, *, error: str) -> None:
        pool = self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT issuer, task_id FROM callback_attempts WHERE id = $1",
                    callback_id,
                )
                await conn.execute(
                    """
                    UPDATE callback_attempts
                    SET delivered_at = NOW(), last_error = $1
                    WHERE id = $2
                    """,
                    error,
                    callback_id,
                )
                if row:
                    await conn.execute(
                        """
                        UPDATE tasks
                        SET callback_state = 'failed'
                        WHERE issuer = $1 AND task_id = $2
                        """,
                        row["issuer"],
                        row["task_id"],
                    )

    async def _init_connection(self, conn: asyncpg.Connection) -> None:
        await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

    async def _apply_migrations(self) -> None:
        pool = self._pool()
        migration_files = sorted(self.migrations_dir.glob("*.sql"))
        async with pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_ID)
            try:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS alp_migrations (
                      version TEXT PRIMARY KEY,
                      applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                for migration in migration_files:
                    version = migration.name
                    applied = await conn.fetchval(
                        "SELECT 1 FROM alp_migrations WHERE version = $1",
                        version,
                    )
                    if applied:
                        continue
                    await conn.execute(migration.read_text())
                    await conn.execute(
                        "INSERT INTO alp_migrations (version) VALUES ($1)",
                        version,
                    )
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_ID)

    def _pool(self) -> asyncpg.Pool:
        if not self.pool:
            raise RuntimeError("PostgresTaskStore is not started")
        return self.pool
