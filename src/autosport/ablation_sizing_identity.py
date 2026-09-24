"""Deterministic scientific identity for sizing-policy ablation.

This module provides identity/equality evidence only.  It does not prove that an
upstream risk authority is valid and it never grants stake, risk, execution, or
promotion authority.  A scientific protocol may use the identity to prove that
two otherwise identical arms changed the sizing policy, while the existing
canonical authorities remain responsible for whether either policy may act.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import unicodedata
from typing import Final


SCHEMA: Final = "autosport.ablation-sizing-policy-identity"
SCHEMA_VERSION: Final = 1
AUTHORITY_FAMILY: Final = "science.causal-ablation-attribution.sizing-identity-v1"


class AblationSizingIdentityError(ValueError):
    """Raised when sizing-ablation identity inputs are not canonical."""


def _text(value: object, field: str) -> str:
    if type(value) is not str:
        raise AblationSizingIdentityError(f"{field} must be text")
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or normalized != normalized.strip():
        raise AblationSizingIdentityError(f"{field} must be non-empty trimmed text")
    try:
        normalized.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise AblationSizingIdentityError(f"{field} must be valid UTF-8") from exc
    return normalized


def _sha256_hex(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise AblationSizingIdentityError(f"{field} must be lowercase SHA-256 hex")
    return text


def _decimal_text(value: Decimal) -> str:
    """Return an exact, ambient-context-independent canonical Decimal spelling."""
    if not value.is_finite():
        raise AblationSizingIdentityError("Decimal parameters must be finite")
    if value.is_zero():
        return "0"

    sign, raw_digits, exponent = value.as_tuple()
    digits = list(raw_digits)
    # Decimal values with different lexical trailing zeros are semantically equal.
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1

    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        body = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            body = coefficient[:point] + "." + coefficient[point:]
        else:
            body = "0." + ("0" * (-point)) + coefficient
    return ("-" if sign else "") + body


def _canonical_value(value: object, field: str) -> object:
    if value is None:
        return {"type": "null"}
    if type(value) is bool:
        return {"type": "bool", "value": value}
    if type(value) is int:
        return {"type": "int", "value": str(value)}
    if type(value) is Decimal:
        return {"type": "decimal", "value": _decimal_text(value)}
    if type(value) is str:
        return {"type": "text", "value": unicodedata.normalize("NFC", value)}
    if type(value) is dict:
        normalized: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = _text(raw_key, f"{field} key")
            if key in normalized:
                raise AblationSizingIdentityError(
                    f"{field} contains duplicate keys after Unicode normalization"
                )
            normalized[key] = _canonical_value(raw_value, f"{field}.{key}")
        return {
            "type": "mapping",
            "value": [[key, normalized[key]] for key in sorted(normalized)],
        }
    if type(value) in {list, tuple}:
        return {
            "type": "sequence",
            "value": [
                _canonical_value(item, f"{field}[{index}]")
                for index, item in enumerate(value)
            ],
        }
    raise AblationSizingIdentityError(
        f"{field} contains unsupported value type {type(value).__name__}"
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SizingPolicyIdentity:
    """Immutable scientific identity; never a financial permission."""

    implementation_sha256: str
    risk_authority_identity: str
    canonical_parameters_json: str
    parameters_sha256: str
    policy_fingerprint: str

    def __post_init__(self) -> None:
        _sha256_hex(self.implementation_sha256, "implementation_sha256")
        _text(self.risk_authority_identity, "risk_authority_identity")
        _sha256_hex(self.parameters_sha256, "parameters_sha256")
        _sha256_hex(self.policy_fingerprint, "policy_fingerprint")
        if type(self.canonical_parameters_json) is not str:
            raise AblationSizingIdentityError("canonical_parameters_json must be text")
        try:
            parsed = json.loads(self.canonical_parameters_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise AblationSizingIdentityError(
                "canonical_parameters_json must be canonical JSON"
            ) from exc
        if _canonical_json(parsed) != self.canonical_parameters_json:
            raise AblationSizingIdentityError(
                "canonical_parameters_json must use canonical serialization"
            )
        if _digest(parsed) != self.parameters_sha256:
            raise AblationSizingIdentityError("parameters_sha256 does not match parameters")
        expected = _digest(
            {
                "authority_family": AUTHORITY_FAMILY,
                "implementation_sha256": self.implementation_sha256,
                "parameters_sha256": self.parameters_sha256,
                "risk_authority_identity": self.risk_authority_identity,
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
            }
        )
        if expected != self.policy_fingerprint:
            raise AblationSizingIdentityError("policy_fingerprint does not match identity")

    @property
    def authorizes_stake(self) -> bool:
        return False

    @property
    def authorizes_risk(self) -> bool:
        return False

    @property
    def authorizes_execution(self) -> bool:
        return False

    @property
    def authorizes_promotion(self) -> bool:
        return False


def derive_sizing_policy_identity(
    *,
    implementation_sha256: str,
    risk_authority_identity: str,
    parameters: Mapping[str, object],
) -> SizingPolicyIdentity:
    """Derive a deterministic sizing component identity from frozen inputs."""
    implementation = _sha256_hex(implementation_sha256, "implementation_sha256")
    risk_identity = _text(risk_authority_identity, "risk_authority_identity")
    if type(parameters) is not dict:
        raise AblationSizingIdentityError("parameters must be a mapping")
    normalized_parameters = _canonical_value(parameters, "parameters")
    canonical_parameters_json = _canonical_json(normalized_parameters)
    parameters_sha256 = _digest(normalized_parameters)
    policy_fingerprint = _digest(
        {
            "authority_family": AUTHORITY_FAMILY,
            "implementation_sha256": implementation,
            "parameters_sha256": parameters_sha256,
            "risk_authority_identity": risk_identity,
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
        }
    )
    return SizingPolicyIdentity(
        implementation_sha256=implementation,
        risk_authority_identity=risk_identity,
        canonical_parameters_json=canonical_parameters_json,
        parameters_sha256=parameters_sha256,
        policy_fingerprint=policy_fingerprint,
    )


@dataclass(frozen=True, slots=True)
class SizingAblationContext:
    """Frozen non-target context that must remain identical across ablation arms."""

    experiment_id: str
    data_window_id: str
    evaluator_id: str
    market_universe_id: str
    provider_snapshot_id: str
    non_target_policy_fingerprint: str

    def __post_init__(self) -> None:
        for field in (
            "experiment_id",
            "data_window_id",
            "evaluator_id",
            "market_universe_id",
            "provider_snapshot_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(
            self,
            "non_target_policy_fingerprint",
            _sha256_hex(
                self.non_target_policy_fingerprint,
                "non_target_policy_fingerprint",
            ),
        )

    @property
    def context_fingerprint(self) -> str:
        return _digest(
            {
                "data_window_id": self.data_window_id,
                "evaluator_id": self.evaluator_id,
                "experiment_id": self.experiment_id,
                "market_universe_id": self.market_universe_id,
                "non_target_policy_fingerprint": self.non_target_policy_fingerprint,
                "provider_snapshot_id": self.provider_snapshot_id,
            }
        )


@dataclass(frozen=True, slots=True)
class SizingAblationAssessment:
    """Mechanical single-factor identity check, not a causal-effect estimate."""

    eligible: bool
    reason: str
    baseline_sizing_fingerprint: str
    candidate_sizing_fingerprint: str
    baseline_context_fingerprint: str
    candidate_context_fingerprint: str
    mismatch_fields: tuple[str, ...]

    @property
    def causal_effect_proven(self) -> bool:
        return False

    @property
    def authorizes_stake(self) -> bool:
        return False

    @property
    def authorizes_execution(self) -> bool:
        return False

    @property
    def authorizes_promotion(self) -> bool:
        return False


def assess_sizing_only_ablation(
    *,
    baseline_sizing: SizingPolicyIdentity,
    candidate_sizing: SizingPolicyIdentity,
    baseline_context: SizingAblationContext,
    candidate_context: SizingAblationContext,
) -> SizingAblationAssessment:
    """Check whether sizing identity is the only declared changed factor."""
    if type(baseline_sizing) is not SizingPolicyIdentity:
        raise TypeError("baseline_sizing must be SizingPolicyIdentity")
    if type(candidate_sizing) is not SizingPolicyIdentity:
        raise TypeError("candidate_sizing must be SizingPolicyIdentity")
    if type(baseline_context) is not SizingAblationContext:
        raise TypeError("baseline_context must be SizingAblationContext")
    if type(candidate_context) is not SizingAblationContext:
        raise TypeError("candidate_context must be SizingAblationContext")

    context_fields = (
        "experiment_id",
        "data_window_id",
        "evaluator_id",
        "market_universe_id",
        "provider_snapshot_id",
        "non_target_policy_fingerprint",
    )
    mismatch_fields = tuple(
        field
        for field in context_fields
        if getattr(baseline_context, field) != getattr(candidate_context, field)
    )
    if mismatch_fields:
        eligible = False
        reason = "non_target_context_mismatch"
    elif baseline_sizing.policy_fingerprint == candidate_sizing.policy_fingerprint:
        eligible = False
        reason = "sizing_identity_unchanged"
    else:
        eligible = True
        reason = "single_factor_sizing_identity_diff"

    return SizingAblationAssessment(
        eligible=eligible,
        reason=reason,
        baseline_sizing_fingerprint=baseline_sizing.policy_fingerprint,
        candidate_sizing_fingerprint=candidate_sizing.policy_fingerprint,
        baseline_context_fingerprint=baseline_context.context_fingerprint,
        candidate_context_fingerprint=candidate_context.context_fingerprint,
        mismatch_fields=mismatch_fields,
    )


__all__ = [
    "AUTHORITY_FAMILY",
    "SCHEMA",
    "SCHEMA_VERSION",
    "AblationSizingIdentityError",
    "SizingAblationAssessment",
    "SizingAblationContext",
    "SizingPolicyIdentity",
    "assess_sizing_only_ablation",
    "derive_sizing_policy_identity",
]
