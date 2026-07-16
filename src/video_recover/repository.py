from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from video_recover.domain import (
    Task,
    TaskEvent,
    TaskStatus,
    TranscriptionLease,
    require_transition,
)
from video_recover.errors import UserFacingError

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL UNIQUE,
    original_url TEXT NOT NULL,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
    message TEXT NOT NULL,
    source TEXT NOT NULL,
    transcribe INTEGER NOT NULL DEFAULT 1 CHECK(transcribe IN (0, 1)),
    aweme_id TEXT,
    output_dir TEXT,
    metadata_json TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcription_leases (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
    worker_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_leases_expires_at ON transcription_leases(expires_at);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class Repository:
    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.database_path = database_path
        self.clock = clock
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_or_get_task(
        self,
        canonical_url: str,
        *,
        original_url: str | None = None,
        source: str = "web",
        transcribe: bool = True,
    ) -> tuple[Task, bool]:
        now = _iso(self.clock())
        task_id = str(uuid4())
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM tasks WHERE canonical_url=?",
                (canonical_url,),
            ).fetchone()
            if existing is not None:
                return self._task(existing), False
            connection.execute(
                """
                INSERT INTO tasks(
                    id, canonical_url, original_url, status, progress, message,
                    source, transcribe, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id,
                    canonical_url,
                    original_url or canonical_url,
                    TaskStatus.QUEUED.value,
                    0,
                    "排队中",
                    source,
                    int(transcribe),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO events(task_id, status, message, created_at) VALUES(?,?,?,?)",
                (task_id, TaskStatus.QUEUED.value, "排队中", now),
            )
        return self.get_task(task_id), True

    def get_task(self, task_id: str) -> Task:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task(row)

    def list_tasks(self, *, limit: int = 100) -> list[Task]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._task(row) for row in rows]

    def delete_task(self, task_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        return cursor.rowcount == 1

    def next_task(self, status: TaskStatus) -> Task | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY created_at LIMIT 1",
                (status.value,),
            ).fetchone()
        return None if row is None else self._task(row)

    def transition(
        self,
        task_id: str,
        target: TaskStatus,
        *,
        progress: int,
        message: str,
    ) -> Task:
        now = _iso(self.clock())
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            require_transition(TaskStatus(row["status"]), target)
            connection.execute(
                """
                UPDATE tasks
                SET status=?, progress=?, message=?, error_code=NULL,
                    error_message=NULL, updated_at=?
                WHERE id=?
                """,
                (target.value, progress, message, now, task_id),
            )
            connection.execute(
                "INSERT INTO events(task_id, status, message, created_at) VALUES(?,?,?,?)",
                (task_id, target.value, message, now),
            )
        return self.get_task(task_id)

    def list_events(self, task_id: str) -> list[TaskEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id",
                (task_id,),
            ).fetchall()
        return [self._event(row) for row in rows]

    def set_media(
        self,
        task_id: str,
        *,
        aweme_id: str,
        output_dir: Path,
        metadata: dict[str, object],
    ) -> Task:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks
                SET aweme_id=?, output_dir=?, metadata_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    aweme_id,
                    str(output_dir),
                    json.dumps(metadata, ensure_ascii=False),
                    _iso(self.clock()),
                    task_id,
                ),
            )
        return self.get_task(task_id)

    def record_failure(
        self,
        task_id: str,
        error: UserFacingError,
        *,
        lease_id: str | None = None,
    ) -> Task:
        now = _iso(self.clock())
        with self.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = TaskStatus(row["status"])
            target = (
                TaskStatus.PARTIAL
                if current in {TaskStatus.AWAITING_TRANSCRIPTION, TaskStatus.TRANSCRIBING}
                else TaskStatus.FAILED
            )
            require_transition(current, target)
            if lease_id:
                connection.execute("DELETE FROM transcription_leases WHERE id=?", (lease_id,))
            connection.execute(
                """
                UPDATE tasks
                SET status=?, message=?, error_code=?, error_message=?, updated_at=?
                WHERE id=?
                """,
                (target.value, error.message, error.code, error.message, now, task_id),
            )
            connection.execute(
                "INSERT INTO events(task_id, status, message, created_at) VALUES(?,?,?,?)",
                (task_id, target.value, error.message, now),
            )
        return self.get_task(task_id)

    def set_setting(self, key: str, value: str) -> None:
        now = _iso(self.clock())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )

    def get_setting(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def acquire_transcription_lease(
        self,
        worker_id: str,
        *,
        ttl_seconds: int,
    ) -> TranscriptionLease | None:
        now_value = self.clock()
        now = _iso(now_value)
        expires_at = _iso(now_value + timedelta(seconds=ttl_seconds))
        lease_id = str(uuid4())
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT tasks.* FROM tasks
                LEFT JOIN transcription_leases leases ON leases.task_id=tasks.id
                WHERE tasks.status=? AND leases.id IS NULL
                ORDER BY tasks.created_at
                LIMIT 1
                """,
                (TaskStatus.AWAITING_TRANSCRIPTION.value,),
            ).fetchone()
            if row is None:
                return None
            require_transition(TaskStatus(row["status"]), TaskStatus.TRANSCRIBING)
            connection.execute(
                """
                INSERT INTO transcription_leases(
                    id, task_id, worker_id, acquired_at, heartbeat_at, expires_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (lease_id, row["id"], worker_id, now, now, expires_at),
            )
            connection.execute(
                "UPDATE tasks SET status=?, progress=?, message=?, updated_at=? WHERE id=?",
                (TaskStatus.TRANSCRIBING.value, 70, "原生 Worker 正在转写", now, row["id"]),
            )
            connection.execute(
                "INSERT INTO events(task_id, status, message, created_at) VALUES(?,?,?,?)",
                (row["id"], TaskStatus.TRANSCRIBING.value, "原生 Worker 正在转写", now),
            )
        return TranscriptionLease(
            id=lease_id,
            task_id=str(row["id"]),
            worker_id=worker_id,
            acquired_at=now_value,
            heartbeat_at=now_value,
            expires_at=now_value + timedelta(seconds=ttl_seconds),
        )

    def heartbeat_lease(self, lease_id: str, *, ttl_seconds: int) -> bool:
        now_value = self.clock()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE transcription_leases SET heartbeat_at=?, expires_at=? WHERE id=?",
                (
                    _iso(now_value),
                    _iso(now_value + timedelta(seconds=ttl_seconds)),
                    lease_id,
                ),
            )
        return cursor.rowcount == 1

    def get_transcription_lease(self, lease_id: str) -> TranscriptionLease:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transcription_leases WHERE id=?",
                (lease_id,),
            ).fetchone()
        if row is None:
            raise KeyError(lease_id)
        return TranscriptionLease(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            worker_id=str(row["worker_id"]),
            acquired_at=_datetime(row["acquired_at"]),
            heartbeat_at=_datetime(row["heartbeat_at"]),
            expires_at=_datetime(row["expires_at"]),
        )

    def recover_expired_leases(self) -> int:
        now = _iso(self.clock())
        recovered = 0
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT * FROM transcription_leases WHERE expires_at<=?",
                (now,),
            ).fetchall()
            for row in rows:
                task = connection.execute(
                    "SELECT status FROM tasks WHERE id=?",
                    (row["task_id"],),
                ).fetchone()
                if task is not None and TaskStatus(task["status"]) == TaskStatus.TRANSCRIBING:
                    require_transition(
                        TaskStatus.TRANSCRIBING,
                        TaskStatus.AWAITING_TRANSCRIPTION,
                    )
                    connection.execute(
                        "UPDATE tasks SET status=?, progress=?, message=?, updated_at=? WHERE id=?",
                        (
                            TaskStatus.AWAITING_TRANSCRIPTION.value,
                            60,
                            "Worker 租约过期，等待重新转写",
                            now,
                            row["task_id"],
                        ),
                    )
                    connection.execute(
                        "INSERT INTO events(task_id, status, message, created_at) VALUES(?,?,?,?)",
                        (
                            row["task_id"],
                            TaskStatus.AWAITING_TRANSCRIPTION.value,
                            "Worker 租约过期，等待重新转写",
                            now,
                        ),
                    )
                    recovered += 1
                connection.execute("DELETE FROM transcription_leases WHERE id=?", (row["id"],))
        return recovered

    def complete_transcription(self, lease_id: str) -> Task:
        now = _iso(self.clock())
        with self.transaction(immediate=True) as connection:
            lease = connection.execute(
                "SELECT * FROM transcription_leases WHERE id=?",
                (lease_id,),
            ).fetchone()
            if lease is None:
                raise KeyError(lease_id)
            task = connection.execute(
                "SELECT * FROM tasks WHERE id=?",
                (lease["task_id"],),
            ).fetchone()
            if task is None:
                raise KeyError(lease["task_id"])
            require_transition(TaskStatus(task["status"]), TaskStatus.COMPLETED)
            connection.execute("DELETE FROM transcription_leases WHERE id=?", (lease_id,))
            connection.execute(
                """
                UPDATE tasks
                SET status=?, progress=100, message=?, error_code=NULL,
                    error_message=NULL, updated_at=?
                WHERE id=?
                """,
                (TaskStatus.COMPLETED.value, "处理完成", now, lease["task_id"]),
            )
            connection.execute(
                "INSERT INTO events(task_id, status, message, created_at) VALUES(?,?,?,?)",
                (lease["task_id"], TaskStatus.COMPLETED.value, "处理完成", now),
            )
        return self.get_task(str(lease["task_id"]))

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        return Task(
            id=str(row["id"]),
            canonical_url=str(row["canonical_url"]),
            original_url=str(row["original_url"]),
            status=TaskStatus(row["status"]),
            progress=int(row["progress"]),
            message=str(row["message"]),
            source=str(row["source"]),
            transcribe=bool(row["transcribe"]),
            aweme_id=row["aweme_id"],
            output_dir=Path(row["output_dir"]) if row["output_dir"] else None,
            metadata_json=row["metadata_json"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> TaskEvent:
        return TaskEvent(
            id=int(row["id"]),
            task_id=str(row["task_id"]),
            status=TaskStatus(row["status"]),
            message=str(row["message"]),
            created_at=_datetime(row["created_at"]),
        )

    def dump_metadata(self, task_id: str, metadata: dict[str, object]) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE tasks SET metadata_json=?, updated_at=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), _iso(self.clock()), task_id),
            )
