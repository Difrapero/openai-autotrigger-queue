"""Non-blocking asynchronous scheduler for FastAPI."""

from __future__ import annotations

import asyncio
import logging
import time

from openai import RateLimitError

from .cooldown import detect_cooldown
from .openai_service import OpenAIService
from .queue_store import TaskStore


logger = logging.getLogger(__name__)


class QueueWorker:
    def __init__(self, store: TaskStore, service: OpenAIService) -> None:
        self.store = store
        self.service = service
        self._wake = asyncio.Event()
        self._stopping = False

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    async def run(self) -> None:
        recovered = self.store.recover_in_progress()
        if recovered:
            logger.info("Recovered %d interrupted task(s)", recovered)

        while not self._stopping:
            # Clear first so an enqueue occurring after the checks below remains
            # observable and wakes wait().
            self._wake.clear()
            task = self.store.claim_next()
            if task is not None:
                await self._process(task)
                continue

            delay = self.store.seconds_until_ready()
            try:
                if delay is None:
                    await self._wake.wait()
                elif delay > 0:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                # delay == 0 simply loops and claims the ready task.
            except TimeoutError:
                pass

    async def _process(self, task: dict[str, object]) -> None:
        task_id = str(task["id"])
        try:
            # The SDK is synchronous; a thread keeps the ASGI event loop free.
            result = await asyncio.to_thread(
                self.service.execute, str(task["prompt"]), str(task["model"])
            )
        except RateLimitError as exc:
            now = time.time()
            cooldown = detect_cooldown(exc, now=now)
            request_id = getattr(exc, "request_id", None)
            if cooldown is None:
                self.store.fail(
                    task_id,
                    "HTTP 429 sin tiempo de reintento proporcionado por OpenAI; no se inventó un delay.",
                    request_id,
                )
                logger.error("Task %s received a 429 without a usable server clock", task_id)
                return
            until = now + cooldown.seconds
            self.store.defer(task_id, until, cooldown.source, str(exc), request_id)
            logger.warning(
                "Task %s rate-limited; retrying in %.3fs from %s",
                task_id,
                cooldown.seconds,
                cooldown.source,
            )
        except asyncio.CancelledError:
            self.store.requeue(task_id, "Worker detenido durante el procesamiento")
            raise
        except Exception as exc:  # noqa: BLE001 - persist per-task failures
            self.store.fail(task_id, f"{type(exc).__name__}: {exc}", getattr(exc, "request_id", None))
            logger.exception("Task %s failed", task_id)
        else:
            self.store.complete(task_id, result.text, result.response_id, result.request_id)
