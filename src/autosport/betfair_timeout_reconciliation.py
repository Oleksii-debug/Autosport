"""Betfair ambiguous-placement-timeout readback resolution.

Complete current+cleared readback is not, by itself, enough to issue NOT_FOUND
immediately after a `placeOrders` ambiguity. Betfair allows up to 15 seconds for a
timed-out order to become visible. This module composes that provider horizon with
the existing sealed #530 Betfair readback authority and the durable execution ledger.

The timeout boundary is never accepted from a caller. It is derived from the
ledger-verified ATTEMPT_UNKNOWN event recorded by the canonical Betfair execution
path, and the provider order reference is reloaded from the same durable attempt.

Definitive absence is also an in-process capability. A complete-empty provider
capture is not enough: the exact absence object must have passed this resolver after
the durable visibility deadline before generic execution reconciliation may consume it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import json
from weakref import ref

from .betfair_account_readonly import BetfairExecutionReadbackEnvelope
from .bookmaker_capability import BookmakerCapabilityProfile
from .real_execution_ledger import AttemptState, ExecutionAction, RealExecutionLedger
from .supervised_provider_evidence import (
    VerifiedProviderAbsenceEvidence,
    VerifiedProviderEffectEvidence,
    VerifiedProviderState,
    verify_betfair_provider_state,
)

BETFAIR_TIMEOUT_VISIBILITY_HORIZON_SECONDS = 15
_BETFAIR_AMBIGUOUS_UNKNOWN_REASON = (
    "betfair_placeOrders_ambiguous_effect_requires_readback"
)


class BetfairTimeoutResolutionError(RuntimeError):
    """Raised when timeout/readback evidence cannot safely resolve provider state."""


class BetfairTimeoutResolutionKind(str, Enum):
    EFFECT_PRESENT = "effect_present"
    INDETERMINATE_BEFORE_VISIBILITY_HORIZON = (
        "indeterminate_before_visibility_horizon"
    )
    ABSENT_AFTER_VISIBILITY_HORIZON = "absent_after_visibility_horizon"


@dataclass(frozen=True, slots=True)
class BetfairTimeoutResolution:
    kind: BetfairTimeoutResolutionKind
    timeout_boundary_at: str
    observed_at: str
    visibility_deadline: str
    ledger_snapshot_sha256: str
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


def _durable_timeout_authority(
    ledger: RealExecutionLedger,
    action: ExecutionAction,
    attempt_id: str,
) -> tuple[str, str, str]:
    """Return durable provider ref, conservative timeout boundary, ledger SHA.

    `recorded_at` is deliberately used instead of the ATTEMPT_UNKNOWN payload's
    caller-supplied `observed_at`. The ledger writes `recorded_at` itself while
    appending the durable event; using that later boundary can only delay absence.
    """

    if type(ledger) is not RealExecutionLedger:
        raise BetfairTimeoutResolutionError("ledger must be exact RealExecutionLedger")
    if type(action) is not ExecutionAction:
        raise BetfairTimeoutResolutionError("action must be exact ExecutionAction")
    if type(attempt_id) is not str or not attempt_id or attempt_id != attempt_id.strip():
        raise BetfairTimeoutResolutionError("attempt_id must be non-empty canonical text")
    try:
        state = ledger.attempt_state(attempt_id)
    except KeyError as exc:
        raise BetfairTimeoutResolutionError("timeout attempt is not durable") from exc
    if state is not AttemptState.UNKNOWN:
        raise BetfairTimeoutResolutionError(
            "timeout resolution requires a durable UNKNOWN attempt"
        )
    provider_order_ref = ledger.provider_order_reference(
        attempt_id=attempt_id,
        provider_id=action.bookmaker_id,
    )
    if provider_order_ref is None:
        raise BetfairTimeoutResolutionError(
            "timeout attempt lacks durable provider order reference"
        )

    snapshot = ledger.verified_snapshot()
    unknown_events: list[dict[str, object]] = []
    reserved_events: list[dict[str, object]] = []
    submitted_events: list[dict[str, object]] = []
    try:
        for raw_line in snapshot.payload.splitlines():
            envelope = json.loads(raw_line.decode("utf-8"))
            event = envelope["event"]
            if event.get("attempt_id") != attempt_id:
                continue
            event_type = event.get("event_type")
            if event_type == "ATTEMPT_RESERVED":
                reserved_events.append(event)
            elif event_type == "ATTEMPT_SUBMITTED":
                submitted_events.append(event)
            elif event_type == "ATTEMPT_UNKNOWN":
                unknown_events.append(event)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, AttributeError) as exc:
        raise BetfairTimeoutResolutionError(
            "verified execution ledger snapshot cannot be decoded"
        ) from exc

    if len(reserved_events) != 1 or reserved_events[0].get("action_id") != action.action_id:
        raise BetfairTimeoutResolutionError(
            "timeout attempt does not bind the exact execution action"
        )
    if len(submitted_events) != 1:
        raise BetfairTimeoutResolutionError(
            "timeout authority requires one durable provider submission boundary"
        )
    if len(unknown_events) != 1:
        raise BetfairTimeoutResolutionError(
            "timeout attempt lacks one canonical uncertainty boundary"
        )
    unknown = unknown_events[0]
    payload = unknown.get("payload")
    if not isinstance(payload, dict) or payload.get("reason") != _BETFAIR_AMBIGUOUS_UNKNOWN_REASON:
        raise BetfairTimeoutResolutionError(
            "UNKNOWN attempt is not a canonical ambiguous Betfair placeOrders timeout"
        )
    recorded_at = unknown.get("recorded_at")
    if not isinstance(recorded_at, str):
        raise BetfairTimeoutResolutionError(
            "durable timeout boundary lacks ledger recorded_at"
        )
    _time(recorded_at, "durable timeout recorded_at")
    return provider_order_ref, recorded_at, snapshot.sha256


def resolve_betfair_timeout_provider_state(
    ledger: RealExecutionLedger,
    action: ExecutionAction,
    profile: BookmakerCapabilityProfile,
    *,
    attempt_id: str,
    expected_profile_sha256: str,
    readback: BetfairExecutionReadbackEnvelope,
) -> BetfairTimeoutResolution:
    """Resolve one ambiguous Betfair placement without premature NOT_FOUND.

    Positive canonical evidence wins immediately. Canonical complete-empty evidence
    remains indeterminate until the fixed Betfair visibility horizon has elapsed.
    The exact boundary is inclusive: an observation at durable-boundary+15s may issue
    absence; any earlier observation may not.

    Provider/readback incompleteness, identity conflicts, wrong cleared-status
    coverage, or stale evidence continue to fail closed in the canonical verifier.
    """

    provider_order_ref, timeout_boundary_at, ledger_sha = _durable_timeout_authority(
        ledger, action, attempt_id
    )
    timeout_boundary = _time(timeout_boundary_at, "durable timeout recorded_at")
    evidence = verify_betfair_provider_state(
        action,
        profile,
        expected_profile_sha256=expected_profile_sha256,
        readback=readback,
        expected_provider_order_ref=provider_order_ref,
    )
    observed = _time(evidence.observed_at, "provider observed_at")
    if observed < timeout_boundary:
        raise BetfairTimeoutResolutionError(
            "provider readback predates durable ambiguous placement boundary"
        )

    deadline = timeout_boundary + timedelta(
        seconds=BETFAIR_TIMEOUT_VISIBILITY_HORIZON_SECONDS
    )
    deadline_raw = deadline.isoformat()

    if isinstance(evidence, VerifiedProviderEffectEvidence):
        return BetfairTimeoutResolution(
            BetfairTimeoutResolutionKind.EFFECT_PRESENT,
            timeout_boundary_at,
            evidence.observed_at,
            deadline_raw,
            ledger_sha,
            evidence,
        )
    if not isinstance(evidence, VerifiedProviderAbsenceEvidence):
        raise BetfairTimeoutResolutionError("provider verifier returned non-canonical state")

    if observed < deadline:
        return BetfairTimeoutResolution(
            BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON,
            timeout_boundary_at,
            evidence.observed_at,
            deadline_raw,
            ledger_sha,
            None,
        )

    return BetfairTimeoutResolution(
        BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON,
        timeout_boundary_at,
        evidence.observed_at,
        deadline_raw,
        ledger_sha,
        evidence,
    )


# A raw complete-empty Betfair capture is not retry authority. The exact absence
# object is separately sealed only when the durable timeout resolver has observed it
# at/after the provider visibility deadline. The private registry is intentionally a
# closure so callers cannot mint the second capability by constructing a dataclass or
# replaying a hash/timestamp.
def _install_betfair_timeout_absence_authority() -> None:
    issued: dict[int, object] = {}
    raw_resolve = resolve_betfair_timeout_provider_state

    def authoritative_resolve(
        ledger: RealExecutionLedger,
        action: ExecutionAction,
        profile: BookmakerCapabilityProfile,
        *,
        attempt_id: str,
        expected_profile_sha256: str,
        readback: BetfairExecutionReadbackEnvelope,
    ) -> BetfairTimeoutResolution:
        result = raw_resolve(
            ledger,
            action,
            profile,
            attempt_id=attempt_id,
            expected_profile_sha256=expected_profile_sha256,
            readback=readback,
        )
        evidence = result.evidence
        if (
            result.kind is BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON
            and isinstance(evidence, VerifiedProviderAbsenceEvidence)
        ):
            evidence_key = id(evidence)

            def forget(_weakref: object, *, key: int = evidence_key) -> None:
                issued.pop(key, None)

            issued[evidence_key] = ref(evidence, forget)
        return result

    def assert_betfair_timeout_absence_authoritative(
        evidence: VerifiedProviderAbsenceEvidence,
    ) -> None:
        if not isinstance(evidence, VerifiedProviderAbsenceEvidence):
            raise BetfairTimeoutResolutionError(
                "timeout absence evidence type is not canonical"
            )
        record = issued.get(id(evidence))
        if record is None or record() is not evidence:
            raise BetfairTimeoutResolutionError(
                "provider absence did not pass durable Betfair timeout visibility authority"
            )

    globals()["resolve_betfair_timeout_provider_state"] = authoritative_resolve
    globals()[
        "assert_betfair_timeout_absence_authoritative"
    ] = assert_betfair_timeout_absence_authoritative


_install_betfair_timeout_absence_authority()
del _install_betfair_timeout_absence_authority
