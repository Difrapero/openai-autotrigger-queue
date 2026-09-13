"""FastAPI application for OpenAI AutoTrigger Queue."""

from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from autotrigger.openai_service import OpenAIService
from autotrigger.queue_store import TaskStore
from autotrigger.worker import QueueWorker


DEFAULT_QUEUE = Path(__file__).resolve().with_name("cola_tareas.json")
QUEUE_PATH = Path(os.getenv("AUTOTRIGGER_QUEUE_PATH", DEFAULT_QUEUE))
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
store = TaskStore(QUEUE_PATH)


class EnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=100_000)
    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=100)


class EnqueueResponse(BaseModel):
    task_id: str
    status: str
    status_url: str


class TaskResponse(BaseModel):
    id: str
    prompt: str
    model: str
    status: str
    created_at: str
    updated_at: str
    attempts: int
    next_attempt_at: float | None = None
    resume_at: str | None = None
    cooldown_source: str | None = None
    request_id: str | None = None
    response_id: str | None = None
    result: str | None = None
    error: str | None = None


def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
    """Require a bearer token only when AUTOTRIGGER_API_KEY is configured."""

    expected = os.getenv("AUTOTRIGGER_API_KEY")
    if not expected:
        return
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    worker = QueueWorker(store, OpenAIService())
    app.state.worker = worker
    background = asyncio.create_task(worker.run(), name="autotrigger-worker")
    try:
        yield
    finally:
        # Let an in-flight synchronous SDK call finish before shutdown. Cancelling
        # asyncio.to_thread would not stop its underlying thread and could cause
        # the same prompt to be sent twice after a restart.
        worker.stop()
        await background


app = FastAPI(
    title="OpenAI AutoTrigger Queue",
    version="1.0.0",
    description="Cola de tareas con reanudación dinámica basada en el reloj HTTP 429 de OpenAI.",
    lifespan=lifespan,
)


@app.post(
    "/enqueue",
    response_model=EnqueueResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(authorize)],
)
async def enqueue(payload: EnqueueRequest, request: Request) -> EnqueueResponse:
    task = store.enqueue(payload.prompt, payload.model)
    request.app.state.worker.wake()
    return EnqueueResponse(
        task_id=task["id"],
        status=task["status"],
        status_url=str(request.url_for("task_status", task_id=task["id"])),
    )


@app.get(
    "/status/{task_id}",
    response_model=TaskResponse,
    dependencies=[Depends(authorize)],
)
async def task_status(task_id: str) -> dict[str, Any]:
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tarea no encontrada")
    return task


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}
