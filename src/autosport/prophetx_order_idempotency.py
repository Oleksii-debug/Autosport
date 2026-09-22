"""ProphetX order identity and ambiguity policy over the canonical execution ledger.

This module deliberately has no provider transport or write capability.  It binds ProphetX
client-order identity to the already-durable RealExecutionLedger attempt authority and
provides transport-specific, fail-closed policy for ambiguous delivery and provider evidence.

REST Direct Link duplicate suppression is *not* treated as replay/return semantics.
FIX same-ClOrdID redelivery is a separately qualified capability and never leaks into REST.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable

from .real_execution_ledger import AttemptState, RealExecutionLedger


PROPHETX_PROVIDER_ID = "prophetx"


class ProphetXOrderIdentityError(RuntimeError):
    """Raised when a durable ProphetX identity cannot be proven safely."""


class ProphetXEvidenceConflict(ProphetXOrderIdentityError):
    """Raised when provider evidence conflicts under a supposedly stable identity."""


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


# Exact durable ExecutionPlan.bookmaker_profile_version values. A writer must choose one
# before reserving an attempt; the adapter infers transport/environment from this durable
# plan fact on every restart rather than accepting a caller-selected transport.
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
    profile.bookmaker_profile_version: profile
    for profile in (
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


REST_DIRECT_LINK_CAPABILITIES = ProphetXTransportCapabilities(
    duplicate_suppression_only=True,
    idempotent_redelivery_identical_economics=False,
    order_status_by_client_or_provider_id=False,
    authoritative_readback_by_external_id=False,
)
FIX_ORDER_ENTRY_CAPABILITIES = ProphetXTransportCapabilities(
    duplicate_suppression_only=False,
    idempotent_redelivery_identical_economics=True,
    order_status_by_client_or_provider_id=True,
    authoritative_readback_by_external_id=False,
)


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
        if len(self.client_order_id) > 32 or not self.client_order_id:
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
    SAME_CLIENT_ID_REDELIVERY_ALLOWED = "SAME_CLIENT_ID_REDELIVERY_ALLOWED"
    SAME_CLIENT_ID_RETRY_AFTER_BACKOFF = "SAME_CLIENT_ID_RETRY_AFTER_BACKOFF"
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
        for field_name in (
            "exec_id",
            "client_order_id",
            "provider_order_id",
            "lifecycle_state",
            "cumulative_filled_quantity",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty text")
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
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be timezone-aware ISO-8601 text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be timezone-aware ISO-8601 text") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware ISO-8601 text")
    return parsed


def _durable_attempt_facts(
    ledger: RealExecutionLedger, attempt_id: str
) -> tuple[str, str]:
    """Return (bookmaker_profile_version, effect_fingerprint) from verified ledger bytes."""

    snapshot = ledger.verified_snapshot()
    try:
        envelopes = [
            json.loads(line)
            for line in snapshot.payload.decode("utf-8").splitlines()
            if line
        ]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # verified_snapshot() has already validated the bytes. Reparse failure is therefore
        # an adapter incompatibility, never permission to mint a new provider identity.
        raise ProphetXOrderIdentityError(
            "verified execution snapshot could not be read by ProphetX adapter"
        ) from exc

    attempt_events = [
        envelope["event"]
        for envelope in envelopes
        if envelope["event"].get("attempt_id") == attempt_id
        and envelope["event"].get("event_type") == "ATTEMPT_RESERVED"
    ]
    if len(attempt_events) != 1:
        raise ProphetXOrderIdentityError(
            "ProphetX identity requires exactly one durable attempt reservation"
        )
    attempt = attempt_events[0]
    plan_id = attempt["plan_id"]
    effect_fingerprint = attempt["payload"].get("effect_fingerprint")
    try:
        _sha256(effect_fingerprint, "effect_fingerprint")
    except (TypeError, ValueError) as exc:
        raise ProphetXOrderIdentityError(
            "durable attempt effect fingerprint is invalid"
        ) from exc

    plan_events = [
        envelope["event"]
        for envelope in envelopes
        if envelope["event"].get("plan_id") == plan_id
        and envelope["event"].get("event_type") == "PLAN_RESERVED"
    ]
    if len(plan_events) != 1:
        raise ProphetXOrderIdentityError(
            "ProphetX identity requires exactly one durable execution plan"
        )
    version = (
        plan_events[0]
        .get("payload", {})
        .get("plan", {})
        .get("bookmaker_profile_version")
    )
    if not isinstance(version, str) or not version:
        raise ProphetXOrderIdentityError(
            "durable execution plan lacks bookmaker profile version"
        )
    return version, effect_fingerprint


def _profile_for_attempt(
    ledger: RealExecutionLedger, attempt_id: str
) -> tuple[ProphetXProfile, str]:
    version, effect_fingerprint = _durable_attempt_facts(ledger, attempt_id)
    profile = _PROFILE_BY_VERSION.get(version)
    if profile is None:
        raise ProphetXOrderIdentityError(
            "attempt is not durably bound to a qualified ProphetX transport profile"
        )
    return profile, effect_fingerprint


def bind_before_effect(
    ledger: RealExecutionLedger, *, attempt_id: str
) -> ProphetXOrderIdentity:
    """Freeze the provider client id while the canonical attempt is still RESERVED."""

    if ledger.attempt_state(attempt_id) is not AttemptState.RESERVED:
        raise ProphetXOrderIdentityError(
            "ProphetX client order identity must be bound before external effect"
        )
    _profile_for_attempt(ledger, attempt_id)
    reference = ledger.bind_provider_order_reference(
        attempt_id=attempt_id,
        provider_id=PROPHETX_PROVIDER_ID,
    )
    return load_identity(ledger, attempt_id=attempt_id, expected_reference=reference)


def load_identity(
    ledger: RealExecutionLedger,
    *,
    attempt_id: str,
    expected_reference: str | None = None,
) -> ProphetXOrderIdentity:
    """Reload the same durable identity after timeout, disconnect or restart."""

    profile, effect_fingerprint = _profile_for_attempt(ledger, attempt_id)
    reference = ledger.provider_order_reference(
        attempt_id=attempt_id,
        provider_id=PROPHETX_PROVIDER_ID,
    )
    if reference is None:
        raise ProphetXOrderIdentityError(
            "attempt has no durable ProphetX client order identity"
        )
    if expected_reference is not None and reference != expected_reference:
        raise ProphetXEvidenceConflict(
            "durable ProphetX client order identity changed"
        )
    return ProphetXOrderIdentity(
        attempt_id=attempt_id,
        environment=profile.environment,
        transport=profile.transport,
        client_order_id=reference,
        effect_fingerprint=effect_fingerprint,
        production_write_qualified=profile.production_write_qualified,
    )


def mark_ambiguous_delivery(
    ledger: RealExecutionLedger,
    *,
    identity: ProphetXOrderIdentity,
    reason: str,
    observed_at: str,
) -> None:
    """Move an unresolved canonical attempt to UNKNOWN without creating a new id."""

    if load_identity(ledger, attempt_id=identity.attempt_id) != identity:
        raise ProphetXEvidenceConflict("ProphetX identity no longer matches durable attempt")
    ledger.mark_unknown(
        identity.attempt_id,
        reason=reason,
        observed_at=observed_at,
    )


def reconciliation_disposition(
    identity: ProphetXOrderIdentity,
    evidence: ProphetXProviderEvidence,
) -> ProphetXReconciliationDisposition:
    """Classify identity correlation without minting provider-origin authority.

    Positive/negative results are deliberately only *candidates*. A caller-constructed
    object here cannot satisfy canonical #1634/#530 provider-origin evidence requirements.
    """

    if evidence.transport is not identity.transport:
        return ProphetXReconciliationDisposition.CONFLICT
    if evidence.effect_fingerprint is not None and (
        evidence.effect_fingerprint != identity.effect_fingerprint
    ):
        return ProphetXReconciliationDisposition.CONFLICT
    if evidence.client_order_id is None:
        return ProphetXReconciliationDisposition.WAIT_EXTERNAL_EVIDENCE
    if evidence.client_order_id != identity.client_order_id:
        return ProphetXReconciliationDisposition.UNRELATED_PROVIDER_EVIDENCE

    if identity.transport is ProphetXTransport.REST_DIRECT_LINK:
        if evidence.kind in {
            ProphetXEvidenceKind.DUPLICATE_REJECTION,
            ProphetXEvidenceKind.WALLET_TRANSACTION,
            ProphetXEvidenceKind.FIX_ORDER_STATUS_UNKNOWN,
            ProphetXEvidenceKind.FIX_RATE_LIMIT_PREPROCESSING_REJECT,
        }:
            return ProphetXReconciliationDisposition.WAIT_EXTERNAL_EVIDENCE
        if (
            evidence.kind is ProphetXEvidenceKind.ORDER_STATE
            and evidence.provider_order_id
        ):
            return ProphetXReconciliationDisposition.MATCHED_PROVIDER_ORDER_CANDIDATE
        return ProphetXReconciliationDisposition.WAIT_EXTERNAL_EVIDENCE

    if evidence.kind is ProphetXEvidenceKind.FIX_ORDER_STATUS_UNKNOWN:
        return ProphetXReconciliationDisposition.PROVIDER_NOT_FOUND_CANDIDATE
    if evidence.kind is ProphetXEvidenceKind.FIX_EXECUTION_REPORT:
        if evidence.provider_order_id:
            return ProphetXReconciliationDisposition.MATCHED_PROVIDER_ORDER_CANDIDATE
        return ProphetXReconciliationDisposition.CONFLICT
    return ProphetXReconciliationDisposition.WAIT_EXTERNAL_EVIDENCE


def retry_disposition(
    identity: ProphetXOrderIdentity,
    *,
    candidate_effect_fingerprint: str,
    exact_provider_profile_qualified: bool,
    preprocessing_rate_limit_reject: bool = False,
) -> ProphetXRetryDisposition:
    """Return only a transport-policy decision; it never performs a retry."""

    _sha256(candidate_effect_fingerprint, "candidate_effect_fingerprint")
    if candidate_effect_fingerprint != identity.effect_fingerprint:
        return ProphetXRetryDisposition.LOCAL_REJECT_DIFFERENT_ECONOMICS
    if not exact_provider_profile_qualified:
        return ProphetXRetryDisposition.PROFILE_UNQUALIFIED
    if identity.transport is ProphetXTransport.REST_DIRECT_LINK:
        return ProphetXRetryDisposition.WAIT_EXTERNAL_EVIDENCE
    if preprocessing_rate_limit_reject:
        return ProphetXRetryDisposition.SAME_CLIENT_ID_RETRY_AFTER_BACKOFF
    return ProphetXRetryDisposition.SAME_CLIENT_ID_REDELIVERY_ALLOWED


def normalize_fix_execution_reports(
    identity: ProphetXOrderIdentity,
    reports: Iterable[ProphetXFixExecutionReport],
) -> tuple[ProphetXFixExecutionReport, ...]:
    """Deduplicate by ExecID and order reports by provider TransactTime, not arrival."""

    if identity.transport is not ProphetXTransport.FIX_ORDER_ENTRY:
        raise ProphetXOrderIdentityError(
            "FIX execution reports cannot be applied to a REST identity"
        )
    by_exec_id: dict[str, ProphetXFixExecutionReport] = {}
    for report in reports:
        if report.client_order_id != identity.client_order_id:
            raise ProphetXEvidenceConflict(
                "FIX report client order id mismatches durable attempt"
            )
        if report.effect_fingerprint != identity.effect_fingerprint:
            raise ProphetXEvidenceConflict(
                "FIX report economics mismatch durable attempt"
            )
        prior = by_exec_id.get(report.exec_id)
        if prior is not None and prior != report:
            raise ProphetXEvidenceConflict(
                "same FIX ExecID has conflicting provider evidence"
            )
        by_exec_id[report.exec_id] = report
    return tuple(
        sorted(
            by_exec_id.values(),
            key=lambda report: (
                _time(report.transact_time, "transact_time"),
                report.exec_id,
            ),
        )
    )
