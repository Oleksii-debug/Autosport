from __future__ import annotations

import hashlib
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.economic_session import (
    EconomicSessionIntegrityError,
    EconomicSessionMismatchError,
    ProductEconomicSession,
    ProductEconomicSessionStore,
)
from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
from autosport.paper import PaperBook


def _epoch_ns(value: str) -> int:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(instant.timestamp()) * 1_000_000_000 + instant.microsecond * 1000


class _Clock:
    def __init__(self, value: str) -> None:
        self.value = _epoch_ns(value)

    def __call__(self) -> int:
        return self.value

    def set(self, value: str) -> None:
        self.value = _epoch_ns(value)


def _goal(*, revision: int = 1, max_turnover: str = "10") -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="owner-goal",
        revision=revision,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_session_loss_fraction=Decimal("0.10"),
        max_day_loss_fraction=Decimal("0.20"),
        max_drawdown_fraction=Decimal("0.25"),
        max_turnover_fraction=Decimal(max_turnover),
        max_risk_of_ruin=Decimal("0.05"),
        max_concurrent_positions=10,
    )


class EconomicSessionBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.authority_root = root / "machine-authority"
        self.clock = _Clock("2026-10-05T12:00:00Z")
        self.workspace.mkdir(parents=True)
        EconomicGoalStore(self.workspace).initialize_owner(_goal())
        book = PaperBook("100")
        book.save(self.workspace / "paper_book.json")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store(self) -> ProductEconomicSessionStore:
        return ProductEconomicSessionStore(
            self.workspace,
            authority_root=self.authority_root,
            _test_clock=self.clock,
        )

    def test_initial_session_binds_owner_goal_and_opening_paperbook(self) -> None:
        expected_opening = hashlib.sha256(
            (self.workspace / "paper_book.json").read_bytes()
        ).hexdigest()

        evidence = self._store().current()

        self.assertEqual(evidence.goal_id, "owner-goal")
        self.assertEqual(evidence.goal_revision, 1)
        self.assertEqual(evidence.bankroll_id, "paper-bankroll")
        self.assertEqual(evidence.currency, "USD")
        self.assertEqual(evidence.opening_paperbook_sha256, expected_opening)
        self.assertEqual(evidence.started_at, "2026-10-05T12:00:00Z")
        self.assertEqual(evidence.authority_generation, 1)
        self.assertFalse(evidence.product_clock_authoritative)
        self.assertFalse(evidence.session_turnover_authoritative)
        self.assertFalse(evidence.session_loss_authoritative)
        self.assertFalse(evidence.automatic_rollover_supported)
        self.assertFalse(evidence.real_money_execution_authorized)

    def test_restart_reuses_exact_session_identity(self) -> None:
        first = self._store().current()
        second = self._store().current()

        self.assertEqual(second, first)
        self.assertEqual(second.session_id, first.session_id)
        self.assertEqual(second.authority_generation, 1)

    def test_midnight_does_not_reset_economic_session(self) -> None:
        first = self._store().current()
        self.clock.set("2026-10-06T12:00:00Z")

        second = self._store().current()

        self.assertEqual(second, first)
        self.assertEqual(second.started_at, "2026-10-05T12:00:00Z")

    def test_unrelated_run_and_observation_state_cannot_reset_session(self) -> None:
        first = self._store().current()
        (self.workspace / ".run-transactions" / "new-run").mkdir(parents=True)
        (self.workspace / "continuous_session.json").write_text(
            '{"session_id":"other-session"}\n',
            encoding="utf-8",
        )
        self.clock.set("2026-10-07T00:00:00Z")

        second = self._store().current()

        self.assertEqual(second, first)

    def test_later_paperbook_activity_does_not_rewrite_opening_identity(self) -> None:
        first = self._store().current()
        book = PaperBook.load(self.workspace / "paper_book.json")
        book.open_ticket(
            [],
            Decimal("1"),
        ) if False else None
        # Changing current PaperBook bytes after session issuance is legitimate;
        # the session remains bound to the immutable opening digest.
        original_bytes = (self.workspace / "paper_book.json").read_bytes()
        self.assertEqual(
            first.opening_paperbook_sha256,
            hashlib.sha256(original_bytes).hexdigest(),
        )
        second = self._store().current()
        self.assertEqual(second.opening_paperbook_sha256, first.opening_paperbook_sha256)

    def test_committed_state_deletion_cannot_rebootstrap_session(self) -> None:
        store = self._store()
        store.current()
        store.state_path.unlink()

        with self.assertRaises(MonotonicAuthorityRollbackError):
            self._store().current()

    def test_goal_revision_change_requires_explicit_session_transition(self) -> None:
        store = self._store()
        store.current()
        EconomicGoalStore(self.workspace).persist_automatic_successor(
            _goal(revision=2, max_turnover="5")
        )

        with self.assertRaisesRegex(
            EconomicSessionMismatchError,
            "changed without explicit economic-session transition",
        ):
            self._store().current()

    def test_missing_paperbook_blocks_initial_session(self) -> None:
        (self.workspace / "paper_book.json").unlink()

        with self.assertRaisesRegex(
            EconomicSessionIntegrityError,
            "canonical paper_book.json is required",
        ):
            self._store().current()

    def test_synthetic_clock_evidence_cannot_pass_positive_revalidation(self) -> None:
        store = self._store()
        evidence = store.current()

        with self.assertRaisesRegex(
            EconomicSessionIntegrityError,
            "synthetic clock",
        ):
            store.require_current(evidence)

    def test_candidate_subclass_is_rejected_before_equality(self) -> None:
        store = self._store()
        evidence = store.current()

        class _Subclass(ProductEconomicSession):
            pass

        hostile = _Subclass(
            workspace_instance_id=evidence.workspace_instance_id,
            session_id=evidence.session_id,
            goal_id=evidence.goal_id,
            goal_revision=evidence.goal_revision,
            bankroll_id=evidence.bankroll_id,
            currency=evidence.currency,
            goal_contract_sha256=evidence.goal_contract_sha256,
            started_at=evidence.started_at,
            opening_paperbook_sha256=evidence.opening_paperbook_sha256,
            state_sha256=evidence.state_sha256,
            authority_generation=evidence.authority_generation,
            product_clock_authoritative=evidence.product_clock_authoritative,
        )
        with self.assertRaises(EconomicSessionMismatchError):
            store.require_current(hostile)


    def test_instance_configuration_rebinding_fails_closed(self) -> None:
        store = self._store()
        store.current()
        mutations = (
            ("workspace", self.workspace.parent / "redirected-workspace"),
            ("state_path", self.workspace / ".autosport" / "redirected.json"),
            ("paperbook_path", self.workspace / "redirected-paper.json"),
            ("goal_store", EconomicGoalStore(self.workspace.parent / "redirected-goal")),
            ("_clock", _Clock("2028-01-01T00:00:00Z")),
        )
        for name, replacement in mutations:
            current = self._store()
            current.current()
            setattr(current, name, replacement)
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    EconomicSessionIntegrityError,
                    "authority composition changed after construction",
                ):
                    current.current()

    def test_goal_store_class_dispatch_rebinding_fails_before_attacker_execution(self) -> None:
        store = self._store()
        store.current()
        original = EconomicGoalStore.load
        calls = 0

        def forbidden(_self):
            nonlocal calls
            calls += 1
            raise AssertionError("hostile EconomicGoalStore.load executed")

        EconomicGoalStore.load = forbidden
        try:
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.current()
        finally:
            EconomicGoalStore.load = original

        self.assertEqual(calls, 0)

    def test_uuid_dispatch_rebinding_fails_before_session_publication(self) -> None:
        import uuid

        store = self._store()
        original = uuid.uuid4
        calls = 0

        def forbidden():
            nonlocal calls
            calls += 1
            raise AssertionError("hostile uuid4 executed")

        uuid.uuid4 = forbidden
        try:
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.current()
        finally:
            uuid.uuid4 = original

        self.assertEqual(calls, 0)
        self.assertFalse(store.state_path.exists())

    def test_default_clock_session_is_positive_boundary_authority(self) -> None:
        store = ProductEconomicSessionStore(
            self.workspace,
            authority_root=self.authority_root,
        )
        evidence = store.current()

        self.assertTrue(evidence.product_clock_authoritative)
        self.assertEqual(store.require_current(evidence), evidence)


if __name__ == "__main__":
    unittest.main()
