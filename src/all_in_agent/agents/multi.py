from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..utils import make_ulid as _make_ulid, iso_now as _iso_now

_LOCK_TIMEOUT = 5.0  # seconds


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class MessageEnvelope:
    msg_id: str
    run_id: str
    from_agent: str
    to_agent: str  # agent_id | "coordinator" | "broadcast"
    msg_type: str
    payload: dict
    ts: str
    ttl_ms: int = 300_000
    idempotency_key: str = ""

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "run_id": self.run_id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "msg_type": self.msg_type,
            "payload": self.payload,
            "ts": self.ts,
            "ttl_ms": self.ttl_ms,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MessageEnvelope":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Task:
    task_id: str
    goal: str
    status: TaskStatus = TaskStatus.PENDING
    dependencies: list[str] = field(default_factory=list)
    assigned_to: str | None = None
    lease_expires_at: int | None = None
    lease_duration_ms: int = 90_000
    renew_every_ms: int = 30_000
    result: dict | None = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status,
            "dependencies": self.dependencies,
            "assigned_to": self.assigned_to,
            "lease_expires_at": self.lease_expires_at,
            "lease_duration_ms": self.lease_duration_ms,
            "renew_every_ms": self.renew_every_ms,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d = dict(d)
        d["status"] = TaskStatus(d.get("status", "PENDING"))
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class MessageBus:
    def __init__(self, run_dir: str | Path):
        self._inbox_dir = Path(run_dir) / "inbox"
        self._inbox_dir.mkdir(parents=True, exist_ok=True)

    def _inbox_path(self, agent_id: str) -> Path:
        return self._inbox_dir / f"{agent_id}.jsonl"

    def _send_to_file_sync(self, path: Path, line: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _read_inbox_sync(self, agent_id: str) -> list[MessageEnvelope]:
        path = self._inbox_path(agent_id)
        if not path.exists():
            return []
        messages = []
        now_ms = int(time.time() * 1000)
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    env = MessageEnvelope.from_dict(d)
                    ts_dt = datetime.fromisoformat(env.ts.replace("Z", "+00:00"))
                    ts_ms = int(ts_dt.timestamp() * 1000)
                    if now_ms - ts_ms <= env.ttl_ms:
                        messages.append(env)
                except (json.JSONDecodeError, Exception):
                    continue
        messages.sort(key=lambda m: m.msg_id)
        return messages

    async def send(self, envelope: MessageEnvelope) -> None:
        targets = self._resolve_targets(envelope)
        for target in targets:
            path = self._inbox_path(target)
            if envelope.idempotency_key:
                existing = await self.read_inbox(target)
                if any(m.idempotency_key == envelope.idempotency_key for m in existing):
                    continue
            line = json.dumps(envelope.to_dict(), ensure_ascii=False)
            await asyncio.to_thread(self._send_to_file_sync, path, line)

    async def read_inbox(self, agent_id: str) -> list[MessageEnvelope]:
        return await asyncio.to_thread(self._read_inbox_sync, agent_id)

    def _resolve_targets(self, envelope: MessageEnvelope) -> list[str]:
        if envelope.to_agent == "broadcast":
            targets = [p.stem for p in self._inbox_dir.glob("*.jsonl")]
            if envelope.from_agent in targets:
                targets.remove(envelope.from_agent)
            return targets
        return [envelope.to_agent]


class TaskManager:
    def __init__(self, run_dir: str | Path):
        self._path = Path(run_dir) / "tasks.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[Task]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return [Task.from_dict(d) for d in data]
        except Exception:
            return []

    def _save(self, tasks: list[Task]) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps([t.to_dict() for t in tasks], indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def _lock(self):
        if sys.platform == "win32":
            return _WinFileLock(self._path)
        return _UnixFileLock(self._path)

    def _create_task_sync(self, goal: str, dependencies: list[str] | None) -> Task:
        with self._lock():
            tasks = self._load()
            task = Task(task_id=_make_ulid(), goal=goal, dependencies=dependencies or [])
            tasks.append(task)
            self._save(tasks)
        return task

    def _update_task_sync(self, task_id: str, **kwargs) -> Task | None:
        with self._lock():
            tasks = self._load()
            for t in tasks:
                if t.task_id == task_id:
                    for k, v in kwargs.items():
                        if hasattr(t, k):
                            setattr(t, k, v)
                    self._save(tasks)
                    return t
        return None

    def _claim_task_sync(self, task_id: str, agent_id: str) -> bool:
        with self._lock():
            tasks = self._load()
            now_ms = int(time.time() * 1000)
            for t in tasks:
                if t.task_id == task_id and t.status in (TaskStatus.PENDING, TaskStatus.CLAIMED):
                    if t.status == TaskStatus.CLAIMED and (not t.lease_expires_at or now_ms <= t.lease_expires_at):
                        return False
                    t.status = TaskStatus.CLAIMED
                    t.assigned_to = agent_id
                    t.lease_expires_at = now_ms + t.lease_duration_ms
                    self._save(tasks)
                    return True
        return False

    def _get_available_sync(self, agent_id: str) -> list[Task]:
        tasks = self._load()
        done_ids = {t.task_id for t in tasks if t.status == TaskStatus.DONE}
        now_ms = int(time.time() * 1000)
        available = []
        for t in tasks:
            if t.status != TaskStatus.PENDING:
                if t.status == TaskStatus.CLAIMED and t.lease_expires_at and now_ms > t.lease_expires_at:
                    available.append(t)  # expired lease, can reclaim
                continue
            if all(dep in done_ids for dep in t.dependencies):
                available.append(t)
        return available

    def _get_all_sync(self) -> list[Task]:
        return self._load()

    async def create_task(self, goal: str, dependencies: list[str] | None = None) -> Task:
        return await asyncio.to_thread(self._create_task_sync, goal, dependencies)

    async def update_task(self, task_id: str, **kwargs) -> Task | None:
        return await asyncio.to_thread(self._update_task_sync, task_id, **kwargs)

    async def claim_task(self, task_id: str, agent_id: str) -> bool:
        return await asyncio.to_thread(self._claim_task_sync, task_id, agent_id)

    async def get_available(self, agent_id: str) -> list[Task]:
        return await asyncio.to_thread(self._get_available_sync, agent_id)

    async def get_all(self) -> list[Task]:
        return await asyncio.to_thread(self._get_all_sync)


class _UnixFileLock:
    def __init__(self, path: Path):
        self._lock_path = path.with_suffix(".lock")

    def __enter__(self):
        import fcntl
        self._f = open(self._lock_path, "w")
        fcntl.flock(self._f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *_):
        import fcntl
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()


class _WinFileLock:
    def __init__(self, path: Path):
        self._lock_path = path.with_suffix(".lock")

    def __enter__(self):
        deadline = time.time() + _LOCK_TIMEOUT
        while time.time() < deadline:
            try:
                self._f = open(self._lock_path, "x")
                return self
            except FileExistsError:
                time.sleep(0.05)
        raise TimeoutError(f"Could not acquire lock on {self._lock_path}")

    def __exit__(self, *_):
        self._f.close()
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass
