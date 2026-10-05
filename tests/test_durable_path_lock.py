import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest

from autosport.integrity import atomic_write_json, durable_path_lock


class DurablePathLockTests(unittest.TestCase):
    def test_outer_fence_blocks_competing_publish_and_is_reentrant(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "authority.json"
            atomic_write_json(path, {"source": "G"})

            attempted = Event()
            finished = Event()

            def competing_writer() -> None:
                attempted.set()
                atomic_write_json(path, {"source": "G2"})
                finished.set()

            with durable_path_lock(path):
                writer = Thread(target=competing_writer, daemon=True)
                writer.start()
                self.assertTrue(attempted.wait(timeout=1.0))
                self.assertFalse(finished.wait(timeout=0.05))
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8")),
                    {"source": "G"},
                )

                # Nested atomic publication is the path used by bounded sport
                # memory while it owns the canonical opponent authority fence.
                atomic_write_json(
                    path,
                    {"source": "G", "derived_snapshot": "A"},
                )
                self.assertFalse(finished.is_set())

            writer.join(timeout=2.0)
            self.assertFalse(writer.is_alive())
            self.assertTrue(finished.is_set())
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"source": "G2"},
            )


if __name__ == "__main__":
    unittest.main()
