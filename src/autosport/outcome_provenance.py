from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_REDISTRIBUTION = frozenset({"prohibited", "internal_only", "permitted"})


@dataclass(frozen=True, slots=True)
class OutcomeProvenance:
    source_identity: str
    source_reference: str
    authority_reference: str
    terms_reference: str
    retention_basis: str
    redistribution_policy: str
    acquired_at: str
    verified_at: str
    source_payload_sha256: str
    quote_outcomes_sha256: str


def _text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"sealed results.outcome_provenance.{key} must be a non-empty string")
    return value.strip()


def _timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    return parsed


def canonical_outcomes_sha256(outcomes: dict[str, Any]) -> str:
    canonical = json.dumps(outcomes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_outcome_provenance(
    results_raw: dict[str, Any],
    *,
    outcome_reveal_after: str,
    dataset_imported_at: str,
) -> OutcomeProvenance:
    """Fail closed unless sealed outcomes carry content-bound authoritative provenance.

    This proves that the dataset explicitly binds its exact quote-outcome map to a
    declared external source/evidence record and rights basis. It does not contact
    that source and therefore does not turn an assertion into independent legal or
    factual verification; callers must retain the referenced source evidence.
    """

    raw = results_raw.get("outcome_provenance")
    if not isinstance(raw, dict):
        raise ValueError("schema v2 historical sealed results require outcome_provenance object")
    if int(raw.get("schema_version", 0)) != 1:
        raise ValueError("sealed results.outcome_provenance.schema_version must be 1")
    if raw.get("kind") != "historical_outcome_provenance":
        raise ValueError(
            "sealed results.outcome_provenance.kind must be historical_outcome_provenance"
        )
    if raw.get("authoritative_outcomes_verified") is not True:
        raise ValueError(
            "sealed results.outcome_provenance.authoritative_outcomes_verified must be true"
        )
    if raw.get("licensing_or_retention_verified") is not True:
        raise ValueError(
            "sealed results.outcome_provenance.licensing_or_retention_verified must be true"
        )
    if raw.get("real_money_execution") is not False:
        raise ValueError("sealed results.outcome_provenance.real_money_execution must be false")

    source_identity = _text(raw, "source_identity")
    source_reference = _text(raw, "source_reference")
    authority_reference = _text(raw, "authority_reference")
    terms_reference = _text(raw, "terms_reference")
    retention_basis = _text(raw, "retention_basis")
    redistribution_policy = _text(raw, "redistribution_policy")
    if redistribution_policy not in _ALLOWED_REDISTRIBUTION:
        raise ValueError(
            "sealed results.outcome_provenance.redistribution_policy must be prohibited, internal_only, or permitted"
        )
    redistribution_verified = raw.get("redistribution_verified")
    if not isinstance(redistribution_verified, bool):
        raise ValueError(
            "sealed results.outcome_provenance.redistribution_verified must be boolean"
        )
    if redistribution_policy == "permitted" and redistribution_verified is not True:
        raise ValueError(
            "permitted outcome redistribution requires outcome_provenance.redistribution_verified=true"
        )

    acquired_at = _text(raw, "acquired_at")
    verified_at = _text(raw, "verified_at")
    acquired_dt = _timestamp(acquired_at, field="sealed results.outcome_provenance.acquired_at")
    verified_dt = _timestamp(verified_at, field="sealed results.outcome_provenance.verified_at")
    reveal_dt = _timestamp(outcome_reveal_after, field="sealed results.outcome_reveal_after")
    imported_dt = _timestamp(dataset_imported_at, field="governance.imported_at")
    if acquired_dt < reveal_dt:
        raise ValueError("outcome provenance acquired_at must not precede outcome_reveal_after")
    if verified_dt < acquired_dt:
        raise ValueError("outcome provenance verified_at must not precede acquired_at")
    if imported_dt < verified_dt:
        raise ValueError("governance.imported_at must not precede outcome provenance verified_at")

    source_payload_sha256 = _text(raw, "source_payload_sha256")
    if _SHA256.fullmatch(source_payload_sha256) is None:
        raise ValueError(
            "sealed results.outcome_provenance.source_payload_sha256 must be 64 lowercase hex characters"
        )
    quote_outcomes_sha256 = _text(raw, "quote_outcomes_sha256")
    if _SHA256.fullmatch(quote_outcomes_sha256) is None:
        raise ValueError(
            "sealed results.outcome_provenance.quote_outcomes_sha256 must be 64 lowercase hex characters"
        )
    outcomes = results_raw.get("quote_outcomes")
    if not isinstance(outcomes, dict):
        raise ValueError("quote_outcomes must be an object")
    actual_outcomes_sha = canonical_outcomes_sha256(outcomes)
    if quote_outcomes_sha256 != actual_outcomes_sha:
        raise ValueError("sealed outcome provenance quote_outcomes_sha256 mismatch")

    return OutcomeProvenance(
        source_identity=source_identity,
        source_reference=source_reference,
        authority_reference=authority_reference,
        terms_reference=terms_reference,
        retention_basis=retention_basis,
        redistribution_policy=redistribution_policy,
        acquired_at=acquired_at,
        verified_at=verified_at,
        source_payload_sha256=source_payload_sha256,
        quote_outcomes_sha256=quote_outcomes_sha256,
    )
