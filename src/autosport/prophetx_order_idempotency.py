"""ProphetX order identity/retry policy over the canonical RealExecutionLedger.

No provider transport or write path exists here. REST duplicate suppression is never
promoted to replay semantics; FIX redelivery remains a separately qualified profile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable

from .real_execution_ledger import (
    AttemptState,
    ExecutionIdentityConflict,
    ExecutionStateError,
    RealExecutionLedger,
)

PROPHETX_PROVIDER_ID = "prophetx"


class ProphetXOrderIdentityError(RuntimeError):
    pass


class ProphetXEvidenceConflict(ProphetXOrderIdentityError):
    pass


_CANONICAL_LEDGER_CLASS = RealExecutionLedger
_CANONICAL_LEDGER_INIT = RealExecutionLedger.__init__
_CANONICAL_LEDGER_INIT_CODE = RealExecutionLedger.__init__.__code__
_CANONICAL_LEDGER_METHOD_NAMES = (
    "verified_snapshot",
    "provider_order_reference",
    "bind_provider_assigned_order_id",
    "provider_assigned_order_id",
)
_CANONICAL_LEDGER_METHODS = {
    name: getattr(RealExecutionLedger, name)
    for name in _CANONICAL_LEDGER_METHOD_NAMES
}
_CANONICAL_LEDGER_METHOD_CODES = {
    name: getattr(method, "__code__", None)
    for name, method in _CANONICAL_LEDGER_METHODS.items()
}
_CANONICAL_LEDGER_BIND_PROVIDER_ASSIGNED_ORDER_ID = (
    _CANONICAL_LEDGER_METHODS["bind_provider_assigned_order_id"]
)
_CANONICAL_LEDGER_PROVIDER_ASSIGNED_ORDER_ID = (
    _CANONICAL_LEDGER_METHODS["provider_assigned_order_id"]
)


def _require_canonical_provider_order_ledger(ledger: object) -> RealExecutionLedger:
    """Require exact top-level ledger dispatch for durable provider OrderID truth."""

    if (
        RealExecutionLedger is not _CANONICAL_LEDGER_CLASS
        or _CANONICAL_LEDGER_CLASS.__init__ is not _CANONICAL_LEDGER_INIT
        or getattr(_CANONICAL_LEDGER_CLASS.__init__, "__code__", None)
        is not _CANONICAL_LEDGER_INIT_CODE
        or type(ledger) is not _CANONICAL_LEDGER_CLASS
    ):
        raise ProphetXOrderIdentityError(
            "canonical execution ledger provider-order authority changed"
        )
    instance_state = vars(ledger)
    for method_name in _CANONICAL_LEDGER_METHOD_NAMES:
        expected_method = _CANONICAL_LEDGER_METHODS[method_name]
        current_method = getattr(_CANONICAL_LEDGER_CLASS, method_name, None)
        bound_method = getattr(ledger, method_name, None)
        if (
            current_method is not expected_method
            or getattr(current_method, "__code__", None)
            is not _CANONICAL_LEDGER_METHOD_CODES[method_name]
            or method_name in instance_state
            or getattr(bound_method, "__self__", None) is not ledger
            or getattr(bound_method, "__func__", None) is not expected_method
        ):
            raise ProphetXOrderIdentityError(
                "canonical execution ledger provider-order dispatch changed"
            )
    return ledger


class ProphetXTransport(str, Enum):
    REST_DIRECT_LINK = "REST_DIRECT_LINK"
    FIX_ORDER_ENTRY = "FIX_ORDER_ENTRY"


class ProphetXEnvironment(str, Enum):
    SANDBOX = "SANDBOX"
    PRODUCTION = "PRODUCTION"


@dataclass(frozen=True, slots=True)
class ProphetXProfile:
    bookmaker_profile_version: str
    environment: ProphetXEnvironment
    transport: ProphetXTransport
    production_write_qualified: bool = False


PROPHETX_SANDBOX_REST_PROFILE = ProphetXProfile(
    "prophetx-sandbox-rest-direct-link-v1",
    ProphetXEnvironment.SANDBOX,
    ProphetXTransport.REST_DIRECT_LINK,
)
PROPHETX_SANDBOX_FIX_PROFILE = ProphetXProfile(
    "prophetx-sandbox-fix-order-entry-v1",
    ProphetXEnvironment.SANDBOX,
    ProphetXTransport.FIX_ORDER_ENTRY,
)
PROPHETX_PRODUCTION_REST_PROFILE = ProphetXProfile(
    "prophetx-production-rest-direct-link-v1",
    ProphetXEnvironment.PRODUCTION,
    ProphetXTransport.REST_DIRECT_LINK,
)
PROPHETX_PRODUCTION_FIX_PROFILE = ProphetXProfile(
    "prophetx-production-fix-order-entry-v1",
    ProphetXEnvironment.PRODUCTION,
    ProphetXTransport.FIX_ORDER_ENTRY,
)
_PROFILE_BY_VERSION = {
    item.bookmaker_profile_version: item
    for item in (
        PROPHETX_SANDBOX_REST_PROFILE,
        PROPHETX_SANDBOX_FIX_PROFILE,
        PROPHETX_PRODUCTION_REST_PROFILE,
        PROPHETX_PRODUCTION_FIX_PROFILE,
    )
}


@dataclass(frozen=True, slots=True)
class ProphetXTransportCapabilities:
    duplicate_suppression_only: bool
    idempotent_redelivery_identical_economics: bool
    order_status_by_client_or_provider_id: bool
    authoritative_readback_by_external_id: bool


REST_DIRECT_LINK_CAPABILITIES = ProphetXTransportCapabilities(True, False, False, False)
FIX_ORDER_ENTRY_CAPABILITIES = ProphetXTransportCapabilities(False, True, True, False)


def capabilities_for(transport: ProphetXTransport) -> ProphetXTransportCapabilities:
    if transport is ProphetXTransport.REST_DIRECT_LINK:
        return REST_DIRECT_LINK_CAPABILITIES
    if transport is ProphetXTransport.FIX_ORDER_ENTRY:
        return FIX_ORDER_ENTRY_CAPABILITIES
    raise ValueError(f"unsupported ProphetX transport: {transport!r}")


@dataclass(frozen=True, slots=True)
class ProphetXOrderIdentity:
    attempt_id: str
    environment: ProphetXEnvironment
    transport: ProphetXTransport
    client_order_id: str
    effect_fingerprint: str
    production_write_qualified: bool

    def __post_init__(self) -> None:
        if not self.attempt_id.strip():
            raise ValueError("attempt_id must be non-empty")
        if not 1 <= len(self.client_order_id) <= 32:
            raise ValueError("client_order_id must be 1..32 characters")
        _sha256(self.effect_fingerprint, "effect_fingerprint")


class ProphetXEvidenceKind(str, Enum):
    ORDER_STATE = "ORDER_STATE"
    DUPLICATE_REJECTION = "DUPLICATE_REJECTION"
    WALLET_TRANSACTION = "WALLET_TRANSACTION"
    FIX_EXECUTION_REPORT = "FIX_EXECUTION_REPORT"
    FIX_ORDER_STATUS_UNKNOWN = "FIX_ORDER_STATUS_UNKNOWN"
    FIX_RATE_LIMIT_PREPROCESSING_REJECT = "FIX_RATE_LIMIT_PREPROCESSING_REJECT"


@dataclass(frozen=True, slots=True)
class ProphetXProviderEvidence:
    evidence_id: str
    kind: ProphetXEvidenceKind
    transport: ProphetXTransport
    observed_at: str
    client_order_id: str | None = None
    provider_order_id: str | None = None
    effect_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _sha256(self.evidence_id, "evidence_id")
        _time(self.observed_at, "observed_at")
        if self.client_order_id is not None and not self.client_order_id.strip():
            raise ValueError("client_order_id must be non-empty when present")
        if self.provider_order_id is not None and not self.provider_order_id.strip():
            raise ValueError("provider_order_id must be non-empty when present")
        if self.effect_fingerprint is not None:
            _sha256(self.effect_fingerprint, "effect_fingerprint")


class ProphetXReconciliationDisposition(str, Enum):
    MATCHED_PROVIDER_ORDER_CANDIDATE = "MATCHED_PROVIDER_ORDER_CANDIDATE"
    PROVIDER_NOT_FOUND_CANDIDATE = "PROVIDER_NOT_FOUND_CANDIDATE"
    WAIT_EXTERNAL_EVIDENCE = "WAIT_EXTERNAL_EVIDENCE"
    UNRELATED_PROVIDER_EVIDENCE = "UNRELATED_PROVIDER_EVIDENCE"
    CONFLICT = "CONFLICT"


class ProphetXRetryDisposition(str, Enum):
    WAIT_EXTERNAL_EVIDENCE = "WAIT_EXTERNAL_EVIDENCE"
    SAME_CLIENT_ID_REDELIVERY_CANDIDATE = "SAME_CLIENT_ID_REDELIVERY_CANDIDATE"
    SAME_CLIENT_ID_RETRY_AFTER_BACKOFF_CANDIDATE = (
        "SAME_CLIENT_ID_RETRY_AFTER_BACKOFF_CANDIDATE"
    )
    LOCAL_REJECT_DIFFERENT_ECONOMICS = "LOCAL_REJECT_DIFFERENT_ECONOMICS"
    PROFILE_UNQUALIFIED = "PROFILE_UNQUALIFIED"


@dataclass(frozen=True, slots=True)
class ProphetXFixExecutionReport:
    exec_id: str
    client_order_id: str
    provider_order_id: str
    transact_time: str
    effect_fingerprint: str
    lifecycle_state: str
    cumulative_filled_quantity: str

    def __post_init__(self) -> None:
        for name in (
            "exec_id",
            "client_order_id",
            "provider_order_id",
            "lifecycle_state",
            "cumulative_filled_quantity",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be non-empty text")
        _time(self.transact_time, "transact_time")
        _sha256(self.effect_fingerprint, "effect_fingerprint")


def _sha256(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{field} must be lowercase SHA-256 hex")
    return value


def _time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field} must be timezone-aware ISO-8601 text") from exc
    if not value.strip() or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware ISO-8601 text")
    return parsed


def _durable_attempt_facts(
    ledger: RealExecutionLedger, attempt_id: str
) -> tuple[ProphetXProfile, str]:
    """Read profile/effect only from ledger bytes already verified by canonical authority."""

    snapshot = ledger.verified_snapshot()
    try:
        events = [
            json.loads(line)["event"]
            for line in snapshot.payload.decode("utf-8").splitlines()
            if line
        ]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProphetXOrderIdentityError(
            "verified execution snapshot is incompatible with ProphetX adapter"
        ) from exc

    attempts = [
        event
        for event in events
        if event.get("attempt_id") == attempt_id
        and event.get("event_type") == "ATTEMPT_RESERVED"
    ]
    if len(attempts) != 1:
        raise ProphetXOrderIdentityError(
            "ProphetX identity requires exactly one durable attempt reservation"
        )
    attempt = attempts[0]
    try:
        effect = _sha256(attempt["payload"]["effect_fingerprint"], "effect_fingerprint")
    except (KeyError, TypeError, ValueError) as exc:
        raise ProphetXOrderIdentityError("durable effect fingerprint is invalid") from exc

    plans = [
        event
        for event in events
        if event.get("plan_id") == attempt.get("plan_id")
        and event.get("event_type") == "PLAN_RESERVED"
    ]
    if len(plans) != 1:
        raise ProphetXOrderIdentityError(
            "ProphetX identity requires exactly one durable execution plan"
        )
    version = plans[0].get("payload", {}).get("plan", {}).get("bookmaker_profile_version")
    profile = _PROFILE_BY_VERSION.get(version)
    if profile is None:
        raise ProphetXOrderIdentityError(
            "attempt is not durably bound to a qualified ProphetX transport profile"
        )
    return profile, effect


def bind_before_effect(
    ledger: RealExecutionLedger, *, attempt_id: str
) -> ProphetXOrderIdentity:
    if ledger.attempt_state(attempt_id) is not AttemptState.RESERVED:
        raise ProphetXOrderIdentityError(
            "ProphetX client order identity must be bound before external effect"
        )
    _durable_attempt_facts(ledger, attempt_id)
    reference = ledger.bind_provider_order_reference(
        attempt_id=attempt_id, provider_id=PROPHETX_PROVIDER_ID
    )
    return load_identity(ledger, attempt_id=attempt_id, expected_reference=reference)


def load_identity(
    ledger: RealExecutionLedger,
    *,
    attempt_id: str,
    expected_reference: str | None = None,
) -> ProphetXOrderIdentity:
    profile, effect = _durable_attempt_facts(ledger, attempt_id)
    reference = ledger.provider_order_reference(
        attempt_id=attempt_id, provider_id=PROPHETX_PROVIDER_ID
    )
    if reference is None:
        raise ProphetXOrderIdentityError(
            "attempt has no durable ProphetX client order identity"
        )
    if expected_reference is not None and reference != expected_reference:
        raise ProphetXEvidenceConflict("durable ProphetX client order identity changed")
    return ProphetXOrderIdentity(
        attempt_id,
        profile.environment,
        profile.transport,
        reference,
        effect,
        profile.production_write_qualified,
    )


def mark_ambiguous_delivery(
    ledger: RealExecutionLedger,
    *,
    identity: ProphetXOrderIdentity,
    reason: str,
    observed_at: str,
) -> None:
    if load_identity(ledger, attempt_id=identity.attempt_id) != identity:
        raise ProphetXEvidenceConflict("ProphetX identity no longer matches durable attempt")
    ledger.mark_unknown(identity.attempt_id, reason=reason, observed_at=observed_at)


def _bind_provider_assigned_order_id_impl(
    ledger: RealExecutionLedger,
    identity: ProphetXOrderIdentity,
    provider_order_id: str,
    *,
    identity_resolver,
    identity_resolver_code: object,
    identity_eq,
    identity_eq_code: object,
) -> bool:
    try:
        _require_canonical_provider_order_ledger(ledger)
        if getattr(identity_resolver, "__code__", None) is not identity_resolver_code:
            return False
        ledger_path = ledger.path
        resolved_identity = identity_resolver(
            ledger,
            attempt_id=identity.attempt_id,
        )
        if (
            getattr(identity_eq, "__code__", None) is not identity_eq_code
            or identity_eq(resolved_identity, identity) is not True
        ):
            return False
        _require_canonical_provider_order_ledger(ledger)
        bound = _CANONICAL_LEDGER_BIND_PROVIDER_ASSIGNED_ORDER_ID(
            ledger,
            attempt_id=identity.attempt_id,
            provider_id=PROPHETX_PROVIDER_ID,
            provider_order_id=provider_order_id,
        )
        if bound != provider_order_id or ledger.path != ledger_path:
            return False
        _require_canonical_provider_order_ledger(ledger)

        # Positive correlation requires a restart-visible durable fact, not merely
        # a successful method return from the caller-held ledger object.
        restarted = object.__new__(_CANONICAL_LEDGER_CLASS)
        _CANONICAL_LEDGER_INIT(restarted, ledger_path)
        _require_canonical_provider_order_ledger(restarted)
        durable = _CANONICAL_LEDGER_PROVIDER_ASSIGNED_ORDER_ID(
            restarted,
            attempt_id=identity.attempt_id,
            provider_id=PROPHETX_PROVIDER_ID,
        )
        _require_canonical_provider_order_ledger(restarted)
        if durable != provider_order_id:
            return False
    except (
        ExecutionIdentityConflict,
        ExecutionStateError,
        ProphetXOrderIdentityError,
    ):
        return False
    return True


def _reconciliation_disposition_impl(
    identity: ProphetXOrderIdentity,
    evidence: ProphetXProviderEvidence,
    *,
    ledger: RealExecutionLedger | None = None,
    bind_provider_order_id,
) -> ProphetXReconciliationDisposition:
    """Return correlation candidates only; never mint #1634/#530 provider truth."""

    if evidence.transport is not identity.transport:
        return ProphetXReconciliationDisposition.CONFLICT
    if (
        evidence.effect_fingerprint is not None
        and evidence.effect_fingerprint != identity.effect_fingerprint
    ):
        return ProphetXReconciliationDisposition.CONFLICT
    if evidence.client_order_id is None:
        return ProphetXReconciliationDisposition.WAIT_EXTERNAL_EVIDENCE
    if evidence.client_order_id != identity.client_order_id:
        return ProphetXReconciliationDisposition.UNRELATED_PROVIDER_EVIDENCE

    if identity.transport is ProphetXTransport.REST_DIRECT_LINK:
        if (
            evidence.kind is ProphetXEvidenceKind.ORDER_STATE
            and evidence.provider_order_id
        ):
            if ledger is None or not bind_provider_order_id(
                ledger, identity, evidence.provider_order_id
            ):
                return ProphetXReconciliationDisposition.CONFLICT
            return ProphetXReconciliationDisposition.MATCHED_PROVIDER_ORDER_CANDIDATE
        return ProphetXReconciliationDisposition.WAIT_EXTERNAL_EVIDENCE

    if evidence.kind is ProphetXEvidenceKind.FIX_ORDER_STATUS_UNKNOWN:
        return ProphetXReconciliationDisposition.PROVIDER_NOT_FOUND_CANDIDATE
    if evidence.kind is ProphetXEvidenceKind.FIX_EXECUTION_REPORT:
        if not evidence.provider_order_id:
            return ProphetXReconciliationDisposition.CONFLICT
        if ledger is None or not bind_provider_order_id(
            ledger, identity, evidence.provider_order_id
        ):
            return ProphetXReconciliationDisposition.CONFLICT
        return ProphetXReconciliationDisposition.MATCHED_PROVIDER_ORDER_CANDIDATE
    return ProphetXReconciliationDisposition.WAIT_EXTERNAL_EVIDENCE


def retry_disposition(
    identity: ProphetXOrderIdentity,
    *,
    candidate_effect_fingerprint: str,
    exact_provider_profile_qualified: bool,
    preprocessing_rate_limit_reject: bool = False,
) -> ProphetXRetryDisposition:
    """Return only a candidate policy outcome; canonical authority still gates effects."""

    _sha256(candidate_effect_fingerprint, "candidate_effect_fingerprint")
    if candidate_effect_fingerprint != identity.effect_fingerprint:
        return ProphetXRetryDisposition.LOCAL_REJECT_DIFFERENT_ECONOMICS
    if not exact_provider_profile_qualified:
        return ProphetXRetryDisposition.PROFILE_UNQUALIFIED
    if identity.transport is ProphetXTransport.REST_DIRECT_LINK:
        return ProphetXRetryDisposition.WAIT_EXTERNAL_EVIDENCE
    if preprocessing_rate_limit_reject:
        return ProphetXRetryDisposition.SAME_CLIENT_ID_RETRY_AFTER_BACKOFF_CANDIDATE
    return ProphetXRetryDisposition.SAME_CLIENT_ID_REDELIVERY_CANDIDATE


def _normalize_fix_execution_reports_impl(
    identity: ProphetXOrderIdentity,
    reports: Iterable[ProphetXFixExecutionReport],
    *,
    ledger: RealExecutionLedger | None = None,
    bind_provider_order_id,
) -> tuple[ProphetXFixExecutionReport, ...]:
    if identity.transport is not ProphetXTransport.FIX_ORDER_ENTRY:
        raise ProphetXOrderIdentityError(
            "FIX execution reports cannot be applied to a REST identity"
        )
    by_exec_id: dict[str, ProphetXFixExecutionReport] = {}
    provider_order_ids: set[str] = set()
    for report in reports:
        if report.client_order_id != identity.client_order_id:
            raise ProphetXEvidenceConflict(
                "FIX report client order id mismatches durable attempt"
            )
        if report.effect_fingerprint != identity.effect_fingerprint:
            raise ProphetXEvidenceConflict("FIX report economics mismatch durable attempt")
        provider_order_ids.add(report.provider_order_id)
        if len(provider_order_ids) > 1:
            raise ProphetXEvidenceConflict(
                "FIX reports conflict on provider assigned order id"
            )
        prior = by_exec_id.get(report.exec_id)
        if prior is not None and prior != report:
            raise ProphetXEvidenceConflict(
                "same FIX ExecID has conflicting provider evidence"
            )
        by_exec_id[report.exec_id] = report

    if by_exec_id:
        if ledger is None:
            raise ProphetXOrderIdentityError(
                "FIX execution reports require durable provider order id binding"
            )
        provider_order_id = next(iter(provider_order_ids))
        if not bind_provider_order_id(
            ledger, identity, provider_order_id
        ):
            raise ProphetXEvidenceConflict(
                "FIX provider assigned order id conflicts with durable attempt"
            )

    return tuple(
        sorted(
            by_exec_id.values(),
            key=lambda report: (_time(report.transact_time, "transact_time"), report.exec_id),
        )
    )


def _build_provider_order_correlation_authority():
    """Closure-own durable identity resolution and positive provider-OrderID admission."""

    identity_resolver = load_identity
    identity_resolver_code = getattr(identity_resolver, "__code__", None)
    durable_attempt_facts = _durable_attempt_facts
    identity_init = ProphetXOrderIdentity.__init__
    identity_post_init = ProphetXOrderIdentity.__post_init__
    identity_eq = ProphetXOrderIdentity.__eq__
    identity_class_methods = (
        ("__init__", identity_init, getattr(identity_init, "__code__", None)),
        (
            "__post_init__",
            identity_post_init,
            getattr(identity_post_init, "__code__", None),
        ),
        ("__eq__", identity_eq, getattr(identity_eq, "__code__", None)),
    )
    identity_eq_code = getattr(identity_eq, "__code__", None)
    bind_impl = _bind_provider_assigned_order_id_impl
    reconciliation_impl = _reconciliation_disposition_impl
    normalize_impl = _normalize_fix_execution_reports_impl

    module_globals = globals()
    dependency_functions = (identity_resolver, durable_attempt_facts)
    dependency_names = tuple(
        sorted(
            {
                global_name
                for helper in dependency_functions
                for global_name in helper.__code__.co_names
                if global_name in module_globals
            }
        )
    )
    dependency_graph = tuple(
        (
            dependency_name,
            module_globals[dependency_name],
            getattr(module_globals[dependency_name], "__code__", None),
        )
        for dependency_name in dependency_names
    )
    profile_map = _PROFILE_BY_VERSION
    profile_entries = tuple(
        (
            version,
            profile,
            profile.bookmaker_profile_version,
            profile.environment,
            profile.transport,
            profile.production_write_qualified,
        )
        for version, profile in sorted(profile_map.items())
    )
    json_module = json
    json_loads = json.loads
    json_loads_code = getattr(json_loads, "__code__", None)

    def require_identity_resolver_graph() -> None:
        for (
            dependency_name,
            expected_dependency,
            expected_dependency_code,
        ) in dependency_graph:
            live_dependency = module_globals.get(dependency_name)
            if (
                live_dependency is not expected_dependency
                or getattr(live_dependency, "__code__", None)
                is not expected_dependency_code
            ):
                raise ProphetXOrderIdentityError(
                    "canonical ProphetX identity transitive dependency changed"
                )
        if (
            module_globals.get("json") is not json_module
            or getattr(json_module, "loads", None) is not json_loads
            or getattr(json_loads, "__code__", None) is not json_loads_code
        ):
            raise ProphetXOrderIdentityError(
                "canonical ProphetX identity transitive dependency changed"
            )
        if (
            module_globals.get("_PROFILE_BY_VERSION") is not profile_map
            or len(profile_map) != len(profile_entries)
            or any(
                version not in profile_map
                or profile_map[version] is not expected_profile
                or expected_profile.bookmaker_profile_version != expected_version
                or expected_profile.environment is not expected_environment
                or expected_profile.transport is not expected_transport
                or expected_profile.production_write_qualified
                is not expected_production_write_qualified
                for (
                    version,
                    expected_profile,
                    expected_version,
                    expected_environment,
                    expected_transport,
                    expected_production_write_qualified,
                ) in profile_entries
            )
        ):
            raise ProphetXOrderIdentityError(
                "canonical ProphetX identity profile authority changed"
            )
        for method_name, expected_method, expected_code in identity_class_methods:
            live_method = getattr(ProphetXOrderIdentity, method_name, None)
            if (
                live_method is not expected_method
                or getattr(live_method, "__code__", None) is not expected_code
            ):
                raise ProphetXOrderIdentityError(
                    "canonical ProphetX identity class dispatch changed"
                )

    def bind_provider_order_id(
        ledger: RealExecutionLedger,
        identity: ProphetXOrderIdentity,
        provider_order_id: str,
    ) -> bool:
        try:
            require_identity_resolver_graph()
        except ProphetXOrderIdentityError:
            return False
        result = bind_impl(
            ledger,
            identity,
            provider_order_id,
            identity_resolver=identity_resolver,
            identity_resolver_code=identity_resolver_code,
            identity_eq=identity_eq,
            identity_eq_code=identity_eq_code,
        )
        try:
            require_identity_resolver_graph()
        except ProphetXOrderIdentityError:
            return False
        return result

    def reconciliation_disposition(
        identity: ProphetXOrderIdentity,
        evidence: ProphetXProviderEvidence,
        *,
        ledger: RealExecutionLedger | None = None,
    ) -> ProphetXReconciliationDisposition:
        return reconciliation_impl(
            identity,
            evidence,
            ledger=ledger,
            bind_provider_order_id=bind_provider_order_id,
        )

    def normalize_fix_execution_reports(
        identity: ProphetXOrderIdentity,
        reports: Iterable[ProphetXFixExecutionReport],
        *,
        ledger: RealExecutionLedger | None = None,
    ) -> tuple[ProphetXFixExecutionReport, ...]:
        return normalize_impl(
            identity,
            reports,
            ledger=ledger,
            bind_provider_order_id=bind_provider_order_id,
        )

    return reconciliation_disposition, normalize_fix_execution_reports


reconciliation_disposition, normalize_fix_execution_reports = (
    _build_provider_order_correlation_authority()
)
del _bind_provider_assigned_order_id_impl
del _reconciliation_disposition_impl
del _normalize_fix_execution_reports_impl
del _build_provider_order_correlation_authority

