"""Product-owned issuance for external-validity ``PolicyEvaluation`` results.

The external-validity comparison DTO is intentionally public and therefore cannot
be an issuance authority by itself.  This module projects one result only from an
already-durable, canonical policy-factory evaluation.  The projection is written
into the existing ``FactoryArtifactStore`` and registered in the existing
``ScientificRegistry`` under a deterministic identity before downstream code may
consume it.

This module owns no promotion, ranking, provider execution, money, or readiness
authority.  The first supported projection is the predictive external-validity
contract backed by the canonical paired policy evaluator.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Mapping, Sequence

from .external_validity_baseline import (
    BaselineKind,
    EvaluationContractFamily,
    ExternalValidityError,
    FrozenBaselineProtocol,
    PolicyEvaluation,
)
from .monotonic_workspace_authority import resolve_monotonic_authority_root
from .monotonic_workspace_binding import (
    WorkspaceBindingConflictError,
    WorkspaceBindingIntegrityError,
    WorkspaceIdentityBinding,
)
from .scientific_registry import EvaluationBundleRef, ScientificRegistry
from .strategy_model_factory import FactoryArtifactStore


class ProductPolicyEvaluationIssuanceError(ExternalValidityError):
    """Raised when product-issued external-validity provenance is not exact."""


@dataclass(frozen=True, slots=True)
class ProductPolicyEvaluationWorkspace:
    """Already-bound product workspace used as the evaluator issuance trust root.

    The issuer never accepts caller-selected registry/store instances.  The expected
    workspace instance identity must come from product/runtime configuration outside
    an issued result and must already be present in both the workspace marker and
    independent machine path binding.
    """

    workspace: Path
    workspace_instance_id: str
    workspace_locator_sha256: str

    @classmethod
    def open(
        cls,
        workspace: str | Path,
        *,
        expected_workspace_instance_id: str,
    ) -> "ProductPolicyEvaluationWorkspace":
        try:
            root = Path(workspace).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ProductPolicyEvaluationIssuanceError(
                "product evaluator workspace cannot be resolved"
            ) from exc
        expected = _text(
            expected_workspace_instance_id,
            "expected_workspace_instance_id",
        )
        try:
            binding, workspace_bound, path_bound = _resolve_existing_workspace_binding(
                root,
                expected,
            )
        except (WorkspaceBindingConflictError, WorkspaceBindingIntegrityError) as exc:
            raise ProductPolicyEvaluationIssuanceError(
                "product evaluator workspace identity is not authoritative"
            ) from exc
        if not workspace_bound or not path_bound:
            raise ProductPolicyEvaluationIssuanceError(
                "product evaluator workspace must be durably bound before issuance"
            )
        return cls(
            root,
            binding.workspace_instance_id,
            binding.workspace_locator_sha256,
        )


_ISSUER_SOURCE_SHA256 = "aa95112ec260878167b36071f2682f196cb27f8731c677e14d1d7659aac28c90"
_RESULT_ARTIFACT_KIND = "external-validity-policy-result"
_BUNDLE_ID_PREFIX = "external-validity-policy-result:"
_BOOTSTRAP_REPLICATES = 1024
_CANONICAL_REGISTRY_FILENAME = "scientific_registry.json"
_CANONICAL_ARTIFACT_DIRECTORY = "factory-artifacts"

# Capture the concrete authority surface at import.  Positive reads/writes never
# dispatch through caller-installed instance or class shadows.
_REGISTRY_TYPE = ScientificRegistry
_STORE_TYPE = FactoryArtifactStore
_REGISTRY_GET = _REGISTRY_TYPE.get
_REGISTRY_APPEND = _REGISTRY_TYPE.append
_REGISTRY_READ = _REGISTRY_TYPE._read
_REGISTRY_VALIDATE_ENTRY = _REGISTRY_TYPE._validate_entry
_REGISTRY_ENTRY = _REGISTRY_TYPE._entry
_REGISTRY_PRIVATE_APPEND = _REGISTRY_TYPE._append
_REGISTRY_APPEND_ENTRY_LOCKED = _REGISTRY_TYPE._append_entry_locked
_STORE_READ = _STORE_TYPE.read
_STORE_SHA256 = _STORE_TYPE.sha256
_STORE_MATERIALIZE = _STORE_TYPE.materialize
_STORE_RECEIPT = _STORE_TYPE.materialization_receipt
_STORE_STABLE_SNAPSHOT = _STORE_TYPE._stable_snapshot
_STORE_DECODE_SNAPSHOT = _STORE_TYPE._decode_snapshot
_STORE_PATH = _STORE_TYPE._path
_STORE_FILENAME = _STORE_TYPE._filename
_STORE_WRITE = _STORE_TYPE.write
_STORE_RECORD_MATERIALIZATION = _STORE_TYPE.record_materialization
_STORE_READ_MATERIALIZATION_LEDGER = _STORE_TYPE._read_materialization_ledger
_STORE_MATERIALIZATION_DIGEST = _STORE_TYPE._materialization_digest
_STORE_MATERIALIZATION_LEDGER_PATH = _STORE_TYPE._materialization_ledger_path
_STORE_BASE_TYPE = _STORE_TYPE.__mro__[1]
_STORE_BASE_WRITE = _STORE_BASE_TYPE.write

# The durable product-workspace identity is itself an authority boundary.  Freeze
# the exact resolver/binding dispatch used to prove that the configured workspace
# is the already-bound canonical product root before opening registry/store state.
_WORKSPACE_ROOT_RESOLVER = resolve_monotonic_authority_root
_WORKSPACE_ROOT_RESOLVER_CODE = resolve_monotonic_authority_root.__code__
_WORKSPACE_ROOT_RESOLVER_GLOBALS = resolve_monotonic_authority_root.__globals__
_WORKSPACE_ROOT_ABSOLUTE_PATH = _WORKSPACE_ROOT_RESOLVER_GLOBALS.get("_absolute_path")
_WORKSPACE_ROOT_DEFAULT = _WORKSPACE_ROOT_RESOLVER_GLOBALS.get(
    "default_monotonic_authority_root"
)
_WORKSPACE_ROOT_ABSOLUTE_PATH_CODE = getattr(
    _WORKSPACE_ROOT_ABSOLUTE_PATH, "__code__", None
)
_WORKSPACE_ROOT_DEFAULT_CODE = getattr(_WORKSPACE_ROOT_DEFAULT, "__code__", None)

_WORKSPACE_BINDING_TYPE = WorkspaceIdentityBinding
_WORKSPACE_BINDING_RESOLVE = WorkspaceIdentityBinding.resolve.__func__
_WORKSPACE_BINDING_RESOLVE_CODE = WorkspaceIdentityBinding.resolve.__func__.__code__
_WORKSPACE_BINDING_VALIDATE = WorkspaceIdentityBinding.validate_existing
_WORKSPACE_BINDING_VALIDATE_CODE = WorkspaceIdentityBinding.validate_existing.__code__
_WORKSPACE_BINDING_READ_MARKER = WorkspaceIdentityBinding._read_workspace_marker_id
_WORKSPACE_BINDING_READ_MARKER_CODE = (
    WorkspaceIdentityBinding._read_workspace_marker_id.__code__
)
_WORKSPACE_BINDING_READ_PATH = WorkspaceIdentityBinding._read_path_binding_id
_WORKSPACE_BINDING_READ_PATH_CODE = WorkspaceIdentityBinding._read_path_binding_id.__code__


def _require_workspace_binding_dispatch_authority() -> None:
    if (
        resolve_monotonic_authority_root is not _WORKSPACE_ROOT_RESOLVER
        or getattr(resolve_monotonic_authority_root, "__code__", None)
        is not _WORKSPACE_ROOT_RESOLVER_CODE
        or _WORKSPACE_ROOT_RESOLVER.__globals__ is not _WORKSPACE_ROOT_RESOLVER_GLOBALS
        or _WORKSPACE_ROOT_RESOLVER_GLOBALS.get("_absolute_path")
        is not _WORKSPACE_ROOT_ABSOLUTE_PATH
        or getattr(_WORKSPACE_ROOT_ABSOLUTE_PATH, "__code__", None)
        is not _WORKSPACE_ROOT_ABSOLUTE_PATH_CODE
        or _WORKSPACE_ROOT_RESOLVER_GLOBALS.get("default_monotonic_authority_root")
        is not _WORKSPACE_ROOT_DEFAULT
        or getattr(_WORKSPACE_ROOT_DEFAULT, "__code__", None)
        is not _WORKSPACE_ROOT_DEFAULT_CODE
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "product workspace authority-root resolver was rebound"
        )

    if WorkspaceIdentityBinding is not _WORKSPACE_BINDING_TYPE:
        raise ProductPolicyEvaluationIssuanceError(
            "product workspace identity binding authority was rebound"
        )
    bound_resolve = getattr(_WORKSPACE_BINDING_TYPE, "resolve", None)
    if (
        getattr(bound_resolve, "__self__", None) is not _WORKSPACE_BINDING_TYPE
        or getattr(bound_resolve, "__func__", None) is not _WORKSPACE_BINDING_RESOLVE
        or getattr(_WORKSPACE_BINDING_RESOLVE, "__code__", None)
        is not _WORKSPACE_BINDING_RESOLVE_CODE
        or _WORKSPACE_BINDING_TYPE.validate_existing
        is not _WORKSPACE_BINDING_VALIDATE
        or getattr(_WORKSPACE_BINDING_VALIDATE, "__code__", None)
        is not _WORKSPACE_BINDING_VALIDATE_CODE
        or _WORKSPACE_BINDING_TYPE._read_workspace_marker_id
        is not _WORKSPACE_BINDING_READ_MARKER
        or getattr(_WORKSPACE_BINDING_READ_MARKER, "__code__", None)
        is not _WORKSPACE_BINDING_READ_MARKER_CODE
        or _WORKSPACE_BINDING_TYPE._read_path_binding_id
        is not _WORKSPACE_BINDING_READ_PATH
        or getattr(_WORKSPACE_BINDING_READ_PATH, "__code__", None)
        is not _WORKSPACE_BINDING_READ_PATH_CODE
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "product workspace identity binding executable authority was rebound"
        )


def _resolve_existing_workspace_binding(
    root: Path,
    expected_workspace_instance_id: str,
) -> tuple[WorkspaceIdentityBinding, bool, bool]:
    _require_workspace_binding_dispatch_authority()
    authority_root = _WORKSPACE_ROOT_RESOLVER(root)
    _require_workspace_binding_dispatch_authority()
    binding = _WORKSPACE_BINDING_RESOLVE(
        _WORKSPACE_BINDING_TYPE,
        workspace=root,
        authority_root=authority_root,
        requested_workspace_instance_id=expected_workspace_instance_id,
    )
    _require_workspace_binding_dispatch_authority()
    if type(binding) is not _WORKSPACE_BINDING_TYPE:
        raise ProductPolicyEvaluationIssuanceError(
            "product workspace identity resolver returned non-canonical authority"
        )
    workspace_bound, path_bound = _WORKSPACE_BINDING_VALIDATE(
        binding,
        register_moved_or_copied_path=False,
    )
    _require_workspace_binding_dispatch_authority()
    if type(workspace_bound) is not bool or type(path_bound) is not bool:
        raise ProductPolicyEvaluationIssuanceError(
            "product workspace identity validation returned non-canonical truth"
        )
    return binding, workspace_bound, path_bound


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ProductPolicyEvaluationIssuanceError(
            f"{field} must be a non-empty canonical string"
        )
    value.encode("utf-8")
    return value


def _sha256(value: object, field: str) -> str:
    text = _text(value, field).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ProductPolicyEvaluationIssuanceError(
            f"{field} must be canonical SHA-256 hex"
        )
    return text


def _instant(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductPolicyEvaluationIssuanceError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductPolicyEvaluationIssuanceError(
            f"{field} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, field: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ProductPolicyEvaluationIssuanceError(f"{field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProductPolicyEvaluationIssuanceError(
            f"{field} must be a finite decimal"
        ) from exc
    if not parsed.is_finite():
        raise ProductPolicyEvaluationIssuanceError(f"{field} must be finite")
    if nonnegative and parsed < 0:
        raise ProductPolicyEvaluationIssuanceError(f"{field} must be non-negative")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ProductPolicyEvaluationIssuanceError("decimal must be finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _digest(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductPolicyEvaluationIssuanceError(
            "policy-evaluation provenance is not canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _require_registry(registry: object) -> ScientificRegistry:
    if type(registry) is not _REGISTRY_TYPE:
        raise ProductPolicyEvaluationIssuanceError(
            "registry must be the exact ScientificRegistry authority"
        )
    if set(vars(registry)) != {"path"}:
        raise ProductPolicyEvaluationIssuanceError(
            "ScientificRegistry instance authority was rebound"
        )
    if (
        _REGISTRY_TYPE.get is not _REGISTRY_GET
        or _REGISTRY_TYPE.append is not _REGISTRY_APPEND
        or _REGISTRY_TYPE._read is not _REGISTRY_READ
        or _REGISTRY_TYPE._validate_entry is not _REGISTRY_VALIDATE_ENTRY
        or _REGISTRY_TYPE._entry is not _REGISTRY_ENTRY
        or _REGISTRY_TYPE._append is not _REGISTRY_PRIVATE_APPEND
        or _REGISTRY_TYPE._append_entry_locked is not _REGISTRY_APPEND_ENTRY_LOCKED
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "ScientificRegistry executable authority was rebound"
        )
    return registry


def _require_store(store: object) -> FactoryArtifactStore:
    if type(store) is not _STORE_TYPE:
        raise ProductPolicyEvaluationIssuanceError(
            "artifact_store must be the exact FactoryArtifactStore authority"
        )
    if set(vars(store)) != {"root", "_clock"}:
        raise ProductPolicyEvaluationIssuanceError(
            "FactoryArtifactStore instance authority was rebound"
        )
    if (
        _STORE_TYPE.read is not _STORE_READ
        or _STORE_TYPE.sha256 is not _STORE_SHA256
        or _STORE_TYPE.materialize is not _STORE_MATERIALIZE
        or _STORE_TYPE.materialization_receipt is not _STORE_RECEIPT
        or _STORE_TYPE._stable_snapshot is not _STORE_STABLE_SNAPSHOT
        or _STORE_TYPE._decode_snapshot is not _STORE_DECODE_SNAPSHOT
        or _STORE_TYPE._path is not _STORE_PATH
        or _STORE_TYPE._filename is not _STORE_FILENAME
        or _STORE_TYPE.write is not _STORE_WRITE
        or _STORE_TYPE.record_materialization is not _STORE_RECORD_MATERIALIZATION
        or _STORE_TYPE._read_materialization_ledger
        is not _STORE_READ_MATERIALIZATION_LEDGER
        or _STORE_TYPE._materialization_digest is not _STORE_MATERIALIZATION_DIGEST
        or _STORE_TYPE._materialization_ledger_path
        is not _STORE_MATERIALIZATION_LEDGER_PATH
        or _STORE_TYPE.__mro__[1] is not _STORE_BASE_TYPE
        or _STORE_BASE_TYPE.write is not _STORE_BASE_WRITE
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "FactoryArtifactStore executable authority was rebound"
        )
    return store


def _open_canonical_authorities(
    authority: ProductPolicyEvaluationWorkspace,
) -> tuple[ScientificRegistry, FactoryArtifactStore]:
    if type(authority) is not ProductPolicyEvaluationWorkspace:
        raise ProductPolicyEvaluationIssuanceError(
            "authority must be an exact ProductPolicyEvaluationWorkspace"
        )
    if ScientificRegistry is not _REGISTRY_TYPE:
        raise ProductPolicyEvaluationIssuanceError(
            "ScientificRegistry constructor authority was rebound"
        )
    if FactoryArtifactStore is not _STORE_TYPE:
        raise ProductPolicyEvaluationIssuanceError(
            "FactoryArtifactStore constructor authority was rebound"
        )
    try:
        root = authority.workspace.expanduser().resolve(strict=False)
        binding, workspace_bound, path_bound = _resolve_existing_workspace_binding(
            root,
            authority.workspace_instance_id,
        )
    except (OSError, RuntimeError, WorkspaceBindingConflictError, WorkspaceBindingIntegrityError) as exc:
        raise ProductPolicyEvaluationIssuanceError(
            "product evaluator workspace identity cannot be re-verified"
        ) from exc
    if (
        root != authority.workspace
        or binding.workspace_instance_id != authority.workspace_instance_id
        or binding.workspace_locator_sha256 != authority.workspace_locator_sha256
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "product evaluator workspace authority was rebound"
        )
    if not workspace_bound or not path_bound:
        raise ProductPolicyEvaluationIssuanceError(
            "product evaluator workspace binding is incomplete"
        )

    registry_path = root / _CANONICAL_REGISTRY_FILENAME
    artifact_root = root / _CANONICAL_ARTIFACT_DIRECTORY
    if not registry_path.is_file():
        raise ProductPolicyEvaluationIssuanceError(
            "canonical product ScientificRegistry is missing"
        )
    if not artifact_root.is_dir():
        raise ProductPolicyEvaluationIssuanceError(
            "canonical product FactoryArtifactStore is missing"
        )
    try:
        registry = _REGISTRY_TYPE(registry_path)
        store = _STORE_TYPE(artifact_root)
    except (OSError, ValueError) as exc:
        raise ProductPolicyEvaluationIssuanceError(
            "canonical product evaluator authorities cannot be reopened"
        ) from exc
    registry = _require_registry(registry)
    store = _require_store(store)
    if registry.path != registry_path:
        raise ProductPolicyEvaluationIssuanceError(
            "canonical ScientificRegistry path was redirected"
        )
    if store.root != artifact_root:
        raise ProductPolicyEvaluationIssuanceError(
            "canonical FactoryArtifactStore root was redirected"
        )
    return registry, store


def _registry_get(registry: ScientificRegistry, record_type: str, record_id: str):
    return _REGISTRY_GET(_require_registry(registry), record_type, record_id)


def _store_read(
    store: FactoryArtifactStore,
    kind: str,
    identity: str,
    *,
    expected_sha256: str,
) -> dict[str, object]:
    return _STORE_READ(
        _require_store(store),
        kind,
        identity,
        expected_sha256=expected_sha256,
    )


def canonical_product_policy_evaluation_bundle_sha256(
    evaluation: PolicyEvaluation,
) -> str:
    """Commit to every externally consumed scientific value except the digest itself.

    This is intentionally byte-for-byte compatible with the external-validity
    registry adapter's canonical evaluation-bundle commitment.  A public caller
    can recompute this digest, but cannot thereby occupy the deterministic
    product-issued bundle identity or manufacture its durable source lineage.
    """

    if type(evaluation) is not PolicyEvaluation:
        raise ProductPolicyEvaluationIssuanceError(
            "evaluation must be an exact PolicyEvaluation value"
        )
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": "autosport_external_validity_policy_evaluation_bundle",
        "policy_id": evaluation.policy_id,
        "policy_artifact_sha256": evaluation.policy_artifact_sha256,
        "protocol_sha256": evaluation.protocol_sha256,
        "evidence_scope_sha256": evaluation.evidence_scope_sha256,
        "cohort_sha256": evaluation.cohort_sha256,
        "primary_metric": evaluation.primary_metric,
        "evaluated_at": evaluation.evaluated_at,
        "metric_value": evaluation.metric_value,
        "uncertainty_low": evaluation.uncertainty_low,
        "uncertainty_high": evaluation.uncertainty_high,
        "observed_count": evaluation.observed_count,
        "scored_count": evaluation.scored_count,
        "abstention_count": evaluation.abstention_count,
        "total_cost": evaluation.total_cost,
        "baseline_definition_sha256": evaluation.baseline_definition_sha256,
    }
    return _digest(payload)


@dataclass(frozen=True, slots=True)
class IssuedPolicyEvaluationRef:
    """Stable reference to one deterministic product-issued external result."""

    issuance_id: str
    workspace_instance_id: str
    workspace_locator_sha256: str
    evaluation_bundle_id: str
    evaluation_bundle_sha256: str
    result_artifact_sha256: str
    source_evaluation_bundle_id: str
    source_evaluation_bundle_sha256: str
    policy_id: str
    baseline_kind: BaselineKind | None = None

    def __post_init__(self) -> None:
        _sha256(self.issuance_id, "issuance_id")
        _text(self.workspace_instance_id, "workspace_instance_id")
        _sha256(self.workspace_locator_sha256, "workspace_locator_sha256")
        _text(self.evaluation_bundle_id, "evaluation_bundle_id")
        _sha256(self.evaluation_bundle_sha256, "evaluation_bundle_sha256")
        _sha256(self.result_artifact_sha256, "result_artifact_sha256")
        _text(self.source_evaluation_bundle_id, "source_evaluation_bundle_id")
        _sha256(
            self.source_evaluation_bundle_sha256,
            "source_evaluation_bundle_sha256",
        )
        _text(self.policy_id, "policy_id")
        if self.baseline_kind is not None and not isinstance(
            self.baseline_kind, BaselineKind
        ):
            raise ProductPolicyEvaluationIssuanceError(
                "baseline_kind must be BaselineKind or None"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 3,
            "kind": "autosport-issued-policy-evaluation-ref-v3",
            "issuance_id": self.issuance_id,
            "workspace_instance_id": self.workspace_instance_id,
            "workspace_locator_sha256": self.workspace_locator_sha256,
            "evaluation_bundle_id": self.evaluation_bundle_id,
            "evaluation_bundle_sha256": self.evaluation_bundle_sha256,
            "result_artifact_sha256": self.result_artifact_sha256,
            "source_evaluation_bundle_id": self.source_evaluation_bundle_id,
            "source_evaluation_bundle_sha256": self.source_evaluation_bundle_sha256,
            "policy_id": self.policy_id,
            "baseline_kind": (
                None if self.baseline_kind is None else self.baseline_kind.value
            ),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "IssuedPolicyEvaluationRef":
        expected = {
            "schema_version",
            "kind",
            "issuance_id",
            "workspace_instance_id",
            "workspace_locator_sha256",
            "evaluation_bundle_id",
            "evaluation_bundle_sha256",
            "result_artifact_sha256",
            "source_evaluation_bundle_id",
            "source_evaluation_bundle_sha256",
            "policy_id",
            "baseline_kind",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise ProductPolicyEvaluationIssuanceError(
                "issued PolicyEvaluation reference fields mismatch"
            )
        if payload.get("schema_version") != 3 or payload.get("kind") != (
            "autosport-issued-policy-evaluation-ref-v3"
        ):
            raise ProductPolicyEvaluationIssuanceError(
                "issued PolicyEvaluation reference schema mismatch"
            )
        raw_kind = payload.get("baseline_kind")
        try:
            baseline_kind = None if raw_kind is None else BaselineKind(raw_kind)
        except (TypeError, ValueError) as exc:
            raise ProductPolicyEvaluationIssuanceError(
                "issued PolicyEvaluation baseline_kind is invalid"
            ) from exc
        return cls(
            issuance_id=payload.get("issuance_id"),
            workspace_instance_id=payload.get("workspace_instance_id"),
            workspace_locator_sha256=payload.get("workspace_locator_sha256"),
            evaluation_bundle_id=payload.get("evaluation_bundle_id"),
            evaluation_bundle_sha256=payload.get("evaluation_bundle_sha256"),
            result_artifact_sha256=payload.get("result_artifact_sha256"),
            source_evaluation_bundle_id=payload.get("source_evaluation_bundle_id"),
            source_evaluation_bundle_sha256=payload.get(
                "source_evaluation_bundle_sha256"
            ),
            policy_id=payload.get("policy_id"),
            baseline_kind=baseline_kind,
        )


@dataclass(frozen=True, slots=True)
class _Target:
    policy_id: str
    policy_artifact_sha256: str
    baseline_definition_sha256: str | None
    baseline_kind: BaselineKind | None


@dataclass(frozen=True, slots=True)
class _SourceEvaluation:
    evaluation_bundle_id: str
    evaluation_bundle_sha256: str
    evaluation_bundle_record_sha256: str
    dataset_snapshot_id: str
    strategy_version_id: str
    model_version_id: str
    experiment_id: str
    completed_at: str
    policy_artifact_sha256: str
    model_artifact_sha256: str
    metrics_artifact_sha256: str
    policy_evaluation_sha256: str
    policy_evaluation: dict[str, object]
    evaluator_config: dict[str, object]


def _target(protocol: FrozenBaselineProtocol, baseline_kind: BaselineKind | None) -> _Target:
    if type(protocol) is not FrozenBaselineProtocol:
        raise ProductPolicyEvaluationIssuanceError(
            "protocol must be an exact FrozenBaselineProtocol value"
        )
    if baseline_kind is None:
        return _Target(
            policy_id=protocol.candidate_id,
            policy_artifact_sha256=protocol.candidate_artifact_sha256,
            baseline_definition_sha256=None,
            baseline_kind=None,
        )
    if not isinstance(baseline_kind, BaselineKind):
        raise ProductPolicyEvaluationIssuanceError(
            "baseline_kind must be BaselineKind or None"
        )
    definition = next(
        (item for item in protocol.baselines if item.kind is baseline_kind), None
    )
    if definition is None:
        raise ProductPolicyEvaluationIssuanceError(
            "baseline kind is absent from frozen protocol"
        )
    if definition.supported is not True:
        raise ProductPolicyEvaluationIssuanceError(
            "unsupported frozen baseline cannot receive product-issued result"
        )
    return _Target(
        policy_id=definition.baseline_id,
        policy_artifact_sha256=definition.implementation_sha256,
        baseline_definition_sha256=definition.definition_sha256,
        baseline_kind=baseline_kind,
    )


def _issuance_id(
    authority: ProductPolicyEvaluationWorkspace,
    protocol: FrozenBaselineProtocol,
    target: _Target,
) -> str:
    return _digest(
        {
            "schema_version": 3,
            "kind": "autosport-external-validity-product-issuance-identity-v3",
            "workspace_instance_id": authority.workspace_instance_id,
            "workspace_locator_sha256": authority.workspace_locator_sha256,
            "protocol_sha256": protocol.identity_sha256,
            "evidence_scope_sha256": protocol.evidence_scope.identity_sha256,
            "cohort_sha256": protocol.evidence_scope.cohort_sha256,
            "policy_id": target.policy_id,
            "policy_artifact_sha256": target.policy_artifact_sha256,
            "baseline_definition_sha256": target.baseline_definition_sha256,
        }
    )


def _require_source_factory_evaluation(
    registry: ScientificRegistry,
    store: FactoryArtifactStore,
    protocol: FrozenBaselineProtocol,
    target: _Target,
    source_evaluation_bundle_id: str,
) -> _SourceEvaluation:
    bundle_id = _text(source_evaluation_bundle_id, "source_evaluation_bundle_id")
    bundle = _registry_get(registry, "EvaluationBundle", bundle_id)
    if bundle is None:
        raise ProductPolicyEvaluationIssuanceError(
            "source product EvaluationBundle is missing"
        )
    bundle_payload = bundle.payload
    source_bundle_sha256 = _sha256(
        bundle_payload.get("bundle_sha256"), "source EvaluationBundle.bundle_sha256"
    )
    evaluation_payload = _store_read(
        store,
        "evaluation",
        bundle_id,
        expected_sha256=source_bundle_sha256,
    )
    required_evaluation_fields = {
        "schema_version",
        "kind",
        "evaluation_bundle_id",
        "experiment_id",
        "research_protocol_id",
        "protocol_sha256",
        "promotion_rule_sha256",
        "dataset_snapshot_id",
        "feature_set_id",
        "model_version_id",
        "strategy_version_id",
        "predecessor_strategy_version_id",
        "predecessor_policy_id",
        "challenger_policy_id",
        "evaluator_source_sha256",
        "evaluator_config",
        "evaluator_config_sha256",
        "dataset_manifest_sha256",
        "policy_evaluation",
        "policy_evaluation_sha256",
        "candidate_metrics",
        "predecessor_metrics",
        "candidate_metrics_artifact_sha256",
        "candidate_metrics_source",
        "effective_sample_size_exact",
        "promotion_verdict",
        "completed_at",
        "decided_at",
        "truth",
    }
    if (
        type(evaluation_payload) is not dict
        or set(evaluation_payload) != required_evaluation_fields
        or evaluation_payload.get("schema_version") != 1
        or evaluation_payload.get("kind")
        != "autosport-policy-specific-factory-evaluation-v1"
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "source evaluation is not the canonical policy-factory result"
        )
    if evaluation_payload.get("evaluation_bundle_id") != bundle_id:
        raise ProductPolicyEvaluationIssuanceError(
            "source evaluation bundle identity mismatch"
        )

    dataset_snapshot_id = _text(
        evaluation_payload.get("dataset_snapshot_id"), "source dataset_snapshot_id"
    )
    strategy_version_id = _text(
        evaluation_payload.get("strategy_version_id"), "source strategy_version_id"
    )
    model_version_id = _text(
        evaluation_payload.get("model_version_id"), "source model_version_id"
    )
    experiment_id = _text(
        evaluation_payload.get("experiment_id"), "source experiment_id"
    )
    completed_at = _text(
        evaluation_payload.get("completed_at"), "source completed_at"
    )
    completed = _instant(completed_at, "source completed_at")

    if bundle_payload.get("dataset_snapshot_id") != dataset_snapshot_id:
        raise ProductPolicyEvaluationIssuanceError(
            "source bundle/evaluation dataset identity mismatch"
        )
    if bundle_payload.get("evaluated_strategy_version_id") != strategy_version_id:
        raise ProductPolicyEvaluationIssuanceError(
            "source bundle/evaluation strategy identity mismatch"
        )
    if bundle_payload.get("evaluated_model_version_id") != model_version_id:
        raise ProductPolicyEvaluationIssuanceError(
            "source bundle/evaluation model identity mismatch"
        )
    if _sha256(
        bundle_payload.get("protocol_sha256"), "source bundle protocol_sha256"
    ) != _sha256(
        evaluation_payload.get("protocol_sha256"), "source evaluation protocol_sha256"
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "source bundle/evaluation research protocol mismatch"
        )
    if _sha256(
        bundle_payload.get("evaluator_source_sha256"),
        "source bundle evaluator_source_sha256",
    ) != _sha256(
        evaluation_payload.get("evaluator_source_sha256"),
        "source evaluation evaluator_source_sha256",
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "source bundle/evaluation evaluator identity mismatch"
        )
    if _instant(bundle_payload.get("created_at"), "source bundle created_at") != completed:
        raise ProductPolicyEvaluationIssuanceError(
            "source bundle was not issued at factory evaluation completion"
        )

    research_protocol_id = _text(
        evaluation_payload.get("research_protocol_id"), "source research_protocol_id"
    )
    research_protocol = _registry_get(
        registry, "ResearchProtocol", research_protocol_id
    )
    if research_protocol is None:
        raise ProductPolicyEvaluationIssuanceError(
            "source factory ResearchProtocol is missing"
        )
    if _sha256(
        research_protocol.payload.get("protocol_sha256"),
        "source ResearchProtocol.protocol_sha256",
    ) != _sha256(
        evaluation_payload.get("protocol_sha256"), "source evaluation protocol_sha256"
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "source factory research protocol commitment mismatch"
        )

    dataset = _registry_get(registry, "DatasetSnapshot", dataset_snapshot_id)
    if dataset is None:
        raise ProductPolicyEvaluationIssuanceError(
            "source factory DatasetSnapshot is missing"
        )
    if _sha256(
        dataset.payload.get("manifest_sha256"), "source DatasetSnapshot.manifest_sha256"
    ) != protocol.evidence_scope.dataset_sha256:
        raise ProductPolicyEvaluationIssuanceError(
            "source factory dataset manifest does not match frozen external scope"
        )
    if _instant(
        dataset.payload.get("causal_cutoff"), "source DatasetSnapshot.causal_cutoff"
    ) != _instant(
        protocol.evidence_scope.dataset_cutoff,
        "FrozenEvidenceScope.dataset_cutoff",
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "source factory dataset cutoff does not match frozen external scope"
        )
    if _instant(dataset.available_at, "source DatasetSnapshot.available_at") > completed:
        raise ProductPolicyEvaluationIssuanceError(
            "source dataset was not durable by evaluation completion"
        )

    strategy = _registry_get(registry, "StrategyVersion", strategy_version_id)
    model = _registry_get(registry, "ModelVersion", model_version_id)
    experiment = _registry_get(registry, "Experiment", experiment_id)
    if strategy is None or model is None or experiment is None:
        raise ProductPolicyEvaluationIssuanceError(
            "source factory strategy/model/experiment lineage is incomplete"
        )
    if strategy.payload.get("model_version_id") != model_version_id:
        raise ProductPolicyEvaluationIssuanceError(
            "source strategy/model lineage mismatch"
        )
    if strategy_version_id != target.policy_id:
        raise ProductPolicyEvaluationIssuanceError(
            "source strategy identity does not match frozen external policy"
        )
    for field, expected in (
        ("evaluation_bundle_id", bundle_id),
        ("strategy_version_id", strategy_version_id),
        ("model_version_id", model_version_id),
        ("dataset_snapshot_id", dataset_snapshot_id),
        ("research_protocol_id", research_protocol_id),
    ):
        if experiment.payload.get(field) != expected:
            raise ProductPolicyEvaluationIssuanceError(
                f"source experiment {field} lineage mismatch"
            )
    if _instant(experiment.available_at, "source Experiment.available_at") != completed:
        raise ProductPolicyEvaluationIssuanceError(
            "source experiment completion does not match evaluation completion"
        )

    policy_evaluation = evaluation_payload.get("policy_evaluation")
    if type(policy_evaluation) is not dict:
        raise ProductPolicyEvaluationIssuanceError(
            "source factory evaluation lacks canonical policy evaluator payload"
        )
    policy_evaluation_sha256 = _sha256(
        evaluation_payload.get("policy_evaluation_sha256"),
        "source policy_evaluation_sha256",
    )
    if _digest(policy_evaluation) != policy_evaluation_sha256:
        raise ProductPolicyEvaluationIssuanceError(
            "source policy evaluator payload commitment mismatch"
        )
    if policy_evaluation.get("challenger_policy_id") != strategy_version_id:
        raise ProductPolicyEvaluationIssuanceError(
            "source challenger policy does not match source strategy"
        )
    if evaluation_payload.get("challenger_policy_id") != strategy_version_id:
        raise ProductPolicyEvaluationIssuanceError(
            "source evaluation challenger identity mismatch"
        )
    if evaluation_payload.get("candidate_metrics_source") != "paired-policy-causal-v1":
        raise ProductPolicyEvaluationIssuanceError(
            "source evaluation is not backed by canonical paired-policy arithmetic"
        )

    artifact_hashes = bundle_payload.get("artifact_hashes")
    if type(artifact_hashes) is not list:
        raise ProductPolicyEvaluationIssuanceError(
            "source EvaluationBundle artifact_hashes are invalid"
        )
    artifact_hashes_checked = tuple(
        _sha256(value, "source EvaluationBundle artifact hash")
        for value in artifact_hashes
    )

    model_artifact_sha256 = _sha256(
        model.payload.get("artifact_sha256"), "source ModelVersion.artifact_sha256"
    )
    if model_artifact_sha256 not in artifact_hashes_checked:
        raise ProductPolicyEvaluationIssuanceError(
            "source EvaluationBundle does not bind the canonical model artifact"
        )
    model_payload = _store_read(
        store,
        "model",
        model_version_id,
        expected_sha256=model_artifact_sha256,
    )
    if (
        model_payload.get("kind") != "autosport-transparent-bandit-policy-model-v1"
        or model_payload.get("model_version_id") != model_version_id
        or model_payload.get("policy_id") != strategy_version_id
        or model_payload.get("policy_evaluation_sha256")
        != policy_evaluation_sha256
        or model_payload.get("dataset_snapshot_id") != dataset_snapshot_id
        or model_payload.get("research_protocol_id") != research_protocol_id
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "source policy model artifact lineage mismatch"
        )
    policy_artifact_sha256 = _sha256(
        model_payload.get("policy_artifact_sha256"),
        "source policy_artifact_sha256",
    )
    if policy_artifact_sha256 != target.policy_artifact_sha256:
        raise ProductPolicyEvaluationIssuanceError(
            "source policy artifact does not match frozen external policy artifact"
        )

    metrics_artifact_sha256 = _sha256(
        evaluation_payload.get("candidate_metrics_artifact_sha256"),
        "source candidate_metrics_artifact_sha256",
    )
    if metrics_artifact_sha256 not in artifact_hashes_checked:
        raise ProductPolicyEvaluationIssuanceError(
            "source EvaluationBundle does not bind canonical candidate metrics"
        )
    metrics_payload = _store_read(
        store,
        "metrics",
        bundle_id,
        expected_sha256=metrics_artifact_sha256,
    )
    if (
        metrics_payload.get("kind") != "autosport-factory-metrics-v1"
        or metrics_payload.get("evaluation_bundle_id") != bundle_id
        or metrics_payload.get("strategy_version_id") != strategy_version_id
        or metrics_payload.get("model_version_id") != model_version_id
        or metrics_payload.get("policy_evaluation_sha256")
        != policy_evaluation_sha256
        or metrics_payload.get("metrics") != evaluation_payload.get("candidate_metrics")
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "source candidate metrics artifact lineage mismatch"
        )

    evaluator_config = evaluation_payload.get("evaluator_config")
    if type(evaluator_config) is not dict:
        raise ProductPolicyEvaluationIssuanceError(
            "source evaluation lacks canonical evaluator config"
        )
    if evaluator_config.get("kind") != "autosport-policy-paired-evaluation-v3":
        raise ProductPolicyEvaluationIssuanceError(
            "source evaluator config kind is unsupported"
        )
    if _digest(evaluator_config) != _sha256(
        evaluation_payload.get("evaluator_config_sha256"),
        "source evaluator_config_sha256",
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "source evaluator config commitment mismatch"
        )

    return _SourceEvaluation(
        evaluation_bundle_id=bundle_id,
        evaluation_bundle_sha256=source_bundle_sha256,
        evaluation_bundle_record_sha256=_sha256(
            bundle.record_sha256, "source EvaluationBundle record_sha256"
        ),
        dataset_snapshot_id=dataset_snapshot_id,
        strategy_version_id=strategy_version_id,
        model_version_id=model_version_id,
        experiment_id=experiment_id,
        completed_at=completed_at,
        policy_artifact_sha256=policy_artifact_sha256,
        model_artifact_sha256=model_artifact_sha256,
        metrics_artifact_sha256=metrics_artifact_sha256,
        policy_evaluation_sha256=policy_evaluation_sha256,
        policy_evaluation=policy_evaluation,
        evaluator_config=evaluator_config,
    )


def _bootstrap_interval(
    protocol: FrozenBaselineProtocol,
    rows: Sequence[tuple[str, str, Decimal]],
) -> tuple[Decimal, Decimal]:
    """Deterministic paired sample-block percentile bootstrap.

    The resampling schedule depends only on the frozen comparison protocol and
    cohort, never on policy results.  Therefore independently issued policies on
    the same frozen cohort consume the same sample-block draws.
    """

    if protocol.uncertainty_method != "paired-block-bootstrap-v1":
        raise ProductPolicyEvaluationIssuanceError(
            "product issuer supports only paired-block-bootstrap-v1"
        )
    if not rows:
        raise ProductPolicyEvaluationIssuanceError(
            "policy evaluator produced no external-validity samples"
        )
    ordered = tuple(sorted(rows, key=lambda item: (item[0], item[1])))
    seed = _digest(
        {
            "schema_version": 1,
            "kind": "autosport-external-validity-paired-block-bootstrap-v1",
            "protocol_sha256": protocol.identity_sha256,
            "cohort_sha256": protocol.evidence_scope.cohort_sha256,
            "sample_blocks": [[sample_id, regime_id] for sample_id, regime_id, _ in ordered],
        }
    )
    means: list[Decimal] = []
    with localcontext() as context:
        context.prec = 80
        count = Decimal(len(ordered))
        for replicate in range(_BOOTSTRAP_REPLICATES):
            total = Decimal(0)
            for draw in range(len(ordered)):
                digest = hashlib.sha256(
                    f"{seed}:{replicate}:{draw}".encode("utf-8")
                ).digest()
                index = int.from_bytes(digest[:8], "big") % len(ordered)
                total += ordered[index][2]
            means.append(total / count)
    means.sort()
    last = len(means) - 1
    low_index = (last * 25) // 1000
    high_index = ((last * 975) + 999) // 1000
    return means[low_index], means[high_index]


def _derive_policy_evaluation(
    protocol: FrozenBaselineProtocol,
    target: _Target,
    source: _SourceEvaluation,
) -> tuple[PolicyEvaluation, dict[str, object]]:
    if protocol.evaluation_contract_family is not (
        EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "canonical policy-factory projection currently supports only predictive external validity"
        )
    if protocol.primary_metric != "predictive_net_utility":
        raise ProductPolicyEvaluationIssuanceError(
            "predictive external-validity primary metric is unsupported"
        )

    policy_evaluation = source.policy_evaluation
    if policy_evaluation.get("kind") != "autosport-policy-paired-causal-evaluation-v1":
        raise ProductPolicyEvaluationIssuanceError(
            "source policy evaluator kind is unsupported"
        )
    if _instant(
        policy_evaluation.get("completed_at"), "source policy evaluator completed_at"
    ) != _instant(source.completed_at, "source evaluation completed_at"):
        raise ProductPolicyEvaluationIssuanceError(
            "source policy evaluator completion mismatch"
        )

    raw_samples = policy_evaluation.get("samples")
    if type(raw_samples) is not list or not raw_samples:
        raise ProductPolicyEvaluationIssuanceError(
            "source policy evaluator samples are missing"
        )
    abstain_action = _text(
        source.evaluator_config.get("abstain_action"), "source abstain_action"
    )
    sample_ids: list[str] = []
    rows: list[tuple[str, str, Decimal]] = []
    total_cost = Decimal(0)
    abstention_count = 0
    with localcontext() as context:
        context.prec = 80
        for raw in raw_samples:
            if type(raw) is not dict:
                raise ProductPolicyEvaluationIssuanceError(
                    "source policy evaluator sample is invalid"
                )
            sample_id = _text(raw.get("sample_id"), "source sample_id")
            regime_id = _text(raw.get("regime_id"), "source regime_id")
            action = _text(raw.get("challenger_action"), "source challenger_action")
            net = _decimal(
                raw.get("challenger_net_reward"), "source challenger_net_reward"
            )
            cost = _decimal(
                raw.get("challenger_cost"),
                "source challenger_cost",
                nonnegative=True,
            )
            case_payload = raw.get("case_payload")
            if type(case_payload) is not dict or case_payload.get("sample_id") != sample_id:
                raise ProductPolicyEvaluationIssuanceError(
                    "source sample does not bind its exact causal case"
                )
            _sha256(
                case_payload.get("source_evidence_sha256"),
                "source case source_evidence_sha256",
            )
            sample_ids.append(sample_id)
            rows.append((sample_id, regime_id, net))
            total_cost += cost
            if action == abstain_action:
                abstention_count += 1

        if len(sample_ids) != len(set(sample_ids)):
            raise ProductPolicyEvaluationIssuanceError(
                "source policy evaluator sample identities are duplicated"
            )
        if tuple(sorted(sample_ids)) != protocol.evidence_scope.cohort_keys:
            raise ProductPolicyEvaluationIssuanceError(
                "source policy evaluator cohort does not match frozen external scope"
            )
        observed_count = len(sample_ids)
        scored_count = observed_count - abstention_count
        metric_value = sum((row[2] for row in rows), Decimal(0)) / Decimal(
            observed_count
        )

    challenger_metrics = policy_evaluation.get("challenger_metrics")
    if type(challenger_metrics) is not dict or "policy_loss" not in challenger_metrics:
        raise ProductPolicyEvaluationIssuanceError(
            "source policy evaluator lacks exact challenger policy_loss"
        )
    policy_loss = _decimal(
        challenger_metrics.get("policy_loss"), "source challenger policy_loss"
    )
    if metric_value != -policy_loss:
        raise ProductPolicyEvaluationIssuanceError(
            "source sample utility does not reconcile to canonical policy_loss"
        )

    low, high = _bootstrap_interval(protocol, rows)
    evaluated_at = _text(source.completed_at, "source completed_at")
    provisional = PolicyEvaluation(
        policy_id=target.policy_id,
        policy_artifact_sha256=target.policy_artifact_sha256,
        protocol_sha256=protocol.identity_sha256,
        evidence_scope_sha256=protocol.evidence_scope.identity_sha256,
        cohort_sha256=protocol.evidence_scope.cohort_sha256,
        primary_metric=protocol.primary_metric,
        evaluated_at=evaluated_at,
        metric_value=_decimal_text(metric_value),
        uncertainty_low=_decimal_text(low),
        uncertainty_high=_decimal_text(high),
        observed_count=observed_count,
        scored_count=scored_count,
        abstention_count=abstention_count,
        total_cost=_decimal_text(total_cost),
        evaluation_bundle_sha256="0" * 64,
        baseline_definition_sha256=target.baseline_definition_sha256,
    )
    commitment = canonical_product_policy_evaluation_bundle_sha256(provisional)
    issued = PolicyEvaluation(
        policy_id=provisional.policy_id,
        policy_artifact_sha256=provisional.policy_artifact_sha256,
        protocol_sha256=provisional.protocol_sha256,
        evidence_scope_sha256=provisional.evidence_scope_sha256,
        cohort_sha256=provisional.cohort_sha256,
        primary_metric=provisional.primary_metric,
        evaluated_at=provisional.evaluated_at,
        metric_value=provisional.metric_value,
        uncertainty_low=provisional.uncertainty_low,
        uncertainty_high=provisional.uncertainty_high,
        observed_count=provisional.observed_count,
        scored_count=provisional.scored_count,
        abstention_count=provisional.abstention_count,
        total_cost=provisional.total_cost,
        evaluation_bundle_sha256=commitment,
        baseline_definition_sha256=provisional.baseline_definition_sha256,
    )
    projection = {
        "source_kind": "autosport-policy-specific-factory-evaluation-v1",
        "source_policy_evaluation_sha256": source.policy_evaluation_sha256,
        "source_metric": "policy_loss",
        "projection_metric": protocol.primary_metric,
        "projection_rule": "predictive_net_utility=-policy_loss",
        "uncertainty_method": protocol.uncertainty_method,
        "bootstrap_replicates": _BOOTSTRAP_REPLICATES,
        "bootstrap_schedule": "sha256-frozen-protocol-cohort-sample-blocks-v1",
        "bootstrap_interval": "percentile-2.5-97.5-v1",
        "abstain_action": abstain_action,
    }
    return issued, projection


def issue_product_policy_evaluation(
    authority: ProductPolicyEvaluationWorkspace,
    protocol: FrozenBaselineProtocol,
    *,
    source_evaluation_bundle_id: str,
    baseline_kind: BaselineKind | None = None,
) -> IssuedPolicyEvaluationRef:
    """Issue one external result from an exact existing product-factory evaluation.

    The caller selects only a frozen protocol slot and an already-durable source
    bundle.  No final metric/count/cost/uncertainty value is accepted from the
    caller.  Re-issuing the same frozen slot is idempotent; trying to bind the
    same slot to changed source truth collides with the immutable result artifact.
    """

    canonical_registry, canonical_store = _open_canonical_authorities(authority)
    target = _target(protocol, baseline_kind)
    issuance_id = _issuance_id(authority, protocol, target)
    source = _require_source_factory_evaluation(
        canonical_registry,
        canonical_store,
        protocol,
        target,
        source_evaluation_bundle_id,
    )
    evaluation, projection = _derive_policy_evaluation(protocol, target, source)

    result_payload = {
        "schema_version": 3,
        "kind": "autosport-product-issued-external-validity-policy-evaluation-v3",
        "issuance_id": issuance_id,
        "workspace_instance_id": authority.workspace_instance_id,
        "workspace_locator_sha256": authority.workspace_locator_sha256,
        "protocol_sha256": protocol.identity_sha256,
        "evidence_scope_sha256": protocol.evidence_scope.identity_sha256,
        "policy_id": target.policy_id,
        "baseline_kind": (
            None if target.baseline_kind is None else target.baseline_kind.value
        ),
        "source_evaluation_bundle_id": source.evaluation_bundle_id,
        "source_evaluation_bundle_sha256": source.evaluation_bundle_sha256,
        "source_evaluation_bundle_record_sha256": source.evaluation_bundle_record_sha256,
        "source_policy_evaluation_sha256": source.policy_evaluation_sha256,
        "source_model_artifact_sha256": source.model_artifact_sha256,
        "source_metrics_artifact_sha256": source.metrics_artifact_sha256,
        "source_experiment_id": source.experiment_id,
        "source_strategy_version_id": source.strategy_version_id,
        "source_model_version_id": source.model_version_id,
        "source_dataset_snapshot_id": source.dataset_snapshot_id,
        "projection": projection,
        "evaluation": evaluation.to_payload(),
        "truth": {
            "product_evaluator_issued": True,
            "caller_completed_dto_accepted": False,
            "winner_selected": False,
            "promotion_authority": False,
            "real_money_execution": False,
        },
    }
    result_artifact_sha256 = _STORE_MATERIALIZE(
        canonical_store,
        _RESULT_ARTIFACT_KIND,
        issuance_id,
        result_payload,
    )
    receipt = _STORE_RECEIPT(
        canonical_store,
        _RESULT_ARTIFACT_KIND,
        issuance_id,
        expected_sha256=result_artifact_sha256,
    )
    materialized_at = _text(
        receipt.get("materialized_at"), "issued result materialized_at"
    )
    if _instant(source.completed_at, "source completed_at") > _instant(
        materialized_at, "issued result materialized_at"
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "issued result materialized before its source evaluation existed"
        )

    evaluation_bundle_id = _BUNDLE_ID_PREFIX + issuance_id
    artifact_hashes = tuple(
        sorted(
            {
                result_artifact_sha256,
                source.evaluation_bundle_sha256,
                source.model_artifact_sha256,
                source.metrics_artifact_sha256,
            }
        )
    )
    bundle = EvaluationBundleRef(
        evaluation_bundle_id=evaluation_bundle_id,
        bundle_sha256=evaluation.evaluation_bundle_sha256,
        evaluator_source_sha256=_ISSUER_SOURCE_SHA256,
        dataset_snapshot_id=source.dataset_snapshot_id,
        protocol_sha256=protocol.identity_sha256,
        artifact_hashes=artifact_hashes,
        created_at=materialized_at,
        evaluated_strategy_version_id=source.strategy_version_id,
        evaluated_model_version_id=source.model_version_id,
    )
    _REGISTRY_APPEND(canonical_registry, bundle)

    ref = IssuedPolicyEvaluationRef(
        issuance_id=issuance_id,
        workspace_instance_id=authority.workspace_instance_id,
        workspace_locator_sha256=authority.workspace_locator_sha256,
        evaluation_bundle_id=evaluation_bundle_id,
        evaluation_bundle_sha256=evaluation.evaluation_bundle_sha256,
        result_artifact_sha256=result_artifact_sha256,
        source_evaluation_bundle_id=source.evaluation_bundle_id,
        source_evaluation_bundle_sha256=source.evaluation_bundle_sha256,
        policy_id=target.policy_id,
        baseline_kind=target.baseline_kind,
    )
    resolve_product_policy_evaluation(
        authority,
        protocol,
        ref,
    )
    return ref


def resolve_product_policy_evaluation(
    authority: ProductPolicyEvaluationWorkspace,
    protocol: FrozenBaselineProtocol,
    reference: IssuedPolicyEvaluationRef,
) -> PolicyEvaluation:
    """Re-resolve and recompute exact product-issued result truth after restart."""

    canonical_registry, canonical_store = _open_canonical_authorities(authority)
    if type(reference) is not IssuedPolicyEvaluationRef:
        raise ProductPolicyEvaluationIssuanceError(
            "reference must be an exact IssuedPolicyEvaluationRef"
        )
    if (
        reference.workspace_instance_id != authority.workspace_instance_id
        or reference.workspace_locator_sha256 != authority.workspace_locator_sha256
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "issued reference belongs to another product workspace"
        )
    target = _target(protocol, reference.baseline_kind)
    expected_issuance_id = _issuance_id(authority, protocol, target)
    if reference.issuance_id != expected_issuance_id:
        raise ProductPolicyEvaluationIssuanceError(
            "issued reference does not match frozen protocol slot"
        )
    if reference.policy_id != target.policy_id:
        raise ProductPolicyEvaluationIssuanceError(
            "issued reference policy identity mismatch"
        )
    expected_bundle_id = _BUNDLE_ID_PREFIX + expected_issuance_id
    if reference.evaluation_bundle_id != expected_bundle_id:
        raise ProductPolicyEvaluationIssuanceError(
            "issued reference bundle identity mismatch"
        )

    bundle = _registry_get(canonical_registry, "EvaluationBundle", expected_bundle_id)
    if bundle is None:
        raise ProductPolicyEvaluationIssuanceError(
            "product-issued EvaluationBundle is missing"
        )
    bundle_payload = bundle.payload
    if _sha256(
        bundle_payload.get("bundle_sha256"), "issued EvaluationBundle.bundle_sha256"
    ) != reference.evaluation_bundle_sha256:
        raise ProductPolicyEvaluationIssuanceError(
            "issued EvaluationBundle commitment mismatch"
        )
    if _sha256(
        bundle_payload.get("evaluator_source_sha256"),
        "issued EvaluationBundle.evaluator_source_sha256",
    ) != _ISSUER_SOURCE_SHA256:
        raise ProductPolicyEvaluationIssuanceError(
            "issued EvaluationBundle evaluator authority mismatch"
        )
    if _sha256(
        bundle_payload.get("protocol_sha256"),
        "issued EvaluationBundle.protocol_sha256",
    ) != protocol.identity_sha256:
        raise ProductPolicyEvaluationIssuanceError(
            "issued EvaluationBundle protocol mismatch"
        )

    result_payload = _store_read(
        canonical_store,
        _RESULT_ARTIFACT_KIND,
        expected_issuance_id,
        expected_sha256=reference.result_artifact_sha256,
    )
    receipt = _STORE_RECEIPT(
        canonical_store,
        _RESULT_ARTIFACT_KIND,
        expected_issuance_id,
        expected_sha256=reference.result_artifact_sha256,
    )
    if _instant(
        bundle_payload.get("created_at"), "issued EvaluationBundle.created_at"
    ) != _instant(receipt.get("materialized_at"), "issued result materialized_at"):
        raise ProductPolicyEvaluationIssuanceError(
            "issued bundle does not bind store-owned materialization time"
        )
    artifact_hashes = bundle_payload.get("artifact_hashes")
    if type(artifact_hashes) is not list:
        raise ProductPolicyEvaluationIssuanceError(
            "issued EvaluationBundle artifact_hashes are invalid"
        )
    checked_artifact_hashes = {
        _sha256(value, "issued EvaluationBundle artifact hash")
        for value in artifact_hashes
    }
    if reference.result_artifact_sha256 not in checked_artifact_hashes:
        raise ProductPolicyEvaluationIssuanceError(
            "issued EvaluationBundle does not bind result artifact"
        )

    if (
        type(result_payload) is not dict
        or result_payload.get("schema_version") != 3
        or result_payload.get("kind")
        != "autosport-product-issued-external-validity-policy-evaluation-v3"
        or result_payload.get("issuance_id") != expected_issuance_id
        or result_payload.get("workspace_instance_id") != authority.workspace_instance_id
        or result_payload.get("workspace_locator_sha256")
        != authority.workspace_locator_sha256
        or result_payload.get("protocol_sha256") != protocol.identity_sha256
        or result_payload.get("evidence_scope_sha256")
        != protocol.evidence_scope.identity_sha256
        or result_payload.get("policy_id") != target.policy_id
        or result_payload.get("baseline_kind")
        != (None if target.baseline_kind is None else target.baseline_kind.value)
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "issued result artifact identity/protocol fields mismatch"
        )
    if result_payload.get("source_evaluation_bundle_id") != (
        reference.source_evaluation_bundle_id
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "issued result source bundle identity mismatch"
        )
    if _sha256(
        result_payload.get("source_evaluation_bundle_sha256"),
        "issued result source_evaluation_bundle_sha256",
    ) != reference.source_evaluation_bundle_sha256:
        raise ProductPolicyEvaluationIssuanceError(
            "issued result source bundle commitment mismatch"
        )

    source = _require_source_factory_evaluation(
        canonical_registry,
        canonical_store,
        protocol,
        target,
        reference.source_evaluation_bundle_id,
    )
    if source.evaluation_bundle_sha256 != reference.source_evaluation_bundle_sha256:
        raise ProductPolicyEvaluationIssuanceError(
            "durable source evaluation changed after issuance"
        )
    for source_hash in (
        source.evaluation_bundle_sha256,
        source.model_artifact_sha256,
        source.metrics_artifact_sha256,
    ):
        if source_hash not in checked_artifact_hashes:
            raise ProductPolicyEvaluationIssuanceError(
                "issued bundle omits canonical source artifact provenance"
            )

    evaluation, projection = _derive_policy_evaluation(protocol, target, source)
    if result_payload.get("projection") != projection:
        raise ProductPolicyEvaluationIssuanceError(
            "issued result projection semantics mismatch"
        )
    if result_payload.get("evaluation") != evaluation.to_payload():
        raise ProductPolicyEvaluationIssuanceError(
            "issued result bytes do not match recomputed product evaluator truth"
        )
    if evaluation.evaluation_bundle_sha256 != reference.evaluation_bundle_sha256:
        raise ProductPolicyEvaluationIssuanceError(
            "issued reference does not match recomputed evaluation commitment"
        )
    if canonical_product_policy_evaluation_bundle_sha256(evaluation) != (
        reference.evaluation_bundle_sha256
    ):
        raise ProductPolicyEvaluationIssuanceError(
            "issued evaluation canonical commitment mismatch"
        )
    if bundle_payload.get("dataset_snapshot_id") != source.dataset_snapshot_id:
        raise ProductPolicyEvaluationIssuanceError(
            "issued EvaluationBundle dataset mismatch"
        )
    if bundle_payload.get("evaluated_strategy_version_id") != source.strategy_version_id:
        raise ProductPolicyEvaluationIssuanceError(
            "issued EvaluationBundle strategy mismatch"
        )
    if bundle_payload.get("evaluated_model_version_id") != source.model_version_id:
        raise ProductPolicyEvaluationIssuanceError(
            "issued EvaluationBundle model mismatch"
        )
    return evaluation


def verify_product_policy_evaluation(
    authority: ProductPolicyEvaluationWorkspace,
    protocol: FrozenBaselineProtocol,
    reference: IssuedPolicyEvaluationRef,
    claimed: PolicyEvaluation,
) -> PolicyEvaluation:
    """Require a caller DTO to be exactly the already-issued durable product result."""

    if type(claimed) is not PolicyEvaluation:
        raise ProductPolicyEvaluationIssuanceError(
            "claimed evaluation must be an exact PolicyEvaluation value"
        )
    issued = resolve_product_policy_evaluation(
        authority,
        protocol,
        reference,
    )
    if claimed.to_payload() != issued.to_payload():
        raise ProductPolicyEvaluationIssuanceError(
            "claimed PolicyEvaluation differs from product-issued result"
        )
    return issued


__all__ = [
    "IssuedPolicyEvaluationRef",
    "ProductPolicyEvaluationIssuanceError",
    "ProductPolicyEvaluationWorkspace",
    "canonical_product_policy_evaluation_bundle_sha256",
    "issue_product_policy_evaluation",
    "resolve_product_policy_evaluation",
    "verify_product_policy_evaluation",
]
