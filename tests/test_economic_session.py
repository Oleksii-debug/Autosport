from __future__ import annotations

import hashlib
import json
import pytest
import unittest
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
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


def _goal(
    *,
    revision: int = 1,
    max_turnover: str = "10",
    bankroll_id: str = "paper-bankroll",
    currency: str = "USD",
) -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="owner-goal",
        revision=revision,
        bankroll_id=bankroll_id,
        currency=currency,
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

    def test_transition_rejects_rebound_product_session_equality(self) -> None:
        store = self._store()
        first = store.current()
        original_eq = ProductEconomicSession.__eq__

        def hostile_eq(self, other):
            raise AssertionError("rebound ProductEconomicSession equality executed")

        ProductEconomicSession.__eq__ = hostile_eq
        try:
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.transition_to_current_goal(first)
        finally:
            ProductEconomicSession.__eq__ = original_eq


    def test_session_class_binding_cannot_be_replaced_by_descriptor_cloned_subclass(self) -> None:
        store = self._store()
        first = store.current()
        import autosport.economic_session as economic_session

        canonical_type = ProductEconomicSession

        class ForgedSession(canonical_type):
            pass

        for name in economic_session._PRODUCT_ECONOMIC_SESSION_FIELD_NAMES:
            type.__setattr__(
                ForgedSession,
                name,
                canonical_type.__dict__[name],
            )

        economic_session.ProductEconomicSession = ForgedSession
        try:
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.current()
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.transition_to_current_goal(first)
        finally:
            economic_session.ProductEconomicSession = canonical_type


    def test_transition_rejects_rebound_product_session_field_descriptors(self) -> None:
        store = self._store()
        first = store.current()
        import autosport.economic_session as economic_session

        original_fields = {
            name: ProductEconomicSession.__dict__[name]
            for name in economic_session._PRODUCT_ECONOMIC_SESSION_FIELD_NAMES
        }

        class HostileDescriptor:
            def __get__(self, instance, owner=None):
                raise AssertionError("rebound ProductEconomicSession field executed")

            def __set__(self, instance, value):
                raise AssertionError("rebound ProductEconomicSession field setter executed")

        for name, original in original_fields.items():
            with self.subTest(field=name):
                setattr(ProductEconomicSession, name, HostileDescriptor())
                try:
                    with self.assertRaisesRegex(
                        EconomicSessionIntegrityError,
                        "authority composition changed after construction",
                    ):
                        store.transition_to_current_goal(first)
                finally:
                    setattr(ProductEconomicSession, name, original)


    def test_transition_guard_ignores_rebound_introspection_primitives(self) -> None:
        store = self._store()
        first = store.current()
        import autosport.economic_session as economic_session

        field_name = "goal_id"
        original_descriptor = ProductEconomicSession.__dict__[field_name]
        marker = object()

        class HostileDescriptor:
            def __get__(self, instance, owner=None):
                raise AssertionError("hostile session field descriptor executed")

            def __set__(self, instance, value):
                raise AssertionError("hostile session field descriptor setter executed")

        rebound = {
            "type": lambda _value: ProductEconomicSessionStore,
            "any": lambda _values: False,
            "tuple": lambda values=(): marker,
            "getattr": lambda obj, name, default=None: object.__getattribute__(obj, name)
            if hasattr(obj, name)
            else default,
            "callable": lambda value: hasattr(value, "__call__"),
            "EconomicSessionIntegrityError": RuntimeError,
        }
        previous = {
            name: economic_session.__dict__.get(name, marker)
            for name in rebound
        }

        setattr(ProductEconomicSession, field_name, HostileDescriptor())
        for name, value in rebound.items():
            setattr(economic_session, name, value)
        try:
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.transition_to_current_goal(first)
        finally:
            setattr(ProductEconomicSession, field_name, original_descriptor)
            for name, value in previous.items():
                if value is marker:
                    economic_session.__dict__.pop(name, None)
                else:
                    setattr(economic_session, name, value)


    def test_transition_rejects_rebound_product_session_constructor_and_validator(self) -> None:
        store = self._store()
        first = store.current()

        originals = (
            ("__init__", ProductEconomicSession.__init__),
            ("__post_init__", ProductEconomicSession.__post_init__),
        )
        for name, original in originals:
            with self.subTest(method=name):
                def hostile(*args, **kwargs):
                    raise AssertionError(
                        f"rebound ProductEconomicSession {name} executed"
                    )

                setattr(ProductEconomicSession, name, hostile)
                try:
                    with self.assertRaisesRegex(
                        EconomicSessionIntegrityError,
                        "authority composition changed after construction",
                    ):
                        store.transition_to_current_goal(first)
                finally:
                    setattr(ProductEconomicSession, name, original)


    def test_transition_rejects_in_place_product_session_constructor_and_validator_code_mutation(self) -> None:
        store = self._store()
        first = store.current()

        for name in ("__init__", "__post_init__"):
            with self.subTest(method=name):
                original = getattr(ProductEconomicSession, name)
                original_code = original.__code__

                def hostile(*args, **kwargs):
                    raise AssertionError(
                        f"mutated ProductEconomicSession {name} executed"
                    )

                try:
                    original.__code__ = hostile.__code__
                    with self.assertRaisesRegex(
                        EconomicSessionIntegrityError,
                        "authority composition changed after construction",
                    ):
                        store.transition_to_current_goal(first)
                finally:
                    original.__code__ = original_code


    def test_transition_rejects_in_place_product_session_equality_code_mutation(self) -> None:
        store = self._store()
        first = store.current()
        original_eq = ProductEconomicSession.__eq__
        original_code = original_eq.__code__

        def hostile_eq(self, other):
            raise AssertionError("mutated ProductEconomicSession equality executed")

        try:
            original_eq.__code__ = hostile_eq.__code__
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.transition_to_current_goal(first)
        finally:
            original_eq.__code__ = original_code


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


    def test_constructor_rejects_path_dependency_rebinding_before_execution(self) -> None:
        import autosport.economic_session as economic_session

        original_path = economic_session.Path
        path_type = economic_session._PATH_TYPE
        original_expanduser = path_type.expanduser
        original_resolve = path_type.resolve

        class HostilePath:
            def __new__(cls, *args, **kwargs):
                raise AssertionError("rebound session Path constructor executed")

        def hostile_expanduser(self, *args, **kwargs):
            raise AssertionError("rebound session Path.expanduser executed")

        def hostile_resolve(self, *args, **kwargs):
            raise AssertionError("rebound session Path.resolve executed")

        mutations = (
            ("module Path", "module", HostilePath),
            ("Path.expanduser", "expanduser", hostile_expanduser),
            ("Path.resolve", "resolve", hostile_resolve),
        )

        for label, target, replacement in mutations:
            with self.subTest(label=label):
                try:
                    if target == "module":
                        economic_session.Path = replacement
                    elif target == "expanduser":
                        path_type.expanduser = replacement
                    else:
                        path_type.resolve = replacement

                    with self.assertRaisesRegex(
                        EconomicSessionIntegrityError,
                        "Path (constructor|expanduser|resolve) authority changed",
                    ):
                        ProductEconomicSessionStore(
                            self.workspace,
                            authority_root=self.authority_root,
                            _test_clock=self.clock,
                        )
                finally:
                    if target == "module":
                        economic_session.Path = original_path
                    elif target == "expanduser":
                        path_type.expanduser = original_expanduser
                    else:
                        path_type.resolve = original_resolve


    def test_lock_lifecycle_rebinding_fails_before_session_io(self) -> None:
        import autosport.economic_session as economic_session

        lifecycle = (
            ("__new__", lambda _lock, *_args: AssertionError("hostile lock __new__ executed")),
            ("__init__", lambda _lock, *_args: AssertionError("hostile lock __init__ executed")),
            ("__enter__", lambda _lock: AssertionError("hostile lock __enter__ executed")),
            ("__exit__", lambda _lock, *_args: AssertionError("hostile lock __exit__ executed")),
            ("acquire", lambda _lock: AssertionError("hostile lock acquire executed")),
            ("release", lambda _lock: AssertionError("hostile lock release executed")),
        )

        for method_name, factory in lifecycle:
            with self.subTest(method=method_name):
                store = self._store()
                store.current()
                lock_type = economic_session._WORKSPACE_LOCK_TYPE
                original = getattr(lock_type, method_name)

                def hostile(*_args, _factory=factory, **_kwargs):
                    error = _factory(None)
                    raise error

                try:
                    if method_name == "__new__":
                        setattr(lock_type, method_name, staticmethod(hostile))
                    else:
                        setattr(lock_type, method_name, hostile)

                    with self.assertRaisesRegex(
                        EconomicSessionIntegrityError,
                        "authority composition changed after construction",
                    ):
                        store.current()
                finally:
                    setattr(lock_type, method_name, original)

    def test_constructor_dependency_rebinding_fails_before_session_authority_creation(self) -> None:
        import autosport.economic_session as economic_session

        original_goal_store = economic_session.EconomicGoalStore
        original_goal_init = EconomicGoalStore.__init__
        original_authority = economic_session.MonotonicWorkspaceAuthority
        original_authority_init = MonotonicWorkspaceAuthority.__init__

        class HostileGoalStore:
            def __new__(cls, *_args, **_kwargs):
                raise AssertionError("hostile EconomicGoalStore constructor executed")

        class HostileAuthority:
            def __new__(cls, *_args, **_kwargs):
                raise AssertionError("hostile MonotonicWorkspaceAuthority constructor executed")

        def hostile_goal_init(self, *_args, **_kwargs):
            raise AssertionError("hostile EconomicGoalStore.__init__ executed")

        def hostile_authority_init(self, *_args, **_kwargs):
            raise AssertionError("hostile MonotonicWorkspaceAuthority.__init__ executed")

        mutations = (
            ("goal store alias", "goal_alias", HostileGoalStore),
            ("goal store init", "goal_init", hostile_goal_init),
            ("authority alias", "authority_alias", HostileAuthority),
            ("authority init", "authority_init", hostile_authority_init),
        )

        for label, target, replacement in mutations:
            with self.subTest(label=label):
                try:
                    if target == "goal_alias":
                        economic_session.EconomicGoalStore = replacement
                    elif target == "goal_init":
                        EconomicGoalStore.__init__ = replacement
                    elif target == "authority_alias":
                        economic_session.MonotonicWorkspaceAuthority = replacement
                    else:
                        MonotonicWorkspaceAuthority.__init__ = replacement

                    with self.assertRaisesRegex(
                        EconomicSessionIntegrityError,
                        r"constructor (authority changed|changed)",
                    ):
                        ProductEconomicSessionStore(
                            self.workspace,
                            authority_root=self.authority_root,
                            _test_clock=self.clock,
                        )
                finally:
                    economic_session.EconomicGoalStore = original_goal_store
                    EconomicGoalStore.__init__ = original_goal_init
                    economic_session.MonotonicWorkspaceAuthority = original_authority
                    MonotonicWorkspaceAuthority.__init__ = original_authority_init


    def test_paperbook_path_exists_rebinding_fails_before_session_io(self) -> None:
        import autosport.economic_session as economic_session

        store = self._store()
        path_type = economic_session._PATH_TYPE
        original = path_type.exists
        calls = 0

        def hostile(_self):
            nonlocal calls
            calls += 1
            raise AssertionError("rebound PaperBook Path.exists executed")

        path_type.exists = hostile
        try:
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.current()
        finally:
            path_type.exists = original

        self.assertEqual(calls, 0)


    def test_lock_and_constructor_in_place_code_mutation_fails_closed(self) -> None:
        import autosport.economic_session as economic_session

        lock_type = economic_session._WORKSPACE_LOCK_TYPE
        store = self._store()
        code_mutations = (
            ("lock __init__", lock_type.__init__),
            ("lock __enter__", lock_type.__enter__),
            ("lock __exit__", lock_type.__exit__),
            ("lock acquire", lock_type.acquire),
            ("lock release", lock_type.release),
        )

        for label, authority in code_mutations:
            with self.subTest(label=label):
                original_code = authority.__code__

                def hostile(*_args, **_kwargs):
                    raise AssertionError(f"hostile {label} executed")

                try:
                    authority.__code__ = hostile.__code__
                    with self.assertRaisesRegex(
                        EconomicSessionIntegrityError,
                        "authority composition changed after construction",
                    ):
                        store.current()
                finally:
                    authority.__code__ = original_code

        constructor_mutations = (
            ("goal store init", EconomicGoalStore),
            ("monotonic authority init", MonotonicWorkspaceAuthority),
        )
        original_goal_init = EconomicGoalStore.__init__
        original_authority_init = MonotonicWorkspaceAuthority.__init__
        try:
            for label, target in constructor_mutations:
                with self.subTest(label=label):
                    authority = target.__init__
                    original_code = authority.__code__

                    def hostile_constructor(*_args, **_kwargs):
                        raise AssertionError(f"hostile {label} constructor executed")

                    try:
                        authority.__code__ = hostile_constructor.__code__
                        with self.assertRaisesRegex(
                            EconomicSessionIntegrityError,
                            "constructor (authority changed|changed)",
                        ):
                            ProductEconomicSessionStore(
                                self.workspace,
                                authority_root=self.authority_root,
                                _test_clock=self.clock,
                            )
                    finally:
                        authority.__code__ = original_code
        finally:
            EconomicGoalStore.__init__ = original_goal_init
            MonotonicWorkspaceAuthority.__init__ = original_authority_init


    def test_decoder_in_place_code_mutation_fails_before_execution(self) -> None:
        store = self._store()
        store.current()
        authority = economic_session._decode_state
        original_code = authority.__code__
        calls = 0

        def hostile(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("hostile economic-session decoder executed")

        try:
            authority.__code__ = hostile.__code__
            with self.assertRaisesRegex(
                EconomicSessionIntegrityError,
                "authority composition changed after construction",
            ):
                store.current()
        finally:
            authority.__code__ = original_code

        self.assertEqual(calls, 0)

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


    def test_internal_authority_binding_rebinding_matrix_fails_closed(self) -> None:
        mutations = (
            ("domain", "forged-economic-session-domain"),
            ("key", "forged-key"),
            ("authority_root_selection", None),
            ("authority_root_binding_path", self.authority_root / "forged-selection.json"),
            ("workspace_binding_path", self.workspace / "forged-workspace-binding.json"),
            (
                "authority_root_activation_path",
                self.authority_root / "forged-activation.json",
            ),
            ("journal_dir", self.authority_root / "forged-journal"),
            ("records_dir", self.authority_root / "forged-records"),
        )

        for name, replacement in mutations:
            with self.subTest(name=name):
                store = self._store()
                store.current()
                setattr(store._authority, name, replacement)
                with self.assertRaisesRegex(
                    EconomicSessionIntegrityError,
                    "authority composition changed after construction",
                ):
                    store.current()

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

    def test_transition_rejects_in_place_post_init_helper_code_mutation(self) -> None:
        store = self._store()
        first = store.current()
        import autosport.economic_session as economic_session

        for name in ("_is_sha256", "_parse_instant"):
            with self.subTest(helper=name):
                helper = getattr(economic_session, name)
                original_code = helper.__code__

                def hostile(*args, **kwargs):
                    raise AssertionError(
                        f"mutated session helper {name} executed"
                    )

                try:
                    helper.__code__ = hostile.__code__
                    with self.assertRaisesRegex(
                        EconomicSessionIntegrityError,
                        "authority composition changed after construction",
                    ):
                        store.transition_to_current_goal(first)
                finally:
                    helper.__code__ = original_code


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

        import autosport.economic_goal_provenance as provenance_module

        provenance_symbol_cases = (
            ("economic_goal_to_payload", "economic_goal_to_payload"),
            ("contract_sha256", "contract_sha256"),
            ("EconomicGoalContract", "EconomicGoalContract"),
        )
        for name, attribute in provenance_symbol_cases:
            original = getattr(provenance_module, attribute)
            calls = 0

            def forbidden(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                raise AssertionError(f"hostile provenance symbol {name} executed")

            setattr(provenance_module, attribute, forbidden)
            try:
                with self.subTest(name=f"provenance:{name}"):
                    self.assertEqual(existing.current(), first)
            finally:
                setattr(provenance_module, attribute, original)
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
            "_parse_instant",
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

    def test_explicit_goal_transition_closes_predecessor_and_publishes_successor(self) -> None:
        store = self._store()
        first = store.current()
        EconomicGoalStore(self.workspace).persist_automatic_successor(
            _goal(revision=2, max_turnover="5")
        )
        self.clock.set("2026-10-05T13:00:00Z")

        second = self._store().transition_to_current_goal(first)

        self.assertNotEqual(second.session_id, first.session_id)
        self.assertEqual(second.goal_revision, 2)
        self.assertEqual(second.authority_generation, 2)
        self.assertEqual(second.started_at, "2026-10-05T13:00:00Z")
        self.assertEqual(second.predecessor_session_id, first.session_id)
        self.assertEqual(second.predecessor_state_sha256, first.state_sha256)
        self.assertEqual(second.predecessor_ended_at, second.started_at)
        self.assertEqual(self._store().current(), second)

    def test_explicit_transition_rejects_unchanged_goal_without_state_change(self) -> None:
        store = self._store()
        first = store.current()

        with self.assertRaisesRegex(
            EconomicSessionMismatchError,
            "EconomicGoal is unchanged",
        ):
            self._store().transition_to_current_goal(first)

        self.assertEqual(self._store().current(), first)

    def test_stale_predecessor_cannot_publish_over_current_successor(self) -> None:
        first = self._store().current()
        EconomicGoalStore(self.workspace).persist_automatic_successor(
            _goal(revision=2, max_turnover="5")
        )
        self.clock.set("2026-10-05T13:00:00Z")
        second = self._store().transition_to_current_goal(first)
        EconomicGoalStore(self.workspace).persist_automatic_successor(
            _goal(revision=3, max_turnover="4")
        )
        self.clock.set("2026-10-05T14:00:00Z")

        with self.assertRaisesRegex(
            EconomicSessionMismatchError,
            "predecessor does not match current durable session",
        ):
            self._store().transition_to_current_goal(first)

        with self.assertRaisesRegex(
            EconomicSessionMismatchError,
            "changed without explicit economic-session transition",
        ):
            self._store().current()
        self.assertEqual(second.authority_generation, 2)

    def test_bankroll_or_currency_change_requires_explicit_successor_scope(self) -> None:
        store = self._store()
        first = store.current()

        EconomicGoalStore(self.workspace).persist_automatic_successor(
            _goal(revision=2, max_turnover="5", bankroll_id="bankroll-eur", currency="EUR")
        )

        with self.assertRaisesRegex(
            EconomicSessionMismatchError,
            "changed without explicit economic-session transition",
        ):
            store.current()

        self.clock.set("2026-10-05T13:00:00Z")
        successor = self._store().transition_to_current_goal(first)

        self.assertNotEqual(successor.session_id, first.session_id)
        self.assertEqual(successor.bankroll_id, "bankroll-eur")
        self.assertEqual(successor.currency, "EUR")
        self.assertEqual(successor.predecessor_session_id, first.session_id)
        self.assertEqual(successor.predecessor_state_sha256, first.state_sha256)

    def test_explicit_successor_survives_fresh_store_re_resolution(self) -> None:
        first_store = self._store()
        first = first_store.current()
        EconomicGoalStore(self.workspace).persist_automatic_successor(_goal(revision=2))
        self.clock.set("2026-10-05T13:00:00Z")

        successor = first_store.transition_to_current_goal(first)
        fresh = self._store().current()

        self.assertEqual(fresh, successor)
        self.assertEqual(fresh.state_sha256, successor.state_sha256)
        self.assertEqual(fresh.authority_generation, successor.authority_generation)

    def test_concurrent_explicit_transition_has_one_winner_and_one_stale_predecessor(self) -> None:
        first = self._store().current()
        EconomicGoalStore(self.workspace).persist_automatic_successor(_goal(revision=2))
        self.clock.set("2026-10-05T13:00:00Z")

        def transition() -> tuple[str, ProductEconomicSession | None]:
            try:
                return ("success", self._store().transition_to_current_goal(first))
            except EconomicSessionMismatchError:
                return ("stale", None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _index: transition(), (0, 1)))

        successes = [value for kind, value in outcomes if kind == "success"]
        stale = [kind for kind, _value in outcomes if kind == "stale"]

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(stale), 1)
        winner = successes[0]
        assert winner is not None
        self.assertNotEqual(winner.session_id, first.session_id)
        self.assertEqual(self._store().current(), winner)
        self.assertEqual(winner.predecessor_session_id, first.session_id)

    def test_transition_clock_cannot_backdate_session_before_predecessor_start(self) -> None:
        store = self._store()
        first = store.current()

        EconomicGoalStore(self.workspace).persist_automatic_successor(_goal(revision=2))
        self.clock.set("2026-10-05T11:59:59Z")

        with self.assertRaisesRegex(
            EconomicSessionIntegrityError,
            "transition clock precedes predecessor start",
        ):
            store.transition_to_current_goal(first)

        self.assertEqual(self._store().current(), first)

    def test_successor_predecessor_digest_is_not_rebindable_after_publication(self) -> None:
        store = self._store()
        first = store.current()
        EconomicGoalStore(self.workspace).persist_automatic_successor(_goal(revision=2))
        self.clock.set("2026-10-05T13:00:00Z")
        successor = store.transition_to_current_goal(first)

        payload = json.loads(store.state_path.read_text(encoding="utf-8"))
        payload["predecessor_state_sha256"] = "0" * 64
        store.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(MonotonicAuthorityRollbackError):
            self._store().current()

        self.assertEqual(successor.predecessor_state_sha256, first.state_sha256)

    def test_pathological_state_json_nesting_is_normalized(self) -> None:
        raw = (b"[" * 1500) + b"0" + (b"]" * 1500)
        self.assertLess(len(raw), economic_session._MAX_STATE_BYTES)

        with self.assertRaisesRegex(
            EconomicSessionIntegrityError,
            "not strict UTF-8 JSON",
        ):
            economic_session._decode_state(
                raw,
                workspace_instance_id="workspace-instance",
            )

    def test_default_clock_session_is_positive_boundary_authority(self) -> None:
        store = ProductEconomicSessionStore(
            self.workspace,
            authority_root=self.authority_root,
        )
        evidence = store.current()

        self.assertTrue(evidence.product_clock_authoritative)
        self.assertEqual(store.require_current(evidence), evidence)




def test_read_regular_bytes_normalizes_close_failure_without_masking_primary_error(tmp_path) -> None:
    target = tmp_path / "economic-session.json"
    target.write_bytes(b"{\"schema\":\"ok\"}")

    def failing_close(_descriptor: int) -> None:
        raise OSError("close failure")

    with pytest.raises(
        economic_session.EconomicSessionIntegrityError,
        match="cannot close test reader descriptor",
    ):
        economic_session._read_regular_bytes(
            target,
            limit=4096,
            label="test reader",
            _close=failing_close,
        )


def test_read_regular_bytes_preserves_primary_domain_error_when_close_also_fails(
    tmp_path,
) -> None:
    target = tmp_path / "economic-session.json"
    target.write_bytes(b"{\"schema\":\"ok\"}")

    def failing_close(_descriptor: int) -> None:
        raise OSError("secondary close failure")

    with pytest.raises(
        economic_session.EconomicSessionIntegrityError,
        match="opened identity is invalid",
    ):
        economic_session._read_regular_bytes(
            target,
            limit=4096,
            _fstat=lambda _descriptor: type(
                "Stat",
                (),
                {
                    "st_mode": 0,
                    "st_nlink": 1,
                    "st_size": 1,
                },
            )(),
            _close=failing_close,
        )




def test_read_regular_bytes_normalizes_primary_read_os_error(tmp_path) -> None:
    target = tmp_path / "economic-session.json"
    target.write_bytes(b"{\"schema\":\"ok\"}")

    def failing_read(_descriptor: int, _size: int) -> bytes:
        raise OSError("read failure")

    with pytest.raises(
        economic_session.EconomicSessionIntegrityError,
        match="cannot safely read test reader",
    ):
        economic_session._read_regular_bytes(
            target,
            limit=4096,
            label="test reader",
            _read=failing_read,
        )


if __name__ == "__main__":
    unittest.main()
