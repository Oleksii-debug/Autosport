from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autosport.workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


class WorkspaceEconomicLockNoteFaultTests(unittest.TestCase):
    def test_acquire_preserves_primary_when_cleanup_error_cannot_be_stringified(self) -> None:
        class BrokenStringCloseError(OSError):
            def __str__(self) -> str:
                raise RuntimeError("broken cleanup formatter")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = WorkspaceEconomicLock(root)
            handle = mock.MagicMock()
            handle.tell.return_value = 1
            handle.close.side_effect = BrokenStringCloseError()
            primary = WorkspaceEconomicLockError(
                "another Autosport process owns the workspace economic-writer lock"
            )

            with (
                mock.patch.object(lock, "_open_lock_handle", return_value=handle),
                mock.patch.object(lock, "_validate_open_handle_identity"),
                mock.patch.object(WorkspaceEconomicLock, "_lock_handle", side_effect=primary),
            ):
                with self.assertRaises(WorkspaceEconomicLockError) as caught:
                    lock.acquire()

            self.assertIs(caught.exception, primary)
            notes = getattr(primary, "__notes__", ())
            self.assertTrue(
                any(
                    "cleaning up acquisition failure" in note
                    and "secondary exception details unavailable" in note
                    for note in notes
                ),
                f"fail-safe secondary evidence missing from primary notes: {notes!r}",
            )
            self.assertIs(lock._handle, handle)
            with self.assertRaisesRegex(WorkspaceEconomicLockError, "already held"):
                lock.acquire()

    def test_context_manager_preserves_primary_when_primary_rejects_notes(self) -> None:
        class BrokenNotePrimary(ValueError):
            def add_note(self, note: str) -> None:
                raise RuntimeError("note sink failed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = WorkspaceEconomicLock(root)
            primary = BrokenNotePrimary("primary economic failure")

            with mock.patch.object(
                WorkspaceEconomicLock,
                "_unlock_handle",
                side_effect=OSError("simulated unlock failure"),
            ):
                with self.assertRaises(BrokenNotePrimary) as caught:
                    with lock:
                        raise primary

            self.assertIs(caught.exception, primary)
            self.assertIsNone(lock._handle)

            # Close-after-unlock-failure proved the OS lock gone, so suppressing a
            # diagnostic note failure must not poison normal lock reuse.
            lock.acquire()
            lock.release()


if __name__ == "__main__":
    unittest.main()
