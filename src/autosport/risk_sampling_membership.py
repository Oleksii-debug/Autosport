from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .scientific_registry import RegistryEntry, ScientificRegistry


_DESIGN_KIND = "autosport-risk-fixed-n-run-membership-v1"
_RISK_METHOD = "CLOPPER_PEARSON_ONE_SIDED"
_DEPENDENCE_STATUS = "SEPARATE_REQUIRED"
_REQUIRED_DESIGN_FIELDS = frozenset(
    {
        "kind",
        "dataset_snapshot_id",
        "planned_run_ids",
        "planned_n",
        "sampling_frame_sha256",
        "risk_method",
        "dependence_qualification",
    }
)
_HEX = frozenset("0123456789abcdef")


class RiskSamplingMembershipError(RuntimeError):
    """Raised when fixed-N membership cannot be re-resolved fail-closed."""


def _canonical_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RiskSamplingMembershipError(f"{name} must be a non-empty canonical string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RiskSamplingMembershipError(f"{name} must be valid UTF-8 text") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _canonical_text(value, name).lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise RiskSamplingMembershipError(f"{name} must be a canonical SHA-256 hex string")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _canonical_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RiskSamplingMembershipError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RiskSamplingMembershipError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RiskSamplingMembershipError(
                f"fixed-N evaluation design contains duplicate JSON key {key!r}"
            )
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise RiskSamplingMembershipError(
        f"fixed-N evaluation design contains non-finite JSON value {value!r}"
    )


def _parse_design(text: object) -> tuple[dict[str, Any], str]:
    raw = _canonical_text(text, "binding.evaluation_design")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise RiskSamplingMembershipError(
            "binding.evaluation_design must be canonical JSON"
        ) from exc
    if type(payload) is not dict or set(payload) != _REQUIRED_DESIGN_FIELDS:
        raise RiskSamplingMembershipError(
            "fixed-N evaluation design fields do not match the supported schema"
        )
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if raw != canonical:
        raise RiskSamplingMembershipError(
            "binding.evaluation_design must use canonical JSON serialization"
        )
    return payload, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _entry(
    registry: ScientificRegistry,
    record_type: str,
    record_id: str,
) -> RegistryEntry:
    value = registry.get(record_type, record_id)
    if value is None:
        raise RiskSamplingMembershipError(
            f"missing canonical {record_type} record {record_id!r}"
        )
    return value


@dataclass(frozen=True, slots=True)
class ResolvedFixedNRiskMembership:
    """Immutable description re-resolved from canonical scientific records.

    This value is not a bearer capability. Financial/risk consumers must invoke
    resolve_fixed_n_risk_membership against the canonical product registry
    rather than trusting a caller-created instance.
    """

    research_protocol_id: str
    protocol_sha256: str
    protocol_record_sha256: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    dataset_record_sha256: str
    causal_cutoff: str
    outcome_reveal_after: str
    precommitted_at: str
    planned_run_ids: tuple[str, ...]
    sampling_frame_sha256: str
    design_sha256: str
    risk_method: str = _RISK_METHOD

    @property
    def planned_n(self) -> int:
        return len(self.planned_run_ids)

    @property
    def iid_qualified(self) -> bool:
        """Membership uniqueness is deliberately not an IID/dependence proof."""
        return False


def resolve_fixed_n_risk_membership(
    registry_path: str | Path,
    *,
    research_protocol_id: str,
    dataset_snapshot_id: str,
) -> ResolvedFixedNRiskMembership:
    """Re-resolve exact fixed-N run membership precommitted before outcome reveal.

    The resolver composes existing ScientificRegistry authority only. It does
    not issue risk evidence and intentionally refuses to infer IID/dependence from
    unique IDs, hashes, or a caller assertion.
    """

    protocol_id = _canonical_text(research_protocol_id, "research_protocol_id")
    dataset_id = _canonical_text(dataset_snapshot_id, "dataset_snapshot_id")
    registry = ScientificRegistry(registry_path)
    protocol = _entry(registry, "ResearchProtocol", protocol_id)
    dataset = _entry(registry, "DatasetSnapshot", dataset_id)

    protocol_payload = protocol.payload
    dataset_payload = dataset.payload
    if protocol_payload.get("research_protocol_id") != protocol_id:
        raise RiskSamplingMembershipError("ResearchProtocol payload identity mismatch")
    if dataset_payload.get("dataset_snapshot_id") != dataset_id:
        raise RiskSamplingMembershipError("DatasetSnapshot payload identity mismatch")

    protocol_sha256 = _sha256(
        protocol_payload.get("protocol_sha256"),
        "ResearchProtocol.protocol_sha256",
    )
    protocol_record_sha256 = _sha256(
        protocol.record_sha256,
        "ResearchProtocol.record_sha256",
    )
    dataset_record_sha256 = _sha256(
        dataset.record_sha256,
        "DatasetSnapshot.record_sha256",
    )
    protocol_manifest = _sha256(
        protocol_payload.get("dataset_manifest_sha256"),
        "ResearchProtocol.dataset_manifest_sha256",
    )
    dataset_manifest = _sha256(
        dataset_payload.get("manifest_sha256"),
        "DatasetSnapshot.manifest_sha256",
    )
    if protocol_manifest != dataset_manifest:
        raise RiskSamplingMembershipError(
            "ResearchProtocol and DatasetSnapshot manifest identities differ"
        )

    binding = protocol_payload.get("binding")
    if type(binding) is not dict:
        raise RiskSamplingMembershipError("ResearchProtocol binding is missing or invalid")
    if binding.get("research_protocol_id") != protocol_id:
        raise RiskSamplingMembershipError("ResearchProtocol binding identity mismatch")
    binding_canonical = json.dumps(
        binding,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    binding_sha256 = hashlib.sha256(binding_canonical.encode("utf-8")).hexdigest()
    if binding_sha256 != protocol_sha256:
        raise RiskSamplingMembershipError(
            "ResearchProtocol protocol_sha256 does not match its exact binding"
        )

    design, design_sha256 = _parse_design(binding.get("evaluation_design"))
    if design.get("kind") != _DESIGN_KIND:
        raise RiskSamplingMembershipError("fixed-N evaluation design kind is unsupported")
    if design.get("dataset_snapshot_id") != dataset_id:
        raise RiskSamplingMembershipError(
            "fixed-N evaluation design dataset identity mismatch"
        )
    if design.get("risk_method") != _RISK_METHOD:
        raise RiskSamplingMembershipError(
            "fixed-N evaluation design risk method is unsupported"
        )
    if design.get("dependence_qualification") != _DEPENDENCE_STATUS:
        raise RiskSamplingMembershipError(
            "fixed-N membership must keep dependence qualification separate"
        )

    planned_n = design.get("planned_n")
    if isinstance(planned_n, bool) or not isinstance(planned_n, int) or planned_n <= 0:
        raise RiskSamplingMembershipError("fixed-N planned_n must be a positive integer")
    raw_run_ids = design.get("planned_run_ids")
    if type(raw_run_ids) is not list or not raw_run_ids:
        raise RiskSamplingMembershipError(
            "fixed-N planned_run_ids must be a non-empty JSON array"
        )
    run_ids = tuple(
        _canonical_text(value, f"planned_run_ids[{index}]")
        for index, value in enumerate(raw_run_ids)
    )
    if len(run_ids) != len(set(run_ids)):
        raise RiskSamplingMembershipError(
            "fixed-N planned_run_ids must not contain duplicates"
        )
    if run_ids != tuple(sorted(run_ids)):
        raise RiskSamplingMembershipError(
            "fixed-N planned_run_ids must use canonical lexical order"
        )
    if planned_n != len(run_ids):
        raise RiskSamplingMembershipError(
            "fixed-N planned_n must equal the exact precommitted run membership"
        )

    sampling_frame_sha256 = _sha256(
        design.get("sampling_frame_sha256"),
        "fixed-N sampling_frame_sha256",
    )

    binding_cutoff = _canonical_text(binding.get("causal_cutoff"), "binding.causal_cutoff")
    dataset_cutoff = _canonical_text(
        dataset_payload.get("causal_cutoff"),
        "DatasetSnapshot.causal_cutoff",
    )
    if binding_cutoff != dataset_cutoff:
        raise RiskSamplingMembershipError(
            "ResearchProtocol and DatasetSnapshot causal cutoffs differ"
        )

    frozen_at = _instant(binding.get("frozen_at_utc"), "binding.frozen_at_utc")
    dataset_available_at = _instant(
        dataset.available_at,
        "DatasetSnapshot.available_at",
    )
    protocol_available_at = _instant(
        protocol.available_at,
        "ResearchProtocol.available_at",
    )
    cutoff_at = _instant(dataset_cutoff, "DatasetSnapshot.causal_cutoff")
    reveal_text = dataset_payload.get("outcome_reveal_after")
    if reveal_text is None:
        raise RiskSamplingMembershipError(
            "DatasetSnapshot outcome_reveal_after is required for fixed-N precommit authority"
        )
    reveal_at = _instant(reveal_text, "DatasetSnapshot.outcome_reveal_after")

    if cutoff_at > dataset_available_at:
        raise RiskSamplingMembershipError(
            "DatasetSnapshot cannot be available before its causal cutoff"
        )
    if dataset_available_at > protocol_available_at:
        raise RiskSamplingMembershipError(
            "fixed-N protocol was recorded before the exact DatasetSnapshot was available"
        )
    if frozen_at > protocol_available_at:
        raise RiskSamplingMembershipError(
            "ResearchProtocol available_at precedes its frozen_at_utc"
        )
    if protocol_available_at >= reveal_at:
        raise RiskSamplingMembershipError(
            "fixed-N membership must be committed strictly before outcome reveal"
        )

    return ResolvedFixedNRiskMembership(
        research_protocol_id=protocol_id,
        protocol_sha256=protocol_sha256,
        protocol_record_sha256=protocol_record_sha256,
        dataset_snapshot_id=dataset_id,
        dataset_manifest_sha256=dataset_manifest,
        dataset_record_sha256=dataset_record_sha256,
        causal_cutoff=dataset_cutoff,
        outcome_reveal_after=_canonical_text(
            reveal_text,
            "DatasetSnapshot.outcome_reveal_after",
        ),
        precommitted_at=_canonical_text(
            protocol.available_at,
            "ResearchProtocol.available_at",
        ),
        planned_run_ids=run_ids,
        sampling_frame_sha256=sampling_frame_sha256,
        design_sha256=design_sha256,
    )
