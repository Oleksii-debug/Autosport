from __future__ import annotations

import unittest

from autosport.ingestion import IngestionEngine
from autosport.ingestion_health import SourceHealthState


class _FailingProvider:
    source_id = "source"

    def read_batch(self, max_items: int = 1000):
        raise RuntimeError("provider unavailable")


class _SingleReadIdentityFailingProvider:
    def __init__(self) -> None:
        self.source_id_reads = 0

    @property
    def source_id(self) -> str:
        self.source_id_reads += 1
        if self.source_id_reads > 1:
            raise RuntimeError("provider source_id was re-read")
        return "source"

    def read_batch(self, max_items: int = 1000):
        raise RuntimeError("provider unavailable")


class _HealthProjectionFailure:
    def get(self, source_id: str) -> SourceHealthState:
        return SourceHealthState(source_id=source_id)

    def record_failure(
        self,
        source_id: str,
        *,
        now: str,
        error: BaseException,
        failure_kind: str | None = None,
    ):
        raise OSError("health failure publication failed")


class _CapturingFailureHealth:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, BaseException, str | None]] = []

    def record_failure(
        self,
        source_id: str,
        *,
        now: str,
        error: BaseException,
        failure_kind: str | None = None,
    ):
        self.calls.append((source_id, now, error, failure_kind))
        return None


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

    def test_provider_failure_uses_once_bound_source_identity(self) -> None:
        provider = _SingleReadIdentityFailingProvider()
        health = _CapturingFailureHealth()
        engine = IngestionEngine(
            _UnusedBus(),  # type: ignore[arg-type]
            health_store=health,  # type: ignore[arg-type]
            clock=lambda: "2026-09-14T08:00:00+00:00",
        )

        with self.assertRaisesRegex(RuntimeError, "provider unavailable") as raised:
            engine.poll_once(provider, max_items=10)

        self.assertEqual(provider.source_id_reads, 1)
        self.assertEqual(len(health.calls), 1)
        source_id, now, recorded_error, failure_kind = health.calls[0]
        self.assertEqual(source_id, "source")
        self.assertEqual(now, "2026-09-14T08:00:00+00:00")
        self.assertIs(recorded_error, raised.exception)
        self.assertEqual(failure_kind, "provider_or_validation")


if __name__ == "__main__":
    unittest.main()
