from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import autosport.risk_day_window as day_window
from autosport.integrity import atomic_write_json
from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
from autosport.risk_day_window import (
    ProductDayRiskWindow,
    ProductDayRiskWindowStore,
    RiskDayWindowClockRollbackError,
    RiskDayWindowIntegrityError,
)


def _epoch_ns(value: str) -> int:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    seconds = int(instant.timestamp())
    return seconds * 1_000_000_000 + instant.microsecond * 1000


class _FakeClock:
    def __init__(self, value: str) -> None:
        self.value = _epoch_ns(value)

    def __call__(self) -> int:
        return self.value

    def set(self, value: str) -> None:
        self.value = _epoch_ns(value)


class ProductDayRiskWindowStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.authority_root = root / "machine-authority"
        self.clock = _FakeClock("2026-09-23T12:30:00Z")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store(self) -> ProductDayRiskWindowStore:
        return ProductDayRiskWindowStore(
            self.workspace,
            authority_root=self.authority_root,
            _test_clock=self.clock,
        )

    def test_issues_complete_utc_day_without_headroom_authority(self) -> None:
        evidence = self._store().current()

        self.assertEqual(evidence.day_key, "2026-09-23")
        self.assertEqual(evidence.window_start, "2026-09-23T00:00:00Z")
        self.assertEqual(
            evidence.window_end_exclusive,
            "2026-09-24T00:00:00Z",
        )
        self.assertEqual(evidence.timezone, "UTC")
        self.assertFalse(evidence.product_clock_authoritative)
        self.assertFalse(evidence.turnover_headroom_authoritative)
        self.assertFalse(evidence.session_boundary_authoritative)
        self.assertFalse(evidence.real_money_execution_authorized)

    def test_same_day_restart_reuses_exact_durable_authority(self) -> None:
        first = self._store().current()
        second = self._store().current()

        self.assertEqual(second, first)
        self.assertEqual(second.authority_generation, 1)

    def test_next_utc_day_advances_once_and_restart_is_idempotent(self) -> None:
        first = self._store().current()
        self.clock.set("2026-09-24T00:00:01Z")

        second = self._store().current()
        restarted = self._store().current()

        self.assertEqual(first.day_key, "2026-09-23")
        self.assertEqual(second.day_key, "2026-09-24")
        self.assertEqual(second.window_start, "2026-09-24T00:00:00Z")
        self.assertEqual(
            second.window_end_exclusive,
            "2026-09-25T00:00:00Z",
        )
        self.assertGreater(second.authority_generation, first.authority_generation)
        self.assertNotEqual(second.state_sha256, first.state_sha256)
        self.assertEqual(restarted, second)

    def test_clock_rollback_after_day_advance_fails_closed(self) -> None:
        self._store().current()
        self.clock.set("2026-09-24T00:00:01Z")
        advanced = self._store().current()
        state_before = self._store().state_path.read_bytes()

        self.clock.set("2026-09-23T23:59:59Z")
        with self.assertRaises(RiskDayWindowClockRollbackError):
            self._store().current()

        self.assertEqual(self._store().state_path.read_bytes(), state_before)
        self.assertEqual(advanced.day_key, "2026-09-24")

    def test_workspace_state_rollback_is_rejected(self) -> None:
        store = self._store()
        store.current()
        old_bytes = store.state_path.read_bytes()
        self.clock.set("2026-09-24T08:00:00Z")
        store.current()

        store.state_path.write_bytes(old_bytes)

        with self.assertRaises(MonotonicAuthorityRollbackError):
            self._store().current()

    def test_committed_state_deletion_cannot_rebootstrap_day_authority(self) -> None:
        store = self._store()
        store.current()
        store.state_path.unlink()

        with self.assertRaises(MonotonicAuthorityRollbackError):
            self._store().current()

    def test_narrowed_day_state_fails_before_it_can_mint_headroom(self) -> None:
        store = self._store()
        store.current()
        payload = json.loads(store.state_path.read_text(encoding="utf-8"))
        payload["window_start"] = "2026-09-23T01:00:00Z"
        atomic_write_json(store.state_path, payload)

        with self.assertRaises(RiskDayWindowIntegrityError):
            self._store().current()

    def test_value_contract_rejects_caller_narrowed_boundary(self) -> None:
        with self.assertRaises(RiskDayWindowIntegrityError):
            ProductDayRiskWindow(
                workspace_instance_id="workspace",
                day_key="2026-09-23",
                window_start="2026-09-23T01:00:00Z",
                window_end_exclusive="2026-09-24T00:00:00Z",
                state_sha256="0" * 64,
                authority_generation=1,
                product_clock_authoritative=True,
            )

    def test_injected_clock_cannot_pass_positive_revalidation(self) -> None:
        store = self._store()
        evidence = store.current()

        with self.assertRaisesRegex(
            RiskDayWindowIntegrityError,
            "test/synthetic clock",
        ):
            store.require_current(evidence)

    def test_prepare_without_publish_recovers_old_day_without_reset(self) -> None:
        store = self._store()
        current = store.current()
        future_payload = day_window._state_payload(
            current.workspace_instance_id,
            datetime(2026, 9, 24, tzinfo=timezone.utc).date(),
            transition_id="1" * 32,
        )
        future_sha = day_window._state_digest(future_payload)
        store._authority.prepare(
            tx_id=day_window._transaction_id(future_payload),
            observed_state_sha256=current.state_sha256,
            intended_state_sha256=future_sha,
            semantic_binding_sha256=day_window._semantic_binding(future_payload),
        )

        recovered = self._store().current()

        self.assertEqual(recovered.day_key, "2026-09-23")
        self.assertEqual(recovered.state_sha256, current.state_sha256)

    def test_publish_without_commit_is_recovered_exactly_once(self) -> None:
        store = self._store()
        current = store.current()
        future_payload = day_window._state_payload(
            current.workspace_instance_id,
            datetime(2026, 9, 24, tzinfo=timezone.utc).date(),
            transition_id="2" * 32,
        )
        future_sha = day_window._state_digest(future_payload)
        future_binding = day_window._semantic_binding(future_payload)
        future_tx = day_window._transaction_id(future_payload)
        store._authority.prepare(
            tx_id=future_tx,
            observed_state_sha256=current.state_sha256,
            intended_state_sha256=future_sha,
            semantic_binding_sha256=future_binding,
        )
        atomic_write_json(store.state_path, future_payload)
        self.clock.set("2026-09-24T04:00:00Z")

        recovered = self._store().current()
        restarted = self._store().current()

        self.assertEqual(recovered.day_key, "2026-09-24")
        self.assertEqual(recovered.state_sha256, future_sha)
        self.assertEqual(restarted, recovered)


class ProductClockBoundaryTests(unittest.TestCase):
    def test_default_clock_identity_is_product_authoritative(self) -> None:
        self.assertTrue(day_window._is_product_clock(day_window._PRODUCT_TIME_NS))
        self.assertFalse(day_window._is_product_clock(lambda: 0))


if __name__ == "__main__":
    unittest.main()
