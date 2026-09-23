from __future__ import annotations

"""Fail-closed completeness proof for prospective economic evidence.

This module is deliberately persistence-agnostic. It defines immutable evidence
contracts and a deterministic verifier over evidence already captured by
Autosport's canonical ledgers/registries. It does not own decisioning,
persistence, scheduling, execution, settlement, promotion, or money authority.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Mapping, Sequence


_DOMAIN = "autosport.forward-evidence-completeness.v1"
_HEX = frozenset("0123456789abcdef")
_GENESIS = "GENESIS"


class ForwardEvidenceCompletenessError(ValueError):
    """Raised when an evidence contract is structurally invalid."""


class UniverseResult(StrEnum):
    ADMITTED = "ADMITTED"
    EXCLUDED = "EXCLUDED"


class DecisionState(StrEnum):
    ACTION = "ACTION"
    NO_BET = "NO_BET"
    WAIT = "WAIT"
    RISK_REJECT = "RISK_REJECT"
    STALE_REJECT = "STALE_REJECT"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    ANCHOR_FAILURE = "ANCHOR_FAILURE"
    OTHER_FROZEN_REASON = "OTHER_FROZEN_REASON"


class CampaignCloseState(StrEnum):
    CLOSE_PENDING = "CLOSE_PENDING"
    CLOSED = "CLOSED"


class VerificationCode(StrEnum):
    PASS = "PASS"
    PROTOCOL_PRECOMMIT_FAIL = "PROTOCOL_PRECOMMIT_FAIL"
    PROTOCOL_HASH_CONFLICT = "PROTOCOL_HASH_CONFLICT"
    RUNTIME_IDENTITY_CONFLICT = "RUNTIME_IDENTITY_CONFLICT"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    EVIDENCE_IDENTITY_CONFLICT = "EVIDENCE_IDENTITY_CONFLICT"
    HASH_CHAIN_BROKEN = "HASH_CHAIN_BROKEN"
    HASH_CHAIN_FORK = "HASH_CHAIN_FORK"
    COHORT_OMISSION_DETECTED = "COHORT_OMISSION_DETECTED"
    COHORT_ROOT_MISMATCH = "COHORT_ROOT_MISMATCH"
    COHORT_OPEN = "COHORT_OPEN"
    COHORT_CLOSE_PENDING = "COHORT_CLOSE_PENDING"
    TEMPORAL_ELIGIBILITY_UNKNOWN = "TEMPORAL_ELIGIBILITY_UNKNOWN"
    DENOMINATOR_INCOMPLETE = "DENOMINATOR_INCOMPLETE"
    STOPPING_RULE_VIOLATION = "STOPPING_RULE_VIOLATION"
    ECONOMICS_INCOMPLETE = "ECONOMICS_INCOMPLETE"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ForwardEvidenceCompletenessError(
            f"{name} must be a non-empty canonical string"
        )
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise ForwardEvidenceCompletenessError(
            f"{name} must be a canonical SHA-256 hex string"
        )
    return text


def _instant(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ForwardEvidenceCompletenessError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ForwardEvidenceCompletenessError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _duration(value: timedelta, name: str) -> timedelta:
    if not isinstance(value, timedelta) or value < timedelta(0):
        raise ForwardEvidenceCompletenessError(
            f"{name} must be a non-negative timedelta"
        )
    return value


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    envelope = {"domain": _DOMAIN, "payload": payload}
    return hashlib.sha256(_canonical_json(envelope).encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return _instant(value, "timestamp").isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ForwardEvidenceProtocolEnvelope:
    """Forward-cohort commitment layered on canonical ScientificProtocolBinding.

    ``scientific_protocol_sha256`` must be the existing
    ``ScientificProtocolBinding.binding_sha256`` (or the durable registry record
    that binds it). This envelope only adds forward-campaign completeness
    identities that the canonical scientific protocol does not own.
    """

    campaign_id: str
    scientific_protocol_sha256: str
    candidate_universe_rule_id: str
    candidate_universe_rule_sha256: str
    forward_evaluation_policy_sha256: str
    runtime_identity_sha256: str
    baseline_set_sha256: str
    protective_metric_set_sha256: str
    cost_policy_sha256: str
    precommit_anchor_lower: datetime
    precommit_anchor_upper: datetime
    serializer_version: str = "v1"
    hash_algorithm: str = "sha256"
    predecessor_protocol_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "campaign_id",
            "candidate_universe_rule_id",
            "serializer_version",
            "hash_algorithm",
        ):
            _text(getattr(self, name), name)
        for name in (
            "scientific_protocol_sha256",
            "candidate_universe_rule_sha256",
            "forward_evaluation_policy_sha256",
            "runtime_identity_sha256",
            "baseline_set_sha256",
            "protective_metric_set_sha256",
            "cost_policy_sha256",
        ):
            _sha256(getattr(self, name), name)
        if self.predecessor_protocol_sha256 is not None:
            _sha256(self.predecessor_protocol_sha256, "predecessor_protocol_sha256")
        lower = _instant(self.precommit_anchor_lower, "precommit_anchor_lower")
        upper = _instant(self.precommit_anchor_upper, "precommit_anchor_upper")
        if lower > upper:
            raise ForwardEvidenceCompletenessError(
                "precommit anchor lower must not exceed upper"
            )
        object.__setattr__(self, "precommit_anchor_lower", lower)
        object.__setattr__(self, "precommit_anchor_upper", upper)
        if self.hash_algorithm != "sha256":
            raise ForwardEvidenceCompletenessError("hash_algorithm must be sha256")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "scientific_protocol_sha256": self.scientific_protocol_sha256.lower(),
            "candidate_universe_rule_id": self.candidate_universe_rule_id,
            "candidate_universe_rule_sha256": self.candidate_universe_rule_sha256.lower(),
            "forward_evaluation_policy_sha256": self.forward_evaluation_policy_sha256.lower(),
            "runtime_identity_sha256": self.runtime_identity_sha256.lower(),
            "baseline_set_sha256": self.baseline_set_sha256.lower(),
            "protective_metric_set_sha256": self.protective_metric_set_sha256.lower(),
            "cost_policy_sha256": self.cost_policy_sha256.lower(),
            "precommit_anchor_lower": _iso(self.precommit_anchor_lower),
            "precommit_anchor_upper": _iso(self.precommit_anchor_upper),
            "serializer_version": self.serializer_version,
            "hash_algorithm": self.hash_algorithm,
            "predecessor_protocol_sha256": (
                self.predecessor_protocol_sha256.lower()
                if self.predecessor_protocol_sha256 is not None
                else None
            ),
        }

    @property
    def protocol_sha256(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ForwardOpportunityEnvelope:
    campaign_id: str
    protocol_sha256: str
    candidate_sequence: int
    opportunity_id: str
    source_receipt_id: str
    source_receipt_sha256: str
    causal_cutoff: datetime
    observed_lower: datetime
    observed_upper: datetime
    universe_rule_result: UniverseResult
    universe_rule_reason_code: str
    provider_acquisition_state: str
    reveal_boundary_receipt_id: str
    runtime_identity_sha256: str
    predecessor_opportunity_sha256: str
    decision_state: DecisionState
    decision_id: str | None = None
    quote_or_market_identity: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "campaign_id",
            "opportunity_id",
            "source_receipt_id",
            "universe_rule_reason_code",
            "provider_acquisition_state",
            "reveal_boundary_receipt_id",
        ):
            _text(getattr(self, name), name)
        if isinstance(self.candidate_sequence, bool) or not isinstance(
            self.candidate_sequence, int
        ) or self.candidate_sequence <= 0:
            raise ForwardEvidenceCompletenessError(
                "candidate_sequence must be a positive integer"
            )
        _sha256(self.protocol_sha256, "protocol_sha256")
        _sha256(self.source_receipt_sha256, "source_receipt_sha256")
        _sha256(self.runtime_identity_sha256, "runtime_identity_sha256")
        if self.predecessor_opportunity_sha256 != _GENESIS:
            _sha256(
                self.predecessor_opportunity_sha256,
                "predecessor_opportunity_sha256",
            )
        if not isinstance(self.universe_rule_result, UniverseResult):
            raise ForwardEvidenceCompletenessError(
                "universe_rule_result must be a UniverseResult"
            )
        if not isinstance(self.decision_state, DecisionState):
            raise ForwardEvidenceCompletenessError(
                "decision_state must be a DecisionState"
            )
        if (
            self.universe_rule_result is UniverseResult.EXCLUDED
            and self.decision_state is DecisionState.ACTION
        ):
            raise ForwardEvidenceCompletenessError(
                "an excluded candidate cannot have ACTION decision_state"
            )
        if self.decision_state is DecisionState.ACTION and self.decision_id is None:
            raise ForwardEvidenceCompletenessError(
                "ACTION decision_state requires decision_id"
            )
        if self.decision_id is not None:
            _text(self.decision_id, "decision_id")
        if self.quote_or_market_identity is not None:
            _text(self.quote_or_market_identity, "quote_or_market_identity")
        causal = _instant(self.causal_cutoff, "causal_cutoff")
        lower = _instant(self.observed_lower, "observed_lower")
        upper = _instant(self.observed_upper, "observed_upper")
        if lower > upper:
            raise ForwardEvidenceCompletenessError(
                "observed lower must not exceed upper"
            )
        if causal > upper:
            raise ForwardEvidenceCompletenessError(
                "causal cutoff cannot be after the observation interval"
            )
        object.__setattr__(self, "causal_cutoff", causal)
        object.__setattr__(self, "observed_lower", lower)
        object.__setattr__(self, "observed_upper", upper)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "protocol_sha256": self.protocol_sha256.lower(),
            "candidate_sequence": self.candidate_sequence,
            "opportunity_id": self.opportunity_id,
            "source_receipt_id": self.source_receipt_id,
            "source_receipt_sha256": self.source_receipt_sha256.lower(),
            "causal_cutoff": _iso(self.causal_cutoff),
            "observed_lower": _iso(self.observed_lower),
            "observed_upper": _iso(self.observed_upper),
            "universe_rule_result": self.universe_rule_result.value,
            "universe_rule_reason_code": self.universe_rule_reason_code,
            "provider_acquisition_state": self.provider_acquisition_state,
            "reveal_boundary_receipt_id": self.reveal_boundary_receipt_id,
            "runtime_identity_sha256": self.runtime_identity_sha256.lower(),
            "predecessor_opportunity_sha256": self.predecessor_opportunity_sha256,
            "decision_state": self.decision_state.value,
            "decision_id": self.decision_id,
            "quote_or_market_identity": self.quote_or_market_identity,
        }

    @property
    def opportunity_sha256(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class CohortRootEnvelope:
    campaign_id: str
    protocol_sha256: str
    first_sequence: int
    last_sequence: int
    candidate_count: int
    ordered_leaf_sha256: str
    previous_cohort_root_sha256: str
    anchor_lower: datetime | None
    anchor_upper: datetime | None

    def __post_init__(self) -> None:
        _text(self.campaign_id, "campaign_id")
        _sha256(self.protocol_sha256, "protocol_sha256")
        if self.previous_cohort_root_sha256 != _GENESIS:
            _sha256(
                self.previous_cohort_root_sha256,
                "previous_cohort_root_sha256",
            )
        _sha256(self.ordered_leaf_sha256, "ordered_leaf_sha256")
        for name in ("first_sequence", "last_sequence", "candidate_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ForwardEvidenceCompletenessError(
                    f"{name} must be a positive integer"
                )
        if self.first_sequence > self.last_sequence:
            raise ForwardEvidenceCompletenessError(
                "first_sequence must not exceed last_sequence"
            )
        expected_count = self.last_sequence - self.first_sequence + 1
        if self.candidate_count != expected_count:
            raise ForwardEvidenceCompletenessError(
                "candidate_count must equal the inclusive sequence span"
            )
        if (self.anchor_lower is None) != (self.anchor_upper is None):
            raise ForwardEvidenceCompletenessError(
                "cohort root anchor interval must be fully present or fully absent"
            )
        if self.anchor_lower is not None and self.anchor_upper is not None:
            lower = _instant(self.anchor_lower, "anchor_lower")
            upper = _instant(self.anchor_upper, "anchor_upper")
            if lower > upper:
                raise ForwardEvidenceCompletenessError(
                    "anchor lower must not exceed upper"
                )
            object.__setattr__(self, "anchor_lower", lower)
            object.__setattr__(self, "anchor_upper", upper)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "protocol_sha256": self.protocol_sha256.lower(),
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "candidate_count": self.candidate_count,
            "ordered_leaf_sha256": self.ordered_leaf_sha256.lower(),
            "previous_cohort_root_sha256": self.previous_cohort_root_sha256,
            "anchor_lower": _iso(self.anchor_lower) if self.anchor_lower else None,
            "anchor_upper": _iso(self.anchor_upper) if self.anchor_upper else None,
        }

    @property
    def cohort_root_sha256(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class CampaignCloseEnvelope:
    campaign_id: str
    protocol_sha256: str
    terminal_cohort_root_sha256: str
    final_candidate_count: int
    first_sequence: int
    last_sequence: int
    close_reason: str
    close_state: CampaignCloseState = CampaignCloseState.CLOSED
    anchor_lower: datetime | None = None
    anchor_upper: datetime | None = None
    predecessor_close_sha256: str | None = None

    def __post_init__(self) -> None:
        _text(self.campaign_id, "campaign_id")
        _sha256(self.protocol_sha256, "protocol_sha256")
        _sha256(
            self.terminal_cohort_root_sha256,
            "terminal_cohort_root_sha256",
        )
        _text(self.close_reason, "close_reason")
        if not isinstance(self.close_state, CampaignCloseState):
            raise ForwardEvidenceCompletenessError(
                "close_state must be a CampaignCloseState"
            )
        if (self.anchor_lower is None) != (self.anchor_upper is None):
            raise ForwardEvidenceCompletenessError(
                "close anchor interval must be fully present or fully absent"
            )
        if self.close_state is CampaignCloseState.CLOSED:
            if self.anchor_lower is None or self.anchor_upper is None:
                raise ForwardEvidenceCompletenessError(
                    "CLOSED campaign requires an anchored close envelope"
                )
        elif self.anchor_lower is not None or self.anchor_upper is not None:
            raise ForwardEvidenceCompletenessError(
                "CLOSE_PENDING campaign cannot claim an anchor interval"
            )
        if self.anchor_lower is not None and self.anchor_upper is not None:
            lower = _instant(self.anchor_lower, "anchor_lower")
            upper = _instant(self.anchor_upper, "anchor_upper")
            if lower > upper:
                raise ForwardEvidenceCompletenessError(
                    "close anchor lower must not exceed upper"
                )
            object.__setattr__(self, "anchor_lower", lower)
            object.__setattr__(self, "anchor_upper", upper)
        if self.predecessor_close_sha256 is not None:
            _sha256(self.predecessor_close_sha256, "predecessor_close_sha256")
        for name in ("final_candidate_count", "first_sequence", "last_sequence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ForwardEvidenceCompletenessError(
                    f"{name} must be a positive integer"
                )
        if self.first_sequence > self.last_sequence:
            raise ForwardEvidenceCompletenessError(
                "first_sequence must not exceed last_sequence"
            )
        expected_count = self.last_sequence - self.first_sequence + 1
        if self.final_candidate_count != expected_count:
            raise ForwardEvidenceCompletenessError(
                "final_candidate_count must equal the inclusive sequence span"
            )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "protocol_sha256": self.protocol_sha256.lower(),
            "terminal_cohort_root_sha256": self.terminal_cohort_root_sha256.lower(),
            "final_candidate_count": self.final_candidate_count,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "close_reason": self.close_reason,
            "close_state": self.close_state.value,
            "anchor_lower": _iso(self.anchor_lower) if self.anchor_lower else None,
            "anchor_upper": _iso(self.anchor_upper) if self.anchor_upper else None,
            "predecessor_close_sha256": (
                self.predecessor_close_sha256.lower()
                if self.predecessor_close_sha256 is not None
                else None
            ),
        }

    @property
    def close_sha256(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class RevealBoundaryReceipt:
    receipt_id: str
    campaign_id: str
    event_or_market_id: str
    provider_or_authority_id: str
    boundary_rule_id: str
    source_receipt_id: str
    source_sha256: str
    boundary_lower: datetime
    boundary_upper: datetime
    uncertainty_basis: str
    supersedes_receipt_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "receipt_id",
            "campaign_id",
            "event_or_market_id",
            "provider_or_authority_id",
            "boundary_rule_id",
            "source_receipt_id",
            "uncertainty_basis",
        ):
            _text(getattr(self, name), name)
        _sha256(self.source_sha256, "source_sha256")
        if self.supersedes_receipt_id is not None:
            _text(self.supersedes_receipt_id, "supersedes_receipt_id")
        lower = _instant(self.boundary_lower, "boundary_lower")
        upper = _instant(self.boundary_upper, "boundary_upper")
        if lower > upper:
            raise ForwardEvidenceCompletenessError(
                "boundary lower must not exceed upper"
            )
        object.__setattr__(self, "boundary_lower", lower)
        object.__setattr__(self, "boundary_upper", upper)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "campaign_id": self.campaign_id,
            "event_or_market_id": self.event_or_market_id,
            "provider_or_authority_id": self.provider_or_authority_id,
            "boundary_rule_id": self.boundary_rule_id,
            "source_receipt_id": self.source_receipt_id,
            "source_sha256": self.source_sha256.lower(),
            "boundary_lower": _iso(self.boundary_lower),
            "boundary_upper": _iso(self.boundary_upper),
            "uncertainty_basis": self.uncertainty_basis,
            "supersedes_receipt_id": self.supersedes_receipt_id,
        }

    @property
    def boundary_receipt_sha256(self) -> str:
        return _digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class AuthoritativeSourceReceipt:
    receipt_id: str
    receipt_sha256: str
    campaign_id: str
    opportunity_id: str
    universe_rule_result: UniverseResult

    def __post_init__(self) -> None:
        for name in ("receipt_id", "campaign_id", "opportunity_id"):
            _text(getattr(self, name), name)
        _sha256(self.receipt_sha256, "receipt_sha256")
        if not isinstance(self.universe_rule_result, UniverseResult):
            raise ForwardEvidenceCompletenessError(
                "universe_rule_result must be a UniverseResult"
            )


@dataclass(frozen=True, slots=True)
class CostEvidence:
    candidate_sequence: int
    all_material_costs_known: bool

    def __post_init__(self) -> None:
        if isinstance(self.candidate_sequence, bool) or not isinstance(
            self.candidate_sequence, int
        ) or self.candidate_sequence <= 0:
            raise ForwardEvidenceCompletenessError(
                "candidate_sequence must be a positive integer"
            )
        if type(self.all_material_costs_known) is not bool:
            raise ForwardEvidenceCompletenessError(
                "all_material_costs_known must be boolean"
            )


@dataclass(frozen=True, slots=True)
class CampaignEvidence:
    protocol: ForwardEvidenceProtocolEnvelope
    opportunities: tuple[ForwardOpportunityEnvelope, ...]
    cohort_roots: tuple[CohortRootEnvelope, ...]
    closes: tuple[CampaignCloseEnvelope, ...]
    reveal_boundaries: tuple[RevealBoundaryReceipt, ...]
    authoritative_receipts: tuple[AuthoritativeSourceReceipt, ...]
    denominator_sequences: tuple[int, ...]
    cost_evidence: tuple[CostEvidence, ...]
    safety_margin: timedelta = timedelta(0)
    stopping_rule_satisfied: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.protocol, ForwardEvidenceProtocolEnvelope):
            raise ForwardEvidenceCompletenessError(
                "protocol must be a ForwardEvidenceProtocolEnvelope"
            )
        object.__setattr__(
            self,
            "safety_margin",
            _duration(self.safety_margin, "safety_margin"),
        )
        if type(self.stopping_rule_satisfied) is not bool:
            raise ForwardEvidenceCompletenessError(
                "stopping_rule_satisfied must be boolean"
            )


@dataclass(frozen=True, slots=True)
class VerificationResult:
    ok: bool
    codes: tuple[VerificationCode, ...]
    protocol_sha256: str
    terminal_root_sha256: str | None
    candidate_count: int
    details: tuple[tuple[str, str], ...] = ()


def ordered_leaf_sha256(
    opportunities: Sequence[ForwardOpportunityEnvelope],
) -> str:
    """Commit one ordered opportunity prefix, preserving sequence identity."""

    payload = {
        "leaves": [
            {
                "candidate_sequence": item.candidate_sequence,
                "opportunity_sha256": item.opportunity_sha256,
            }
            for item in opportunities
        ]
    }
    return _digest(payload)


def build_cohort_root(
    opportunities: Sequence[ForwardOpportunityEnvelope],
    *,
    previous_cohort_root_sha256: str = _GENESIS,
    anchor_lower: datetime | None,
    anchor_upper: datetime | None,
) -> CohortRootEnvelope:
    """Create one cumulative prefix-root envelope for tests/adapters."""

    if not opportunities:
        raise ForwardEvidenceCompletenessError(
            "cannot build a cohort root over an empty opportunity sequence"
        )
    ordered = tuple(sorted(opportunities, key=lambda item: item.candidate_sequence))
    return CohortRootEnvelope(
        campaign_id=ordered[0].campaign_id,
        protocol_sha256=ordered[0].protocol_sha256,
        first_sequence=ordered[0].candidate_sequence,
        last_sequence=ordered[-1].candidate_sequence,
        candidate_count=len(ordered),
        ordered_leaf_sha256=ordered_leaf_sha256(ordered),
        previous_cohort_root_sha256=previous_cohort_root_sha256,
        anchor_lower=anchor_lower,
        anchor_upper=anchor_upper,
    )


def _dedupe_opportunities(
    opportunities: Sequence[ForwardOpportunityEnvelope],
) -> tuple[
    tuple[ForwardOpportunityEnvelope, ...],
    bool,
    bool,
]:
    by_sequence: dict[int, ForwardOpportunityEnvelope] = {}
    by_opportunity: dict[str, ForwardOpportunityEnvelope] = {}
    by_source_receipt: dict[str, ForwardOpportunityEnvelope] = {}
    by_decision: dict[str, ForwardOpportunityEnvelope] = {}
    successor_by_predecessor: dict[str, str] = {}
    identity_conflict = False
    fork = False

    for item in opportunities:
        previous = by_sequence.get(item.candidate_sequence)
        if previous is not None and previous.opportunity_sha256 != item.opportunity_sha256:
            identity_conflict = True
        else:
            by_sequence[item.candidate_sequence] = item

        previous = by_opportunity.get(item.opportunity_id)
        if previous is not None and previous.opportunity_sha256 != item.opportunity_sha256:
            identity_conflict = True
        else:
            by_opportunity[item.opportunity_id] = item

        previous = by_source_receipt.get(item.source_receipt_id)
        if previous is not None and previous.opportunity_sha256 != item.opportunity_sha256:
            identity_conflict = True
        else:
            by_source_receipt[item.source_receipt_id] = item

        if item.decision_id is not None:
            previous = by_decision.get(item.decision_id)
            if (
                previous is not None
                and previous.opportunity_sha256 != item.opportunity_sha256
            ):
                identity_conflict = True
            else:
                by_decision[item.decision_id] = item

        prior_successor = successor_by_predecessor.get(
            item.predecessor_opportunity_sha256
        )
        if prior_successor is not None and prior_successor != item.opportunity_sha256:
            fork = True
        else:
            successor_by_predecessor[item.predecessor_opportunity_sha256] = (
                item.opportunity_sha256
            )

    ordered = tuple(sorted(by_sequence.values(), key=lambda item: item.candidate_sequence))
    return ordered, identity_conflict, fork


def _dedupe_roots(
    roots: Sequence[CohortRootEnvelope],
) -> tuple[tuple[CohortRootEnvelope, ...], bool]:
    by_last_sequence: dict[int, CohortRootEnvelope] = {}
    conflict = False
    for root in roots:
        previous = by_last_sequence.get(root.last_sequence)
        if previous is not None and previous.cohort_root_sha256 != root.cohort_root_sha256:
            conflict = True
        else:
            by_last_sequence[root.last_sequence] = root
    return tuple(sorted(by_last_sequence.values(), key=lambda item: item.last_sequence)), conflict


def _resolve_boundary_receipts(
    receipts: Sequence[RevealBoundaryReceipt],
) -> tuple[dict[str, RevealBoundaryReceipt], bool]:
    """Resolve every historical receipt id to its append-only terminal correction."""

    by_id: dict[str, RevealBoundaryReceipt] = {}
    successor_by_parent: dict[str, str] = {}
    conflict = False

    for receipt in receipts:
        previous = by_id.get(receipt.receipt_id)
        if (
            previous is not None
            and previous.boundary_receipt_sha256 != receipt.boundary_receipt_sha256
        ):
            conflict = True
        else:
            by_id[receipt.receipt_id] = receipt

    for receipt in receipts:
        parent = receipt.supersedes_receipt_id
        if parent is None:
            continue
        if parent not in by_id:
            conflict = True
            continue
        parent_receipt = by_id[parent]
        if (
            parent_receipt.campaign_id != receipt.campaign_id
            or parent_receipt.event_or_market_id != receipt.event_or_market_id
            or parent_receipt.provider_or_authority_id
            != receipt.provider_or_authority_id
            or parent_receipt.boundary_rule_id != receipt.boundary_rule_id
        ):
            conflict = True
        previous_successor = successor_by_parent.get(parent)
        if previous_successor is not None and previous_successor != receipt.receipt_id:
            conflict = True
        else:
            successor_by_parent[parent] = receipt.receipt_id

    resolved: dict[str, RevealBoundaryReceipt] = {}
    for receipt_id in by_id:
        current = receipt_id
        visited: set[str] = set()
        while current in successor_by_parent:
            if current in visited:
                conflict = True
                break
            visited.add(current)
            current = successor_by_parent[current]
        terminal = by_id.get(current)
        if terminal is None:
            conflict = True
            continue
        resolved[receipt_id] = terminal

    return resolved, conflict


def verify_campaign(evidence: CampaignEvidence) -> VerificationResult:
    """Verify one confirmatory cohort without mutating any product authority."""

    codes: list[VerificationCode] = []
    details: dict[str, str] = {}
    protocol = evidence.protocol

    opportunities, identity_conflict, fork = _dedupe_opportunities(
        evidence.opportunities
    )
    if any(
        protocol.precommit_anchor_upper >= item.observed_lower
        for item in opportunities
    ):
        codes.append(VerificationCode.PROTOCOL_PRECOMMIT_FAIL)
    if identity_conflict:
        codes.append(VerificationCode.EVIDENCE_IDENTITY_CONFLICT)
    if fork:
        codes.append(VerificationCode.HASH_CHAIN_FORK)

    if any(
        item.campaign_id != protocol.campaign_id
        or item.protocol_sha256 != protocol.protocol_sha256
        for item in opportunities
    ):
        codes.append(VerificationCode.PROTOCOL_HASH_CONFLICT)
    if any(
        item.runtime_identity_sha256 != protocol.runtime_identity_sha256
        for item in opportunities
    ):
        codes.append(VerificationCode.RUNTIME_IDENTITY_CONFLICT)

    if opportunities:
        expected_sequences = tuple(
            range(
                opportunities[0].candidate_sequence,
                opportunities[-1].candidate_sequence + 1,
            )
        )
        actual_sequences = tuple(
            item.candidate_sequence for item in opportunities
        )
        if actual_sequences != expected_sequences or actual_sequences[0] != 1:
            codes.append(VerificationCode.SEQUENCE_GAP)
            details["actual_sequences"] = repr(actual_sequences)

        expected_predecessor = _GENESIS
        for item in opportunities:
            if item.predecessor_opportunity_sha256 != expected_predecessor:
                codes.append(VerificationCode.HASH_CHAIN_BROKEN)
            expected_predecessor = item.opportunity_sha256

    opportunity_by_receipt = {
        item.source_receipt_id: item for item in opportunities
    }
    authoritative_by_receipt: dict[str, AuthoritativeSourceReceipt] = {}
    authoritative_conflict = False
    for receipt in evidence.authoritative_receipts:
        previous = authoritative_by_receipt.get(receipt.receipt_id)
        if previous is not None and previous != receipt:
            authoritative_conflict = True
        else:
            authoritative_by_receipt[receipt.receipt_id] = receipt

        if receipt.campaign_id != protocol.campaign_id:
            codes.append(VerificationCode.COHORT_OMISSION_DETECTED)
            details.setdefault("foreign_authoritative_receipt", receipt.receipt_id)
            continue
        item = opportunity_by_receipt.get(receipt.receipt_id)
        if (
            item is None
            or item.opportunity_id != receipt.opportunity_id
            or item.source_receipt_sha256 != receipt.receipt_sha256
            or item.universe_rule_result is not receipt.universe_rule_result
        ):
            codes.append(VerificationCode.COHORT_OMISSION_DETECTED)
            details.setdefault("omitted_source_receipt", receipt.receipt_id)

    if authoritative_conflict:
        codes.append(VerificationCode.EVIDENCE_IDENTITY_CONFLICT)

    # Coverage is deliberately bidirectional.  The supplied authoritative
    # inventory is still only an assertion until a product-owned inventory
    # commitment is resolved upstream, but it cannot omit or rebind any
    # opportunity that this structural verifier is asked to certify.
    for item in opportunities:
        receipt = authoritative_by_receipt.get(item.source_receipt_id)
        if (
            receipt is None
            or receipt.campaign_id != protocol.campaign_id
            or receipt.opportunity_id != item.opportunity_id
            or receipt.receipt_sha256 != item.source_receipt_sha256
            or receipt.universe_rule_result is not item.universe_rule_result
        ):
            codes.append(VerificationCode.COHORT_OMISSION_DETECTED)
            details.setdefault(
                "missing_authoritative_receipt",
                item.source_receipt_id,
            )

    roots, root_conflict = _dedupe_roots(evidence.cohort_roots)
    if root_conflict:
        codes.append(VerificationCode.EVIDENCE_IDENTITY_CONFLICT)
    previous_root_sha256 = _GENESIS
    for root in roots:
        if (
            root.campaign_id != protocol.campaign_id
            or root.protocol_sha256 != protocol.protocol_sha256
            or root.first_sequence != 1
            or root.last_sequence > len(opportunities)
        ):
            codes.append(VerificationCode.COHORT_ROOT_MISMATCH)
            previous_root_sha256 = root.cohort_root_sha256
            continue
        prefix = opportunities[: root.last_sequence]
        if (
            root.candidate_count != len(prefix)
            or root.ordered_leaf_sha256 != ordered_leaf_sha256(prefix)
            or root.previous_cohort_root_sha256 != previous_root_sha256
        ):
            codes.append(VerificationCode.COHORT_ROOT_MISMATCH)
        previous_root_sha256 = root.cohort_root_sha256

    terminal_root_sha256 = roots[-1].cohort_root_sha256 if roots else None
    if opportunities and (
        not roots or roots[-1].last_sequence != opportunities[-1].candidate_sequence
    ):
        codes.append(VerificationCode.COHORT_ROOT_MISMATCH)

    close_ids = {item.close_sha256 for item in evidence.closes}
    if not evidence.closes:
        codes.append(VerificationCode.COHORT_OPEN)
    elif len(close_ids) != 1:
        codes.append(VerificationCode.EVIDENCE_IDENTITY_CONFLICT)
    else:
        close = evidence.closes[-1]
        if close.close_state is CampaignCloseState.CLOSE_PENDING:
            codes.append(VerificationCode.COHORT_CLOSE_PENDING)
        if not opportunities:
            # A close envelope cannot manufacture terminal membership when no
            # candidate/root evidence exists.  This also prevents a forged
            # non-empty final count from falling through to PASS.
            codes.append(VerificationCode.COHORT_ROOT_MISMATCH)
            details.setdefault("empty_campaign_close", close.close_sha256)
        elif (
            close.campaign_id != protocol.campaign_id
            or close.protocol_sha256 != protocol.protocol_sha256
            or close.terminal_cohort_root_sha256 != terminal_root_sha256
            or close.final_candidate_count != len(opportunities)
            or close.first_sequence != 1
            or close.last_sequence != opportunities[-1].candidate_sequence
        ):
            codes.append(VerificationCode.COHORT_ROOT_MISMATCH)

    boundary_by_id, boundary_conflict = _resolve_boundary_receipts(
        evidence.reveal_boundaries
    )
    if boundary_conflict:
        codes.append(VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN)

    superseded_boundary_ids = {
        receipt.supersedes_receipt_id
        for receipt in evidence.reveal_boundaries
        if receipt.supersedes_receipt_id is not None
    }

    for item in opportunities:
        boundary = boundary_by_id.get(item.reveal_boundary_receipt_id)
        if boundary is None:
            codes.append(VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN)
            continue
        if boundary.campaign_id != protocol.campaign_id:
            # Receipt identifiers are not globally authoritative; a boundary
            # from another campaign cannot authorize this campaign merely
            # because its timing happens to fit.
            codes.append(VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN)
            continue
        if (
            item.reveal_boundary_receipt_id in superseded_boundary_ids
            or boundary.supersedes_receipt_id is not None
        ):
            # The current contract has no product-owned "correction available
            # at" witness.  Resolving a historical id to a later correction
            # could otherwise retroactively turn UNKNOWN into PASS.  Until that
            # causal authority exists, every corrected boundary is fail-closed.
            codes.append(VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN)
            continue
        candidate_roots = [
            root
            for root in roots
            if root.last_sequence >= item.candidate_sequence
            and root.anchor_upper is not None
        ]
        if not candidate_roots:
            codes.append(VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN)
            continue
        earliest_root = min(candidate_roots, key=lambda root: root.anchor_upper)
        assert earliest_root.anchor_upper is not None
        if not (
            earliest_root.anchor_upper
            < boundary.boundary_lower - evidence.safety_margin
        ):
            codes.append(VerificationCode.TEMPORAL_ELIGIBILITY_UNKNOWN)

    denominator = tuple(sorted(evidence.denominator_sequences))
    expected_denominator = tuple(
        item.candidate_sequence for item in opportunities
    )
    if denominator != expected_denominator:
        codes.append(VerificationCode.DENOMINATOR_INCOMPLETE)

    costs: dict[int, bool] = {}
    cost_conflict = False
    for item in evidence.cost_evidence:
        previous = costs.get(item.candidate_sequence)
        if previous is not None and previous != item.all_material_costs_known:
            cost_conflict = True
            costs[item.candidate_sequence] = False
        elif previous is None:
            costs[item.candidate_sequence] = item.all_material_costs_known
    if cost_conflict:
        codes.append(VerificationCode.EVIDENCE_IDENTITY_CONFLICT)
    if any(not costs.get(sequence, False) for sequence in expected_denominator):
        codes.append(VerificationCode.ECONOMICS_INCOMPLETE)

    if not evidence.stopping_rule_satisfied:
        codes.append(VerificationCode.STOPPING_RULE_VIOLATION)

    unique_codes = tuple(dict.fromkeys(codes))
    return VerificationResult(
        ok=not unique_codes,
        codes=unique_codes or (VerificationCode.PASS,),
        protocol_sha256=protocol.protocol_sha256,
        terminal_root_sha256=terminal_root_sha256,
        candidate_count=len(opportunities),
        details=tuple(sorted(details.items())),
    )


__all__ = [
    "AuthoritativeSourceReceipt",
    "CampaignCloseEnvelope",
    "CampaignCloseState",
    "CampaignEvidence",
    "CohortRootEnvelope",
    "CostEvidence",
    "DecisionState",
    "ForwardEvidenceCompletenessError",
    "ForwardOpportunityEnvelope",
    "ForwardEvidenceProtocolEnvelope",
    "RevealBoundaryReceipt",
    "UniverseResult",
    "VerificationCode",
    "VerificationResult",
    "build_cohort_root",
    "ordered_leaf_sha256",
    "verify_campaign",
]
