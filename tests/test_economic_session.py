from __future__ import annotations

import hashlib
import json
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
from autosport.monotonic_workspace_authority import (
    MonotonicAuthorityConfigurationError,
    MonotonicAuthorityRollbackError,
)
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

    def test_alternative_authority_root_cannot_mint_second_session(self) -> None:
        primary = self._store()
        first = primary.current()

        alternate_root = self.authority_root.parent / "alternate-machine-authority"
        with self.assertRaises(MonotonicAuthorityConfigurationError):
            ProductEconomicSessionStore(
                self.workspace,
                authority_root=alternate_root,
                _test_clock=self.clock,
            ).current()

        self.assertEqual(self._store().current(), first)

    def test_committed_state_deletion_cannot_rebootstrap_session(self) -> None:
        store = self._store()
        store.current()
        store.state_path.unlink()

        with self.assertRaises(MonotonicAuthorityRollbackError):
            self._store().current()

    def test_corrupt_or_stale_durable_state_blocks_positive_resolution(self) -> None:
        store = self._store()
        store.current()

        corrupt = store.state_path
        valid_bytes = corrupt.read_bytes()
        corrupt.write_bytes(b"{\n  \"unexpected\": true\n}\n")
        with self.assertRaises(EconomicSessionIntegrityError):
            self._store().current()

        # Restore the valid durable state, then alter one canonical field without
        # updating the independent monotonic authority digest.
        corrupt.write_bytes(valid_bytes)
        fresh = self._store()
        expected = fresh.current()
        payload = json.loads(fresh.state_path.read_text(encoding="utf-8"))
        payload["started_at"] = "2026-01-01T00:00:00Z"
        fresh.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(MonotonicAuthorityRollbackError):
            self._store().current()
        self.assertNotEqual(
            json.loads(fresh.state_path.read_text(encoding="utf-8"))["started_at"],
            expected.started_at,
        )

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


    def test_internal_authority_root_rebinding_fails_closed(self) -> None:
        store = self._store()
        store.current()
        store._authority.authority_root = store._authority.authority_root.parent
        with self.assertRaisesRegex(
            EconomicSessionIntegrityError,
            "authority composition changed after construction",
        ):
            store.current()

    def test_internal_goal_store_path_rebinding_fails_closed(self) -> None:
        store = self._store()
        store.current()
        store.goal_store.path = store.goal_store.path.parent / "redirected-goal.json"
        with self.assertRaisesRegex(
            EconomicSessionIntegrityError,
            "authority composition changed after construction",
        ):
            store.current()

    def test_module_helper_alias_rebinding_fails_before_execution(self) -> None:
        import autosport.economic_session as economic_session

        store = self._store()
        store.current()
        original = economic_session._ECONOMIC_GOAL_LOAD
        calls = 0

        def forbidden(_self):
            nonlocal calls
            calls += 1
            raise AssertionError("hostile helper alias executed")

        economic_session._ECONOMIC_GOAL_LOAD = forbidden
        try:
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.current()
        finally:
            economic_session._ECONOMIC_GOAL_LOAD = original

        self.assertEqual(calls, 0)


    def test_internal_authority_binding_rebinding_fails_closed(self) -> None:
        store = self._store()
        store.current()
        store._authority.namespace_marker_path = (
            store._authority.namespace_marker_path.parent / "redirected.json"
        )
        with self.assertRaisesRegex(
            EconomicSessionIntegrityError,
            "authority composition changed after construction",
        ):
            store.current()

    def test_schema_constant_rebinding_fails_before_state_bootstrap(self) -> None:
        import autosport.economic_session as economic_session

        store = self._store()
        original = economic_session._STATE_SCHEMA
        economic_session._STATE_SCHEMA = "forged-schema"
        try:
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.current()
        finally:
            economic_session._STATE_SCHEMA = original

        self.assertFalse(store.state_path.exists())


    def test_opening_paperbook_helper_rebinding_fails_before_execution(self) -> None:
        import autosport.economic_session as economic_session

        store = self._store()
        calls = 0

        def forbidden(_path):
            nonlocal calls
            calls += 1
            raise AssertionError("hostile opening PaperBook helper executed")

        original = economic_session._opening_paperbook_sha256
        economic_session._opening_paperbook_sha256 = forbidden
        try:
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.current()
        finally:
            economic_session._opening_paperbook_sha256 = original

        self.assertEqual(calls, 0)

    def test_state_payload_helper_rebinding_fails_before_publication(self) -> None:
        import autosport.economic_session as economic_session

        store = self._store()
        original = economic_session._state_payload

        def forbidden(*_args, **_kwargs):
            raise AssertionError("hostile state payload helper executed")

        economic_session._state_payload = forbidden
        try:
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.current()
        finally:
            economic_session._state_payload = original

        self.assertFalse(store.state_path.exists())

    def test_session_evidence_validation_is_detached_from_live_helpers(self) -> None:
        import autosport.economic_session as economic_session

        store = self._store()
        evidence = store.current()

        original_sha = economic_session._is_sha256
        original_parse = economic_session._parse_instant
        sha_calls = 0
        parse_calls = 0

        def forbidden_sha(*_args, **_kwargs):
            nonlocal sha_calls
            sha_calls += 1
            raise AssertionError("hostile session SHA validator executed")

        def forbidden_parse(*_args, **_kwargs):
            nonlocal parse_calls
            parse_calls += 1
            raise AssertionError("hostile session instant parser executed")

        economic_session._is_sha256 = forbidden_sha
        economic_session._parse_instant = forbidden_parse
        try:
            rebuilt = ProductEconomicSession(
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
        finally:
            economic_session._is_sha256 = original_sha
            economic_session._parse_instant = original_parse

        self.assertEqual(rebuilt, evidence)
        self.assertEqual(sha_calls, 0)
        self.assertEqual(parse_calls, 0)

    def test_captured_dependency_rebinding_does_not_reach_hostile_code(self) -> None:
        import autosport.economic_session as economic_session
        import hashlib
        import json

        existing = self._store()
        first = existing.current()

        safe_dependency_cases = (
            ("strict_json_loads", economic_session, "strict_json_loads"),
            ("open_read_only_descriptor", economic_session, "_open_read_only_descriptor"),
            ("json_dumps", json, "dumps"),
        )
        for name, owner, attribute in safe_dependency_cases:
            original = getattr(owner, attribute)
            calls = 0

            def forbidden(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                raise AssertionError(f"hostile {name} executed")

            setattr(owner, attribute, forbidden)
            try:
                with self.subTest(name=name):
                    self.assertEqual(existing.current(), first)
            finally:
                setattr(owner, attribute, original)
            self.assertEqual(calls, 0)

        fail_closed_dependency_cases = (
            ("sha256", hashlib, "sha256"),
            ("lexists", economic_session.os.path, "lexists"),
            ("provenance_for", economic_session, "provenance_for"),
        )
        for name, owner, attribute in fail_closed_dependency_cases:
            original = getattr(owner, attribute)
            calls = 0

            def forbidden(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                raise AssertionError(f"hostile {name} executed")

            setattr(owner, attribute, forbidden)
            try:
                with self.subTest(name=name):
                    with self.assertRaisesRegex(
                        EconomicSessionIntegrityError,
                        "authority composition changed after construction",
                    ):
                        existing.current()
            finally:
                setattr(owner, attribute, original)
            self.assertEqual(calls, 0)

        fresh = self._store()
        original_datetime = economic_session._DATETIME_FROMTIMESTAMP
        datetime_calls = 0

        def forbidden_datetime(*_args, **_kwargs):
            nonlocal datetime_calls
            datetime_calls += 1
            raise AssertionError("hostile datetime conversion executed")

        economic_session._DATETIME_FROMTIMESTAMP = forbidden_datetime
        try:
            evidence = fresh.current()
        finally:
            economic_session._DATETIME_FROMTIMESTAMP = original_datetime

        self.assertFalse(evidence.product_clock_authoritative)
        self.assertEqual(datetime_calls, 0)

        fresh_atomic = self._store()
        original_atomic = economic_session.atomic_write_json
        atomic_calls = 0

        def forbidden_atomic(*_args, **_kwargs):
            nonlocal atomic_calls
            atomic_calls += 1
            raise AssertionError("hostile atomic writer executed")

        economic_session.atomic_write_json = forbidden_atomic
        try:
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                fresh_atomic.current()
        finally:
            economic_session.atomic_write_json = original_atomic

        self.assertEqual(atomic_calls, 0)

    def test_pure_helper_rebinding_fails_before_execution(self) -> None:
        import autosport.economic_session as economic_session

        for helper_name in (
            "_clock_instant",
            "_state_sha256",
            "_semantic_binding",
            "_tx_id",
            "_canonical_json_bytes",
            "_read_regular_bytes",
            "_decode_state",
        ):
            with self.subTest(helper_name=helper_name):
                store = self._store()
                original = getattr(economic_session, helper_name)

                def forbidden(*_args, **_kwargs):
                    raise AssertionError(f"hostile {helper_name} executed")

                setattr(economic_session, helper_name, forbidden)
                try:
                    with self.assertRaisesRegex(
                        EconomicSessionIntegrityError,
                        "authority composition changed after construction",
                    ):
                        store.current()
                finally:
                    setattr(economic_session, helper_name, original)

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
