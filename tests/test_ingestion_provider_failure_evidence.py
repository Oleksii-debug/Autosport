from __future__ import annotations

import unittest

from autosport.ingestion import IngestionEngine
from autosport.ingestion_health import SourceHealthState


class _FailingProvider:
    source_id = "source"

    def read_batch(self, max_items: int = 1000):
        raise RuntimeError("provider unavailable")


class _HealthProjectionFailure:
    def get(self, source_id: str) -> SourceHealthState:
        return SourceHealthState(source_id=source_id)

    def record_failure(self, source_id: str, *, now: str, error: BaseException):
        raise OSError("health failure publication failed")


class _UnusedBus:
    def publish_many(self, events):
        raise AssertionError("provider failure must occur before market persistence")


class ProviderFailureEvidenceTests(unittest.TestCase):
    def test_health_projection_failure_does_not_replace_provider_error(self) -> None:
        engine = IngestionEngine(
            _UnusedBus(),  # type: ignore[arg-type]
            health_store=_HealthProjectionFailure(),  # type: ignore[arg-type]
            clock=lambda: "2026-09-14T08:00:00+00:00",
        )

        with self.assertRaisesRegex(RuntimeError, "provider unavailable") as raised:
            engine.poll_once(_FailingProvider(), max_items=10)

        error = raised.exception
        self.assertIsInstance(error.__cause__, OSError)
        self.assertIn("health failure publication failed", str(error.__cause__))
        self.assertTrue(
            any(
                "source health failure persistence also failed" in note
                for note in getattr(error, "__notes__", ())
            )
        )


if __name__ == "__main__":
    unittest.main()
