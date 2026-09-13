from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx
from openai import RateLimitError

from autotrigger.openai_service import OpenAIResult
from autotrigger.queue_store import TaskStore
from autotrigger.worker import QueueWorker


class RateLimitedOnceService:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, prompt: str, model: str) -> OpenAIResult:
        self.calls += 1
        if self.calls == 1:
            request = httpx.Request("POST", "https://api.openai.com/v1/responses")
            response = httpx.Response(
                429,
                request=request,
                headers={"retry-after": "0.025", "x-request-id": "req_limited"},
                json={"error": {"message": "rate limited"}},
            )
            raise RateLimitError("rate limited", response=response, body=response.json())
        return OpenAIResult("resultado", "resp_ok", "req_ok")


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_reschedules_from_server_clock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "queue.json")
            service = RateLimitedOnceService()
            queued = store.enqueue("prompt", "test-model")
            worker = QueueWorker(store, service)  # type: ignore[arg-type]
            background = asyncio.create_task(worker.run())
            worker.wake()
            try:
                async with asyncio.timeout(2):
                    while store.get(queued["id"])["status"] != "completed":
                        await asyncio.sleep(0.005)
            finally:
                worker.stop()
                await background

            task = store.get(queued["id"])
            self.assertEqual(service.calls, 2)
            self.assertEqual(task["attempts"], 2)
            self.assertEqual(task["result"], "resultado")
            self.assertEqual(task["request_id"], "req_ok")


if __name__ == "__main__":
    unittest.main()
