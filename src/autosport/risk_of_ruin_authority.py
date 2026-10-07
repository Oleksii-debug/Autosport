from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ._scientific_registry_read_authority import (
    ScientificRegistryReadAuthorityError,
    require_scientific_registry_read_authority,
)
from .risk_of_ruin_evaluator import (
    IssuedRiskOfRuinResult,
    ProductRiskOfRuinEvaluator,
    RiskOfRuinIssuanceError,
    RiskTargetKind,
)
from .scientific_registry import RegistryEntry, ScientificRegistry


_AUTHORITY_KIND = "autosport.risk-of-ruin-product-authority.v2"
_BOUND_SEMANTICS = "probability_upper_bound"
_CONFIDENCE_SEMANTICS = "protocol_defined_upper_bound"
_MAX_FIXED_POINT_MATERIALIZATION_LENGTH = 512
_MAX_AUTHORITY_TEXT_LENGTH = 512
_MAX_AUTHORITY_TIMESTAMP_LENGTH = 128
_MAX_SUPPORTED_EVALUATED_STAKES = 10_000
_MAX_SUPPORTED_EFFECTIVE_SAMPLE_SIZE = 10_000
_CANONICAL_AUTHORITY_PATH_CONSTRUCTOR = Path
_CANONICAL_AUTHORITY_PATH_TYPE = type(Path("."))
_CANONICAL_AUTHORITY_PATH_EXPANDUSER = Path.expanduser
_CANONICAL_AUTHORITY_PATH_RESOLVE = Path.resolve
_CANONICAL_AUTHORITY_PATH_IS_FILE = Path.is_file
_CANONICAL_JSON_DUMPS = json.dumps
_CANONICAL_SHA256_HASH = hashlib.sha256
_CANONICAL_DATETIME_TYPE = datetime
_CANONICAL_TIMEZONE_UTC = timezone.utc
_CANONICAL_SCIENTIFIC_REGISTRY_TYPE = ScientificRegistry
_CANONICAL_REGISTRY_ENTRY_TYPE = RegistryEntry
_CANONICAL_REQUIRE_SCIENTIFIC_REGISTRY_READ_AUTHORITY = (
    require_scientific_registry_read_authority
)
_CANONICAL_ISSUED_RISK_RESULT_TYPE = IssuedRiskOfRuinResult
_CANONICAL_RISK_TARGET_SINGLE = RiskTargetKind.SINGLE
_CANONICAL_RISK_TARGET_VECTOR = RiskTargetKind.VECTOR
_CANONICAL_AUTHORITY_KIND = "autosport.risk-of-ruin-product-authority.v2"
_CANONICAL_BOUND_SEMANTICS = "probability_upper_bound"
_CANONICAL_CONFIDENCE_SEMANTICS = "protocol_defined_upper_bound"
_CANONICAL_MAX_FIXED_POINT_MATERIALIZATION_LENGTH = 512
_CANONICAL_MAX_AUTHORITY_TEXT_LENGTH = 512
_CANONICAL_MAX_AUTHORITY_TIMESTAMP_LENGTH = 128
_CANONICAL_MAX_SUPPORTED_EVALUATED_STAKES = 10_000
_CANONICAL_MAX_SUPPORTED_EFFECTIVE_SAMPLE_SIZE = 10_000


def _fixed_point_materialization_length(value: Decimal) -> int:
    """Return fixed-point text size without materializing attacker-scaled text."""

    if value.is_zero():
        return 1
    sign, digits, exponent = value.as_tuple()
    sign_length = 1 if sign else 0
    digit_count = len(digits)
    if exponent >= 0:
        return sign_length + digit_count + exponent
    if digit_count + exponent > 0:
        return sign_length + digit_count + 1
    return sign_length + 2 - exponent


def _canonical_decimal(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("risk-of-ruin authority requires finite Decimal values")
    if value.is_zero():
        return "0"
    if (
        _CANONICAL_AUTHORITY_FIXED_POINT_LENGTH(value)
        > _CANONICAL_MAX_FIXED_POINT_MATERIALIZATION_LENGTH
    ):
        raise ValueError(
            "risk-of-ruin authority Decimal fixed-point representation exceeds "
            "supported canonical size"
        )
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _bounded_text(
    value: object,
    name: str,
    *,
    max_length: int = _MAX_AUTHORITY_TEXT_LENGTH,
) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > max_length
        or "\x00" in value
    ):
        raise ValueError(f"{name} must be bounded canonical text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    return value


def _canonical_sha256(value: object, name: str) -> str:
    value = _CANONICAL_AUTHORITY_BOUNDED_TEXT(value, name, max_length=64)
    if (
        len(value) != 64
        or value != value.lower()
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ValueError(f"{name} must be a canonical SHA-256 digest")
    return value


def _instant(value: str) -> datetime:
    value = _CANONICAL_AUTHORITY_BOUNDED_TEXT(
        value,
        "risk-of-ruin authority timestamp",
        max_length=_CANONICAL_MAX_AUTHORITY_TIMESTAMP_LENGTH,
    )
    parsed = _CANONICAL_DATETIME_TYPE.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("risk-of-ruin authority timestamp must include timezone")
    return parsed.astimezone(_CANONICAL_TIMEZONE_UTC)


def _payload(evidence: object, *, kind: str) -> dict[str, Any]:
    if type(kind) is not str:
        raise ValueError("unsupported risk-of-ruin authority kind")

    # Import at runtime to avoid the module-initialization cycle documented by
    # autosport.risk. Exact type is required before any evidence attribute read.
    from .risk import (
        _CANONICAL_RISK_OF_RUIN_EVIDENCE_TYPE,
        _CANONICAL_RISK_OF_RUIN_EVIDENCE_VALIDATOR,
        _CANONICAL_RISK_OF_RUIN_VECTOR_EVIDENCE_TYPE,
        _CANONICAL_RISK_OF_RUIN_VECTOR_EVIDENCE_VALIDATOR,
    )

    if kind == "single":
        if type(evidence) is not _CANONICAL_RISK_OF_RUIN_EVIDENCE_TYPE:
            raise ValueError(
                "single risk-of-ruin authority requires canonical RiskOfRuinEvidence"
            )
        _CANONICAL_RISK_OF_RUIN_EVIDENCE_VALIDATOR(evidence)
        candidate = {
            "candidate_sha256": _CANONICAL_AUTHORITY_SHA256_TEXT(
                getattr(evidence, "candidate_sha256"), "candidate_sha256"
            ),
            "evaluated_stake": _CANONICAL_AUTHORITY_DECIMAL(getattr(evidence, "evaluated_stake")),
        }
    elif kind == "vector":
        if type(evidence) is not _CANONICAL_RISK_OF_RUIN_VECTOR_EVIDENCE_TYPE:
            raise ValueError(
                "vector risk-of-ruin authority requires canonical RiskOfRuinVectorEvidence"
            )
        _CANONICAL_RISK_OF_RUIN_VECTOR_EVIDENCE_VALIDATOR(evidence)
        stakes = getattr(evidence, "evaluated_stakes")
        if type(stakes) is not tuple:
            raise ValueError("vector risk-of-ruin authority requires tuple stakes")
        if len(stakes) > _CANONICAL_MAX_SUPPORTED_EVALUATED_STAKES:
            raise ValueError(
                "vector risk-of-ruin authority stake vector exceeds supported size"
            )
        candidate = {
            "candidate_vector_sha256": _CANONICAL_AUTHORITY_SHA256_TEXT(
                getattr(evidence, "candidate_vector_sha256"),
                "candidate_vector_sha256",
            ),
            "evaluated_stakes": [_CANONICAL_AUTHORITY_DECIMAL(value) for value in stakes],
        }
    else:
        raise ValueError("unsupported risk-of-ruin authority kind")

    evidence_id = _CANONICAL_AUTHORITY_BOUNDED_TEXT(
        getattr(evidence, "evidence_id"), "risk-of-ruin evidence_id"
    )
    producer_identity = _CANONICAL_AUTHORITY_BOUNDED_TEXT(
        getattr(evidence, "producer_identity"), "risk-of-ruin producer_identity"
    )
    bankroll_id = _CANONICAL_AUTHORITY_BOUNDED_TEXT(
        getattr(evidence, "bankroll_id"), "risk-of-ruin bankroll_id"
    )
    currency = _CANONICAL_AUTHORITY_BOUNDED_TEXT(
        getattr(evidence, "currency"), "risk-of-ruin currency", max_length=3
    )
    if (
        len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or currency != currency.upper()
    ):
        raise ValueError(
            "risk-of-ruin currency must be a three-letter uppercase ASCII code"
        )
    causal_cutoff = _CANONICAL_AUTHORITY_BOUNDED_TEXT(
        getattr(evidence, "causal_cutoff"),
        "risk-of-ruin causal_cutoff",
        max_length=_MAX_AUTHORITY_TIMESTAMP_LENGTH,
    )
    evaluated_at = _CANONICAL_AUTHORITY_BOUNDED_TEXT(
        getattr(evidence, "evaluated_at"),
        "risk-of-ruin evaluated_at",
        max_length=_MAX_AUTHORITY_TIMESTAMP_LENGTH,
    )
    _CANONICAL_AUTHORITY_INSTANT(causal_cutoff)
    _CANONICAL_AUTHORITY_INSTANT(evaluated_at)

    return {
        "schema": _CANONICAL_AUTHORITY_KIND,
        "kind": kind,
        "evidence_id": evidence_id,
        "research_protocol_sha256": _CANONICAL_AUTHORITY_SHA256_TEXT(
            getattr(evidence, "research_protocol_sha256"),
            "research_protocol_sha256",
        ),
        "reproducibility_bundle_sha256": _CANONICAL_AUTHORITY_SHA256_TEXT(
            getattr(evidence, "reproducibility_bundle_sha256"),
            "reproducibility_bundle_sha256",
        ),
        "producer_identity": producer_identity,
        "causal_cutoff": causal_cutoff,
        "evaluated_at": evaluated_at,
        "bankroll_id": bankroll_id,
        "currency": currency,
        "base_portfolio_sha256": _CANONICAL_AUTHORITY_SHA256_TEXT(
            getattr(evidence, "base_portfolio_sha256"),
            "base_portfolio_sha256",
        ),
        "upper_bound": _CANONICAL_AUTHORITY_DECIMAL(getattr(evidence, "upper_bound")),
        "bound_semantics": _CANONICAL_BOUND_SEMANTICS,
        "confidence_semantics": _CANONICAL_CONFIDENCE_SEMANTICS,
        **candidate,
    }


_CANONICAL_AUTHORITY_FIXED_POINT_LENGTH = _fixed_point_materialization_length
_CANONICAL_AUTHORITY_DECIMAL = _canonical_decimal
_CANONICAL_AUTHORITY_BOUNDED_TEXT = _bounded_text
_CANONICAL_AUTHORITY_SHA256_TEXT = _canonical_sha256
_CANONICAL_AUTHORITY_INSTANT = _instant
_CANONICAL_AUTHORITY_PAYLOAD = _payload


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
    """Digest the exact issued result plus canonical evaluation identity."""

    dataset_snapshot_id = _CANONICAL_AUTHORITY_BOUNDED_TEXT(
        dataset_snapshot_id,
        "dataset_snapshot_id",
    )
    if (
        type(effective_sample_size) is not int
        or effective_sample_size <= 0
        or effective_sample_size > _CANONICAL_MAX_SUPPORTED_EFFECTIVE_SAMPLE_SIZE
    ):
        raise ValueError(
            "effective_sample_size must be an exact integer between 1 and "
            f"{_CANONICAL_MAX_SUPPORTED_EFFECTIVE_SAMPLE_SIZE}"
        )
    evaluation_available_at = _CANONICAL_AUTHORITY_INSTANT(evaluation_available_at).isoformat()
    payload = {
        **_CANONICAL_AUTHORITY_PAYLOAD(evidence, kind=kind),
        "evaluation": {
            "evaluator_source_sha256": _CANONICAL_AUTHORITY_SHA256_TEXT(
                evaluator_source_sha256, "evaluator_source_sha256"
            ),
            "dataset_snapshot_id": dataset_snapshot_id,
            "dataset_manifest_sha256": _CANONICAL_AUTHORITY_SHA256_TEXT(
                dataset_manifest_sha256, "dataset_manifest_sha256"
            ),
            "effective_sample_size": effective_sample_size,
            "available_at": evaluation_available_at,
        },
    }
    encoded = _CANONICAL_JSON_DUMPS(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _CANONICAL_SHA256_HASH(encoded).hexdigest()


_CANONICAL_RISK_RESULT_SHA256 = risk_of_ruin_result_sha256
_CANONICAL_REGISTRY_READ_AUTHORITY_ERROR = ScientificRegistryReadAuthorityError


def _read_exact_registry_state(registry: ScientificRegistry) -> dict[str, Any]:
    if type(registry) is not _CANONICAL_SCIENTIFIC_REGISTRY_TYPE:
        raise ValueError("risk-of-ruin registry must be exact ScientificRegistry")
    if set(vars(registry)) != {"path"}:
        raise ValueError("risk-of-ruin registry instance read authority was rebound")
    read_fn, validate_entry, _get_fn, _causal_fn = _CANONICAL_REQUIRE_SCIENTIFIC_REGISTRY_READ_AUTHORITY()
    state = read_fn(registry)
    records = state.get("records")
    if type(records) is not list:
        raise ValueError("risk-of-ruin registry records must be a list")
    for raw in records:
        validate_entry(raw)
    return state


def _entry_from_state(
    state: dict[str, Any], record_type: str, record_id: str
) -> RegistryEntry | None:
    for raw in state["records"]:
        if raw["record_type"] == record_type and raw["record_id"] == record_id:
            return _CANONICAL_REGISTRY_ENTRY_TYPE(**raw)
    return None


_CANONICAL_READ_EXACT_REGISTRY_STATE = _read_exact_registry_state
_CANONICAL_ENTRY_FROM_STATE = _entry_from_state


def _resolve_product_evaluator_result_impl(
    workspace: Path,
    result_id: str,
    *,
    evaluator_class,
    evaluator_init,
    evaluator_resolve,
) -> IssuedRiskOfRuinResult:
    if (
        ProductRiskOfRuinEvaluator is not evaluator_class
        or ProductRiskOfRuinEvaluator.__init__ is not evaluator_init
        or ProductRiskOfRuinEvaluator.resolve is not evaluator_resolve
    ):
        raise RiskOfRuinIssuanceError(
            "risk-of-ruin product evaluator executable authority was rebound"
        )
    evaluator = object.__new__(evaluator_class)
    evaluator_init(evaluator, workspace=workspace)
    result = evaluator_resolve(evaluator, result_id)
    if type(result) is not _CANONICAL_ISSUED_RISK_RESULT_TYPE:
        raise RiskOfRuinIssuanceError(
            "risk-of-ruin product evaluator returned unsupported result type"
        )
    if (
        ProductRiskOfRuinEvaluator is not evaluator_class
        or ProductRiskOfRuinEvaluator.__init__ is not evaluator_init
        or ProductRiskOfRuinEvaluator.resolve is not evaluator_resolve
    ):
        raise RiskOfRuinIssuanceError(
            "risk-of-ruin product evaluator dispatch changed during resolution"
        )
    return result


def _bind_product_evaluator_resolver(
    resolver_impl, evaluator_class, evaluator_init, evaluator_resolve
):
    resolver_code = None
    resolver_impl_code = resolver_impl.__code__
    evaluator_init_code = evaluator_init.__code__
    evaluator_resolve_code = evaluator_resolve.__code__

    def _resolve_product_evaluator_result(
        workspace: Path, result_id: str
    ) -> IssuedRiskOfRuinResult:
        if (
            _resolve_product_evaluator_result.__code__ is not resolver_code
            or resolver_impl.__code__ is not resolver_impl_code
            or evaluator_init.__code__ is not evaluator_init_code
            or evaluator_resolve.__code__ is not evaluator_resolve_code
        ):
            raise RiskOfRuinIssuanceError(
                "risk-of-ruin product evaluator resolver executable changed"
            )
        result = resolver_impl(
            workspace,
            result_id,
            evaluator_class=evaluator_class,
            evaluator_init=evaluator_init,
            evaluator_resolve=evaluator_resolve,
        )
        if (
            _resolve_product_evaluator_result.__code__ is not resolver_code
            or resolver_impl.__code__ is not resolver_impl_code
            or evaluator_init.__code__ is not evaluator_init_code
            or evaluator_resolve.__code__ is not evaluator_resolve_code
        ):
            raise RiskOfRuinIssuanceError(
                "risk-of-ruin product evaluator resolver executable changed during resolution"
            )
        return result

    resolver_code = _resolve_product_evaluator_result.__code__
    return _resolve_product_evaluator_result


_resolve_product_evaluator_result = _bind_product_evaluator_resolver(
    _resolve_product_evaluator_result_impl,
    ProductRiskOfRuinEvaluator,
    ProductRiskOfRuinEvaluator.__init__,
    ProductRiskOfRuinEvaluator.resolve,
)
del _bind_product_evaluator_resolver


def _product_result_matches_evidence(
    result: IssuedRiskOfRuinResult,
    evidence: object,
    *,
    kind: str,
    available_by: str,
    evaluation_available_at: str,
    dataset_snapshot_id: str,
    dataset_manifest_sha256: str,
    evaluator_source_sha256: str,
    effective_sample_size: int,
) -> bool:
    expected_kind = _CANONICAL_RISK_TARGET_SINGLE if kind == "single" else _CANONICAL_RISK_TARGET_VECTOR
    if result.target_kind is not expected_kind:
        return False
    if result.result_id != getattr(evidence, "evidence_id"):
        return False
    if (
        result.research_protocol_sha256 != getattr(evidence, "research_protocol_sha256").lower()
        or result.reproducibility_bundle_sha256
        != getattr(evidence, "reproducibility_bundle_sha256").lower()
        or result.producer_identity != getattr(evidence, "producer_identity")
        or result.bankroll_id != getattr(evidence, "bankroll_id")
        or result.currency != getattr(evidence, "currency")
        or result.base_portfolio_sha256 != getattr(evidence, "base_portfolio_sha256").lower()
        or result.causal_cutoff != _CANONICAL_AUTHORITY_INSTANT(getattr(evidence, "causal_cutoff")).isoformat()
        or result.evaluated_at != _CANONICAL_AUTHORITY_INSTANT(getattr(evidence, "evaluated_at")).isoformat()
        or result.upper_bound != getattr(evidence, "upper_bound")
        or result.dataset_snapshot_id != dataset_snapshot_id
        or result.dataset_manifest_sha256 != dataset_manifest_sha256.lower()
        or result.evaluator_source_sha256 != evaluator_source_sha256.lower()
        or result.independent_units != effective_sample_size
        or _CANONICAL_AUTHORITY_INSTANT(result.issued_at) != _CANONICAL_AUTHORITY_INSTANT(evaluation_available_at)
        or _CANONICAL_AUTHORITY_INSTANT(result.issued_at) > _CANONICAL_AUTHORITY_INSTANT(available_by)
    ):
        return False
    if kind == "single":
        return (
            result.target_sha256 == getattr(evidence, "candidate_sha256").lower()
            and result.evaluated_stakes == (getattr(evidence, "evaluated_stake"),)
        )
    if kind == "vector":
        return (
            result.target_sha256 == getattr(evidence, "candidate_vector_sha256").lower()
            and result.evaluated_stakes == getattr(evidence, "evaluated_stakes")
        )
    return False


def verify_risk_of_ruin_authority(
    registry_path: str | Path | None,
    evidence: object,
    *,
    kind: str,
    available_by: str,
) -> tuple[bool, str]:
    """Validate durable evidence but never mint missing positive product authority.

    The product evaluator bridge is intentionally not an executable capability of
    this public verifier.  Until a separate product-owned issuer is implemented,
    durable scientific rows remain assertion-only and the authority boundary must
    end fail-closed.  Keeping this function closure-free also prevents callers from
    replacing a captured resolver through writable Python closure cells.
    """

    if type(kind) is not str or kind not in {"single", "vector"}:
        return False, "risk-of-ruin authority kind is invalid"
    prefix = "portfolio" if kind == "single" else "portfolio vector"

    if registry_path is not None and type(registry_path) not in {
        str,
        type(Path(".")),
    }:
        return False, f"{prefix} risk-of-ruin registry path is invalid"

    # The public verifier must reject arbitrary evidence objects before any
    # attribute access. Import lazily to avoid the risk.py import cycle during
    # module initialization.
    from .risk import (
        _CANONICAL_RISK_OF_RUIN_EVIDENCE_TYPE,
        _CANONICAL_RISK_OF_RUIN_EVIDENCE_VALIDATOR,
        _CANONICAL_RISK_OF_RUIN_VECTOR_EVIDENCE_TYPE,
        _CANONICAL_RISK_OF_RUIN_VECTOR_EVIDENCE_VALIDATOR,
    )

    evidence_type = (
        _CANONICAL_RISK_OF_RUIN_EVIDENCE_TYPE
        if kind == "single"
        else _CANONICAL_RISK_OF_RUIN_VECTOR_EVIDENCE_TYPE
    )
    evidence_validator = (
        _CANONICAL_RISK_OF_RUIN_EVIDENCE_VALIDATOR
        if kind == "single"
        else _CANONICAL_RISK_OF_RUIN_VECTOR_EVIDENCE_VALIDATOR
    )
    if type(evidence) is not evidence_type:
        return False, f"{prefix} risk-of-ruin evidence type is invalid"
    try:
        evidence_validator(evidence)
    except (TypeError, ValueError):
        return False, f"{prefix} risk-of-ruin evidence is non-canonical"

    if registry_path is None:
        return False, f"{prefix} risk-of-ruin evidence lacks product-issued durable authority"
    try:
        raw_registry_path = _CANONICAL_AUTHORITY_PATH_CONSTRUCTOR(registry_path)
        expanded_registry_path = _CANONICAL_AUTHORITY_PATH_EXPANDUSER(raw_registry_path)
        resolved_registry_path = _CANONICAL_AUTHORITY_PATH_RESOLVE(
            expanded_registry_path,
            strict=True,
        )
        if not _CANONICAL_AUTHORITY_PATH_IS_FILE(resolved_registry_path):
            raise ValueError("risk-of-ruin registry path must be a regular file")
        registry = _CANONICAL_SCIENTIFIC_REGISTRY_TYPE(resolved_registry_path)
        state = _CANONICAL_READ_EXACT_REGISTRY_STATE(registry)
        evidence_id = getattr(evidence, "evidence_id")
        entry = _CANONICAL_ENTRY_FROM_STATE(state, "EvaluationBundle", evidence_id)
        if entry is None:
            return False, f"{prefix} risk-of-ruin evidence is not product-issued"
        bundle = entry.payload
        if bundle.get("created_at") != entry.available_at:
            return False, f"{prefix} risk-of-ruin durable evaluation identity is inconsistent"
        if (
            bundle.get("bundle_sha256") != getattr(evidence, "reproducibility_bundle_sha256").lower()
            or bundle.get("protocol_sha256") != getattr(evidence, "research_protocol_sha256").lower()
        ):
            return False, f"{prefix} risk-of-ruin durable authority lineage does not match"

        issued_at = _CANONICAL_AUTHORITY_INSTANT(entry.available_at)
        evaluated_at = _CANONICAL_AUTHORITY_INSTANT(getattr(evidence, "evaluated_at"))
        proposal_time = _CANONICAL_AUTHORITY_INSTANT(available_by)
        if issued_at < evaluated_at:
            return False, f"{prefix} risk-of-ruin authority predates its claimed evaluation"
        if issued_at > proposal_time:
            return False, f"{prefix} risk-of-ruin authority was not available at proposal time"

        dataset_id = bundle.get("dataset_snapshot_id")
        if type(dataset_id) is not str or not dataset_id:
            return False, f"{prefix} risk-of-ruin authority lacks canonical dataset lineage"
        dataset = _CANONICAL_ENTRY_FROM_STATE(state, "DatasetSnapshot", dataset_id)
        if dataset is None:
            return False, f"{prefix} risk-of-ruin authority references missing dataset"
        dataset_available_at = _CANONICAL_AUTHORITY_INSTANT(dataset.available_at)
        if dataset_available_at > evaluated_at:
            return False, f"{prefix} risk-of-ruin authority uses data unavailable at evaluation time"
        if dataset_available_at > issued_at:
            return False, f"{prefix} risk-of-ruin authority uses a future dataset"
        outcome_reveal_after = dataset.payload.get("outcome_reveal_after")
        if outcome_reveal_after is not None:
            if type(outcome_reveal_after) is not str:
                return False, f"{prefix} risk-of-ruin authority has invalid outcome visibility"
            if _CANONICAL_AUTHORITY_INSTANT(outcome_reveal_after) > evaluated_at:
                return False, f"{prefix} risk-of-ruin outcomes were not causally available at evaluation time"
        dataset_cutoff = dataset.payload.get("causal_cutoff")
        if type(dataset_cutoff) is not str or _CANONICAL_AUTHORITY_INSTANT(dataset_cutoff) > _CANONICAL_AUTHORITY_INSTANT(
            getattr(evidence, "causal_cutoff")
        ):
            return False, f"{prefix} risk-of-ruin authority dataset exceeds causal cutoff"
        manifest_sha256 = dataset.payload.get("manifest_sha256")
        evaluator_source_sha256 = bundle.get("evaluator_source_sha256")
        effective_sample_size = bundle.get("effective_sample_size")
        if (
            type(manifest_sha256) is not str
            or type(evaluator_source_sha256) is not str
            or type(effective_sample_size) is not int
            or effective_sample_size <= 0
        ):
            return False, f"{prefix} risk-of-ruin scientific sufficiency is unknown"

        result_sha256 = _CANONICAL_RISK_RESULT_SHA256(
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
    except (
        AttributeError,
        OSError,
        ScientificRegistryReadAuthorityError,
        TypeError,
        ValueError,
    ):
        return False, f"{prefix} risk-of-ruin durable authority is invalid"

    return False, f"{prefix} risk-of-ruin evidence lacks canonical product-issued evaluator authority"
