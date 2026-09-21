from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from .scientific_registry import ScientificRegistry

if TYPE_CHECKING:
    from .risk import RiskOfRuinEvidence, RiskOfRuinVectorEvidence


_BOUND_SEMANTICS = "ONE_SIDED_UPPER_CONFIDENCE_BOUND"
_HEX = frozenset("0123456789abcdef")


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    value.encode("utf-8", errors="strict")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or text != text.lower() or any(char not in _HEX for char in text):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _confidence(value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("confidence_level must be a finite exact Decimal")
    if value <= Decimal("0") or value > Decimal("1"):
        raise ValueError("confidence_level must be in (0, 1]")
    return value


def _canonical_decimal(value: Decimal, name: str) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite exact Decimal")
    return str(value)


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class RiskOfRuinAuthorityRef:
    """Reference to one exact durable scientific evaluation authority.

    The reference is assertion-only. Positive authority exists only when
    ``RiskOfRuinScientificAuthority`` re-resolves every field against the canonical
    ``ScientificRegistry`` and finds the exact risk-result digest among the durable
    ``EvaluationBundle`` artifacts.
    """

    research_protocol_id: str
    research_protocol_record_sha256: str
    dataset_snapshot_id: str
    dataset_snapshot_record_sha256: str
    dataset_manifest_sha256: str
    evaluation_bundle_id: str
    evaluation_bundle_record_sha256: str
    evaluator_source_sha256: str
    estimation_method: str
    estimation_version: str
    effective_sample_size: int
    confidence_level: Decimal
    bound_semantics: str
    available_at: str
    valid_until: str

    def __post_init__(self) -> None:
        for name in (
            "research_protocol_id",
            "dataset_snapshot_id",
            "evaluation_bundle_id",
            "estimation_method",
            "estimation_version",
        ):
            _text(getattr(self, name), name)
        for name in (
            "research_protocol_record_sha256",
            "dataset_snapshot_record_sha256",
            "dataset_manifest_sha256",
            "evaluation_bundle_record_sha256",
            "evaluator_source_sha256",
        ):
            _sha256(getattr(self, name), name)
        _positive_int(self.effective_sample_size, "effective_sample_size")
        _confidence(self.confidence_level)
        if self.bound_semantics != _BOUND_SEMANTICS:
            raise ValueError(
                f"bound_semantics must be {_BOUND_SEMANTICS}"
            )
        available = _instant(self.available_at, "available_at")
        valid_until = _instant(self.valid_until, "valid_until")
        if valid_until <= available:
            raise ValueError("valid_until must be after available_at")


def risk_of_ruin_result_sha256(
    *,
    evidence_kind: Literal["SINGLE", "VECTOR"],
    producer_identity: str,
    research_protocol_sha256: str,
    dataset_snapshot_id: str,
    dataset_manifest_sha256: str,
    evaluator_source_sha256: str,
    estimation_method: str,
    estimation_version: str,
    effective_sample_size: int,
    confidence_level: Decimal,
    bound_semantics: str,
    causal_cutoff: str,
    evaluated_at: str,
    available_at: str,
    valid_until: str,
    bankroll_id: str,
    currency: str,
    base_portfolio_sha256: str,
    candidate_identity_sha256: str,
    evaluated_stakes: tuple[Decimal, ...],
    upper_bound: Decimal,
) -> str:
    """Commit one risk result before its EvaluationBundle is published.

    This helper is intentionally public and deterministic. Knowing or recomputing
    the digest is not authority: the consumer later requires the digest to be
    present in the exact re-resolved product EvaluationBundle.
    """

    if evidence_kind not in {"SINGLE", "VECTOR"}:
        raise ValueError("evidence_kind must be SINGLE or VECTOR")
    _text(producer_identity, "producer_identity")
    _sha256(research_protocol_sha256, "research_protocol_sha256")
    _text(dataset_snapshot_id, "dataset_snapshot_id")
    _sha256(dataset_manifest_sha256, "dataset_manifest_sha256")
    _sha256(evaluator_source_sha256, "evaluator_source_sha256")
    _text(estimation_method, "estimation_method")
    _text(estimation_version, "estimation_version")
    _positive_int(effective_sample_size, "effective_sample_size")
    _confidence(confidence_level)
    if bound_semantics != _BOUND_SEMANTICS:
        raise ValueError(f"bound_semantics must be {_BOUND_SEMANTICS}")
    cutoff = _instant(causal_cutoff, "causal_cutoff")
    evaluated = _instant(evaluated_at, "evaluated_at")
    available = _instant(available_at, "available_at")
    valid_until_instant = _instant(valid_until, "valid_until")
    if cutoff > evaluated or evaluated > available or available >= valid_until_instant:
        raise ValueError("risk result causal/evaluation/availability times are invalid")
    _text(bankroll_id, "bankroll_id")
    currency_value = _text(currency, "currency")
    if (
        len(currency_value) != 3
        or not currency_value.isascii()
        or not currency_value.isalpha()
        or currency_value != currency_value.upper()
    ):
        raise ValueError("currency must be a three-letter uppercase ASCII code")
    _sha256(base_portfolio_sha256, "base_portfolio_sha256")
    _sha256(candidate_identity_sha256, "candidate_identity_sha256")
    if type(evaluated_stakes) is not tuple or not evaluated_stakes:
        raise ValueError("evaluated_stakes must be a non-empty tuple")
    stake_values: list[str] = []
    has_positive = False
    for stake in evaluated_stakes:
        text = _canonical_decimal(stake, "evaluated_stake")
        if stake < 0:
            raise ValueError("evaluated_stakes must be non-negative")
        has_positive = has_positive or stake > 0
        stake_values.append(text)
    if not has_positive:
        raise ValueError("evaluated_stakes must contain a positive value")
    bound_text = _canonical_decimal(upper_bound, "upper_bound")
    if upper_bound < 0 or upper_bound > 1:
        raise ValueError("upper_bound must be between 0 and 1")

    return _digest(
        {
            "schema": "autosport.risk-of-ruin-scientific-result.v1",
            "evidence_kind": evidence_kind,
            "producer_identity": producer_identity,
            "research_protocol_sha256": research_protocol_sha256,
            "dataset_snapshot_id": dataset_snapshot_id,
            "dataset_manifest_sha256": dataset_manifest_sha256,
            "evaluator_source_sha256": evaluator_source_sha256,
            "estimation_method": estimation_method,
            "estimation_version": estimation_version,
            "effective_sample_size": effective_sample_size,
            "confidence_level": str(confidence_level),
            "bound_semantics": bound_semantics,
            "causal_cutoff": causal_cutoff,
            "evaluated_at": evaluated_at,
            "available_at": available_at,
            "valid_until": valid_until,
            "bankroll_id": bankroll_id,
            "currency": currency_value,
            "base_portfolio_sha256": base_portfolio_sha256,
            "candidate_identity_sha256": candidate_identity_sha256,
            "evaluated_stakes": stake_values,
            "upper_bound": bound_text,
        }
    )


@dataclass(frozen=True, slots=True)
class RiskOfRuinScientificAuthority:
    """Read-only resolver over the existing canonical ScientificRegistry."""

    registry: ScientificRegistry

    def __post_init__(self) -> None:
        if type(self.registry) is not ScientificRegistry:
            raise TypeError("registry must be the canonical ScientificRegistry")

    @property
    def registry_path_sha256(self) -> str:
        return hashlib.sha256(
            str(self.registry.path.resolve()).encode("utf-8")
        ).hexdigest()

    def _resolve_common(
        self,
        *,
        evidence: object,
        authority_ref: RiskOfRuinAuthorityRef,
        evidence_kind: Literal["SINGLE", "VECTOR"],
        candidate_identity_sha256: str,
        evaluated_stakes: tuple[Decimal, ...],
        as_of: str,
    ) -> bool:
        if type(authority_ref) is not RiskOfRuinAuthorityRef:
            return False
        try:
            as_of_instant = _instant(as_of, "as_of")
            available_at = _instant(authority_ref.available_at, "available_at")
            valid_until = _instant(authority_ref.valid_until, "valid_until")
            if available_at > as_of_instant or as_of_instant >= valid_until:
                return False

            protocol = self.registry.get(
                "ResearchProtocol", authority_ref.research_protocol_id
            )
            dataset = self.registry.get(
                "DatasetSnapshot", authority_ref.dataset_snapshot_id
            )
            bundle = self.registry.get(
                "EvaluationBundle", authority_ref.evaluation_bundle_id
            )
            if protocol is None or dataset is None or bundle is None:
                return False
            if (
                protocol.record_sha256
                != authority_ref.research_protocol_record_sha256
                or dataset.record_sha256
                != authority_ref.dataset_snapshot_record_sha256
                or bundle.record_sha256
                != authority_ref.evaluation_bundle_record_sha256
            ):
                return False

            research_protocol_sha256 = getattr(
                evidence, "research_protocol_sha256", None
            )
            reproducibility_bundle_sha256 = getattr(
                evidence, "reproducibility_bundle_sha256", None
            )
            causal_cutoff = getattr(evidence, "causal_cutoff", None)
            evaluated_at = getattr(evidence, "evaluated_at", None)
            producer_identity = getattr(evidence, "producer_identity", None)
            bankroll_id = getattr(evidence, "bankroll_id", None)
            currency = getattr(evidence, "currency", None)
            base_portfolio_sha256 = getattr(
                evidence, "base_portfolio_sha256", None
            )
            upper_bound = getattr(evidence, "upper_bound", None)

            protocol_payload = protocol.payload
            dataset_payload = dataset.payload
            bundle_payload = bundle.payload
            binding = protocol_payload.get("binding")
            if type(binding) is not dict:
                return False
            if (
                protocol_payload.get("protocol_sha256")
                != research_protocol_sha256
                or protocol_payload.get("dataset_manifest_sha256")
                != authority_ref.dataset_manifest_sha256
                or binding.get("causal_cutoff") != causal_cutoff
            ):
                return False
            if (
                dataset_payload.get("manifest_sha256")
                != authority_ref.dataset_manifest_sha256
                or dataset_payload.get("causal_cutoff") != causal_cutoff
            ):
                return False
            if (
                bundle_payload.get("dataset_snapshot_id")
                != authority_ref.dataset_snapshot_id
                or bundle_payload.get("protocol_sha256")
                != research_protocol_sha256
                or bundle_payload.get("bundle_sha256")
                != reproducibility_bundle_sha256
                or bundle_payload.get("evaluator_source_sha256")
                != authority_ref.evaluator_source_sha256
                or bundle_payload.get("effective_sample_size")
                != authority_ref.effective_sample_size
                or bundle.available_at != authority_ref.available_at
            ):
                return False

            if _instant(protocol.available_at, "protocol.available_at") > available_at:
                return False
            if _instant(dataset.available_at, "dataset.available_at") > available_at:
                return False
            if _instant(evaluated_at, "evaluated_at") > available_at:
                return False

            result_sha256 = risk_of_ruin_result_sha256(
                evidence_kind=evidence_kind,
                producer_identity=producer_identity,
                research_protocol_sha256=research_protocol_sha256,
                dataset_snapshot_id=authority_ref.dataset_snapshot_id,
                dataset_manifest_sha256=authority_ref.dataset_manifest_sha256,
                evaluator_source_sha256=authority_ref.evaluator_source_sha256,
                estimation_method=authority_ref.estimation_method,
                estimation_version=authority_ref.estimation_version,
                effective_sample_size=authority_ref.effective_sample_size,
                confidence_level=authority_ref.confidence_level,
                bound_semantics=authority_ref.bound_semantics,
                causal_cutoff=causal_cutoff,
                evaluated_at=evaluated_at,
                available_at=authority_ref.available_at,
                valid_until=authority_ref.valid_until,
                bankroll_id=bankroll_id,
                currency=currency,
                base_portfolio_sha256=base_portfolio_sha256,
                candidate_identity_sha256=candidate_identity_sha256,
                evaluated_stakes=evaluated_stakes,
                upper_bound=upper_bound,
            )
            artifact_hashes = bundle_payload.get("artifact_hashes")
            if type(artifact_hashes) is not list or result_sha256 not in artifact_hashes:
                return False
            return True
        except (ArithmeticError, AttributeError, KeyError, TypeError, ValueError):
            return False

    def resolve_single(
        self,
        evidence: "RiskOfRuinEvidence",
        *,
        as_of: str,
    ) -> bool:
        authority_ref = getattr(evidence, "authority_ref", None)
        candidate_sha256 = getattr(evidence, "candidate_sha256", None)
        evaluated_stake = getattr(evidence, "evaluated_stake", None)
        if type(authority_ref) is not RiskOfRuinAuthorityRef:
            return False
        try:
            _sha256(candidate_sha256, "candidate_sha256")
        except (TypeError, ValueError):
            return False
        return self._resolve_common(
            evidence=evidence,
            authority_ref=authority_ref,
            evidence_kind="SINGLE",
            candidate_identity_sha256=candidate_sha256,
            evaluated_stakes=(evaluated_stake,),
            as_of=as_of,
        )

    def resolve_vector(
        self,
        evidence: "RiskOfRuinVectorEvidence",
        *,
        as_of: str,
    ) -> bool:
        authority_ref = getattr(evidence, "authority_ref", None)
        candidate_vector_sha256 = getattr(
            evidence, "candidate_vector_sha256", None
        )
        evaluated_stakes = getattr(evidence, "evaluated_stakes", None)
        if type(authority_ref) is not RiskOfRuinAuthorityRef:
            return False
        try:
            _sha256(candidate_vector_sha256, "candidate_vector_sha256")
        except (TypeError, ValueError):
            return False
        return self._resolve_common(
            evidence=evidence,
            authority_ref=authority_ref,
            evidence_kind="VECTOR",
            candidate_identity_sha256=candidate_vector_sha256,
            evaluated_stakes=evaluated_stakes,
            as_of=as_of,
        )
