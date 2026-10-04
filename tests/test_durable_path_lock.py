import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

from autosport import integrity
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


    def test_symlinked_sidecar_fails_before_mutating_target(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "authority.json"
            target = root / "external-lock-target.bin"
            lock_path = root / ".authority.json.lock"
            target.write_bytes(b"")
            try:
                lock_path.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"file symlink creation unavailable: {exc}")

            with self.assertRaises(OSError):
                with durable_path_lock(destination):
                    self.fail("aliased durable lock must never be acquired")

            self.assertTrue(lock_path.is_symlink())
            self.assertEqual(target.read_bytes(), b"")

    def test_hard_linked_sidecar_fails_before_mutating_shared_inode(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "authority.json"
            target = root / "external-lock-target.bin"
            lock_path = root / ".authority.json.lock"
            target.write_bytes(b"")
            try:
                os.link(target, lock_path)
            except OSError as exc:
                self.skipTest(f"hard links unavailable: {exc}")

            with self.assertRaises(OSError):
                with durable_path_lock(destination):
                    self.fail("hard-linked durable lock must never be acquired")

            self.assertEqual(target.read_bytes(), b"")
            self.assertEqual(os.stat(target).st_nlink, 2)

    def test_path_replacement_during_prelock_identity_check_fails_before_sentinel(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "authority.json"
            lock_path = root / ".authority.json.lock"
            replacement = root / "replacement-lock.bin"
            lock_path.write_bytes(b"")
            replacement.write_bytes(b"")

            real_open = integrity._open_read_only_no_follow_descriptor
            calls = 0

            def redirect_final_verification(path: Path) -> int:
                nonlocal calls
                if Path(path) == lock_path:
                    calls += 1
                    if calls == 2:
                        return real_open(replacement)
                return real_open(path)

            with patch.object(
                integrity,
                "_open_read_only_no_follow_descriptor",
                side_effect=redirect_final_verification,
            ):
                with self.assertRaises(OSError):
                    with durable_path_lock(destination):
                        self.fail("replaced durable lock must never be acquired")

            self.assertEqual(calls, 2)
            self.assertEqual(lock_path.read_bytes(), b"")
            self.assertEqual(replacement.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
