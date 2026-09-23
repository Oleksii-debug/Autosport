"""Fail closed when ScientificRegistry read authority is rebound at runtime.

ScientificRegistry publication advances an independent machine-local
MonotonicWorkspaceAuthority.  Trial-family and registry reads additionally verify
that the live class dispatch still points at the product-owned executable source
before any authority-bearing bytes are consumed.
"""

from __future__ import annotations

import json
from types import FunctionType
from typing import Any

from . import integrity as _integrity
from . import resolver_semantics as _resolver_semantics
from . import scientific_registry as _registry


class ScientificRegistryReadAuthorityError(RuntimeError):
    """Raised when live ScientificRegistry read dispatch is not canonical."""


def _source_owned_function(
    candidate: object,
    *,
    module: object,
    qualname: str,
) -> FunctionType:
    """Require one live function to match its canonical module source code.

    This deliberately derives executable identity from the integrated
    resolver_semantics source authority on every check.  No mutable expected
    delegate or cached digest is accepted as a trust root.
    """

    module_name = getattr(module, "__name__", None)
    namespace = getattr(module, "__dict__", None)
    if (
        type(candidate) is not FunctionType
        or type(module_name) is not str
        or type(namespace) is not dict
        or candidate.__module__ != module_name
        or candidate.__qualname__ != qualname
        or candidate.__globals__ is not namespace
    ):
        raise ScientificRegistryReadAuthorityError(
            f"ScientificRegistry executable authority changed: {qualname}"
        )
    try:
        source = _resolver_semantics._module_source(candidate)
        parts = _resolver_semantics._qualname_parts(candidate)
        expected = _resolver_semantics._compiled_resolver_code(source, parts)
        if _resolver_semantics._code_payload(candidate.__code__) != _resolver_semantics._code_payload(expected):
            raise ScientificRegistryReadAuthorityError(
                f"ScientificRegistry executable semantics changed: {qualname}"
            )
    except _resolver_semantics.ResolverSemanticIdentityError as exc:
        raise ScientificRegistryReadAuthorityError(
            f"ScientificRegistry executable source is unverifiable: {qualname}"
        ) from exc
    return candidate


def _raw_class_function(name: str) -> FunctionType:
    raw = vars(_registry.ScientificRegistry).get(name)
    if type(raw) is staticmethod or type(raw) is classmethod:
        candidate = raw.__func__
    else:
        candidate = raw
    if type(candidate) is not FunctionType:
        raise ScientificRegistryReadAuthorityError(
            f"ScientificRegistry class dispatch changed: {name}"
        )
    return candidate


def _require_durable_read_dependencies() -> FunctionType:
    """Verify only the executable dependencies used by the durable ``_read`` hook.

    The hook is installed process-wide, so coupling it to public ``get`` or
    ``causal_precedes`` dispatch would make unrelated ScientificRegistry consumers
    inherit trial-family authority policy.  Trial-family callers still use the
    stronger ``require_scientific_registry_read_authority`` boundary below.
    """

    if vars(_registry.ScientificRegistry).get("SCHEMA_VERSION") != 1:
        raise ScientificRegistryReadAuthorityError(
            "ScientificRegistry schema authority changed"
        )

    validate_fn = _source_owned_function(
        _raw_class_function("_validate_entry"),
        module=_registry,
        qualname="ScientificRegistry._validate_entry",
    )
    _source_owned_function(
        _raw_class_function("__init__"),
        module=_registry,
        qualname="ScientificRegistry.__init__",
    )
    read_text_fn = _source_owned_function(
        _integrity.read_verified_scientific_registry_text,
        module=_integrity,
        qualname="read_verified_scientific_registry_text",
    )
    baseline_fn = _source_owned_function(
        _integrity.establish_validated_scientific_registry_read_baseline,
        module=_integrity,
        qualname="establish_validated_scientific_registry_read_baseline",
    )
    _source_owned_function(
        _registry._reject_duplicate_keys,
        module=_registry,
        qualname="_reject_duplicate_keys",
    )
    _source_owned_function(
        _registry._reject_nonfinite,
        module=_registry,
        qualname="_reject_nonfinite",
    )

    if _integrity.read_verified_scientific_registry_text is not read_text_fn:
        raise ScientificRegistryReadAuthorityError(
            "ScientificRegistry durable reader binding changed"
        )
    if (
        _integrity.establish_validated_scientific_registry_read_baseline
        is not baseline_fn
    ):
        raise ScientificRegistryReadAuthorityError(
            "ScientificRegistry read-baseline binding changed"
        )

    return validate_fn


def require_scientific_registry_read_authority() -> tuple[
    FunctionType,
    FunctionType,
    FunctionType,
    FunctionType,
]:
    """Revalidate the complete trial-family ScientificRegistry read boundary.

    The returned functions are captured only after their currently installed code
    is re-derived from canonical source.  Callers use these verified local
    capabilities instead of looking the same methods up again through mutable
    instance/class dispatch.
    """

    validate_fn = _require_durable_read_dependencies()
    read_fn = _source_owned_function(
        _raw_class_function("_read"),
        module=__import__(__name__, fromlist=["_read_authority_verified"]),
        qualname="_read_authority_verified",
    )
    get_fn = _source_owned_function(
        _raw_class_function("get"),
        module=_registry,
        qualname="ScientificRegistry.get",
    )
    causal_fn = _source_owned_function(
        _raw_class_function("causal_precedes"),
        module=_registry,
        qualname="ScientificRegistry.causal_precedes",
    )

    # Ensure the live trial-family read path itself resolves through the verified
    # durable authority module rather than a same-shaped rebound global.
    if read_fn.__globals__.get("_integrity") is not _integrity:
        raise ScientificRegistryReadAuthorityError(
            "ScientificRegistry durable reader module binding changed"
        )

    return read_fn, validate_fn, get_fn, causal_fn


def _read_authority_verified(self: _registry.ScientificRegistry) -> dict[str, Any]:
    validate_entry = _require_durable_read_dependencies()
    validate_negative_repeat = _source_owned_function(
        _raw_class_function("_validate_negative_repeat_authorization"),
        module=_registry,
        qualname="ScientificRegistry._validate_negative_repeat_authorization",
    )
    validate_promotion = _source_owned_function(
        _raw_class_function("_validate_persisted_promotion_decision"),
        module=_registry,
        qualname="ScientificRegistry._validate_persisted_promotion_decision",
    )
    validate_ablation = _source_owned_function(
        _raw_class_function("_validate_ablation_authority_causal_inputs"),
        module=_registry,
        qualname="ScientificRegistry._validate_ablation_authority_causal_inputs",
    )
    raw = _integrity.read_verified_scientific_registry_text(self.path)
    try:
        state = json.loads(
            raw,
            object_pairs_hook=_registry._reject_duplicate_keys,
            parse_constant=_registry._reject_nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("scientific registry must be valid UTF-8 JSON") from exc
    if (
        type(state) is not dict
        or type(state.get("schema_version")) is not int
        or state["schema_version"] != 1
    ):
        raise ValueError("scientific registry schema_version mismatch")
    records = state.get("records")
    if type(records) is not list:
        raise ValueError("scientific registry records must be a list")
    seen: set[tuple[str, str]] = set()
    for index, raw_entry in enumerate(records):
        validate_entry(raw_entry)
        key = (raw_entry["record_type"], raw_entry["record_id"])
        if key in seen:
            raise ValueError("scientific registry contains duplicate record identity")
        seen.add(key)
        if raw_entry["record_type"] == "Experiment":
            fingerprint = raw_entry["payload"].get("fingerprint")
            prior_state = {
                "schema_version": _registry.ScientificRegistry.SCHEMA_VERSION,
                "records": records[:index],
            }
            matching_experiments = [
                existing
                for existing in records[:index]
                if existing["record_type"] == "Experiment"
                and existing["payload"].get("fingerprint") == fingerprint
            ]
            negative_history = [
                existing
                for existing in matching_experiments
                if existing["payload"].get("outcome")
                != _registry.ResearchOutcome.POSITIVE.value
            ]
            has_repeat_provenance = (
                raw_entry["payload"].get("repeat_of_experiment_id") is not None
            )
            if negative_history:
                if not has_repeat_provenance:
                    raise _registry.DuplicateExperimentFingerprintError(
                        "persisted negative-result repeat lacks durable repeat provenance"
                    )
                validate_negative_repeat(
                    prior_state,
                    raw_entry,
                    negative_history,
                )
            elif has_repeat_provenance:
                raise _registry.DuplicateExperimentFingerprintError(
                    "repeat provenance requires prior non-positive experiment history"
                )

    for raw_entry in records:
        if raw_entry["record_type"] == "PromotionDecision":
            validate_promotion(records, raw_entry)
        if raw_entry["record_type"] == "AblationAuthorityEvidence":
            validate_ablation(records, raw_entry)

    # Only fully validated non-pristine bytes may establish a missing machine
    # authority baseline. The helper rechecks the exact bytes under the durable
    # path lock before PREPARE/COMMIT, closing the first-read legacy TOCTOU gap.
    _integrity.establish_validated_scientific_registry_read_baseline(self.path, raw)
    return state


_registry.ScientificRegistry._read = _read_authority_verified
