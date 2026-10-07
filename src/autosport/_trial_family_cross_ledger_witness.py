"""Cross-ledger causal witness for trial-family sequential promotion authority.

A sequential decision and a ScientificRegistry PromotionEvidence record live in
separate durable journals. Product timestamps alone cannot prove which write was
actually durable first. This guard seals the registry prefix observed when an
exact sequential look becomes usable by the trial family, then requires the
PromotionEvidence record to be outside (after) that witnessed prefix.

The guard composes the existing journals and workspace lock. It does not create a
second registry or a second multiplicity authority.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import trial_family_accounting as _tfa
from .integrity import atomic_write_json, sha256_file
from .research_multiplicity import SequentialDecision, SequentialLookEvidence
from .scientific_registry import PromotionEvidence, ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock

_WITNESS_KIND = "SEQUENTIAL_LOOK_REGISTERED"
_WITNESS_FIELDS = {
    "attempt_id",
    "member_authority_id",
    "hypothesis_id",
    "experiment_id",
    "evaluation_bundle_id",
    "evaluation_bundle_sha256",
    "look_index",
    "evidence_sha256",
    "decision",
    "observed_at",
    "registry_record_count",
    "registry_prefix_sha256",
}

_ORIGINAL_REPLAY = _tfa.TrialFamilyAccountingStore._replay_events
_ORIGINAL_REGISTER = _tfa.TrialFamilyAccountingStore.register_sequential_look
_ORIGINAL_ASSERT_PROMOTION = _tfa.TrialFamilyAccountingStore.assert_promotion_evidence_eligible


def _validate_witness_payload(
    payload: object,
    *,
    event_at: str,
    attempts_by_id: Mapping[str, _tfa.TrialAttemptView],
    seen_evidence: set[str],
) -> None:
    if type(payload) is not dict or set(payload) != _WITNESS_FIELDS:
        raise ValueError("sequential-look witness payload fields mismatch")

    attempt_id = _tfa._sha256(payload["attempt_id"], "attempt_id")
    member_id = _tfa._sha256(payload["member_authority_id"], "member_authority_id")
    _tfa._text(payload["hypothesis_id"], "hypothesis_id")
    experiment_id = _tfa._text(payload["experiment_id"], "experiment_id")
    _tfa._text(payload["evaluation_bundle_id"], "evaluation_bundle_id")
    _tfa._sha256(payload["evaluation_bundle_sha256"], "evaluation_bundle_sha256")
    if type(payload["look_index"]) is not int or payload["look_index"] < 1:
        raise ValueError("sequential-look witness look_index must be an integer >= 1")
    evidence_sha = _tfa._sha256(payload["evidence_sha256"], "evidence_sha256")
    SequentialDecision(payload["decision"])
    observed_at = _tfa._iso(payload["observed_at"], "observed_at")
    if type(payload["registry_record_count"]) is not int or payload["registry_record_count"] < 0:
        raise ValueError("sequential-look witness registry_record_count must be non-negative")
    _tfa._sha256(payload["registry_prefix_sha256"], "registry_prefix_sha256")

    if _tfa._instant(event_at, "event_at") < _tfa._instant(observed_at, "observed_at"):
        raise ValueError("sequential-look witness publication predates its observed look")
    if evidence_sha in seen_evidence:
        raise ValueError("duplicate sequential-look witness identity in durable history")
    seen_evidence.add(evidence_sha)

    attempt = attempts_by_id.get(attempt_id)
    if attempt is None or attempt.status is not _tfa.TrialAttemptStatus.COMPLETED:
        raise ValueError("sequential-look witness requires a completed durable attempt")
    if (
        attempt.member_authority_id != member_id
        or attempt.hypothesis_id != payload["hypothesis_id"]
        or attempt.experiment_id != experiment_id
    ):
        raise ValueError("sequential-look witness does not match its completed attempt")
    if attempt.result_available_at is None or _tfa._instant(observed_at, "observed_at") < _tfa._instant(
        attempt.result_available_at, "result_available_at"
    ):
        raise ValueError("sequential-look witness predates the durable experiment result")


def _replay_events_with_sequential_witness(
    family: _tfa.TrialFamilyDefinition,
    plan: Any,
    events: tuple[dict[str, Any], ...],
    *,
    cutoff: Any,
) -> tuple[_tfa.TrialAttemptView, ...]:
    attempt_events = tuple(event for event in events if event.get("kind") != _WITNESS_KIND)
    full_attempts = _ORIGINAL_REPLAY(family, plan, attempt_events, cutoff=None)
    attempts_by_id = {attempt.attempt_id: attempt for attempt in full_attempts}
    seen_evidence: set[str] = set()
    for event in events:
        if event.get("kind") != _WITNESS_KIND:
            continue
        _validate_witness_payload(
            event.get("payload"),
            event_at=event.get("event_at"),
            attempts_by_id=attempts_by_id,
            seen_evidence=seen_evidence,
        )
    if cutoff is None:
        return full_attempts
    return _ORIGINAL_REPLAY(family, plan, attempt_events, cutoff=cutoff)


def _matching_witnesses(
    state: Mapping[str, Any],
    *,
    evidence_sha256: str,
) -> tuple[dict[str, Any], ...]:
    wanted = _tfa._sha256(evidence_sha256, "evidence_sha256")
    return tuple(
        event["payload"]
        for event in state["events"]
        if event["kind"] == _WITNESS_KIND
        and event["payload"].get("evidence_sha256") == wanted
    )


def _require_exact_witness_identity(
    payload: Mapping[str, Any],
    *,
    attempt: _tfa.TrialAttemptView,
    evidence: SequentialLookEvidence,
    decision: SequentialDecision,
) -> None:
    expected = {
        "attempt_id": attempt.attempt_id,
        "member_authority_id": evidence.member_authority_id,
        "hypothesis_id": evidence.hypothesis_id,
        "experiment_id": evidence.experiment_id,
        "evaluation_bundle_id": evidence.evaluation_bundle_id,
        "evaluation_bundle_sha256": evidence.evaluation_bundle_sha256.lower(),
        "look_index": evidence.look_index,
        "evidence_sha256": evidence.evidence_sha256,
        "decision": decision.value,
        "observed_at": evidence.observed_at,
    }
    for field, wanted in expected.items():
        if payload.get(field) != wanted:
            raise ValueError(f"sequential-look witness {field} does not match durable sequential truth")


def _publish_sequential_witness(
    store: _tfa.TrialFamilyAccountingStore,
    *,
    attempt_id: str,
    evidence: SequentialLookEvidence,
    registry: ScientificRegistry,
    decision: SequentialDecision,
) -> None:
    attempt_id = _tfa._sha256(attempt_id, "attempt_id")
    with WorkspaceEconomicLock(store.workspace_root):
        state = store._read_state()
        family = _tfa.TrialFamilyDefinition.from_payload(state["family"])
        plan = _tfa.ExperimentFamilyPlan.from_payload(state["multiplicity_plan"])
        attempts = store._replay_events(family, plan, tuple(state["events"]), cutoff=None)
        attempt = next((value for value in attempts if value.attempt_id == attempt_id), None)
        if attempt is None or attempt.status is not _tfa.TrialAttemptStatus.COMPLETED:
            raise ValueError("sequential-look witness requires a completed durable attempt")
        if (
            evidence.member_authority_id != attempt.member_authority_id
            or evidence.hypothesis_id != attempt.hypothesis_id
            or evidence.experiment_id != attempt.experiment_id
        ):
            raise ValueError("sequential-look witness does not match completed durable attempt")

        durable_assessments = tuple(
            value
            for value in store._sequential(state).assessments(evidence.member_authority_id)
            if value.evidence.evidence_sha256 == evidence.evidence_sha256
        )
        if len(durable_assessments) != 1:
            raise ValueError("sequential-look witness requires exact durable sequential evidence")
        durable_assessment = durable_assessments[0]
        if durable_assessment.decision is not decision:
            raise ValueError("sequential-look witness decision does not match durable sequential truth")

        existing = _matching_witnesses(state, evidence_sha256=evidence.evidence_sha256)
        if existing:
            if len(existing) != 1:
                raise ValueError("duplicate sequential-look witness identity in durable history")
            _require_exact_witness_identity(
                existing[0], attempt=attempt, evidence=evidence, decision=decision
            )
            return

        canonical_registry = store._require_canonical_registry(registry, state)
        registry_state = canonical_registry._read()
        record_count = len(registry_state["records"])
        payload = {
            "attempt_id": attempt.attempt_id,
            "member_authority_id": evidence.member_authority_id,
            "hypothesis_id": evidence.hypothesis_id,
            "experiment_id": evidence.experiment_id,
            "evaluation_bundle_id": evidence.evaluation_bundle_id,
            "evaluation_bundle_sha256": evidence.evaluation_bundle_sha256.lower(),
            "look_index": evidence.look_index,
            "evidence_sha256": evidence.evidence_sha256,
            "decision": decision.value,
            "observed_at": evidence.observed_at,
            "registry_record_count": record_count,
            "registry_prefix_sha256": _tfa._registry_prefix_sha256(
                registry_state, record_count
            ),
        }

        events = list(state["events"])
        event_at = evidence.observed_at
        high_water_candidates = [event_at]
        if events:
            high_water_candidates.append(events[-1]["event_at"])
        assessments = store._sequential(state).assessments()
        high_water_candidates.extend(value.evidence.observed_at for value in assessments)
        event_at = max(
            high_water_candidates,
            key=lambda value: _tfa._instant(value, "sequential witness high-water"),
        )

        previous_sha = events[-1]["event_sha256"] if events else None
        envelope = {
            "sequence": len(events) + 1,
            "kind": _WITNESS_KIND,
            "event_at": event_at,
            "payload": payload,
            "prev_event_sha256": previous_sha,
        }
        event = {**envelope, "event_sha256": _tfa._digest(envelope)}
        next_state = {**state, "events": [*events, event]}
        store._replay_events(family, plan, tuple(next_state["events"]), cutoff=None)

        observed = sha256_file(store.path)
        intended = _tfa._state_sha256(next_state)
        binding = _tfa._digest(
            {"family_id": family.family_id, "event_sha256": event["event_sha256"]}
        )
        tx_id = f"event-{event['event_sha256']}"
        authority = store._authority(family.family_id)
        authority.prepare(
            tx_id=tx_id,
            observed_state_sha256=observed,
            intended_state_sha256=intended,
            semantic_binding_sha256=binding,
        )
        atomic_write_json(store.path, next_state)
        published = sha256_file(store.path)
        if published != intended:
            raise ValueError("published trial-family bytes do not match prepared authority digest")
        authority.commit(
            tx_id=tx_id,
            observed_state_sha256=published,
            semantic_binding_sha256=binding,
        )
        store._read_state()


def _register_sequential_look_with_witness(
    self: _tfa.TrialFamilyAccountingStore,
    *,
    attempt_id: str,
    evidence: SequentialLookEvidence,
    registry: ScientificRegistry,
) -> SequentialDecision:
    decision = _ORIGINAL_REGISTER(
        self, attempt_id=attempt_id, evidence=evidence, registry=registry
    )
    _publish_sequential_witness(
        self,
        attempt_id=attempt_id,
        evidence=evidence,
        registry=registry,
        decision=decision,
    )
    return decision


def _assert_promotion_evidence_eligible_with_witness(
    self: _tfa.TrialFamilyAccountingStore,
    *,
    evidence: PromotionEvidence,
    registry: ScientificRegistry,
    accounted_attempt_count: int,
) -> _tfa.TrialFamilySnapshot:
    snapshot = _ORIGINAL_ASSERT_PROMOTION(
        self,
        evidence=evidence,
        registry=registry,
        accounted_attempt_count=accounted_attempt_count,
    )

    state = self._read_state()
    canonical_registry = self._require_canonical_registry(registry, state)
    attempts = self._attempts(as_of=evidence.created_at)
    matching_attempts = tuple(
        value
        for value in attempts
        if value.status is _tfa.TrialAttemptStatus.COMPLETED
        and value.experiment_id == evidence.experiment_id
    )
    if len(matching_attempts) != 1:
        raise ValueError("PromotionEvidence lacks exactly one completed witnessed attempt")
    attempt = matching_attempts[0]

    target_looks = tuple(
        value
        for value in self._sequential(state).assessments()
        if value.evidence.experiment_id == evidence.experiment_id
        and _tfa._instant(value.evidence.observed_at, "observed_at")
        <= _tfa._instant(evidence.created_at, "created_at")
    )
    if not target_looks:
        raise ValueError("promotion eligibility requires registered sequential evidence")
    assessment = target_looks[-1]
    witnesses = _matching_witnesses(
        state, evidence_sha256=assessment.evidence.evidence_sha256
    )
    if len(witnesses) != 1:
        raise ValueError("promotion eligibility requires one exact sequential-look registry witness")
    witness = witnesses[0]
    _require_exact_witness_identity(
        witness,
        attempt=attempt,
        evidence=assessment.evidence,
        decision=assessment.decision,
    )
    if assessment.decision is not SequentialDecision.REJECT_NULL:
        raise ValueError("sequential-look witness does not authorize rejecting the null hypothesis")

    registry_state = canonical_registry._read()
    record_count = witness["registry_record_count"]
    witnessed_prefix = _tfa._sha256(
        witness["registry_prefix_sha256"], "registry_prefix_sha256"
    )
    if _tfa._registry_prefix_sha256(registry_state, record_count) != witnessed_prefix:
        raise ValueError("ScientificRegistry no longer matches sequential-look prefix witness")

    promotion_position: int | None = None
    for index, raw in enumerate(registry_state["records"]):
        if (
            raw["record_type"] == "PromotionEvidence"
            and raw["record_id"] == evidence.promotion_evidence_id
        ):
            promotion_position = index
            break
    if promotion_position is None:
        raise ValueError("PromotionEvidence is missing from ScientificRegistry")
    if promotion_position < record_count:
        raise ValueError(
            "PromotionEvidence was already durable before sequential-look registration witness"
        )
    return snapshot


_tfa._EVENT_KINDS = frozenset((*_tfa._EVENT_KINDS, _WITNESS_KIND))
_tfa.TrialFamilyAccountingStore._replay_events = staticmethod(
    _replay_events_with_sequential_witness
)
_tfa.TrialFamilyAccountingStore.register_sequential_look = (
    _register_sequential_look_with_witness
)
_tfa.TrialFamilyAccountingStore.assert_promotion_evidence_eligible = (
    _assert_promotion_evidence_eligible_with_witness
)
