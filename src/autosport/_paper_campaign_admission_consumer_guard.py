"""Fail closed on mutable PAPER-admission authority objects.

Exact concrete ledger/runtime types prevent subclass-based authority forgery, but
same-process callers can also replace class methods and ordinary module globals.
Authority-bearing admission reads therefore use an installation-time closure seal:
the exact coordinator methods, DecisionLedger/ExecutionLedger read implementations,
capability objects and binding store are captured outside caller-writable module
namespaces. Module globals remain compatibility/debug mirrors only and are never
read to authorize admission.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from threading import RLock
from weakref import WeakKeyDictionary

from . import campaign_forward_universe_cycle_binding as _campaign_forward
from .decision_ledger import JsonlDecisionLedger
from .paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)
from .paper_campaign_runtime import PaperCampaignRuntime
from .paper_execution_reality import PaperExecutionLedger
from .paper_settlement_learning import PaperSettlementLearningBridge


_GUARD_MARKER = "__autosport_exact_admission_authority_guard_v7__"
_SEAL_MARKER = "autosport.paper_campaign_admission.consumer_guard.seal.v1"
_RUNTIME_METHODS = (
    "_parameters_with_reflection_commitment",
    "begin_and_bind_paper_ticket",
)
_BRIDGE_METHODS = ("_read",)


def _sealed_tuple_from_callable(candidate: object):
    closure = getattr(candidate, "__closure__", None)
    if not closure:
        return None
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if (
            type(value) is tuple
            and len(value) == 18
            and value[0] == _SEAL_MARKER
        ):
            return value
    return None


def _initial_seal():
    return (
        _SEAL_MARKER,
        PaperCampaignAdmissionCoordinator,
        JsonlDecisionLedger,
        PaperExecutionLedger,
        PaperCampaignRuntime,
        PaperSettlementLearningBridge,
        PaperCampaignAdmissionCoordinator.__init__,
        PaperCampaignAdmissionCoordinator.__getattribute__,
        PaperCampaignAdmissionCoordinator._resolved_execution_decision_id,
        PaperCampaignAdmissionCoordinator._execution_attempt,
        JsonlDecisionLedger.verified_records,
        PaperExecutionLedger.events,
        WeakKeyDictionary(),
        RLock(),
        PaperCampaignAdmissionCoordinator.admit,
        _campaign_forward,
        _campaign_forward.CampaignForwardEvidenceVerification,
        _campaign_forward.verify_campaign_forward_evidence,
    )


def _build_guard(seal):
    seal_marker = seal[0]
    coordinator_cls = seal[1]
    decision_ledger_cls = seal[2]
    execution_ledger_cls = seal[3]
    runtime_cls = seal[4]
    settlement_bridge_cls = seal[5]
    original_init = seal[6]
    original_getattribute = seal[7]
    original_resolved_execution_decision_id = seal[8]
    original_execution_attempt = seal[9]
    decision_verified_records = seal[10]
    execution_events = seal[11]
    authority_bindings = seal[12]
    authority_bindings_lock = seal[13]
    original_admit = seal[14]
    forward_module = seal[15]
    forward_verification_type = seal[16]
    forward_verify = seal[17]
    runtime_methods = _RUNTIME_METHODS
    bridge_methods = _BRIDGE_METHODS
    path_cls = Path
    rlock_factory = RLock
    mapping_cls = Mapping
    exact_type = type
    exact_dict = dict
    exact_tuple = tuple
    exact_len = len
    isinstance_fn = isinstance
    datetime_cls = datetime
    exact_str = str
    parse_errors = (TypeError, ValueError)
    object_getattribute = object.__getattribute__
    forward_payload_key = "campaign_forward_verification"
    forward_parameter_key = "campaign_forward_verification_sha256"
    forward_scope = "CYCLE_BOUND_PROVIDER_UNIVERSE_STRUCTURAL_ONLY"
    forward_fields = (
        "schema_version",
        "campaign_id",
        "protocol_sha256",
        "structural_result_sha256",
        "structural_ok",
        "structural_codes",
        "terminal_root_sha256",
        "candidate_count",
        "campaign_cycle_authority_sha256",
        "prospective_evaluation_plan_sha256",
        "universe_sha256",
        "membership_sha256",
        "verification_scope",
        "provider_universe_authority_resolved",
        "promotion_ready",
        "real_money_ready",
        "receipt_sha256",
    )

    def has_instance_shadow(value: object, method_name: str) -> bool:
        try:
            namespace = object.__getattribute__(value, "__dict__")
        except (AttributeError, TypeError):
            return False
        return type(namespace) is dict and method_name in namespace

    def reject_instance_shadow(value: object, method_name: str, label: str) -> None:
        if has_instance_shadow(value, method_name):
            raise PaperCampaignAdmissionError(
                f"{label} authority method is shadowed on the exact instance"
            )

    def assert_decision_ledger_class_method() -> None:
        if decision_ledger_cls.verified_records is not decision_verified_records:
            raise PaperCampaignAdmissionError(
                "Decision Ledger authority class method changed after guard installation"
            )

    def assert_execution_ledger_class_method() -> None:
        if execution_ledger_cls.events is not execution_events:
            raise PaperCampaignAdmissionError(
                "PAPER execution authority class method changed after guard installation"
            )

    def reject_runtime_shadows(runtime: PaperCampaignRuntime) -> None:
        for method_name in runtime_methods:
            reject_instance_shadow(runtime, method_name, "PAPER campaign runtime")

    def reject_bridge_shadows(bridge: PaperSettlementLearningBridge) -> None:
        for method_name in bridge_methods:
            reject_instance_shadow(
                bridge,
                method_name,
                "PAPER settlement-learning bridge",
            )

    def canonical_decision_path(state_path: object) -> Path:
        return (path_cls(state_path).parent / "decisions.jsonl").resolve(strict=False)

    def assert_decision_ledger_path(
        decision_ledger: JsonlDecisionLedger,
        expected_path: Path,
    ) -> None:
        try:
            actual_path = object.__getattribute__(decision_ledger, "path")
        except (AttributeError, TypeError) as exc:
            raise PaperCampaignAdmissionError(
                "Decision Ledger path authority is unavailable"
            ) from exc
        if (
            type(actual_path) is not type(expected_path)
            or actual_path.resolve(strict=False) != expected_path
        ):
            raise PaperCampaignAdmissionError(
                "Decision Ledger must be the canonical workspace decisions.jsonl"
            )

    class ExactDecisionLedgerReadView:
        __slots__ = ("_ledger",)

        def __init__(self, ledger: JsonlDecisionLedger) -> None:
            self._ledger = ledger

        def verified_records(self):
            return decision_verified_records(self._ledger)

    class ExactExecutionLedgerReadView:
        __slots__ = ("_ledger",)

        def __init__(self, ledger: PaperExecutionLedger) -> None:
            self._ledger = ledger

        def events(self, run_id: str):
            return execution_events(self._ledger, run_id)

    class PinnedCoordinatorReadView:
        """Run legacy resolver logic without mutable public authority dispatch."""

        __slots__ = ("_coordinator", "decision_ledger", "execution_ledger")

        def __init__(
            self,
            coordinator: PaperCampaignAdmissionCoordinator,
            *,
            decision_ledger: JsonlDecisionLedger,
            execution_ledger: PaperExecutionLedger,
        ) -> None:
            self._coordinator = coordinator
            self.decision_ledger = ExactDecisionLedgerReadView(decision_ledger)
            self.execution_ledger = ExactExecutionLedgerReadView(execution_ledger)

        def __getattr__(self, name: str):
            return original_getattribute(self._coordinator, name)

    def binding_for_optional(self: PaperCampaignAdmissionCoordinator):
        with authority_bindings_lock:
            return authority_bindings.get(self)

    def binding_for(self: PaperCampaignAdmissionCoordinator):
        binding = binding_for_optional(self)
        if binding is None:
            raise PaperCampaignAdmissionError(
                "PAPER admission authority binding is unavailable"
            )
        return binding

    def guarded_getattribute(self: PaperCampaignAdmissionCoordinator, name: str):
        if seal[0] != seal_marker:
            raise RuntimeError("PAPER admission executable authority seal changed")
        value = original_getattribute(self, name)
        if name == "admit":
            reject_instance_shadow(self, "admit", "PAPER campaign admission")
            if coordinator_cls.admit is not guarded_admit:
                raise PaperCampaignAdmissionError(
                    "PAPER campaign admission entry point changed after installation"
                )
            return value
        if name not in {"decision_ledger", "execution_ledger", "runtime"}:
            return value
        binding = binding_for_optional(self)
        if binding is None:
            return value
        expected = {
            "decision_ledger": binding[0],
            "execution_ledger": binding[1],
            "runtime": binding[3],
        }[name]
        if value is not expected:
            labels = {
                "decision_ledger": "Decision Ledger",
                "execution_ledger": "PAPER execution",
                "runtime": "PAPER campaign runtime",
            }
            raise PaperCampaignAdmissionError(
                f"{labels[name]} authority changed after admission construction"
            )
        return value

    def assert_raw_authority_fields(
        self: PaperCampaignAdmissionCoordinator,
        *,
        decision_ledger: JsonlDecisionLedger,
        execution_ledger: PaperExecutionLedger,
        runtime: PaperCampaignRuntime,
    ) -> None:
        if original_getattribute(self, "decision_ledger") is not decision_ledger:
            raise PaperCampaignAdmissionError(
                "Decision Ledger authority changed after admission construction"
            )
        if original_getattribute(self, "execution_ledger") is not execution_ledger:
            raise PaperCampaignAdmissionError(
                "PAPER execution authority changed after admission construction"
            )
        if original_getattribute(self, "runtime") is not runtime:
            raise PaperCampaignAdmissionError(
                "PAPER campaign runtime authority changed after admission construction"
            )

    def assert_runtime_binding(
        self: PaperCampaignAdmissionCoordinator,
        runtime: PaperCampaignRuntime,
        settlement_bridge: PaperSettlementLearningBridge,
        environment: object,
        agent_loop: object,
    ) -> None:
        if original_getattribute(self, "runtime") is not runtime:
            raise PaperCampaignAdmissionError(
                "PAPER campaign runtime authority changed after admission construction"
            )
        if type(runtime) is not runtime_cls:
            raise PaperCampaignAdmissionError(
                "PAPER campaign runtime must remain exact PaperCampaignRuntime"
            )
        if runtime.settlement_bridge is not settlement_bridge:
            raise PaperCampaignAdmissionError(
                "PAPER campaign settlement authority changed after admission construction"
            )
        if type(settlement_bridge) is not settlement_bridge_cls:
            raise PaperCampaignAdmissionError(
                "PAPER campaign settlement authority must remain exact "
                "PaperSettlementLearningBridge"
            )
        if runtime.environment is not environment:
            raise PaperCampaignAdmissionError(
                "PAPER campaign environment authority changed after admission construction"
            )
        if settlement_bridge.agent_loop is not agent_loop:
            raise PaperCampaignAdmissionError(
                "PAPER campaign AgentLoop authority changed after admission construction"
            )
        reject_runtime_shadows(runtime)
        reject_bridge_shadows(settlement_bridge)

    def guarded_init(
        self: PaperCampaignAdmissionCoordinator,
        state_path,
        *,
        paper_book_path,
        decision_ledger: JsonlDecisionLedger,
        runtime,
        execution_ledger: PaperExecutionLedger,
    ) -> None:
        if seal[0] != seal_marker:
            raise RuntimeError("PAPER admission executable authority seal changed")
        if type(runtime) is not runtime_cls:
            raise TypeError("runtime must be exact PaperCampaignRuntime")
        if type(runtime.settlement_bridge) is not settlement_bridge_cls:
            raise TypeError(
                "runtime settlement_bridge must be exact PaperSettlementLearningBridge"
            )
        assert_decision_ledger_class_method()
        assert_execution_ledger_class_method()
        reject_runtime_shadows(runtime)
        reject_bridge_shadows(runtime.settlement_bridge)
        expected_decision_path = canonical_decision_path(state_path)
        if type(decision_ledger) is decision_ledger_cls:
            assert_decision_ledger_path(decision_ledger, expected_decision_path)
            reject_instance_shadow(
                decision_ledger,
                "verified_records",
                "Decision Ledger",
            )
        if type(execution_ledger) is execution_ledger_cls:
            reject_instance_shadow(
                execution_ledger,
                "events",
                "PAPER execution",
            )
        original_init(
            self,
            state_path,
            paper_book_path=paper_book_path,
            decision_ledger=decision_ledger,
            runtime=runtime,
            execution_ledger=execution_ledger,
        )
        with authority_bindings_lock:
            authority_bindings[self] = (
                decision_ledger,
                execution_ledger,
                expected_decision_path,
                runtime,
                runtime.settlement_bridge,
                runtime.environment,
                runtime.settlement_bridge.agent_loop,
                rlock_factory(),
            )

    def guarded_resolved_execution_decision_id(self, *args, **kwargs):
        if seal[0] != seal_marker:
            raise RuntimeError("PAPER admission executable authority seal changed")
        (
            decision_ledger,
            execution_ledger,
            expected_decision_path,
            runtime,
            settlement_bridge,
            environment,
            agent_loop,
            lock,
        ) = binding_for(self)
        with lock:
            assert_raw_authority_fields(
                self,
                decision_ledger=decision_ledger,
                execution_ledger=execution_ledger,
                runtime=runtime,
            )
            assert_runtime_binding(
                self,
                runtime,
                settlement_bridge,
                environment,
                agent_loop,
            )
            assert_decision_ledger_path(decision_ledger, expected_decision_path)
            assert_decision_ledger_class_method()
            reject_instance_shadow(
                decision_ledger,
                "verified_records",
                "Decision Ledger",
            )
            reader = PinnedCoordinatorReadView(
                self,
                decision_ledger=decision_ledger,
                execution_ledger=execution_ledger,
            )
            result = original_resolved_execution_decision_id(reader, *args, **kwargs)
            assert_raw_authority_fields(
                self,
                decision_ledger=decision_ledger,
                execution_ledger=execution_ledger,
                runtime=runtime,
            )
            assert_decision_ledger_path(decision_ledger, expected_decision_path)
            assert_decision_ledger_class_method()
            reject_instance_shadow(
                decision_ledger,
                "verified_records",
                "Decision Ledger",
            )
            return result

    def guarded_execution_attempt(self, *args, **kwargs):
        if seal[0] != seal_marker:
            raise RuntimeError("PAPER admission executable authority seal changed")
        (
            decision_ledger,
            execution_ledger,
            expected_decision_path,
            runtime,
            settlement_bridge,
            environment,
            agent_loop,
            lock,
        ) = binding_for(self)
        with lock:
            assert_raw_authority_fields(
                self,
                decision_ledger=decision_ledger,
                execution_ledger=execution_ledger,
                runtime=runtime,
            )
            assert_runtime_binding(
                self,
                runtime,
                settlement_bridge,
                environment,
                agent_loop,
            )
            assert_decision_ledger_path(decision_ledger, expected_decision_path)
            assert_execution_ledger_class_method()
            reject_instance_shadow(
                execution_ledger,
                "events",
                "PAPER execution",
            )
            reader = PinnedCoordinatorReadView(
                self,
                decision_ledger=decision_ledger,
                execution_ledger=execution_ledger,
            )
            result = original_execution_attempt(reader, *args, **kwargs)
            assert_raw_authority_fields(
                self,
                decision_ledger=decision_ledger,
                execution_ledger=execution_ledger,
                runtime=runtime,
            )
            assert_decision_ledger_path(decision_ledger, expected_decision_path)
            assert_execution_ledger_class_method()
            reject_instance_shadow(
                execution_ledger,
                "events",
                "PAPER execution",
            )
            return result

    def guarded_admit(
        self: PaperCampaignAdmissionCoordinator,
        *,
        campaign_forward_verification,
        campaign_forward_precommit_locator,
        campaign_forward_collector_store,
        campaign_forward_source_spec,
        campaign_forward_cycle_receipt,
        campaign_forward_provider_evidence_store,
        campaign_forward_universe_store,
        campaign_forward_event_lifecycle,
        campaign_forward_evidence,
        **kwargs,
    ):
        """Re-resolve exact forward evidence before any admission PREPARED mutation."""

        if seal[0] != seal_marker:
            raise RuntimeError("PAPER admission executable authority seal changed")
        reject_instance_shadow(self, "admit", "PAPER campaign admission")
        if coordinator_cls.admit is not guarded_admit:
            raise PaperCampaignAdmissionError(
                "PAPER campaign admission entry point changed after installation"
            )

        def require_forward_surface() -> None:
            if (
                forward_module.CampaignForwardEvidenceVerification
                is not forward_verification_type
                or forward_module.verify_campaign_forward_evidence is not forward_verify
            ):
                raise PaperCampaignAdmissionError(
                    "campaign forward verification authority surface changed"
                )

        def forward_snapshot(value: object) -> tuple[tuple[str, object], ...]:
            return exact_tuple(
                (name, object_getattribute(value, name))
                for name in forward_fields
            )

        require_forward_surface()
        if exact_type(campaign_forward_verification) is not forward_verification_type:
            raise TypeError(
                "campaign_forward_verification must be exact "
                "CampaignForwardEvidenceVerification"
            )
        carried_snapshot = forward_snapshot(campaign_forward_verification)

        resolved = forward_verify(
            precommit_locator=campaign_forward_precommit_locator,
            collector_store=campaign_forward_collector_store,
            source_spec=campaign_forward_source_spec,
            cycle_receipt=campaign_forward_cycle_receipt,
            provider_evidence_store=campaign_forward_provider_evidence_store,
            universe_store=campaign_forward_universe_store,
            event_lifecycle=campaign_forward_event_lifecycle,
            evidence=campaign_forward_evidence,
        )

        require_forward_surface()
        reject_instance_shadow(self, "admit", "PAPER campaign admission")
        if coordinator_cls.admit is not guarded_admit:
            raise PaperCampaignAdmissionError(
                "PAPER campaign admission entry point changed during forward verification"
            )
        if exact_type(resolved) is not forward_verification_type:
            raise PaperCampaignAdmissionError(
                "campaign forward verifier returned noncanonical receipt"
            )

        resolved_snapshot = forward_snapshot(resolved)
        if resolved_snapshot != carried_snapshot:
            raise PaperCampaignAdmissionError(
                "carried campaign forward verification changed on canonical re-resolution"
            )
        resolved_values = exact_dict(resolved_snapshot)
        if (
            resolved_values["structural_ok"] is not True
            or resolved_values["structural_codes"] != ("PASS",)
            or resolved_values["verification_scope"] != forward_scope
            or resolved_values["provider_universe_authority_resolved"] is not True
            or resolved_values["promotion_ready"] is not False
            or resolved_values["real_money_ready"] is not False
        ):
            raise PaperCampaignAdmissionError(
                "campaign forward verification is not exact structural PASS/non-promotion truth"
            )

        def parse_instant(value: object, label: str):
            if exact_type(value) is not exact_str:
                raise PaperCampaignAdmissionError(f"{label} must be canonical ISO text")
            try:
                parsed = datetime_cls.fromisoformat(value.replace("Z", "+00:00"))
            except parse_errors as exc:
                raise PaperCampaignAdmissionError(f"{label} is not valid ISO time") from exc
            if parsed.tzinfo is None:
                raise PaperCampaignAdmissionError(f"{label} must be timezone-aware")
            return parsed

        decision_instant = parse_instant(kwargs.get("decision_at"), "decision_at")
        latest_evidence_instant = parse_instant(
            object_getattribute(campaign_forward_cycle_receipt, "completed_at"),
            "campaign forward cycle completed_at",
        )
        for collection_name, instant_name in (
            ("opportunities", "observed_upper"),
            ("cohort_roots", "anchor_upper"),
            ("closes", "anchor_upper"),
            ("reveal_boundaries", "boundary_upper"),
        ):
            collection = object_getattribute(campaign_forward_evidence, collection_name)
            for item in collection:
                instant = object_getattribute(item, instant_name)
                if exact_type(instant) is not datetime_cls or instant.tzinfo is None:
                    raise PaperCampaignAdmissionError(
                        "campaign forward evidence availability instant is noncanonical"
                    )
                if instant > latest_evidence_instant:
                    latest_evidence_instant = instant
        if decision_instant < latest_evidence_instant:
            raise PaperCampaignAdmissionError(
                "PAPER admission decision predates campaign forward evidence availability"
            )

        decision_payload = kwargs.get("decision_payload")
        if decision_payload is None:
            payload = {}
        elif isinstance_fn(decision_payload, mapping_cls):
            payload = exact_dict(decision_payload)
        else:
            raise TypeError("decision_payload must be a mapping or None")
        if forward_payload_key in payload:
            raise PaperCampaignAdmissionError(
                "decision_payload attempts to replace campaign forward verification authority"
            )
        payload[forward_payload_key] = exact_dict(resolved_snapshot)
        kwargs["decision_payload"] = payload

        action_parameters = kwargs.get("action_parameters", ())
        if exact_type(action_parameters) is not exact_tuple:
            raise TypeError("action_parameters must be a canonical tuple")
        for item in action_parameters:
            if (
                exact_type(item) is exact_tuple
                and exact_len(item) == 2
                and item[0] == forward_parameter_key
            ):
                raise PaperCampaignAdmissionError(
                    "action_parameters attempt to replace campaign forward verification authority"
                )
        kwargs["action_parameters"] = exact_tuple(
            (*action_parameters, (forward_parameter_key, resolved_values["receipt_sha256"]))
        )

        require_forward_surface()
        reject_instance_shadow(self, "admit", "PAPER campaign admission")
        if coordinator_cls.admit is not guarded_admit:
            raise PaperCampaignAdmissionError(
                "PAPER campaign admission entry point changed before admission commit"
            )
        result = original_admit(self, **kwargs)
        require_forward_surface()
        if coordinator_cls.admit is not guarded_admit:
            raise PaperCampaignAdmissionError(
                "PAPER campaign admission entry point changed during admission"
            )
        return result

    return (
        guarded_init,
        guarded_getattribute,
        guarded_resolved_execution_decision_id,
        guarded_execution_attempt,
        guarded_admit,
        reject_instance_shadow,
        assert_decision_ledger_class_method,
        assert_execution_ledger_class_method,
    )


def _install() -> None:
    installed_resolver = PaperCampaignAdmissionCoordinator._resolved_execution_decision_id
    seal = _sealed_tuple_from_callable(installed_resolver)
    marker = bool(getattr(PaperCampaignAdmissionCoordinator, _GUARD_MARKER, False))

    if marker and seal is None:
        raise RuntimeError("PAPER admission executable authority seal is unavailable")
    if seal is None:
        seal = _initial_seal()

    # On reload, every installed entry point must still carry the same closure seal.
    if marker:
        for candidate in (
            PaperCampaignAdmissionCoordinator.__init__,
            PaperCampaignAdmissionCoordinator.__getattribute__,
            PaperCampaignAdmissionCoordinator._resolved_execution_decision_id,
            PaperCampaignAdmissionCoordinator._execution_attempt,
            PaperCampaignAdmissionCoordinator.admit,
        ):
            if _sealed_tuple_from_callable(candidate) is not seal:
                raise RuntimeError(
                    "PAPER admission executable authority entry point changed after installation"
                )

    (
        guarded_init,
        guarded_getattribute,
        guarded_resolved_execution_decision_id,
        guarded_execution_attempt,
        guarded_admit,
        reject_instance_shadow,
        assert_decision_ledger_class_method,
        assert_execution_ledger_class_method,
    ) = _build_guard(seal)

    # Compatibility/debug mirrors only. The installed wrappers above never read
    # these names; mutating them cannot redirect an authority-bearing read. The
    # authoritative binding map/lock are intentionally NOT aliased here.
    global _ORIGINAL_INIT
    global _ORIGINAL_GETATTRIBUTE
    global _ORIGINAL_RESOLVED_EXECUTION_DECISION_ID
    global _ORIGINAL_EXECUTION_ATTEMPT
    global _ORIGINAL_ADMIT
    global _PINNED_DECISION_VERIFIED_RECORDS
    global _PINNED_EXECUTION_EVENTS
    global _AUTHORITY_BINDINGS
    global _AUTHORITY_BINDINGS_LOCK
    global _reject_instance_shadow
    global _assert_decision_ledger_class_method
    global _assert_execution_ledger_class_method
    _ORIGINAL_INIT = seal[6]
    _ORIGINAL_GETATTRIBUTE = seal[7]
    _ORIGINAL_RESOLVED_EXECUTION_DECISION_ID = seal[8]
    _ORIGINAL_EXECUTION_ATTEMPT = seal[9]
    _ORIGINAL_ADMIT = seal[14]
    _PINNED_DECISION_VERIFIED_RECORDS = seal[10]
    _PINNED_EXECUTION_EVENTS = seal[11]
    _AUTHORITY_BINDINGS = WeakKeyDictionary()
    _AUTHORITY_BINDINGS_LOCK = RLock()
    _reject_instance_shadow = reject_instance_shadow
    _assert_decision_ledger_class_method = assert_decision_ledger_class_method
    _assert_execution_ledger_class_method = assert_execution_ledger_class_method

    if marker:
        return

    PaperCampaignAdmissionCoordinator.__init__ = guarded_init
    PaperCampaignAdmissionCoordinator.__getattribute__ = guarded_getattribute
    PaperCampaignAdmissionCoordinator._resolved_execution_decision_id = (
        guarded_resolved_execution_decision_id
    )
    PaperCampaignAdmissionCoordinator._execution_attempt = guarded_execution_attempt
    PaperCampaignAdmissionCoordinator.admit = guarded_admit
    setattr(PaperCampaignAdmissionCoordinator, _GUARD_MARKER, True)


_install()


__all__ = []
