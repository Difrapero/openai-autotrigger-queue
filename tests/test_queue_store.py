from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autotrigger.queue_store import TaskStore


class TaskStoreTests(unittest.TestCase):
    def test_lifecycle_and_global_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "cola_tareas.json")
            first = store.enqueue("uno", "test-model")
            second = store.enqueue("dos", "test-model")
            claimed = store.claim_next(now=1000.0)
            self.assertEqual(claimed["id"], first["id"])

            store.defer(first["id"], 1014.25, "header:retry-after", "429")
            self.assertIsNone(store.claim_next(now=1001.0))
            self.assertAlmostEqual(store.seconds_until_ready(now=1001.0), 13.25)

            claimed_after = store.claim_next(now=1014.25)
            self.assertEqual(claimed_after["id"], first["id"])
            store.complete(first["id"], "ok", "resp_1", "req_1")
            self.assertEqual(store.claim_next(now=1014.25)["id"], second["id"])


if __name__ == "__main__":
    unittest.main()
