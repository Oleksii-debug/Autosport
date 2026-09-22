from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .scientific_registry import RegistryEntry, ScientificRegistry


_AUTHORITY_KIND = "autosport.risk-of-ruin-product-authority.v2"
_BOUND_SEMANTICS = "probability_upper_bound"
_CONFIDENCE_SEMANTICS = "protocol_defined_upper_bound"

# Keep this authority fence local to the risk consumer. Importing the stronger
# process-wide ScientificRegistry read-authority hook here would mutate
# ScientificRegistry._read for every consumer merely because autosport.risk was
# imported. These captured functions instead let this verifier detect runtime
# class/instance rebinding without widening unrelated registry semantics.
_SCIENTIFIC_REGISTRY_GET = ScientificRegistry.get
_SCIENTIFIC_REGISTRY_READ = ScientificRegistry._read
_SCIENTIFIC_REGISTRY_VALIDATE_ENTRY = ScientificRegistry._validate_entry


def _canonical_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("risk-of-ruin authority requires finite Decimal values")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _canonical_sha256(value: object, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a SHA-256 string")
    value = value.lower()
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{name} must be a canonical SHA-256 digest")
    return value


def _instant(value: str) -> datetime:
    if type(value) is not str or not value:
        raise ValueError("risk-of-ruin authority timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("risk-of-ruin authority timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _payload(evidence: object, *, kind: str) -> dict[str, Any]:
    if kind == "single":
        candidate = {
            "candidate_sha256": getattr(evidence, "candidate_sha256"),
            "evaluated_stake": _canonical_decimal(getattr(evidence, "evaluated_stake")),
        }
    elif kind == "vector":
        stakes = getattr(evidence, "evaluated_stakes")
        if type(stakes) is not tuple:
            raise ValueError("vector risk-of-ruin authority requires tuple stakes")
        candidate = {
            "candidate_vector_sha256": getattr(evidence, "candidate_vector_sha256"),
            "evaluated_stakes": [_canonical_decimal(value) for value in stakes],
        }
    else:
        raise ValueError("unsupported risk-of-ruin authority kind")

    return {
        "schema": _AUTHORITY_KIND,
        "kind": kind,
        "evidence_id": getattr(evidence, "evidence_id"),
        "research_protocol_sha256": getattr(evidence, "research_protocol_sha256"),
        "reproducibility_bundle_sha256": getattr(
            evidence, "reproducibility_bundle_sha256"
        ),
        "producer_identity": getattr(evidence, "producer_identity"),
        "causal_cutoff": getattr(evidence, "causal_cutoff"),
        "evaluated_at": getattr(evidence, "evaluated_at"),
        "bankroll_id": getattr(evidence, "bankroll_id"),
        "currency": getattr(evidence, "currency"),
        "base_portfolio_sha256": getattr(evidence, "base_portfolio_sha256"),
        "upper_bound": _canonical_decimal(getattr(evidence, "upper_bound")),
        "bound_semantics": _BOUND_SEMANTICS,
        "confidence_semantics": _CONFIDENCE_SEMANTICS,
        **candidate,
    }


def risk_of_ruin_result_sha256(
    evidence: object,
    *,
    kind: str,
    evaluator_source_sha256: str,
    dataset_snapshot_id: str,
    dataset_manifest_sha256: str,
    effective_sample_size: int,
    evaluation_available_at: str,
) -> str:
    """Digest the exact issued result plus canonical evaluation identity.

    Secrecy is deliberately irrelevant. The digest binds method/source version,
    dataset identity/content, sufficiency, evaluation availability, protocol and
    the exact scalar/vector result. Positive authority still requires this digest
    to be present in the immutable durable EvaluationBundle record.
    """

    if type(dataset_snapshot_id) is not str or not dataset_snapshot_id:
        raise ValueError("dataset_snapshot_id must be a non-empty string")
    if (
        isinstance(effective_sample_size, bool)
        or not isinstance(effective_sample_size, int)
        or effective_sample_size <= 0
    ):
        raise ValueError("effective_sample_size must be a positive integer")
    evaluation_available_at = _instant(evaluation_available_at).isoformat()
    payload = {
        **_payload(evidence, kind=kind),
        "evaluation": {
            "evaluator_source_sha256": _canonical_sha256(
                evaluator_source_sha256, "evaluator_source_sha256"
            ),
            "dataset_snapshot_id": dataset_snapshot_id,
            "dataset_manifest_sha256": _canonical_sha256(
                dataset_manifest_sha256, "dataset_manifest_sha256"
            ),
            "effective_sample_size": effective_sample_size,
            "available_at": evaluation_available_at,
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_exact_registry_state(registry: ScientificRegistry) -> dict[str, Any]:
    """Read one generation only through the exact captured registry implementation."""

    if type(registry) is not ScientificRegistry:
        raise ValueError("risk-of-ruin registry must be exact ScientificRegistry")
    if set(vars(registry)) != {"path"}:
        raise ValueError("risk-of-ruin registry instance read authority was rebound")

    def _require_live_bindings() -> None:
        # Generic get() is not used for resolution, but a rebound public read
        # surface is still evidence that this registry object is not the exact
        # supported authority shape.  _read itself is intentionally NOT compared
        # here: other canonical product modules may install the verified
        # process-wide reader after this module imports.  We invoke the captured
        # implementation directly, so such later dispatch changes cannot retarget
        # this verifier or make its behavior test-order dependent.
        if (
            ScientificRegistry.get is not _SCIENTIFIC_REGISTRY_GET
            or ScientificRegistry._validate_entry is not _SCIENTIFIC_REGISTRY_VALIDATE_ENTRY
        ):
            raise ValueError("risk-of-ruin registry executable read authority was rebound")

    _require_live_bindings()
    state = _SCIENTIFIC_REGISTRY_READ(registry)
    # Revalidate every returned envelope through the captured validator rather
    # than trusting any dynamically-dispatched validation used inside _read.
    records = state.get("records")
    if type(records) is not list:
        raise ValueError("risk-of-ruin registry records must be a list")
    for raw in records:
        _SCIENTIFIC_REGISTRY_VALIDATE_ENTRY(raw)
    _require_live_bindings()
    return state


def _entry_from_state(
    state: dict[str, Any],
    record_type: str,
    record_id: str,
) -> RegistryEntry | None:
    """Resolve one record from one already-verified registry generation."""

    for raw in state["records"]:
        if raw["record_type"] == record_type and raw["record_id"] == record_id:
            return RegistryEntry(**raw)
    return None


def verify_risk_of_ruin_authority(
    registry_path: str | Path | None,
    evidence: object,
    *,
    kind: str,
    available_by: str,
) -> tuple[bool, str]:
    """Re-resolve one risk result from durable product-owned scientific history."""

    prefix = "portfolio" if kind == "single" else "portfolio vector"
    if registry_path is None:
        return False, f"{prefix} risk-of-ruin evidence lacks product-issued durable authority"
    try:
        registry = ScientificRegistry(registry_path)
        state = _read_exact_registry_state(registry)
        evidence_id = getattr(evidence, "evidence_id")
        entry = _entry_from_state(state, "EvaluationBundle", evidence_id)
        if entry is None:
            return False, f"{prefix} risk-of-ruin evidence is not product-issued"
        bundle = entry.payload
        if bundle.get("created_at") != entry.available_at:
            return False, f"{prefix} risk-of-ruin durable evaluation identity is inconsistent"
        if (
            bundle.get("bundle_sha256")
            != getattr(evidence, "reproducibility_bundle_sha256").lower()
            or bundle.get("protocol_sha256")
            != getattr(evidence, "research_protocol_sha256").lower()
        ):
            return False, f"{prefix} risk-of-ruin durable authority lineage does not match"

        issued_at = _instant(entry.available_at)
        evaluated_at = _instant(getattr(evidence, "evaluated_at"))
        proposal_time = _instant(available_by)
        if issued_at < evaluated_at:
            return False, f"{prefix} risk-of-ruin authority predates its claimed evaluation"
        if issued_at > proposal_time:
            return False, f"{prefix} risk-of-ruin authority was not available at proposal time"

        dataset_id = bundle.get("dataset_snapshot_id")
        if type(dataset_id) is not str or not dataset_id:
            return False, f"{prefix} risk-of-ruin authority lacks canonical dataset lineage"
        dataset = _entry_from_state(state, "DatasetSnapshot", dataset_id)
        if dataset is None:
            return False, f"{prefix} risk-of-ruin authority references missing dataset"
        dataset_available_at = _instant(dataset.available_at)
        if dataset_available_at > evaluated_at:
            return (
                False,
                f"{prefix} risk-of-ruin authority uses data unavailable at evaluation time",
            )
        if dataset_available_at > issued_at:
            return False, f"{prefix} risk-of-ruin authority uses a future dataset"
        outcome_reveal_after = dataset.payload.get("outcome_reveal_after")
        if outcome_reveal_after is not None:
            if type(outcome_reveal_after) is not str:
                return (
                    False,
                    f"{prefix} risk-of-ruin authority has invalid outcome visibility",
                )
            if _instant(outcome_reveal_after) > evaluated_at:
                return (
                    False,
                    f"{prefix} risk-of-ruin outcomes were not causally available at evaluation time",
                )
        dataset_cutoff = dataset.payload.get("causal_cutoff")
        if type(dataset_cutoff) is not str or _instant(dataset_cutoff) > _instant(
            getattr(evidence, "causal_cutoff")
        ):
            return False, f"{prefix} risk-of-ruin authority dataset exceeds causal cutoff"
        manifest_sha256 = dataset.payload.get("manifest_sha256")
        evaluator_source_sha256 = bundle.get("evaluator_source_sha256")
        effective_sample_size = bundle.get("effective_sample_size")
        if (
            type(manifest_sha256) is not str
            or type(evaluator_source_sha256) is not str
            or isinstance(effective_sample_size, bool)
            or not isinstance(effective_sample_size, int)
            or effective_sample_size <= 0
        ):
            return False, f"{prefix} risk-of-ruin scientific sufficiency is unknown"

        result_sha256 = risk_of_ruin_result_sha256(
            evidence,
            kind=kind,
            evaluator_source_sha256=evaluator_source_sha256,
            dataset_snapshot_id=dataset_id,
            dataset_manifest_sha256=manifest_sha256,
            effective_sample_size=effective_sample_size,
            evaluation_available_at=entry.available_at,
        )
        artifacts = bundle.get("artifact_hashes")
        if type(artifacts) is not list or result_sha256 not in artifacts:
            return False, f"{prefix} risk-of-ruin durable result digest does not match"
    except (AttributeError, OSError, TypeError, ValueError):
        return False, f"{prefix} risk-of-ruin durable authority is invalid"

    # A generic ScientificRegistry row proves durable provenance and integrity,
    # not product issuance. The public registry constructor/append APIs are
    # deliberately usable by ordinary callers, so accepting registry membership
    # here would recreate the caller-minting defect this gate exists to prevent.
    #
    # Positive financial authority can be enabled only when an independent,
    # durable product-owned risk evaluator/issuer is available and this verifier
    # can re-resolve that issuer identity in addition to the scientific lineage.
    return (
        False,
        f"{prefix} risk-of-ruin evidence lacks canonical product-issued evaluator authority",
    )
