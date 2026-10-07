import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport import integrity


class EnsureDurableFileTests(unittest.TestCase):
    def test_creates_missing_file_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "decisions.jsonl"

            integrity.ensure_durable_file(destination)

            self.assertTrue(destination.is_file())
            self.assertEqual(destination.read_bytes(), b"")

    def test_missing_file_creation_syncs_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "decisions.jsonl"

            with patch.object(
                integrity,
                "_sync_parent_directory",
                wraps=integrity._sync_parent_directory,
            ) as sync_parent:
                integrity.ensure_durable_file(destination)

            sync_parent.assert_called_once_with(destination)

    def test_existing_file_durability_does_not_require_namespace_republication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "decisions.jsonl"
            destination.write_bytes(b"existing\n")

            with patch.object(
                integrity,
                "_sync_parent_directory",
                wraps=integrity._sync_parent_directory,
            ) as sync_parent:
                integrity.ensure_durable_file(destination)

            sync_parent.assert_not_called()
            self.assertEqual(destination.read_bytes(), b"existing\n")

    def test_concurrent_create_before_open_preserves_new_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "decisions.jsonl"
            original_open = Path.open
            injected = False

            def racing_open(path: Path, mode: str = "r", *args, **kwargs):
                nonlocal injected
                if path == destination and not injected:
                    injected = True
                    with original_open(path, "wb") as competitor:
                        competitor.write(b"concurrent-state\n")
                return original_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", autospec=True, side_effect=racing_open):
                integrity.ensure_durable_file(destination)

            self.assertTrue(injected)
            self.assertEqual(destination.read_bytes(), b"concurrent-state\n")


class AtomicWriteJsonTests(unittest.TestCase):
    def test_concurrent_writers_serialize_publication_after_independent_temp_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "state.json"
            barrier = threading.Barrier(2)
            original_dump = integrity.json.dump
            original_replace = integrity.os.replace
            replace_active = threading.Lock()
            errors: list[BaseException] = []
            payloads = ({"writer": 1, "value": "alpha"}, {"writer": 2, "value": "beta"})

            def synchronized_dump(payload, handle, **kwargs) -> None:
                barrier.wait(timeout=5)
                original_dump(payload, handle, **kwargs)
                barrier.wait(timeout=5)

            def collision_sensitive_replace(source, target) -> None:
                if not replace_active.acquire(blocking=False):
                    raise PermissionError(13, "Access is denied")
                try:
                    # Model the Windows same-destination publication window that
                    # exposed the post-#192 race on Python 3.12 CI.
                    time.sleep(0.05)
                    original_replace(source, target)
                finally:
                    replace_active.release()

            def writer(payload: dict[str, object]) -> None:
                try:
                    integrity.atomic_write_json(destination, payload)
                except BaseException as exc:  # pragma: no cover - assertion captures worker failure
                    errors.append(exc)

            with (
                patch.object(integrity.json, "dump", side_effect=synchronized_dump),
                patch.object(integrity.os, "replace", side_effect=collision_sensitive_replace),
            ):
                threads = [threading.Thread(target=writer, args=(payload,)) for payload in payloads]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertIn(json.loads(destination.read_text(encoding="utf-8")), payloads)
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*.tmp")), [])

    def test_atomic_publication_syncs_parent_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "state.json"
            calls: list[str] = []
            original_replace = integrity.os.replace
            original_sync = integrity._sync_parent_directory

            def observed_replace(source, target) -> None:
                original_replace(source, target)
                calls.append("replace")

            def observed_sync(path: Path) -> None:
                calls.append("sync-parent")
                original_sync(path)

            with (
                patch.object(integrity.os, "replace", side_effect=observed_replace),
                patch.object(integrity, "_sync_parent_directory", side_effect=observed_sync),
            ):
                integrity.atomic_write_json(destination, {"value": 1})

            self.assertEqual(calls, ["replace", "sync-parent"])
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), {"value": 1})

    def test_parent_directory_sync_failure_is_not_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "state.json"

            with patch.object(
                integrity,
                "_sync_parent_directory",
                side_effect=RuntimeError("directory fsync failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "directory fsync failed"):
                    integrity.atomic_write_json(destination, {"value": 1})

            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), {"value": 1})
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*.tmp")), [])

    def test_serialization_failure_preserves_destination_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "state.json"
            integrity.atomic_write_json(destination, {"stable": True})

            with self.assertRaises(TypeError):
                integrity.atomic_write_json(destination, {"invalid": object()})

            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), {"stable": True})
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*.tmp")), [])

    def test_non_finite_number_preserves_destination_and_never_publishes_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "state.json"
            integrity.atomic_write_json(destination, {"stable": True})
            stable_bytes = destination.read_bytes()

            for invalid in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(value=repr(invalid)):
                    with patch.object(integrity.os, "replace", wraps=integrity.os.replace) as replace:
                        with self.assertRaises(ValueError):
                            integrity.atomic_write_json(
                                destination,
                                {"nested": {"invalid": invalid}},
                            )
                        replace.assert_not_called()

                    self.assertEqual(destination.read_bytes(), stable_bytes)
                    self.assertEqual(
                        list(destination.parent.glob(f".{destination.name}.*.tmp")),
                        [],
                    )


if __name__ == "__main__":
    unittest.main()
