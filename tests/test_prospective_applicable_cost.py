from __future__ import annotations

from datetime import datetime, tzinfo

from decimal import Decimal
import inspect

import pytest

from autosport.betfair_standard_limit_price_bound import (
    resolve_betfair_standard_limit_price_bound,
)
from autosport.campaign_cost_evidence import CostClass, REQUIRED_COST_CLASSES
from autosport.real_execution_ledger import RealExecutionLedger
import autosport.prospective_applicable_cost as subject
from prospective_applicable_cost_test_support import canonical_applicable_cost_case
from test_supervised_plan_issuance import _active_runtime_profile, _issue


def _resolve_real():
    with canonical_applicable_cost_case() as (intent, plan, store, request, decision_at):
        return subject.resolve_prospective_applicable_costs(
            intent=intent,
            plan=plan,
            router_store=store,
            model_request_id=request.request_id,
            decision_at=decision_at,
        )


def _copy_component(component):
    copy = object.__new__(subject.ProspectiveApplicableCostComponent)
    for field in (
        "cost_class",
        "status",
        "reason",
        "dependency_axes",
        "source_family",
        "source_evidence_id",
        "source_sha256",
    ):
        object.__setattr__(copy, field, object.__getattribute__(component, field))
    return copy


def _copy_resolution(result, *, components=None):
    copy = object.__new__(subject.ProspectiveApplicableCostResolution)
    object.__setattr__(copy, "intent_sha256", result.intent_sha256)
    object.__setattr__(copy, "opportunity_id", result.opportunity_id)
    object.__setattr__(copy, "portfolio_plan_sha256", result.portfolio_plan_sha256)
    object.__setattr__(copy, "decision_at", result.decision_at)
    object.__setattr__(copy, "components", result.components if components is None else components)
    object.__setattr__(copy, "completeness", result.completeness)
    object.__setattr__(copy, "total_subtractable_amount", None)
    object.__setattr__(copy, "currency", None)
    return copy


def test_real_types_compose_through_sealed_router_and_model_money_authority():
    result = _resolve_real()

    assert type(result) is subject.ProspectiveApplicableCostResolution
    assert result.completeness is subject.ProspectiveApplicableCostCompleteness.INCOMPLETE
    assert result.total_subtractable_amount is None
    assert result.currency is None
    assert tuple(item.cost_class for item in result.components) == tuple(
        sorted(REQUIRED_COST_CLASSES, key=lambda value: value.value)
    )
    by_class = {item.cost_class: item for item in result.components}
    assert by_class[CostClass.EXECUTION_SLIPPAGE].dependency_axes == (
        subject.ProspectiveCostDependencyAxis.EXECUTION_STATE,
    )
    assert by_class[CostClass.EXECUTION_FEES_COMMISSION_TAX].dependency_axes == (
        subject.ProspectiveCostDependencyAxis.EXECUTION_STATE,
        subject.ProspectiveCostDependencyAxis.TERMINAL_STATE,
    )
    assert by_class[CostClass.MODEL_COMPUTE_AI].source_evidence_id is not None


def test_public_resolver_has_no_caller_money_applicability_or_source_inputs():
    parameters = set(inspect.signature(subject.resolve_prospective_applicable_costs).parameters)
    assert parameters == {"intent", "plan", "router_store", "model_request_id", "decision_at"}
    assert not parameters.intersection(
        {"amount", "currency", "costs", "applicability", "source", "source_ref", "known_zero"}
    )


def test_public_constructors_cannot_mint_positive_or_component_authority():
    with pytest.raises(subject.ProspectiveApplicableCostError, match="resolver-owned"):
        subject.ProspectiveApplicableCostComponent()
    with pytest.raises(subject.ProspectiveApplicableCostError, match="resolver-owned"):
        subject.ProspectiveApplicableCostResolution(
            completeness="COMPLETE",
            total_subtractable_amount=Decimal("1"),
            currency="EUR",
        )


def test_sealed_validator_rejects_exact_object_new_positive_resolution():
    result = _resolve_real()
    forged = _copy_resolution(result)
    object.__setattr__(forged, "completeness", "COMPLETE")
    object.__setattr__(forged, "total_subtractable_amount", Decimal("1"))
    object.__setattr__(forged, "currency", "EUR")

    with pytest.raises(
        subject.ProspectiveApplicableCostError,
        match="cannot represent COMPLETE",
    ):
        subject._SEALED_RESOLUTION_VALIDATOR(forged)


def test_sealed_validator_rejects_semantic_table_reauthoring():
    result = _resolve_real()
    components = list(result.components)
    index = next(
        i for i, item in enumerate(components)
        if item.cost_class is CostClass.EXECUTION_SLIPPAGE
    )
    forged = _copy_component(components[index])
    object.__setattr__(
        forged,
        "status",
        subject.ProspectiveCostResolutionStatus.TERMINAL_STATE_DEPENDENT,
    )
    object.__setattr__(
        forged,
        "dependency_axes",
        (subject.ProspectiveCostDependencyAxis.TERMINAL_STATE,),
    )
    components[index] = forged
    resolution = _copy_resolution(result, components=tuple(components))

    with pytest.raises(
        subject.ProspectiveApplicableCostError,
        match="sealed canonical schema-v3 semantic tuple",
    ):
        subject._SEALED_RESOLUTION_VALIDATOR(resolution)


def test_sealed_validator_rejects_duplicate_or_missing_required_class():
    result = _resolve_real()
    components = result.components[:-1] + (result.components[0],)
    forged = _copy_resolution(result, components=components)

    with pytest.raises(
        subject.ProspectiveApplicableCostError,
        match="each required cost class exactly once",
    ):
        subject._SEALED_RESOLUTION_VALIDATOR(forged)


def test_module_type_model_resolver_and_semantic_rebinding_cannot_redirect_sealed_resolver(
    monkeypatch,
):
    sealed_resolver = subject.resolve_prospective_applicable_costs
    attacker_called = False

    def attacker_model_resolver(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("forged model-money resolver must never execute")

    monkeypatch.setattr(subject, "OpportunityIntent", object)
    monkeypatch.setattr(subject, "PortfolioPlan", object)
    monkeypatch.setattr(subject, "ModelComputeRouterStore", object)
    monkeypatch.setattr(subject, "ProspectiveModelComputeMoneyEvidence", object)
    monkeypatch.setattr(subject, "resolve_prospective_model_compute_money", attacker_model_resolver)
    monkeypatch.setattr(subject, "ProspectiveApplicableCostComponent", object)
    monkeypatch.setattr(subject, "ProspectiveApplicableCostResolution", object)
    subject._STATUS_AXES.clear()
    subject._COMPONENT_SEMANTICS.clear()
    monkeypatch.setattr(subject, "REQUIRED_COST_CLASSES", frozenset())

    with canonical_applicable_cost_case() as (intent, plan, store, request, decision_at):
        result = sealed_resolver(
            intent=intent,
            plan=plan,
            router_store=store,
            model_request_id=request.request_id,
            decision_at=decision_at,
        )

    assert attacker_called is False
    sealed_types = subject._SEALED_CANONICAL_TYPES
    assert type(result) is sealed_types[2]
    subject._SEALED_RESOLUTION_VALIDATOR(result)
    assert len(result.components) == 5


def test_public_resolver_name_rebinding_does_not_mutate_preinstalled_sealed_capability(
    monkeypatch,
):
    sealed_resolver = subject.resolve_prospective_applicable_costs
    attacker_called = False

    def attacker_resolver(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound public mirror is not sealed authority")

    monkeypatch.setattr(subject, "resolve_prospective_applicable_costs", attacker_resolver)

    with canonical_applicable_cost_case() as (intent, plan, store, request, decision_at):
        result = sealed_resolver(
            intent=intent,
            plan=plan,
            router_store=store,
            model_request_id=request.request_id,
            decision_at=decision_at,
        )

    assert attacker_called is False
    subject._SEALED_RESOLUTION_VALIDATOR(result)


def _known_zero_slippage_assertion(result, *, with_source=True):
    components = list(result.components)
    index = next(
        i for i, item in enumerate(components)
        if item.cost_class is CostClass.EXECUTION_SLIPPAGE
    )
    source = "a" * 64 if with_source else None
    candidate = _copy_component(components[index])
    object.__setattr__(
        candidate,
        "status",
        subject.ProspectiveCostResolutionStatus.KNOWN_ZERO,
    )
    object.__setattr__(
        candidate,
        "reason",
        subject.ProspectiveApplicableCostReason.BETFAIR_STANDARD_LIMIT_ZERO_ADVERSE_PRICE,
    )
    object.__setattr__(candidate, "dependency_axes", ())
    object.__setattr__(
        candidate,
        "source_family",
        (
            "autosport.betfair_standard_limit_price_bound"
            if with_source
            else None
        ),
    )
    object.__setattr__(candidate, "source_evidence_id", source)
    object.__setattr__(candidate, "source_sha256", source)
    components[index] = candidate
    return _copy_resolution(result, components=tuple(components))


def test_schema_v3_can_represent_source_backed_known_zero_slippage_without_complete_money():
    result = _resolve_real()
    source_backed = _known_zero_slippage_assertion(result)

    subject._SEALED_RESOLUTION_VALIDATOR(source_backed)

    by_class = {item.cost_class: item for item in source_backed.components}
    slippage = by_class[CostClass.EXECUTION_SLIPPAGE]
    assert slippage.status is subject.ProspectiveCostResolutionStatus.KNOWN_ZERO
    assert slippage.dependency_axes == ()
    assert slippage.source_evidence_id == "a" * 64
    assert source_backed.completeness is (
        subject.ProspectiveApplicableCostCompleteness.INCOMPLETE
    )
    assert source_backed.total_subtractable_amount is None
    assert source_backed.currency is None


def test_schema_v3_known_zero_slippage_requires_product_source_identity():
    result = _resolve_real()
    source_less = _known_zero_slippage_assertion(result, with_source=False)

    with pytest.raises(
        subject.ProspectiveApplicableCostError,
        match="source family|source authority|requires evidence",
    ):
        subject._SEALED_RESOLUTION_VALIDATOR(source_less)


def test_schema_v3_known_zero_slippage_rejects_hostile_source_family_equality():
    result = _resolve_real()
    source_backed = _known_zero_slippage_assertion(result)
    slippage = next(
        item
        for item in source_backed.components
        if item.cost_class is CostClass.EXECUTION_SLIPPAGE
    )

    class HostileSourceFamily:
        def __eq__(self, _other):
            return True

        def __ne__(self, _other):
            return False

    object.__setattr__(slippage, "source_family", HostileSourceFamily())

    with pytest.raises(
        subject.ProspectiveApplicableCostError,
        match="source_family must be a non-empty canonical string",
    ):
        subject._SEALED_RESOLUTION_VALIDATOR(source_backed)


def test_product_slippage_resolver_rejects_verified_source_from_different_intent_and_plan(
    monkeypatch,
    tmp_path,
):
    bound, approval, issuance_store, _issued = _issue(monkeypatch, tmp_path)
    action = bound.execution_plan.actions[0]
    ledger = RealExecutionLedger(issuance_store.workspace / "execution-ledger.jsonl")
    ledger.reserve_plan(bound.execution_plan)
    ledger.bind_supervised_approval(
        plan_id=bound.execution_plan.plan_id,
        approval_id=approval.ledger_identity,
        approval_fingerprint=approval.fingerprint,
        approved_at=approval.approved_at,
        evidence_sha256=approval.evidence_sha256,
    )
    evidence = resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action.action_id,
    )

    with canonical_applicable_cost_case() as (
        intent,
        plan,
        router_store,
        request,
        decision_at,
    ):
        with _active_runtime_profile(issuance_store.workspace) as runtime_profile:
            with pytest.raises(
                subject.ProspectiveApplicableCostError,
                match="intent mismatch|portfolio plan mismatch",
            ):
                subject.resolve_prospective_applicable_costs_with_betfair_standard_limit(
                    intent=intent,
                    plan=plan,
                    router_store=router_store,
                    model_request_id=request.request_id,
                    decision_at=decision_at,
                    slippage_evidence=evidence,
                    ledger=ledger,
                    issuance_store=issuance_store,
                    runtime_profile=runtime_profile,
                    execution_plan_id=bound.execution_plan.plan_id,
                    action_id=action.action_id,
                )


def test_product_slippage_resolver_does_not_accept_caller_money_or_known_zero_flags():
    parameters = set(
        inspect.signature(
            subject.resolve_prospective_applicable_costs_with_betfair_standard_limit
        ).parameters
    )
    assert {
        "slippage_evidence",
        "ledger",
        "issuance_store",
        "runtime_profile",
        "execution_plan_id",
        "action_id",
    } <= parameters
    assert not parameters.intersection(
        {"amount", "currency", "known_zero", "slippage_amount", "commission_rate"}
    )


def test_schema_v2_dependency_axes_cannot_be_flattened_to_scalar_money():
    result = _resolve_real()
    by_class = {item.cost_class: item for item in result.components}
    slippage = by_class[CostClass.EXECUTION_SLIPPAGE]
    fees = by_class[CostClass.EXECUTION_FEES_COMMISSION_TAX]

    assert slippage.status is subject.ProspectiveCostResolutionStatus.EXECUTION_STATE_DEPENDENT
    assert subject.ProspectiveCostDependencyAxis.TERMINAL_STATE not in slippage.dependency_axes
    assert fees.status is (
        subject.ProspectiveCostResolutionStatus.EXECUTION_AND_TERMINAL_STATE_DEPENDENT
    )
    assert subject.ProspectiveCostDependencyAxis.TERMINAL_STATE in fees.dependency_axes
    assert all(item.to_dict()["amount"] is None for item in result.components)
    assert all(item.to_dict()["currency"] is None for item in result.components)


def test_exact_input_subclasses_are_rejected_before_product_authority_read():
    sealed_types = subject._SEALED_CANONICAL_TYPES
    intent_cls, plan_cls, store_cls = sealed_types[3:]

    class IntentSubclass(intent_cls):
        pass

    class PlanSubclass(plan_cls):
        pass

    class StoreSubclass(store_cls):
        pass

    with canonical_applicable_cost_case() as (intent, plan, store, request, decision_at):
        cases = (
            (IntentSubclass.__new__(IntentSubclass), plan, store, "OpportunityIntent"),
            (intent, PlanSubclass.__new__(PlanSubclass), store, "PortfolioPlan"),
            (intent, plan, StoreSubclass.__new__(StoreSubclass), "ModelComputeRouterStore"),
        )
        for candidate_intent, candidate_plan, candidate_store, error in cases:
            with pytest.raises(subject.ProspectiveApplicableCostError, match=error):
                subject.resolve_prospective_applicable_costs(
                    intent=candidate_intent,
                    plan=candidate_plan,
                    router_store=candidate_store,
                    model_request_id=request.request_id,
                    decision_at=decision_at,
                )


def test_betfair_cost_resolver_rejects_in_place_base_resolver_code_mutation():
    resolver = subject.resolve_prospective_applicable_costs
    original_code = resolver.__code__

    def attacker_resolver(**_kwargs):
        raise AssertionError("mutated base resolver body must never execute")

    try:
        resolver.__code__ = attacker_resolver.__code__
        with pytest.raises(
            subject.ProspectiveApplicableCostError,
            match="base applicable-cost resolver authority changed",
        ):
            subject.resolve_prospective_applicable_costs_with_betfair_standard_limit(
                intent=None,
                plan=None,
                router_store=None,
                model_request_id="blocked-before-validation",
                decision_at=None,
                slippage_evidence=None,
                ledger=None,
                issuance_store=None,
                runtime_profile=None,
                execution_plan_id="blocked-before-validation",
                action_id="blocked-before-validation",
            )
    finally:
        resolver.__code__ = original_code


def test_applicable_cost_resolver_rejects_in_place_model_resolver_code_mutation():
    resolver = subject.resolve_prospective_model_compute_money
    original_code = resolver.__code__

    def attacker_resolver(**_kwargs):
        raise AssertionError("mutated model resolver body must never execute")

    try:
        resolver.__code__ = attacker_resolver.__code__
        with canonical_applicable_cost_case() as (
            intent,
            plan,
            router_store,
            request,
            decision_at,
        ):
            with pytest.raises(
                subject.ProspectiveApplicableCostError,
                match="model-compute resolver authority changed",
            ):
                subject.resolve_prospective_applicable_costs(
                    intent=intent,
                    plan=plan,
                    router_store=router_store,
                    model_request_id=request.request_id,
                    decision_at=decision_at,
                )
    finally:
        resolver.__code__ = original_code


def test_applicable_cost_resolver_rejects_in_place_json_serializer_code_mutation():
    serializer = subject.json.dumps
    original_code = serializer.__code__

    def attacker_serializer(*_args, **_kwargs):
        return "{}"

    with canonical_applicable_cost_case() as (
        intent,
        plan,
        router_store,
        request,
        decision_at,
    ):
        try:
            serializer.__code__ = attacker_serializer.__code__
            with pytest.raises(
                subject.ProspectiveApplicableCostError,
                match="JSON proof serializer authority changed",
            ):
                subject.resolve_prospective_applicable_costs(
                    intent=intent,
                    plan=plan,
                    router_store=router_store,
                    model_request_id=request.request_id,
                    decision_at=decision_at,
                )
        finally:
            serializer.__code__ = original_code


def test_applicable_cost_resolver_ignores_hash_constructor_global_rebinding(monkeypatch):
    attacker_called = False

    def attacker_sha256(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound hashlib.sha256 must never execute")

    with canonical_applicable_cost_case() as (
        intent,
        plan,
        router_store,
        request,
        decision_at,
    ):
        monkeypatch.setattr(subject.hashlib, "sha256", attacker_sha256)
        result = subject.resolve_prospective_applicable_costs(
            intent=intent,
            plan=plan,
            router_store=router_store,
            model_request_id=request.request_id,
            decision_at=decision_at,
        )

    assert attacker_called is False
    subject._SEALED_RESOLUTION_VALIDATOR(result)


def test_resolver_rejects_mutated_nested_risk_context_before_hostile_attribute_read():
    attacker_called = False

    class HostileRiskContext:
        def __getattribute__(self, _name):
            nonlocal attacker_called
            attacker_called = True
            raise AssertionError("hostile nested risk-context hook must never execute")

    with canonical_applicable_cost_case() as (
        intent,
        plan,
        router_store,
        request,
        decision_at,
    ):
        original = object.__getattribute__(intent, "risk_context")
        try:
            object.__setattr__(intent, "risk_context", HostileRiskContext())
            with pytest.raises(
                subject.ProspectiveApplicableCostError,
                match="exact canonical ProposedTicketRiskContext",
            ):
                subject.resolve_prospective_applicable_costs(
                    intent=intent,
                    plan=plan,
                    router_store=router_store,
                    model_request_id=request.request_id,
                    decision_at=decision_at,
                )
        finally:
            object.__setattr__(intent, "risk_context", original)

    assert attacker_called is False


def test_resolver_rejects_datetime_subclass_before_timezone_hook_executes():
    attacker_called = False

    class HostileDatetime(datetime):
        def astimezone(self, *_args, **_kwargs):
            nonlocal attacker_called
            attacker_called = True
            raise AssertionError("datetime subclass hook must never execute")

    with canonical_applicable_cost_case() as (
        intent,
        plan,
        router_store,
        request,
        decision_at,
    ):
        hostile = HostileDatetime.fromisoformat(decision_at.isoformat())
        with pytest.raises(
            subject.ProspectiveApplicableCostError,
            match="timezone-aware datetime/ISO-8601",
        ):
            subject.resolve_prospective_applicable_costs(
                intent=intent,
                plan=plan,
                router_store=router_store,
                model_request_id=request.request_id,
                decision_at=hostile,
            )

    assert attacker_called is False


def test_resolver_validates_plan_identity_elements_before_hostile_equality():
    attacker_called = False

    class HostileDigest(str):
        def __eq__(self, _other):
            nonlocal attacker_called
            attacker_called = True
            raise AssertionError("hostile plan digest equality must never execute")

    with canonical_applicable_cost_case() as (
        intent,
        plan,
        router_store,
        request,
        decision_at,
    ):
        original = object.__getattribute__(plan, "intent_sha256s")
        mutated = tuple(
            HostileDigest(value) if index == 0 else value
            for index, value in enumerate(original)
        )
        try:
            object.__setattr__(plan, "intent_sha256s", mutated)
            with pytest.raises(
                subject.ProspectiveApplicableCostError,
                match="portfolio plan intent_sha256 must be a non-empty canonical string",
            ):
                subject.resolve_prospective_applicable_costs(
                    intent=intent,
                    plan=plan,
                    router_store=router_store,
                    model_request_id=request.request_id,
                    decision_at=decision_at,
                )
        finally:
            object.__setattr__(plan, "intent_sha256s", original)

    assert attacker_called is False



def test_slippage_source_digest_ignores_mutable_evidence_id_property(
    monkeypatch,
    tmp_path,
):
    bound, _approval, _issuance_store, _issued = _issue(monkeypatch, tmp_path)
    action = bound.execution_plan.actions[0]
    evidence = resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action.action_id,
    )
    canonical_evidence_id = evidence.evidence_id
    closure = inspect.getclosurevars(
        subject.resolve_prospective_applicable_costs_with_betfair_standard_limit
    )
    sealed_digest = closure.nonlocals["slippage_evidence_id"]
    attacker_called = False

    def attacker_property(_self):
        nonlocal attacker_called
        attacker_called = True
        return "f" * 64

    monkeypatch.setattr(
        type(evidence),
        "evidence_id",
        property(attacker_property),
    )

    assert sealed_digest(evidence) == canonical_evidence_id
    assert attacker_called is False


def test_resolver_rejects_intent_digest_property_rebinding_before_getter_executes(
    monkeypatch,
):
    attacker_called = False

    def attacker_getter(_self):
        nonlocal attacker_called
        attacker_called = True
        return "f" * 64

    with canonical_applicable_cost_case() as (
        intent,
        plan,
        router_store,
        request,
        decision_at,
    ):
        monkeypatch.setattr(type(intent), "intent_sha256", property(attacker_getter))
        with pytest.raises(
            subject.ProspectiveApplicableCostError,
            match="OpportunityIntent digest authority changed",
        ):
            subject.resolve_prospective_applicable_costs(
                intent=intent,
                plan=plan,
                router_store=router_store,
                model_request_id=request.request_id,
                decision_at=decision_at,
            )

    assert attacker_called is False


def test_resolver_rejects_plan_digest_property_rebinding_before_getter_executes(
    monkeypatch,
):
    attacker_called = False

    def attacker_getter(_self):
        nonlocal attacker_called
        attacker_called = True
        return "f" * 64

    with canonical_applicable_cost_case() as (
        intent,
        plan,
        router_store,
        request,
        decision_at,
    ):
        monkeypatch.setattr(type(plan), "plan_sha256", property(attacker_getter))
        with pytest.raises(
            subject.ProspectiveApplicableCostError,
            match="PortfolioPlan digest authority changed",
        ):
            subject.resolve_prospective_applicable_costs(
                intent=intent,
                plan=plan,
                router_store=router_store,
                model_request_id=request.request_id,
                decision_at=decision_at,
            )

    assert attacker_called is False



def test_slippage_source_digest_matches_canonical_non_ascii_encoding(
    monkeypatch,
    tmp_path,
):
    bound, _approval, _issuance_store, _issued = _issue(monkeypatch, tmp_path)
    action = bound.execution_plan.actions[0]
    evidence = resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action.action_id,
    )
    object.__setattr__(evidence, "account_id", "рахунок-1")
    canonical_evidence_id = evidence.evidence_id
    closure = inspect.getclosurevars(
        subject.resolve_prospective_applicable_costs_with_betfair_standard_limit
    )
    sealed_digest = closure.nonlocals["slippage_evidence_id"]

    assert sealed_digest(evidence) == canonical_evidence_id


def test_resolver_rejects_custom_tzinfo_before_timezone_hooks_execute():
    attacker_called = False

    class HostileTimezone(tzinfo):
        def utcoffset(self, _dt):
            nonlocal attacker_called
            attacker_called = True
            raise AssertionError("custom tzinfo hook must never execute")

        def dst(self, _dt):
            nonlocal attacker_called
            attacker_called = True
            raise AssertionError("custom tzinfo hook must never execute")

        def tzname(self, _dt):
            return "HOSTILE"

    with canonical_applicable_cost_case() as (
        intent,
        plan,
        router_store,
        request,
        decision_at,
    ):
        hostile = datetime(
            decision_at.year,
            decision_at.month,
            decision_at.day,
            decision_at.hour,
            decision_at.minute,
            decision_at.second,
            decision_at.microsecond,
            tzinfo=HostileTimezone(),
        )
        with pytest.raises(
            subject.ProspectiveApplicableCostError,
            match="exact UTC timezone authority",
        ):
            subject.resolve_prospective_applicable_costs(
                intent=intent,
                plan=plan,
                router_store=router_store,
                model_request_id=request.request_id,
                decision_at=hostile,
            )

    assert attacker_called is False
