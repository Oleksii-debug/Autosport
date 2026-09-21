"""Betfair ambiguous-placement-timeout readback resolution.

Betfair documents that an order can remain invisible for a short period after a
`placeOrders` TIMEOUT.  Complete current+cleared readback is therefore not, by
itself, enough to issue NOT_FOUND immediately.  This module layers the provider
visibility horizon over the existing sealed #530 Betfair readback authority.

The underlying verifier already requires exact action/account/customerOrderRef
scope, complete current pagination, and complete BET-level cleared pagination for
SETTLED, VOIDED, LAPSED, and CANCELLED.  This resolver adds the missing temporal
condition without creating another provider client or execution ledger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .betfair_account_readonly import BetfairExecutionReadbackEnvelope
from .bookmaker_capability import BookmakerCapabilityProfile
from .real_execution_ledger import ExecutionAction
from .supervised_provider_evidence import (
    VerifiedProviderAbsenceEvidence,
    VerifiedProviderEffectEvidence,
    VerifiedProviderState,
    verify_betfair_provider_state,
)

BETFAIR_TIMEOUT_VISIBILITY_HORIZON_SECONDS = 15


class BetfairTimeoutResolutionError(RuntimeError):
    """Raised when timeout/readback timing cannot safely resolve provider state."""


class BetfairTimeoutResolutionKind(str, Enum):
    EFFECT_PRESENT = "effect_present"
    INDETERMINATE_BEFORE_VISIBILITY_HORIZON = (
        "indeterminate_before_visibility_horizon"
    )
    ABSENT_AFTER_VISIBILITY_HORIZON = "absent_after_visibility_horizon"


@dataclass(frozen=True, slots=True)
class BetfairTimeoutResolution:
    kind: BetfairTimeoutResolutionKind
    timeout_at: str
    observed_at: str
    visibility_deadline: str
    evidence: VerifiedProviderState | None

    @property
    def definitive(self) -> bool:
        return self.kind is not BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON


def _time(value: str, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairTimeoutResolutionError(f"{name} must be non-empty canonical text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairTimeoutResolutionError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairTimeoutResolutionError(f"{name} must be timezone-aware")
    return parsed


def _provider_order_ref(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BetfairTimeoutResolutionError(
            "expected_provider_order_ref must be <=32 lowercase hex characters"
        )
    return value


def resolve_betfair_timeout_provider_state(
    action: ExecutionAction,
    profile: BookmakerCapabilityProfile,
    *,
    expected_profile_sha256: str,
    readback: BetfairExecutionReadbackEnvelope,
    expected_provider_order_ref: str,
    timeout_at: str,
) -> BetfairTimeoutResolution:
    """Resolve an ambiguous Betfair placement timeout without premature NOT_FOUND.

    Positive canonical evidence wins immediately.  Canonical complete-empty evidence
    remains indeterminate until the fixed Betfair visibility horizon has elapsed.
    The exact boundary is inclusive: an observation at timeout+15s may issue absence;
    any earlier observation may not.

    Provider/readback incompleteness or identity conflicts continue to fail closed in
    ``verify_betfair_provider_state`` and can never be converted here into absence.
    """

    provider_order_ref = _provider_order_ref(expected_provider_order_ref)
    timeout = _time(timeout_at, "timeout_at")
    evidence = verify_betfair_provider_state(
        action,
        profile,
        expected_profile_sha256=expected_profile_sha256,
        readback=readback,
        expected_provider_order_ref=provider_order_ref,
    )
    observed = _time(evidence.observed_at, "provider observed_at")
    if observed < timeout:
        raise BetfairTimeoutResolutionError(
            "provider readback predates ambiguous placement timeout"
        )

    deadline = timeout + timedelta(seconds=BETFAIR_TIMEOUT_VISIBILITY_HORIZON_SECONDS)
    deadline_raw = deadline.isoformat()

    if isinstance(evidence, VerifiedProviderEffectEvidence):
        return BetfairTimeoutResolution(
            BetfairTimeoutResolutionKind.EFFECT_PRESENT,
            timeout_at,
            evidence.observed_at,
            deadline_raw,
            evidence,
        )
    if not isinstance(evidence, VerifiedProviderAbsenceEvidence):
        raise BetfairTimeoutResolutionError("provider verifier returned non-canonical state")

    if observed < deadline:
        # Do not leak the verifier-issued absence capability before the provider's
        # documented visibility window closes.  Downstream reconciliation therefore
        # has no object it could use to release UNKNOWN/retry early.
        return BetfairTimeoutResolution(
            BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON,
            timeout_at,
            evidence.observed_at,
            deadline_raw,
            None,
        )

    return BetfairTimeoutResolution(
        BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON,
        timeout_at,
        evidence.observed_at,
        deadline_raw,
        evidence,
    )
