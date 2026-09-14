import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.integrity import atomic_write_json


class AtomicWriteJsonTests(unittest.TestCase):
    def test_concurrent_writers_use_independent_temporary_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "state.json"
            payloads = [
                {"writer": "alpha", "values": list(range(100))},
                {"writer": "beta", "values": list(range(100, 200))},
            ]
            replace_barrier = threading.Barrier(2)
            observed_sources: list[Path] = []
            errors: list[BaseException] = []
            lock = threading.Lock()
            real_replace = os.replace

            def synchronized_replace(source, target):
                with lock:
                    observed_sources.append(Path(source))
                replace_barrier.wait(timeout=5)
                return real_replace(source, target)

            def writer(payload):
                try:
                    atomic_write_json(destination, payload)
                except BaseException as exc:  # test captures worker failures for the main thread
                    with lock:
                        errors.append(exc)

            with patch("autosport.integrity.os.replace", side_effect=synchronized_replace):
                threads = [threading.Thread(target=writer, args=(payload,)) for payload in payloads]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(observed_sources), 2)
            self.assertEqual(len(set(observed_sources)), 2)
            self.assertIn(json.loads(destination.read_text(encoding="utf-8")), payloads)
            self.assertEqual([item for item in destination.parent.iterdir() if item != destination], [])

    def test_serialization_failure_preserves_destination_and_cleans_temporary_file(self):
        class NotJsonSerializable:
            pass

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "state.json"
            original = '{\n  "stable": true\n}\n'
            destination.write_text(original, encoding="utf-8", newline="\n")

            with self.assertRaises(TypeError):
                atomic_write_json(destination, {"bad": NotJsonSerializable()})

            self.assertEqual(destination.read_text(encoding="utf-8"), original)
            self.assertEqual([item for item in destination.parent.iterdir() if item != destination], [])


if __name__ == "__main__":
    unittest.main()
