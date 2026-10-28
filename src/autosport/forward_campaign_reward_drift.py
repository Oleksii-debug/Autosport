"""Read-only binding of PAPER reward drift to exact forward-training members.

DriftMonitor and ScientificRegistry remain the only drift/statistical authorities.
This adapter proves scope, precommitted baseline and exact observed-reward window
membership. It never reads RETENTION/HOLDOUT labels into a diagnostic window,
invents an abstention reward, trains a policy, or grants promotion authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .drift_control import DriftKind, DriftMetric, DriftMonitor, DriftWindow
from .forward_campaign_assembly import ForwardCampaignAssembly
from .forward_campaign_population import ForwardPopulationSample, PopulationRole
from .forward_evidence_completeness import (
    CampaignEvidence, DecisionState, ForwardEvidenceProtocolEnvelope, verify_campaign,
)


class ForwardRewardDriftBindingError(ValueError):
    """Canonical drift evidence does not describe this exact frozen population."""


def _bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ForwardRewardDriftBindingError(f"{name} must be canonical non-empty text")
    value.encode("utf-8", errors="strict")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ForwardRewardDriftBindingError(f"{name} must be lowercase SHA-256")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardRewardDriftBindingError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardRewardDriftBindingError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: str) -> str:
    return _instant(value, "timestamp").isoformat().replace("+00:00", "Z")


def _reward_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ForwardRewardDriftBindingError("observed reward must be finite exact Decimal")
    if value.is_zero():
        return "0"
    digits = value.as_tuple()
    # Bound expansion before format(), not after allocating attacker-sized text.
    # This adapter ceiling also fits the canonical drift owner's text bound.
    fixed_length = max(len(digits.digits) + digits.exponent, 1) + max(-digits.exponent, 0) + 2
    if fixed_length > 8192:
        raise ForwardRewardDriftBindingError("observed reward exceeds drift text resource limit")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


PAPER_REWARD_DRIFT_METRIC_SHA256 = _digest({
    "schema": "autosport.forward_paper_action_reward_drift",
    "schema_version": 1,
    "values": "observed ACTION reward, ordered by sample_id, NEW/REPLAY only",
    "observed_at": "outcome_available_at",
    "available_at": "outcome_available_at",
    "abstention_reward": "EXCLUDED_NOT_ZERO",
    "evaluation_populations": "EXCLUDED",
    "interpretation": "statistical reward-performance diagnostic, not an edge claim",
})


@dataclass(frozen=True, slots=True)
class FrozenForwardRewardDriftPlan:
    """Baseline-set commitment layered on the existing forward protocol."""

    plan_id: str
    model_version_id: str
    strategy_version_id: str
    feature_set_id: str
    references: tuple[tuple[str, str, str, str], ...]

    def __post_init__(self) -> None:
        for name in ("plan_id", "model_version_id", "strategy_version_id", "feature_set_id"):
            _text(getattr(self, name), name)
        if type(self.references) is not tuple or not self.references:
            raise ForwardRewardDriftBindingError("precommitted drift references are required")
        keys = []
        for member in self.references:
            if type(member) is not tuple or len(member) != 4:
                raise ForwardRewardDriftBindingError(
                    "references must be scope/ref/experiment tuples"
                )
            _text(member[0], "source_id")
            _text(member[1], "regime_id")
            _sha(member[2], "reference_id")
            _text(member[3], "experiment_id")
            keys.append(member[:2])
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ForwardRewardDriftBindingError("reference scopes must be sorted and unique")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.forward_reward_drift_plan", "schema_version": 1,
            "plan_id": self.plan_id, "model_version_id": self.model_version_id,
            "strategy_version_id": self.strategy_version_id,
            "feature_set_id": self.feature_set_id,
            "metric_definition_sha256": PAPER_REWARD_DRIFT_METRIC_SHA256,
            "references": [list(member) for member in self.references],
        }

    @property
    def plan_sha256(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ForwardRewardDriftScopeBinding:
    source_id: str
    regime_id: str
    finding_id: str
    finding_record_sha256: str
    reference_record_sha256: str
    observation_record_sha256: str
    current_window_sha256: str
    training_sample_sha256s: tuple[str, ...]
    action_count: int
    negative_count: int
    state: str
    recommendation: str

    def canonical_payload(self) -> dict[str, object]:
        return {
            name: list(value) if type(value) is tuple else value
            for name in self.__dataclass_fields__
            for value in (getattr(self, name),)
        }


@dataclass(frozen=True, slots=True)
class ForwardSessionRewardDriftBinding:
    session_id: str
    opened_at: str
    closed_at: str
    protocol_sha256: str
    plan_sha256: str
    excluded_evaluation_count: int
    scopes: tuple[ForwardRewardDriftScopeBinding, ...]

    def payload_without_sha(self) -> dict[str, object]:
        return {
            "schema": "autosport.forward_session_reward_drift", "schema_version": 1,
            "session_id": self.session_id, "opened_at": _timestamp(self.opened_at),
            "closed_at": _timestamp(self.closed_at), "protocol_sha256": self.protocol_sha256,
            "plan_sha256": self.plan_sha256,
            "excluded_evaluation_count": self.excluded_evaluation_count,
            "scopes": [scope.canonical_payload() for scope in self.scopes],
            "profitability_claim": False, "promotion_authorized": False,
            "real_money_execution": False,
        }

    @property
    def binding_sha256(self) -> str:
        return _digest(self.payload_without_sha())

    def canonical_payload(self) -> dict[str, object]:
        return {**self.payload_without_sha(), "binding_sha256": self.binding_sha256}


def bind_forward_session_reward_drift(
    protocol: ForwardEvidenceProtocolEnvelope,
    *,
    plan: FrozenForwardRewardDriftPlan,
    monitor: DriftMonitor,
    session_id: str,
    opened_at: str,
    closed_at: str,
    samples: tuple[ForwardPopulationSample, ...],
    findings: tuple[tuple[str, str, str], ...],
) -> ForwardSessionRewardDriftBinding:
    """Read/re-prove findings; compare their complete windows to eligible samples.

    ``findings`` contains one (source, regime, finding_id) per NEW/REPLAY scope,
    including scopes that have only negative decisions and hence an empty reward
    window. Such a window remains INSUFFICIENT_EVIDENCE under DriftMonitor.
    """

    if type(protocol) is not ForwardEvidenceProtocolEnvelope:
        raise TypeError("protocol must be exact ForwardEvidenceProtocolEnvelope")
    if type(plan) is not FrozenForwardRewardDriftPlan or type(monitor) is not DriftMonitor:
        raise TypeError("plan and monitor must use exact canonical types")
    plan.__post_init__()
    if protocol.baseline_set_sha256 != plan.plan_sha256:
        raise ForwardRewardDriftBindingError("drift plan differs from baseline-set precommit")
    _text(session_id, "session_id")
    opened = _instant(opened_at, "opened_at")
    closed = _instant(closed_at, "closed_at")
    if closed < opened:
        raise ForwardRewardDriftBindingError("session closes before it opens")
    if type(samples) is not tuple or not samples:
        raise ForwardRewardDriftBindingError("session samples must be a non-empty exact tuple")
    eligible = []
    excluded_count = 0
    seen = set()
    for sample in samples:
        if type(sample) is not ForwardPopulationSample:
            raise TypeError("samples must be exact ForwardPopulationSample")
        sample.__post_init__()
        if sample.sample_id in seen or sample.session_id != session_id:
            raise ForwardRewardDriftBindingError("duplicate or foreign session sample")
        seen.add(sample.sample_id)
        if (_instant(sample.observed_at, "observed_at") < opened
                or _instant(sample.settled_at, "settled_at") > closed):
            raise ForwardRewardDriftBindingError("sample lies outside the closed session")
        if sample.role in {PopulationRole.NEW, PopulationRole.REPLAY}:
            eligible.append(sample)
        else:
            excluded_count += 1
    groups = sorted({(sample.source_id, sample.regime_id) for sample in eligible})
    if type(findings) is not tuple:
        raise TypeError("findings must be an exact tuple")
    by_scope = {}
    for member in findings:
        if type(member) is not tuple or len(member) != 3:
            raise ForwardRewardDriftBindingError("findings must be exact scope/finding triples")
        for value in member:
            _text(value, "finding member")
        key = member[:2]
        if key in by_scope:
            raise ForwardRewardDriftBindingError("duplicate finding scope")
        by_scope[key] = member[2]
    if set(by_scope) != set(groups):
        raise ForwardRewardDriftBindingError("finding scopes do not cover training population")
    references = {member[:2]: member[2:] for member in plan.references}
    result = []
    for source_id, regime_id in groups:
        finding_id = by_scope[(source_id, regime_id)]
        finding, reference, observation = monitor.require_canonical_finding(
            finding_id, as_of=closed_at
        )
        planned = references.get((source_id, regime_id))
        if planned is None or planned[0] != reference.record_id:
            raise ForwardRewardDriftBindingError("finding baseline is not the precommitted scope")
        if (
            _instant(reference.available_at, "reference availability")
            > protocol.precommit_anchor_lower
        ):
            raise ForwardRewardDriftBindingError("drift baseline was unavailable at precommit")
        for name in ("model_version_id", "strategy_version_id", "feature_set_id"):
            if finding.payload.get(name) != getattr(plan, name):
                raise ForwardRewardDriftBindingError(f"drift finding {name} differs from plan")
        if finding.payload.get("experiment_id") != planned[1]:
            raise ForwardRewardDriftBindingError("drift finding experiment differs from plan")
        if (reference.payload.get("metric_definition_sha256") != PAPER_REWARD_DRIFT_METRIC_SHA256
                or finding.payload.get("drift_kind") != DriftKind.FORECAST_PERFORMANCE.value
                or finding.payload.get("metric") != DriftMetric.MEAN_ABSOLUTE_SHIFT.value):
            raise ForwardRewardDriftBindingError(
                "drift metric is not observed PAPER action reward"
            )
        current = observation.payload
        if current.get("source_identity") != source_id or current.get("regime") != regime_id:
            raise ForwardRewardDriftBindingError(
                "drift observation source/regime differs from samples"
            )
        if (reference.payload.get("source_identity") != source_id
                or reference.payload.get("regime") != regime_id
                or reference.payload.get("sport") != current.get("sport")
                or reference.payload.get("league") != current.get("league")):
            raise ForwardRewardDriftBindingError("drift baseline scope differs from observation")
        selected = sorted(
            (sample for sample in eligible
             if sample.source_id == source_id and sample.regime_id == regime_id),
            key=lambda sample: sample.sample_id,
        )
        actions = [sample for sample in selected if sample.decision_state is DecisionState.ACTION]
        expected = DriftWindow.from_samples(
            dataset_snapshot_id=current["dataset_snapshot_id"], source_identity=source_id,
            window_start=opened_at, window_end=closed_at, as_of=closed_at,
            values=tuple(_reward_text(sample.observed_reward) for sample in actions),
            value_observed_at=tuple(sample.outcome_available_at for sample in actions),
            value_available_at=tuple(sample.outcome_available_at for sample in actions),
            effective_sample_size=current.get("effective_sample_size"),
            sport=current.get("sport"), league=current.get("league"), regime=regime_id,
        )
        if expected.evidence_sha256 != current.get("evidence_sha256"):
            raise ForwardRewardDriftBindingError(
                "drift window differs from exact training rewards"
            )
        result.append(ForwardRewardDriftScopeBinding(
            source_id=source_id, regime_id=regime_id, finding_id=finding_id,
            finding_record_sha256=finding.record_sha256,
            reference_record_sha256=reference.record_sha256,
            observation_record_sha256=observation.record_sha256,
            current_window_sha256=expected.evidence_sha256,
            training_sample_sha256s=tuple(sample.sample_sha256 for sample in selected),
            action_count=len(actions), negative_count=len(selected) - len(actions),
            state=finding.payload["state"], recommendation=finding.payload["recommendation"],
        ))
    return ForwardSessionRewardDriftBinding(
        session_id=session_id, opened_at=opened_at, closed_at=closed_at,
        protocol_sha256=protocol.protocol_sha256, plan_sha256=plan.plan_sha256,
        excluded_evaluation_count=excluded_count, scopes=tuple(result),
    )


def verify_forward_campaign_reward_drift(
    assembly: ForwardCampaignAssembly,
    evidence: CampaignEvidence,
    *,
    plan: FrozenForwardRewardDriftPlan,
    monitor: DriftMonitor,
    bindings: tuple[ForwardSessionRewardDriftBinding, ...],
) -> bool:
    """Re-prove every session's drift digest and exact canonical registry records."""

    if type(assembly) is not ForwardCampaignAssembly or type(evidence) is not CampaignEvidence:
        raise TypeError("assembly and evidence must use exact canonical types")
    assembly.__post_init__()
    verdict = verify_campaign(evidence)
    if (not verdict.ok or verdict.protocol_sha256 != assembly.protocol_sha256
            or verdict.terminal_root_sha256 != assembly.terminal_root_sha256):
        raise ForwardRewardDriftBindingError("campaign protocol/root differs from assembly")
    if type(bindings) is not tuple or any(
        type(binding) is not ForwardSessionRewardDriftBinding for binding in bindings
    ):
        raise TypeError("bindings must be an exact tuple of session drift bindings")
    by_session = {binding.session_id: binding for binding in bindings}
    sessions = assembly.manifest.sessions
    if len(by_session) != len(bindings) or set(by_session) != {s.session_id for s in sessions}:
        raise ForwardRewardDriftBindingError(
            "drift bindings do not exactly cover campaign sessions"
        )
    for session in sessions:
        binding = by_session[session.session_id]
        rebuilt = bind_forward_session_reward_drift(
            evidence.protocol, plan=plan, monitor=monitor, session_id=session.session_id,
            opened_at=session.opened_at, closed_at=session.closed_at,
            samples=tuple(sample for sample in assembly.manifest.samples
                          if sample.session_id == session.session_id),
            findings=tuple((scope.source_id, scope.regime_id, scope.finding_id)
                           for scope in binding.scopes),
        )
        if (_bytes(rebuilt.canonical_payload()) != _bytes(binding.canonical_payload())
                or rebuilt.binding_sha256 != session.drift_evidence_sha256):
            raise ForwardRewardDriftBindingError(
                "session drift digest or authority replay mismatch"
            )
    return True


def restore_forward_session_reward_drift(
    payload: object,
    protocol: ForwardEvidenceProtocolEnvelope,
    *,
    plan: FrozenForwardRewardDriftPlan,
    monitor: DriftMonitor,
    session_id: str,
    opened_at: str,
    closed_at: str,
    samples: tuple[ForwardPopulationSample, ...],
    findings: tuple[tuple[str, str, str], ...],
) -> ForwardSessionRewardDriftBinding:
    """Restore only by canonical authority replay, not by trusting saved fields."""

    rebuilt = bind_forward_session_reward_drift(
        protocol, plan=plan, monitor=monitor, session_id=session_id,
        opened_at=opened_at, closed_at=closed_at, samples=samples, findings=findings,
    )
    if _bytes(payload) != _bytes(rebuilt.canonical_payload()):
        raise ForwardRewardDriftBindingError(
            "session drift restart payload differs from authorities"
        )
    return rebuilt


__all__ = [
    "PAPER_REWARD_DRIFT_METRIC_SHA256", "ForwardRewardDriftBindingError",
    "FrozenForwardRewardDriftPlan", "ForwardRewardDriftScopeBinding",
    "ForwardSessionRewardDriftBinding", "bind_forward_session_reward_drift",
    "restore_forward_session_reward_drift", "verify_forward_campaign_reward_drift",
]
