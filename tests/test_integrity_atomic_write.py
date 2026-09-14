import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport import integrity


class AtomicWriteJsonTests(unittest.TestCase):
    def test_concurrent_writers_use_independent_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "state.json"
            barrier = threading.Barrier(2)
            original_dump = integrity.json.dump
            errors: list[BaseException] = []
            payloads = ({"writer": 1, "value": "alpha"}, {"writer": 2, "value": "beta"})

            def synchronized_dump(payload, handle, **kwargs) -> None:
                barrier.wait(timeout=5)
                original_dump(payload, handle, **kwargs)
                barrier.wait(timeout=5)

            def writer(payload: dict[str, object]) -> None:
                try:
                    integrity.atomic_write_json(destination, payload)
                except BaseException as exc:  # pragma: no cover - assertion captures worker failure
                    errors.append(exc)

            with patch.object(integrity.json, "dump", side_effect=synchronized_dump):
                threads = [threading.Thread(target=writer, args=(payload,)) for payload in payloads]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertIn(json.loads(destination.read_text(encoding="utf-8")), payloads)
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*.tmp")), [])

    def test_serialization_failure_preserves_destination_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "state.json"
            integrity.atomic_write_json(destination, {"stable": True})

            with self.assertRaises(TypeError):
                integrity.atomic_write_json(destination, {"invalid": object()})

            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), {"stable": True})
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
