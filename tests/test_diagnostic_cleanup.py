from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.diagnostic as diagnostic


class _TrackingStore:
    last_instance: "_TrackingStore | None" = None

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.closed = False
        _TrackingStore.last_instance = self

    def append(self, event) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


class _CloseFailingStore(diagnostic.SQLiteMarketStore):
    def close(self) -> None:
        super().close()
        raise OSError("diagnostic store close failed")


class _BrokenStringError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("exception stringification failed")


class _SurrogateStringError(RuntimeError):
    def __str__(self) -> str:
        return chr(0xD800)


class _BrokenStringCloseFailingStore(diagnostic.SQLiteMarketStore):
    def close(self) -> None:
        super().close()
        raise _BrokenStringError()


class _SurrogateStringCloseFailingStore(diagnostic.SQLiteMarketStore):
    def close(self) -> None:
        super().close()
        raise _SurrogateStringError()


class _FailingReplayEngine:
    def __init__(self, events, firewall) -> None:
        del events, firewall

    def run(self, sink, *, run_id: str):
        del sink, run_id
        raise RuntimeError("diagnostic replay failed")


class _BrokenStringReplayEngine:
    def __init__(self, events, firewall) -> None:
        del events, firewall

    def run(self, sink, *, run_id: str):
        del sink, run_id
        raise _BrokenStringError()


class _SurrogateStringReplayEngine:
    def __init__(self, events, firewall) -> None:
        del events, firewall

    def run(self, sink, *, run_id: str):
        del sink, run_id
        raise _SurrogateStringError()


class DiagnosticCleanupTests(unittest.TestCase):
    def test_failure_closes_temporary_market_store_before_reporting(self) -> None:
        _TrackingStore.last_instance = None
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "diagnostic.json"
            with (
                patch.object(diagnostic, "SQLiteMarketStore", _TrackingStore),
                patch.object(diagnostic, "ReplayEngine", _FailingReplayEngine),
            ):
                exit_code = diagnostic.run_machine_diagnostic(output_path)

            self.assertEqual(exit_code, 1)
            self.assertIsNotNone(_TrackingStore.last_instance)
            assert _TrackingStore.last_instance is not None
            self.assertTrue(_TrackingStore.last_instance.closed)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual(payload["error"], "RuntimeError: diagnostic replay failed")
            self.assertNotIn("error_notes", payload)
            self.assertFalse(payload["real_money_execution"])
            self.assertFalse(payload["human_tested"])
            self.assertFalse(payload["nvda_verified"])

    def test_primary_diagnostic_failure_is_not_masked_by_close_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "diagnostic.json"
            with (
                patch.object(diagnostic, "SQLiteMarketStore", _CloseFailingStore),
                patch.object(diagnostic, "ReplayEngine", _FailingReplayEngine),
            ):
                exit_code = diagnostic.run_machine_diagnostic(output_path)

            self.assertEqual(exit_code, 1)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual(payload["error"], "RuntimeError: diagnostic replay failed")
            self.assertEqual(
                payload["error_notes"],
                [
                    "SQLiteMarketStore.close() also failed while preserving the primary diagnostic failure: "
                    "OSError: diagnostic store close failed"
                ],
            )

    def test_primary_failure_with_broken_stringification_still_publishes_fail_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "diagnostic.json"
            with patch.object(diagnostic, "ReplayEngine", _BrokenStringReplayEngine):
                exit_code = diagnostic.run_machine_diagnostic(output_path)

            self.assertEqual(exit_code, 1)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual(payload["error"], "RuntimeError: exception details unavailable")
            self.assertNotIn("error_notes", payload)

    def test_cleanup_failure_with_broken_stringification_preserves_primary_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "diagnostic.json"
            with (
                patch.object(diagnostic, "SQLiteMarketStore", _BrokenStringCloseFailingStore),
                patch.object(diagnostic, "ReplayEngine", _FailingReplayEngine),
            ):
                exit_code = diagnostic.run_machine_diagnostic(output_path)

            self.assertEqual(exit_code, 1)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual(payload["error"], "RuntimeError: diagnostic replay failed")
            self.assertEqual(
                payload["error_notes"],
                [
                    "SQLiteMarketStore.close() also failed while preserving the primary diagnostic failure: "
                    "RuntimeError: exception details unavailable"
                ],
            )

    def test_primary_failure_with_surrogate_stringification_publishes_utf8_fail_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "diagnostic.json"
            with patch.object(diagnostic, "ReplayEngine", _SurrogateStringReplayEngine):
                exit_code = diagnostic.run_machine_diagnostic(output_path)

            self.assertEqual(exit_code, 1)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual(payload["error"], "RuntimeError: �")
            self.assertNotIn("error_notes", payload)

    def test_cleanup_failure_with_surrogate_stringification_preserves_primary_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "diagnostic.json"
            with (
                patch.object(diagnostic, "SQLiteMarketStore", _SurrogateStringCloseFailingStore),
                patch.object(diagnostic, "ReplayEngine", _FailingReplayEngine),
            ):
                exit_code = diagnostic.run_machine_diagnostic(output_path)

            self.assertEqual(exit_code, 1)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual(payload["error"], "RuntimeError: diagnostic replay failed")
            self.assertEqual(
                payload["error_notes"],
                [
                    "SQLiteMarketStore.close() also failed while preserving the primary diagnostic failure: "
                    "RuntimeError: �"
                ],
            )

    def test_close_failure_after_success_is_reported_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "diagnostic.json"
            with patch.object(diagnostic, "SQLiteMarketStore", _CloseFailingStore):
                exit_code = diagnostic.run_machine_diagnostic(output_path)

            self.assertEqual(exit_code, 1)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual(payload["error"], "OSError: diagnostic store close failed")
            self.assertNotIn("error_notes", payload)
            self.assertFalse(payload["real_money_execution"])
            self.assertFalse(payload["human_tested"])
            self.assertFalse(payload["nvda_verified"])

    def test_pass_publication_failure_is_converted_to_atomic_fail_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "diagnostic.json"
            writes: list[dict[str, object]] = []

            def flaky_atomic_write(path: str | Path, payload: dict[str, object]) -> None:
                writes.append(dict(payload))
                if len(writes) == 1:
                    raise OSError("diagnostic evidence publication failed")
                Path(path).write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

            with patch.object(diagnostic, "atomic_write_json", side_effect=flaky_atomic_write):
                exit_code = diagnostic.run_machine_diagnostic(output_path)

            self.assertEqual(exit_code, 1)
            self.assertEqual([payload["status"] for payload in writes], ["PASS", "FAIL"])
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual(payload["error"], "OSError: diagnostic evidence publication failed")
            self.assertNotIn("error_notes", payload)
            self.assertFalse(payload["real_money_execution"])
            self.assertFalse(payload["human_tested"])
            self.assertFalse(payload["nvda_verified"])


if __name__ == "__main__":
    unittest.main()
