from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.drift_control import (
    DriftCausalityError, DriftKind, DriftMetric, DriftMonitor, DriftState, DriftWindow,
)
from autosport.forward_campaign_assembly import assemble_forward_campaign
from autosport.forward_campaign_population import PopulationRole
from autosport.forward_campaign_reward_drift import (
    PAPER_REWARD_DRIFT_METRIC_SHA256,
    ForwardRewardDriftBindingError,
    FrozenForwardRewardDriftPlan,
    bind_forward_session_reward_drift,
    restore_forward_session_reward_drift,
    verify_forward_campaign_reward_drift,
)
from autosport.forward_campaign_reward_drift import _reward_text
from autosport.forward_evidence_completeness import (
    DecisionState, build_cohort_root, verify_campaign,
)
from autosport.scientific_registry import (
    DatasetSnapshot, ExperimentRecord, FeatureSet, ModelVersion, ResearchOutcome,
    ScientificRegistry, StrategyVersion,
)
from test_forward_campaign_assembly import SOURCE, _inputs, _ts


REGIMES = ("table-tennis:live", "table-tennis:pre-match")


def _baseline_registry(tmp_path, *, baseline_as_of=-180, metric_sha=None, crosswire=False):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "drift-registry.json")
    registry.append(DatasetSnapshot(
        "training", "a" * 64, SOURCE, "fixture-only-license", _ts(-300), _ts(-290),
    ))
    registry.append(FeatureSet("features", "v1", "b" * 64, "c" * 64, _ts(-290)))
    registry.append(ModelVersion(
        model_version_id="model", model_family="fixture-baseline", artifact_sha256="a" * 64,
        source_sha256="b" * 64, environment_sha256="c" * 64,
        dataset_snapshot_id="training", feature_set_id="features",
        research_protocol_id="scientific-protocol", seed=7, config_sha256="d" * 64,
        created_at=_ts(-280),
    ))
    registry.append(StrategyVersion(
        strategy_version_id="strategy", canonical_strategy_id="fixture-strategy",
        source_sha256="b" * 64, environment_sha256="c" * 64, config_sha256="d" * 64,
        created_at=_ts(-270), model_version_id="model",
    ))
    monitor = DriftMonitor(registry)
    references = []
    for index, regime in enumerate(REGIMES):
        dataset = f"baseline-{index}"
        window = DriftWindow.from_samples(
            dataset_snapshot_id=dataset, source_identity=SOURCE,
            window_start=_ts(-200), window_end=_ts(-190), as_of=_ts(baseline_as_of),
            values=("-3", "-3"), value_observed_at=(_ts(-195), _ts(-191)),
            value_available_at=(_ts(-190), _ts(-190)),
            sport="table-tennis", league="fixture-league", regime=regime,
        )
        registry.append(DatasetSnapshot(
            dataset, window.evidence_sha256, SOURCE, "fixture-only-license",
            _ts(-190), _ts(-185),
        ))
        experiment = f"baseline-experiment-{index}"
        registry.append(ExperimentRecord(
            experiment_id=experiment, research_protocol_id="scientific-protocol",
            dataset_snapshot_id=dataset, feature_set_id="features", strategy_version_id="strategy",
            evaluation_bundle_id=f"baseline-evaluation-{index}", seed=7, config_sha256="d" * 64,
            outcome=ResearchOutcome.INCONCLUSIVE, created_at=_ts(-184),
            model_version_id="model", completed_at=_ts(-182),
        ))
        reference = monitor.create_reference(
            drift_kind=DriftKind.FORECAST_PERFORMANCE, metric=DriftMetric.MEAN_ABSOLUTE_SHIFT,
            model_version_id="model", strategy_version_id="strategy", feature_set_id="features",
            experiment_id=experiment, baseline=window, min_samples=1, threshold="0.5",
            metric_definition_sha256=metric_sha or PAPER_REWARD_DRIFT_METRIC_SHA256,
        )
        references.append((SOURCE, regime, reference.reference_id, experiment))
    if crosswire:
        first, second = references
        references = [(*first[:2], *second[2:]), (*second[:2], *first[2:])]
    plan = FrozenForwardRewardDriftPlan(
        "forward-reward-drift-v1", "model", "strategy", "features", tuple(references),
    )
    return registry, monitor, plan


def _rebind_evidence(evidence, plan):
    protocol = replace(evidence.protocol, baseline_set_sha256=plan.plan_sha256)
    previous = "GENESIS"
    opportunities = []
    for opportunity in evidence.opportunities:
        value = replace(
            opportunity, protocol_sha256=protocol.protocol_sha256,
            predecessor_opportunity_sha256=previous,
        )
        opportunities.append(value)
        previous = value.opportunity_sha256
    previous = "GENESIS"
    roots = []
    for root in evidence.cohort_roots:
        value = build_cohort_root(
            tuple(opportunities[:root.last_sequence]), previous_cohort_root_sha256=previous,
            anchor_lower=root.anchor_lower, anchor_upper=root.anchor_upper,
        )
        roots.append(value)
        previous = value.cohort_root_sha256
    closes = tuple(replace(close, protocol_sha256=protocol.protocol_sha256,
                           terminal_cohort_root_sha256=previous) for close in evidence.closes)
    result = replace(evidence, protocol=protocol, opportunities=tuple(opportunities),
                     cohort_roots=tuple(roots), closes=closes)
    assert verify_campaign(result).ok
    return result


def _record_findings(monitor, plan, session, samples, *, variant=None):
    references = {member[:2]: member[2] for member in plan.references}
    selected = tuple(sample for sample in samples
                     if sample.role in {PopulationRole.NEW, PopulationRole.REPLAY})
    groups = sorted({(sample.source_id, sample.regime_id) for sample in selected})
    result = []
    for index, (source, regime) in enumerate(groups):
        actions = sorted((sample for sample in selected
                          if sample.source_id == source and sample.regime_id == regime
                          and sample.decision_state is DecisionState.ACTION),
                         key=lambda sample: sample.sample_id)
        values = tuple(str(sample.observed_reward) for sample in actions)
        observed = tuple(sample.outcome_available_at for sample in actions)
        available = observed
        if variant == "wrong-reward" and actions:
            values = tuple("999" for _ in actions)
        if variant == "retroactive-availability" and actions:
            observed = available = tuple(sample.decided_at for sample in actions)
        if variant == "include-holdout" and actions:
            holdout = next(sample for sample in samples
                           if sample.role is PopulationRole.HOLDOUT)
            values = (*values, "123")
            observed = (*observed, holdout.outcome_available_at)
            available = observed
        if variant == "invent-negative-zero" and not actions:
            values, observed, available = ("0",), (session.closed_at,), (session.closed_at,)
        window = DriftWindow.from_samples(
            dataset_snapshot_id=f"current-{session.session_id}-{index}", source_identity=source,
            window_start=session.opened_at, window_end=session.closed_at, as_of=session.closed_at,
            values=values, value_observed_at=observed, value_available_at=available,
            sport="table-tennis", league="fixture-league", regime=regime,
        )
        monitor.scientific_registry.append(DatasetSnapshot(
            window.dataset_snapshot_id, window.evidence_sha256, source,
            "fixture-only-license", session.closed_at, session.closed_at,
        ))
        finding = monitor.evaluate(
            references[(source, regime)], window, evaluated_at=session.closed_at
        )
        monitor.require_canonical_finding(finding.finding_id, as_of=session.closed_at)
        result.append((source, regime, finding.finding_id))
    return tuple(result)


def _scenario(
    tmp_path, *, session_count=2, variant=None, baseline_as_of=-180,
    metric_sha=None, crosswire=False,
):
    inputs = _inputs(tmp_path, session_count=session_count)
    registry, monitor, plan = _baseline_registry(
        tmp_path, baseline_as_of=baseline_as_of, metric_sha=metric_sha, crosswire=crosswire,
    )
    evidence = _rebind_evidence(inputs["evidence"], plan)
    inputs = {**inputs, "evidence": evidence}
    provisional = assemble_forward_campaign(**inputs)
    args, bindings = [], []
    for session in provisional.manifest.sessions:
        samples = tuple(sample for sample in provisional.manifest.samples
                        if sample.session_id == session.session_id)
        findings = _record_findings(monitor, plan, session, samples, variant=variant)
        args.append({
            "protocol": evidence.protocol, "plan": plan, "monitor": monitor,
            "session_id": session.session_id, "opened_at": session.opened_at,
            "closed_at": session.closed_at, "samples": samples, "findings": findings,
        })
    if variant is None and baseline_as_of < 0 and metric_sha is None and not crosswire:
        bindings = tuple(bind_forward_session_reward_drift(**item) for item in args)
        windows = tuple(
            replace(window, drift_evidence_sha256=binding.binding_sha256)
            for window, binding in zip(inputs["session_windows"], bindings, strict=True)
        )
        result = assemble_forward_campaign(**{**inputs, "session_windows": windows})
    else:
        result = provisional
    return registry, monitor, plan, evidence, result, tuple(bindings), tuple(args)


def test_exact_session_scope_drift_binds_canonical_findings_without_training_holdout(tmp_path):
    registry, monitor, plan, evidence, assembly, bindings, _ = _scenario(tmp_path)
    before = registry.path.read_bytes()
    assert verify_forward_campaign_reward_drift(
        assembly, evidence, plan=plan, monitor=monitor, bindings=bindings,
    )
    assert registry.path.read_bytes() == before
    assert all(binding.excluded_evaluation_count == 2 for binding in bindings)
    assert bindings[0].scopes[0].state == DriftState.INSUFFICIENT_EVIDENCE.value
    assert bindings[0].scopes[0].action_count == 0
    assert bindings[0].scopes[0].negative_count == 1
    assert bindings[0].scopes[1].state == DriftState.NO_DRIFT.value
    assert bindings[1].scopes[1].state == DriftState.DRIFT_DETECTED.value
    assert bindings[1].scopes[1].recommendation == "RESEARCH_RETRAIN_CHALLENGER"
    assert all(binding.canonical_payload()["promotion_authorized"] is False
               for binding in bindings)


@pytest.mark.parametrize("variant", (
    "wrong-reward", "retroactive-availability", "include-holdout", "invent-negative-zero",
))
def test_even_canonically_valid_findings_cannot_use_wrong_population_window(tmp_path, variant):
    *_, args = _scenario(tmp_path, variant=variant)
    with pytest.raises(ForwardRewardDriftBindingError, match="exact training rewards"):
        bind_forward_session_reward_drift(**args[0])


def test_holdout_and_retention_reward_changes_do_not_change_training_drift_receipt(tmp_path):
    *_, bindings, args = _scenario(tmp_path)
    for original, binding in zip(args, bindings, strict=True):
        changed_samples = tuple(
            replace(sample, observed_reward=Decimal("999"))
            if sample.role in {PopulationRole.RETENTION, PopulationRole.HOLDOUT}
            and sample.observed_reward is not None else sample
            for sample in original["samples"]
        )
        rebuilt = bind_forward_session_reward_drift(**{**original, "samples": changed_samples})
        assert rebuilt.binding_sha256 == binding.binding_sha256


def test_missing_extra_or_duplicate_scope_is_rejected(tmp_path):
    *_, args = _scenario(tmp_path)
    first = args[0]
    findings = first["findings"]
    for changed in (findings[:1], (*findings, findings[0]),
                    (*findings, (SOURCE, "foreign-regime", findings[0][2]))):
        with pytest.raises(ForwardRewardDriftBindingError, match="scope"):
            bind_forward_session_reward_drift(**{**first, "findings": changed})


def test_baseline_metric_and_its_availability_are_part_of_frozen_contract(tmp_path):
    *_, args = _scenario(tmp_path, baseline_as_of=1)
    with pytest.raises(ForwardRewardDriftBindingError, match="unavailable at precommit"):
        bind_forward_session_reward_drift(**args[0])


def test_wrong_metric_definition_is_rejected_despite_valid_drift_arithmetic(tmp_path):
    *_, args = _scenario(tmp_path, metric_sha="f" * 64)
    with pytest.raises(ForwardRewardDriftBindingError, match="not observed PAPER"):
        bind_forward_session_reward_drift(**args[0])


def test_drift_plan_and_context_cannot_be_switched_after_protocol_precommit(tmp_path):
    *_, args = _scenario(tmp_path)
    first = args[0]
    changed = replace(first["plan"], model_version_id="other-model")
    with pytest.raises(ForwardRewardDriftBindingError, match="baseline-set precommit"):
        bind_forward_session_reward_drift(**{**first, "plan": changed})


def test_precommitted_but_crosswired_baseline_regime_is_not_a_valid_scope(tmp_path):
    *_, args = _scenario(tmp_path, crosswire=True)
    with pytest.raises(ForwardRewardDriftBindingError, match="baseline scope differs"):
        bind_forward_session_reward_drift(**args[0])


def test_findings_cannot_be_available_after_the_closed_session(tmp_path):
    *_, args = _scenario(tmp_path)
    first = args[0]
    with pytest.raises(DriftCausalityError, match="unavailable"):
        bind_forward_session_reward_drift(**{**first, "closed_at": _ts(130)})


def test_campaign_verifier_rejects_missing_binding_and_tampered_receipt(tmp_path):
    _, monitor, plan, evidence, assembly, bindings, _ = _scenario(tmp_path)
    with pytest.raises(ForwardRewardDriftBindingError, match="cover campaign sessions"):
        verify_forward_campaign_reward_drift(
            assembly, evidence, plan=plan, monitor=monitor, bindings=bindings[:1],
        )
    tampered = replace(bindings[0], excluded_evaluation_count=True)
    with pytest.raises(ForwardRewardDriftBindingError, match="authority replay mismatch"):
        verify_forward_campaign_reward_drift(
            assembly, evidence, plan=plan, monitor=monitor, bindings=(tampered, bindings[1]),
        )


def test_arbitrary_session_hash_cannot_substitute_for_canonical_drift_binding(tmp_path):
    _, monitor, plan, evidence, _, bindings, args = _scenario(tmp_path)
    inputs = _inputs(tmp_path)
    unbound = assemble_forward_campaign(**{**inputs, "evidence": evidence})
    with pytest.raises(ForwardRewardDriftBindingError, match="drift digest"):
        verify_forward_campaign_reward_drift(
            unbound, evidence, plan=plan, monitor=monitor, bindings=bindings,
        )


def test_restart_reopens_registry_and_replays_exact_statistical_authorities(tmp_path):
    registry, _, plan, evidence, assembly, bindings, args = _scenario(tmp_path)
    payloads = json.loads(json.dumps([binding.canonical_payload() for binding in bindings]))
    reopened = DriftMonitor(ScientificRegistry(registry.path))
    assert verify_forward_campaign_reward_drift(
        assembly, evidence, plan=plan, monitor=reopened, bindings=bindings,
    )
    assert payloads == [binding.canonical_payload() for binding in bindings]
    for payload, original, binding in zip(payloads, args, bindings, strict=True):
        restored = restore_forward_session_reward_drift(
            payload, **{**original, "monitor": reopened},
        )
        assert restored.binding_sha256 == binding.binding_sha256
    with pytest.raises(ForwardRewardDriftBindingError, match="restart payload"):
        restore_forward_session_reward_drift(
            {**payloads[0], "promotion_authorized": True},
            **{**args[0], "monitor": reopened},
        )


def test_evaluation_only_session_requires_no_training_finding_and_exports_no_signal(tmp_path):
    *_, args = _scenario(tmp_path)
    original = args[0]
    evaluation_only = tuple(sample for sample in original["samples"]
                            if sample.role in {PopulationRole.RETENTION, PopulationRole.HOLDOUT})
    binding = bind_forward_session_reward_drift(
        **{**original, "samples": evaluation_only, "findings": ()},
    )
    assert binding.scopes == ()
    assert binding.excluded_evaluation_count == 2


def test_duplicate_reference_scope_or_foreign_session_member_is_rejected(tmp_path):
    *_, args = _scenario(tmp_path)
    original = args[0]
    plan = original["plan"]
    with pytest.raises(ForwardRewardDriftBindingError, match="sorted and unique"):
        replace(plan, references=(*plan.references, plan.references[0]))
    sample = replace(original["samples"][0], session_id="foreign-session")
    with pytest.raises(ForwardRewardDriftBindingError, match="foreign session"):
        bind_forward_session_reward_drift(
            **{**original, "samples": (sample, *original["samples"][1:])},
        )


def test_multisession_endurance_has_exact_recomputed_drift_hashes(tmp_path):
    registry, monitor, plan, evidence, assembly, bindings, _ = _scenario(
        tmp_path, session_count=10
    )
    assert len(bindings) == len(assembly.manifest.sessions) == 10
    assert sum(scope.action_count + scope.negative_count
               for binding in bindings for scope in binding.scopes) == 20
    assert verify_forward_campaign_reward_drift(
        assembly, evidence, plan=plan,
        monitor=DriftMonitor(ScientificRegistry(registry.path)), bindings=bindings,
    )


def test_reward_canonicalization_bounds_exponent_expansion_before_materializing_text():
    assert _reward_text(Decimal("1E+2")) == "100"
    assert _reward_text(Decimal("-0E+1000000000")) == "0"
    for value in (Decimal("1E+1000000000"), Decimal("1E-1000000000")):
        with pytest.raises(ForwardRewardDriftBindingError, match="resource limit"):
            _reward_text(value)
