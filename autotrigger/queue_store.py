"""Thread-safe JSON persistence for the task queue."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


FINAL_STATES = {"completed", "failed"}


def utc_iso(timestamp: float | None = None) -> str:
    value = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class TaskStore:
    """Persist queue mutations atomically in ``cola_tareas.json``.

    The lock protects FastAPI request handlers and the background worker inside
    one process. Run Uvicorn with a single worker when using this file backend.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 1, "cooldown": None, "tasks": []}

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Cola JSON inválida: {self.path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise RuntimeError(f"Formato de cola inválido: {self.path}")
        data.setdefault("version", 1)
        data.setdefault("cooldown", None)
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temp_path, self.path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _mutate(self, operation: Callable[[dict[str, Any]], Any]) -> Any:
        with self._lock:
            data = self._read_unlocked()
            result = operation(data)
            self._write_unlocked(data)
            return result

    def enqueue(self, prompt: str, model: str) -> dict[str, Any]:
        cleaned = prompt.strip()
        if not cleaned:
            raise ValueError("El prompt no puede estar vacío")
        now = time.time()
        task = {
            "id": str(uuid.uuid4()),
            "prompt": cleaned,
            "model": model,
            "status": "queued",
            "created_at": utc_iso(now),
            "updated_at": utc_iso(now),
            "attempts": 0,
            "next_attempt_at": None,
            "resume_at": None,
            "cooldown_source": None,
            "request_id": None,
            "response_id": None,
            "result": None,
            "error": None,
        }

        def add(data: dict[str, Any]) -> dict[str, Any]:
            data["tasks"].append(task)
            return dict(task)

        return self._mutate(add)

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(task) for task in self._read_unlocked()["tasks"]]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            for task in self._read_unlocked()["tasks"]:
                if task.get("id") == task_id:
                    return dict(task)
        return None

    def cooldown(self) -> dict[str, Any] | None:
        with self._lock:
            value = self._read_unlocked().get("cooldown")
            return dict(value) if isinstance(value, dict) else None

    def recover_in_progress(self) -> int:
        """Put tasks interrupted by a prior process shutdown back in the queue."""

        def recover(data: dict[str, Any]) -> int:
            count = 0
            for task in data["tasks"]:
                if task.get("status") == "processing":
                    task["status"] = "queued"
                    task["updated_at"] = utc_iso()
                    task["error"] = "Recuperada tras una interrupción del proceso"
                    count += 1
            return count

        return self._mutate(recover)

    def claim_next(self, now: float | None = None) -> dict[str, Any] | None:
        current = time.time() if now is None else now

        def claim(data: dict[str, Any]) -> dict[str, Any] | None:
            cooldown = data.get("cooldown")
            if isinstance(cooldown, dict) and float(cooldown.get("until", 0)) > current:
                return None
            if cooldown:
                data["cooldown"] = None

            eligible = [
                task
                for task in data["tasks"]
                if task.get("status") in {"queued", "waiting"}
                and (task.get("next_attempt_at") is None or float(task["next_attempt_at"]) <= current)
            ]
            if not eligible:
                return None
            task = min(eligible, key=lambda item: item.get("created_at", ""))
            task["status"] = "processing"
            task["attempts"] = int(task.get("attempts", 0)) + 1
            task["updated_at"] = utc_iso(current)
            task["error"] = None
            return dict(task)

        return self._mutate(claim)

    def seconds_until_ready(self, now: float | None = None) -> float | None:
        current = time.time() if now is None else now
        with self._lock:
            data = self._read_unlocked()
            cooldown = data.get("cooldown")
            if isinstance(cooldown, dict) and float(cooldown.get("until", 0)) > current:
                return float(cooldown["until"]) - current
            future = [
                float(task["next_attempt_at"])
                for task in data["tasks"]
                if task.get("status") == "waiting" and task.get("next_attempt_at") is not None
            ]
            if any(task.get("status") == "queued" for task in data["tasks"]):
                return 0.0
            return max(0.0, min(future) - current) if future else None

    def complete(
        self, task_id: str, result: str, response_id: str | None, request_id: str | None
    ) -> None:
        def finish(data: dict[str, Any]) -> None:
            task = self._require_task(data, task_id)
            task.update(
                status="completed",
                updated_at=utc_iso(),
                next_attempt_at=None,
                resume_at=None,
                cooldown_source=None,
                result=result,
                response_id=response_id,
                request_id=request_id,
                error=None,
            )

        self._mutate(finish)

    def defer(
        self,
        task_id: str,
        until: float,
        source: str,
        error: str,
        request_id: str | None = None,
    ) -> None:
        def postpone(data: dict[str, Any]) -> None:
            task = self._require_task(data, task_id)
            task.update(
                status="waiting",
                updated_at=utc_iso(),
                next_attempt_at=until,
                resume_at=utc_iso(until),
                cooldown_source=source,
                error=error,
                request_id=request_id,
            )
            # A 429 normally applies to the shared project/model bucket, so stop
            # all model calls while keeping API endpoints responsive.
            existing = data.get("cooldown")
            existing_until = float(existing.get("until", 0)) if isinstance(existing, dict) else 0
            if until >= existing_until:
                data["cooldown"] = {
                    "until": until,
                    "resume_at": utc_iso(until),
                    "source": source,
                }

        self._mutate(postpone)

    def fail(self, task_id: str, error: str, request_id: str | None = None) -> None:
        def mark_failed(data: dict[str, Any]) -> None:
            task = self._require_task(data, task_id)
            task.update(
                status="failed",
                updated_at=utc_iso(),
                next_attempt_at=None,
                resume_at=None,
                error=error,
                request_id=request_id,
            )

        self._mutate(mark_failed)

    def requeue(self, task_id: str, reason: str) -> None:
        def put_back(data: dict[str, Any]) -> None:
            task = self._require_task(data, task_id)
            task.update(status="queued", updated_at=utc_iso(), error=reason)

        self._mutate(put_back)

    @staticmethod
    def _require_task(data: dict[str, Any], task_id: str) -> dict[str, Any]:
        for task in data["tasks"]:
            if task.get("id") == task_id:
                return task
        raise KeyError(f"Tarea inexistente: {task_id}")
