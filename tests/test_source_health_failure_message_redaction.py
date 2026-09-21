import tempfile
import unittest
from pathlib import Path

from autosport.ingestion_health import SourceHealthStore


class _UnrenderableProviderError(RuntimeError):
    def __str__(self) -> str:
        raise AssertionError("provider error text must not be rendered for durable health evidence")


class SourceHealthFailureMessageRedactionTests(unittest.TestCase):
    def test_provider_controlled_failure_text_is_not_persisted(self) -> None:
        secret = "Bearer session-secret-very-sensitive"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            store = SourceHealthStore(path)

            state = store.record_failure(
                "source",
                now="2026-09-21T17:10:00+00:00",
                error=RuntimeError(f"upstream echoed {secret}"),
            )

            self.assertEqual(state.last_error, "RuntimeError: provider failure")
            persisted = path.read_text(encoding="utf-8")
            self.assertNotIn(secret, persisted)
            self.assertNotIn("upstream echoed", persisted)
            self.assertEqual(
                SourceHealthStore(path).get("source").last_error,
                "RuntimeError: provider failure",
            )

    def test_failure_evidence_never_stringifies_provider_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            store = SourceHealthStore(path)

            state = store.record_failure(
                "source",
                now="2026-09-21T17:10:01+00:00",
                error=_UnrenderableProviderError(),
            )

            self.assertEqual(
                state.last_error,
                "_UnrenderableProviderError: provider failure",
            )
            self.assertEqual(SourceHealthStore(path).get("source"), state)

    def test_non_exception_failure_input_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            store = SourceHealthStore(path)
            baseline = path.read_bytes()

            with self.assertRaisesRegex(TypeError, "error must be BaseException"):
                store.record_failure(
                    "source",
                    now="2026-09-21T17:10:02+00:00",
                    error="session-secret",  # type: ignore[arg-type]
                )

            self.assertEqual(path.read_bytes(), baseline)
            self.assertEqual(store.get("source").status, "unknown")

    def test_unsafe_exception_class_name_falls_back_without_echoing_it(self) -> None:
        unsafe_type = type("ProviderSecret", (RuntimeError,), {})
        unsafe_type.__name__ = "provider-secret-\N{SNOWMAN}"
        error = unsafe_type("session-secret")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            store = SourceHealthStore(path)

            state = store.record_failure(
                "source",
                now="2026-09-21T17:10:02+00:00",
                error=error,
            )

            self.assertEqual(state.last_error, "Exception: provider failure")
            persisted = path.read_text(encoding="utf-8")
            self.assertNotIn("session-secret", persisted)
            self.assertNotIn("provider-secret", persisted)


if __name__ == "__main__":
    unittest.main()
