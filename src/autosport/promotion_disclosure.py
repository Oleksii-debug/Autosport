from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from . import _scientific_registry_read_authority as _registry_authority
from .holdout_disclosure import (
    DisclosureChannel,
    DisclosureKind,
    HoldoutDisclosureDecision,
    HoldoutDisclosureGate,
)
from .point_in_time_evidence import HoldoutConsumptionLedger
from .scientific_registry import (
    DatasetSnapshot,
    ScientificRegistry,
    promotion_holdout_access_id,
)


class PromotionDisclosureError(ValueError):
    """A promotion-result disclosure cannot be bound to canonical scientific truth."""


@dataclass(frozen=True, slots=True)
class PromotionDisclosurePayload:
    """Outcome-derived promotion projection released only after holdout consumption."""

    promotion_decision_id: str
    promotion_decision_record_sha256: str
    promotion_action: str
    promotion_evidence_id: str
    promotion_evidence_record_sha256: str
    experiment_id: str
    experiment_record_sha256: str
    experiment_outcome: str
    evaluation_bundle_id: str
    evaluation_bundle_sha256: str
    evaluation_bundle_record_sha256: str
    dataset_snapshot_id: str
    dataset_snapshot_record_sha256: str
    research_protocol_id: str
    protocol_sha256: str
    research_protocol_record_sha256: str
    confirmation_trial_family_id: str
    holdout_access_id: str
    holdout_consumption_id: str
    effective_sample_size: int
    effect_interval_low: str
    effect_interval_high: str
    practical_improvement: str
    guardrails_passed: bool
    validity: str

    def to_payload(self) -> dict[str, object]:
        return {
            "promotion_decision_id": self.promotion_decision_id,
            "promotion_decision_record_sha256": self.promotion_decision_record_sha256,
            "promotion_action": self.promotion_action,
            "promotion_evidence_id": self.promotion_evidence_id,
            "promotion_evidence_record_sha256": self.promotion_evidence_record_sha256,
            "experiment_id": self.experiment_id,
            "experiment_record_sha256": self.experiment_record_sha256,
            "experiment_outcome": self.experiment_outcome,
            "evaluation_bundle_id": self.evaluation_bundle_id,
            "evaluation_bundle_sha256": self.evaluation_bundle_sha256,
            "evaluation_bundle_record_sha256": self.evaluation_bundle_record_sha256,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_snapshot_record_sha256": self.dataset_snapshot_record_sha256,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "research_protocol_record_sha256": self.research_protocol_record_sha256,
            "confirmation_trial_family_id": self.confirmation_trial_family_id,
            "holdout_access_id": self.holdout_access_id,
            "holdout_consumption_id": self.holdout_consumption_id,
            "effective_sample_size": self.effective_sample_size,
            "effect_interval_low": self.effect_interval_low,
            "effect_interval_high": self.effect_interval_high,
            "practical_improvement": self.practical_improvement,
            "guardrails_passed": self.guardrails_passed,
            "validity": self.validity,
        }


@dataclass(frozen=True, slots=True)
class PromotionDisclosureReceipt:
    """Evidence that the canonical holdout was consumed before the sink returned."""

    disclosure: HoldoutDisclosureDecision
    payload: PromotionDisclosurePayload


def _instant(value: object, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise PromotionDisclosureError(f"{name} must be a canonical timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PromotionDisclosureError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PromotionDisclosureError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PromotionDisclosureError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PromotionDisclosureError(f"{name} must be a canonical SHA-256 hex string")
    return text


def _require_entry(
    entries: dict[tuple[str, str], dict[str, object]],
    record_type: str,
    record_id: object,
) -> dict[str, object]:
    identity = _text(record_id, f"{record_type}.record_id")
    entry = entries.get((record_type, identity))
    if entry is None:
        raise PromotionDisclosureError(
            f"canonical ScientificRegistry is missing {record_type}:{identity}"
        )
    payload = entry.get("payload")
    if type(payload) is not dict:
        raise PromotionDisclosureError(f"{record_type} payload is invalid")
    return entry


def _payload(entry: dict[str, object], record_type: str) -> dict[str, object]:
    value = entry.get("payload")
    if type(value) is not dict:
        raise PromotionDisclosureError(f"{record_type} payload is invalid")
    return value


def _dataset_from_entry(entry: dict[str, object]) -> DatasetSnapshot:
    payload = _payload(entry, "DatasetSnapshot")
    try:
        return DatasetSnapshot(
            dataset_snapshot_id=payload["dataset_snapshot_id"],
            manifest_sha256=payload["manifest_sha256"],
            source_identity=payload["source_identity"],
            license_identity=payload["license_identity"],
            causal_cutoff=payload["causal_cutoff"],
            available_at_utc=payload["available_at"],
            outcome_reveal_after=payload.get("outcome_reveal_after"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PromotionDisclosureError(
            "canonical DatasetSnapshot payload cannot be reconstructed"
        ) from exc


class PromotionDisclosureProjector:
    """Bind one durable PROMOTE result to its physical holdout before publication.

    This is a narrow outward projection seam, not a wrapper around generic
    ScientificRegistry reads. Internal sealed evaluation remains unaffected.
    The caller selects only the already-durable PromotionDecision identity and
    audit descriptors. Dataset, protocol and confirmation-family identities are
    re-resolved from product-owned durable scientific records.

    The holdout is consumed before the sink receives any outcome-derived payload.
    If the sink raises or the process dies after consumption, the durable
    consumption remains conservative and an exact retry reuses that record.
    """

    def __init__(
        self,
        *,
        registry: ScientificRegistry,
        ledger: HoldoutConsumptionLedger,
    ) -> None:
        if type(registry) is not ScientificRegistry:
            raise PromotionDisclosureError(
                "registry must be the exact canonical ScientificRegistry"
            )
        if type(ledger) is not HoldoutConsumptionLedger:
            raise PromotionDisclosureError(
                "ledger must be the exact canonical HoldoutConsumptionLedger"
            )
        if registry.path.parent.resolve(strict=False) != ledger.path.parent.resolve(
            strict=False
        ):
            raise PromotionDisclosureError(
                "ScientificRegistry and holdout ledger must share one workspace"
            )
        self._registry = registry
        self._gate = HoldoutDisclosureGate(ledger)

    def disclose(
        self,
        *,
        promotion_decision_id: str,
        channel: DisclosureChannel,
        kind: DisclosureKind,
        accessible_to_adaptive_actor: bool,
        disclosed_at_utc: str,
        sink: Callable[[PromotionDisclosurePayload], None],
    ) -> PromotionDisclosureReceipt:
        decision_id = _text(promotion_decision_id, "promotion_decision_id")
        if not callable(sink):
            raise PromotionDisclosureError("sink must be callable")

        try:
            read_registry, _, _, _ = (
                _registry_authority.require_scientific_registry_read_authority()
            )
            state = read_registry(self._registry)
        except (RuntimeError, OSError, ValueError) as exc:
            raise PromotionDisclosureError(
                "canonical ScientificRegistry read authority is unavailable"
            ) from exc

        raw_records = state.get("records")
        if type(raw_records) is not list:
            raise PromotionDisclosureError(
                "canonical ScientificRegistry records are invalid"
            )
        entries: dict[tuple[str, str], dict[str, object]] = {}
        positions: dict[tuple[str, str], int] = {}
        for position, raw in enumerate(raw_records):
            if type(raw) is not dict:
                raise PromotionDisclosureError(
                    "canonical ScientificRegistry entry is invalid"
                )
            record_type = raw.get("record_type")
            record_id = raw.get("record_id")
            if type(record_type) is not str or type(record_id) is not str:
                raise PromotionDisclosureError(
                    "canonical ScientificRegistry record identity is invalid"
                )
            key = (record_type, record_id)
            if key in entries:
                raise PromotionDisclosureError(
                    "canonical ScientificRegistry record identity is ambiguous"
                )
            entries[key] = raw
            positions[key] = position

        decision = _require_entry(entries, "PromotionDecision", decision_id)
        dp = _payload(decision, "PromotionDecision")
        if dp.get("action") != "PROMOTE":
            raise PromotionDisclosureError(
                "this outward seam accepts only durable PROMOTE decisions"
            )

        evidence = _require_entry(
            entries,
            "PromotionEvidence",
            dp.get("promotion_evidence_id"),
        )
        ep = _payload(evidence, "PromotionEvidence")
        experiment = _require_entry(entries, "Experiment", ep.get("experiment_id"))
        xp = _payload(experiment, "Experiment")
        bundle = _require_entry(
            entries,
            "EvaluationBundle",
            ep.get("evaluation_bundle_id"),
        )
        bp = _payload(bundle, "EvaluationBundle")
        protocol = _require_entry(
            entries,
            "ResearchProtocol",
            ep.get("research_protocol_id"),
        )
        pp = _payload(protocol, "ResearchProtocol")
        dataset = _require_entry(
            entries,
            "DatasetSnapshot",
            ep.get("dataset_snapshot_id"),
        )
        dsp = _payload(dataset, "DatasetSnapshot")

        relations = (
            (
                dp.get("promotion_evidence_id"),
                evidence.get("record_id"),
                "PromotionDecision/PromotionEvidence identity",
            ),
            (
                dp.get("research_protocol_id"),
                ep.get("research_protocol_id"),
                "PromotionDecision/PromotionEvidence protocol",
            ),
            (
                dp.get("candidate_strategy_version_id"),
                ep.get("candidate_strategy_version_id"),
                "PromotionDecision/PromotionEvidence strategy",
            ),
            (
                dp.get("candidate_model_version_id"),
                ep.get("candidate_model_version_id"),
                "PromotionDecision/PromotionEvidence model",
            ),
            (
                dp.get("evaluation_bundle_id"),
                ep.get("evaluation_bundle_id"),
                "PromotionDecision/PromotionEvidence evaluation",
            ),
            (
                dp.get("evaluation_bundle_sha256"),
                ep.get("evaluation_bundle_sha256"),
                "PromotionDecision/PromotionEvidence evaluation digest",
            ),
            (
                ep.get("evaluation_bundle_id"),
                xp.get("evaluation_bundle_id"),
                "PromotionEvidence/Experiment evaluation",
            ),
            (
                ep.get("research_protocol_id"),
                xp.get("research_protocol_id"),
                "PromotionEvidence/Experiment protocol",
            ),
            (
                ep.get("candidate_strategy_version_id"),
                xp.get("strategy_version_id"),
                "PromotionEvidence/Experiment strategy",
            ),
            (
                ep.get("candidate_model_version_id"),
                xp.get("model_version_id"),
                "PromotionEvidence/Experiment model",
            ),
            (
                ep.get("dataset_snapshot_id"),
                xp.get("dataset_snapshot_id"),
                "PromotionEvidence/Experiment dataset",
            ),
            (
                ep.get("evaluation_bundle_id"),
                bp.get("evaluation_bundle_id"),
                "PromotionEvidence/EvaluationBundle identity",
            ),
            (
                ep.get("evaluation_bundle_sha256"),
                bp.get("bundle_sha256"),
                "PromotionEvidence/EvaluationBundle digest",
            ),
            (
                ep.get("dataset_snapshot_id"),
                bp.get("dataset_snapshot_id"),
                "PromotionEvidence/EvaluationBundle dataset",
            ),
            (
                ep.get("candidate_strategy_version_id"),
                bp.get("evaluated_strategy_version_id"),
                "PromotionEvidence/EvaluationBundle strategy",
            ),
            (
                ep.get("candidate_model_version_id"),
                bp.get("evaluated_model_version_id"),
                "PromotionEvidence/EvaluationBundle model",
            ),
            (
                ep.get("research_protocol_id"),
                pp.get("research_protocol_id"),
                "PromotionEvidence/ResearchProtocol identity",
            ),
            (
                dp.get("protocol_sha256"),
                pp.get("protocol_sha256"),
                "PromotionDecision/ResearchProtocol digest",
            ),
            (
                dp.get("protocol_sha256"),
                bp.get("protocol_sha256"),
                "PromotionDecision/EvaluationBundle protocol digest",
            ),
            (
                ep.get("dataset_snapshot_id"),
                dsp.get("dataset_snapshot_id"),
                "PromotionEvidence/DatasetSnapshot identity",
            ),
            (
                pp.get("dataset_manifest_sha256"),
                dsp.get("manifest_sha256"),
                "ResearchProtocol/DatasetSnapshot manifest",
            ),
        )
        for actual, expected, label in relations:
            if actual != expected:
                raise PromotionDisclosureError(f"{label} mismatch")

        if ep.get("holdout_consumed") is not False:
            raise PromotionDisclosureError(
                "durable PROMOTE evidence must describe the unconsumed confirmation holdout"
            )

        lineage_keys = (
            (
                "ResearchProtocol",
                _text(ep.get("research_protocol_id"), "research_protocol_id"),
            ),
            (
                "DatasetSnapshot",
                _text(ep.get("dataset_snapshot_id"), "dataset_snapshot_id"),
            ),
            (
                "EvaluationBundle",
                _text(ep.get("evaluation_bundle_id"), "evaluation_bundle_id"),
            ),
            ("Experiment", _text(ep.get("experiment_id"), "experiment_id")),
            (
                "PromotionEvidence",
                _text(evidence.get("record_id"), "promotion_evidence_id"),
            ),
            ("PromotionDecision", decision_id),
        )
        lineage_positions = [positions[key] for key in lineage_keys]
        if (
            lineage_positions != sorted(lineage_positions)
            or len(set(lineage_positions)) != len(lineage_positions)
        ):
            raise PromotionDisclosureError(
                "scientific disclosure lineage is not durably ordered"
            )

        dataset_snapshot = _dataset_from_entry(dataset)
        trial_family_id = _text(
            ep.get("confirmation_trial_family_id"),
            "confirmation_trial_family_id",
        )
        protocol_id = _text(ep.get("research_protocol_id"), "research_protocol_id")
        expected_access_id = promotion_holdout_access_id(
            research_protocol_id=protocol_id,
            dataset_manifest_sha256=dataset_snapshot.manifest_sha256,
            source_identity=dataset_snapshot.source_identity,
            license_identity=dataset_snapshot.license_identity,
            confirmation_trial_family_id=trial_family_id,
        )
        if ep.get("holdout_access_id") != expected_access_id:
            raise PromotionDisclosureError(
                "PromotionEvidence holdout identity does not match canonical physical holdout"
            )

        disclosed_at = _instant(disclosed_at_utc, "disclosed_at_utc")
        for record_type, entry in (
            ("PromotionDecision", decision),
            ("PromotionEvidence", evidence),
            ("Experiment", experiment),
            ("EvaluationBundle", bundle),
            ("DatasetSnapshot", dataset),
            ("ResearchProtocol", protocol),
        ):
            if disclosed_at < _instant(
                entry.get("available_at"),
                f"{record_type}.available_at",
            ):
                raise PromotionDisclosureError(
                    f"disclosure predates canonical {record_type} availability"
                )

        disclosure = self._gate.record(
            dataset_snapshot=dataset_snapshot,
            research_protocol_id=protocol_id,
            confirmation_trial_family_id=trial_family_id,
            channel=channel,
            kind=kind,
            accessible_to_adaptive_actor=accessible_to_adaptive_actor,
            disclosed_at_utc=disclosed_at_utc,
        )

        effective_sample_size = ep.get("effective_sample_size")
        if (
            type(effective_sample_size) is not int
            or effective_sample_size <= 0
        ):
            raise PromotionDisclosureError(
                "PromotionEvidence effective_sample_size is invalid"
            )
        guardrails_passed = ep.get("guardrails_passed")
        if type(guardrails_passed) is not bool:
            raise PromotionDisclosureError(
                "PromotionEvidence guardrails_passed is invalid"
            )

        payload = PromotionDisclosurePayload(
            promotion_decision_id=decision_id,
            promotion_decision_record_sha256=_sha256(
                decision.get("record_sha256"),
                "PromotionDecision.record_sha256",
            ),
            promotion_action=_text(dp.get("action"), "PromotionDecision.action"),
            promotion_evidence_id=_text(
                evidence.get("record_id"),
                "PromotionEvidence.record_id",
            ),
            promotion_evidence_record_sha256=_sha256(
                evidence.get("record_sha256"),
                "PromotionEvidence.record_sha256",
            ),
            experiment_id=_text(
                experiment.get("record_id"),
                "Experiment.record_id",
            ),
            experiment_record_sha256=_sha256(
                experiment.get("record_sha256"),
                "Experiment.record_sha256",
            ),
            experiment_outcome=_text(xp.get("outcome"), "Experiment.outcome"),
            evaluation_bundle_id=_text(
                bundle.get("record_id"),
                "EvaluationBundle.record_id",
            ),
            evaluation_bundle_sha256=_sha256(
                bp.get("bundle_sha256"),
                "EvaluationBundle.bundle_sha256",
            ),
            evaluation_bundle_record_sha256=_sha256(
                bundle.get("record_sha256"),
                "EvaluationBundle.record_sha256",
            ),
            dataset_snapshot_id=_text(
                dataset.get("record_id"),
                "DatasetSnapshot.record_id",
            ),
            dataset_snapshot_record_sha256=_sha256(
                dataset.get("record_sha256"),
                "DatasetSnapshot.record_sha256",
            ),
            research_protocol_id=protocol_id,
            protocol_sha256=_sha256(
                pp.get("protocol_sha256"),
                "ResearchProtocol.protocol_sha256",
            ),
            research_protocol_record_sha256=_sha256(
                protocol.get("record_sha256"),
                "ResearchProtocol.record_sha256",
            ),
            confirmation_trial_family_id=trial_family_id,
            holdout_access_id=expected_access_id,
            holdout_consumption_id=disclosure.consumption.consumption_id,
            effective_sample_size=effective_sample_size,
            effect_interval_low=_text(
                ep.get("effect_interval_low"),
                "PromotionEvidence.effect_interval_low",
            ),
            effect_interval_high=_text(
                ep.get("effect_interval_high"),
                "PromotionEvidence.effect_interval_high",
            ),
            practical_improvement=_text(
                ep.get("practical_improvement"),
                "PromotionEvidence.practical_improvement",
            ),
            guardrails_passed=guardrails_passed,
            validity=_text(ep.get("validity"), "PromotionEvidence.validity"),
        )

        sink(payload)
        return PromotionDisclosureReceipt(
            disclosure=disclosure,
            payload=payload,
        )
