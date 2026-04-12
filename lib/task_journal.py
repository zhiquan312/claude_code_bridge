"""Durable JSONL task journal for CCB request lifecycle tracking."""
from __future__ import annotations

import enum
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from askd_runtime import run_dir


class TaskState(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


_TERMINAL = {
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.INCOMPLETE,
    TaskState.CANCELLED,
    TaskState.EXPIRED,
}


class TaskJournal:
    """Append-only JSONL journal for request state transitions."""

    def __init__(self, journal_dir: str | Path | None = None) -> None:
        if journal_dir is None:
            journal_dir = str(run_dir())
        self._dir = Path(journal_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "task_journal.jsonl"
        self._lock = threading.Lock()

    def record(
        self,
        req_id: str,
        provider: str,
        state: TaskState,
        meta: dict | None = None,
    ) -> None:
        entry = {
            "req_id": req_id,
            "provider": provider,
            "state": state.value,
            "timestamp": time.time(),
            "meta": meta or {},
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def read_all(self) -> List[dict]:
        if not self._path.exists():
            return []
        entries: List[dict] = []
        with self._lock:
            with self._path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        data = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict):
                        entries.append(data)
        return entries

    def get_latest_state(self, req_id: str) -> Optional[TaskState]:
        latest_state: Optional[TaskState] = None
        for entry in self.read_all():
            if entry.get("req_id") != req_id:
                continue
            try:
                latest_state = TaskState(str(entry.get("state")))
            except ValueError:
                continue
        return latest_state

    def get_pending_tasks(self) -> Dict[str, dict]:
        latest_by_req_id: Dict[str, dict] = {}
        for entry in self.read_all():
            req_id = entry.get("req_id")
            if not isinstance(req_id, str) or not req_id:
                continue
            latest_by_req_id[req_id] = entry

        pending: Dict[str, dict] = {}
        for req_id, entry in latest_by_req_id.items():
            try:
                state = TaskState(str(entry.get("state")))
            except ValueError:
                continue
            if state not in _TERMINAL:
                pending[req_id] = entry
        return pending

    def expire_stale(self, max_age_s: float = 3600.0) -> List[str]:
        expired: List[str] = []
        now = time.time()
        for req_id, entry in self.get_pending_tasks().items():
            timestamp = entry.get("timestamp")
            if not isinstance(timestamp, (int, float)):
                continue
            if now - float(timestamp) < max_age_s:
                continue
            provider = str(entry.get("provider") or "unknown")
            self.record(
                req_id,
                provider,
                TaskState.EXPIRED,
                meta={"reason": "stale", "max_age_s": max_age_s},
            )
            expired.append(req_id)
        return expired

    def truncate(self, keep_last_n: int = 1000) -> None:
        with self._lock:
            if not self._path.exists():
                return
            with self._path.open("r", encoding="utf-8") as handle:
                lines = handle.readlines()
            if len(lines) <= keep_last_n:
                return
            keep_lines = lines[-keep_last_n:]
            fd, tmp_name = tempfile.mkstemp(
                prefix=".task_journal.",
                suffix=".tmp",
                dir=str(self._dir),
                text=True,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.writelines(keep_lines)
                os.replace(tmp_name, self._path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
