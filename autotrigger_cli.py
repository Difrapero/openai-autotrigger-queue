#!/usr/bin/env python3
"""Command-line interface for OpenAI AutoTrigger Queue."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from autotrigger.openai_service import OpenAIService
from autotrigger.queue_store import TaskStore
from autotrigger.runner import run_queue


DEFAULT_QUEUE = Path(__file__).resolve().with_name("cola_tareas.json")
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cola persistente de OpenAI con cooldown detectado desde HTTP 429."
    )
    parser.add_argument(
        "--queue",
        type=Path,
        default=Path(os.getenv("AUTOTRIGGER_QUEUE_PATH", DEFAULT_QUEUE)),
        help="Ruta de cola_tareas.json.",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"Modelo al añadir una tarea (predeterminado: {DEFAULT_MODEL})."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--add", metavar="PROMPT", help="Añade un prompt a la cola.")
    action.add_argument("--list", action="store_true", help="Muestra las tareas y el cooldown global.")
    action.add_argument("--run", action="store_true", help="Procesa hasta vaciar las tareas pendientes.")
    return parser


def _print_queue(store: TaskStore) -> None:
    cooldown = store.cooldown()
    if cooldown:
        print(
            f"Cooldown global: hasta {cooldown['resume_at']} "
            f"({cooldown.get('source', 'origen desconocido')})"
        )
    tasks = store.list_tasks()
    if not tasks:
        print("La cola está vacía.")
        return
    for task in tasks:
        schedule = f" | reanuda={task['resume_at']}" if task.get("resume_at") else ""
        error = f" | error={task['error']}" if task.get("error") else ""
        print(
            f"{task['id']} | {task['status']} | intentos={task['attempts']} "
            f"| modelo={task['model']}{schedule}{error}\n  {task['prompt']}"
        )


def main() -> int:
    args = build_parser().parse_args()
    store = TaskStore(args.queue)
    if args.add is not None:
        task = store.enqueue(args.add, args.model)
        print(f"Tarea añadida: {task['id']}")
        return 0
    if args.list:
        _print_queue(store)
        return 0
    try:
        run_queue(store, OpenAIService())
    except KeyboardInterrupt:
        print("\nEjecución interrumpida; la tarea queda recuperable.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
