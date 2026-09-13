"""Synchronous queue runner used by the CLI."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

from openai import RateLimitError

from .cooldown import detect_cooldown
from .openai_service import OpenAIService
from .queue_store import TaskStore


def local_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="milliseconds")


def run_queue(
    store: TaskStore,
    service: OpenAIService,
    emit: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    store.recover_in_progress()
    while True:
        task = store.claim_next()
        if task is None:
            delay = store.seconds_until_ready()
            if delay is None:
                emit("Cola terminada: no quedan tareas pendientes.")
                return
            if delay > 0:
                cooldown = store.cooldown()
                until = time.time() + delay
                source = cooldown.get("source", "tarea programada") if cooldown else "tarea programada"
                emit(
                    f"Cooldown activo ({source}). Reanudación: {local_time(until)} "
                    f"(en {delay:.3f} s)."
                )
                sleep(delay)
            continue

        emit(f"Procesando {task['id']} (intento {task['attempts']}, modelo {task['model']})…")
        try:
            result = service.execute(task["prompt"], task["model"])
        except RateLimitError as exc:
            now = time.time()
            cooldown = detect_cooldown(exc, now=now)
            request_id = getattr(exc, "request_id", None)
            if cooldown is None:
                store.fail(
                    task["id"],
                    "HTTP 429 sin tiempo de reintento proporcionado por OpenAI; no se inventó un delay.",
                    request_id,
                )
                emit(f"Tarea {task['id']} fallida: el 429 no contiene un reloj utilizable.")
                continue
            resume_at = now + cooldown.seconds
            store.defer(task["id"], resume_at, cooldown.source, str(exc), request_id)
            emit(
                f"Límite detectado en {cooldown.source}={cooldown.raw_value!r}. "
                f"Reanudación exacta: {local_time(resume_at)} "
                f"(espera {cooldown.seconds:.3f} s)."
            )
            sleep(cooldown.seconds)
        except KeyboardInterrupt:
            store.requeue(task["id"], "Interrumpida por el usuario")
            raise
        except Exception as exc:  # noqa: BLE001 - task failures belong in queue state
            store.fail(task["id"], f"{type(exc).__name__}: {exc}", getattr(exc, "request_id", None))
            emit(f"Tarea {task['id']} fallida: {type(exc).__name__}: {exc}")
        else:
            store.complete(task["id"], result.text, result.response_id, result.request_id)
            emit(f"Tarea {task['id']} completada.")
