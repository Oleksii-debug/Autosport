from __future__ import annotations

import hashlib
import json
import threading
import weakref
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum
from typing import Any

from .real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    ExecutionAttemptReadView,
    RealExecutionLedger,
    VerifiedExecutionPlanView,
)


class ExecutionCapitalAtRiskError(RuntimeError):
    """Base error for conservative real-execution capital-at-risk derivation."""


class ExecutionCapitalAtRiskUnsupported(ExecutionCapitalAtRiskError):
    """The durable execution facts do not bound monetary liability exactly enough."""


class ExecutionCapitalAtRiskStale(ExecutionCapitalAtRiskError):
    """The ledger moved after the evidence snapshot was derived."""


class CapitalRiskTruth(str, Enum):
    CONSERVATIVE_BOUND = "CONSERVATIVE_BOUND"


@dataclass(frozen=True, slots=True)
class AttemptCapitalAtRisk:
    attempt_id: str
    action_id: str
    state: AttemptState
    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    requested_stake: Decimal
    requested_odds: Decimal
    requested_capital_at_limit: Decimal
    confirmed_open_capital: Decimal
    contingent_unknown_capital: Decimal
    confirmed_released_capital: Decimal
    max_plausible_capital_at_risk: Decimal

    @property
    def truth(self) -> CapitalRiskTruth:
        return CapitalRiskTruth.CONSERVATIVE_BOUND


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ExecutionCapitalAtRiskEvidence:
    """Conservative exposure facts from one immutable verified ledger snapshot.

    This object never grants execution authority and never treats generic ledger
    REJECTED/RECONCILED_NOT_FOUND facts as provider-origin capital-release proof.
    """

    snapshot_sha256: str
    event_count: int
    plan_id: str
    plan_fingerprint: str
    plan_stale: bool
    attempts: tuple[AttemptCapitalAtRisk, ...]
    confirmed_open_capital: Decimal
    contingent_unknown_capital: Decimal
    confirmed_released_capital: Decimal
    max_plausible_capital_at_risk: Decimal
    evidence_sha256: str
    execution_authority: bool = False
    capital_release_authority: bool = False
    residual_capacity_authority: bool = False

    @property
    def truth(self) -> CapitalRiskTruth:
        return CapitalRiskTruth.CONSERVATIVE_BOUND

    def assert_issued_current(self, ledger: RealExecutionLedger) -> None:
        """Prove canonical in-process issuance and unchanged durable ledger bytes.

        This is deliberately weaker than provider-origin or execution authority.
        It only proves that this exact object came from the canonical resolver in
        this process and still names the current verified ledger snapshot.
        """

        if type(ledger) is not RealExecutionLedger:
            raise ExecutionCapitalAtRiskError(
                "ledger must be exact RealExecutionLedger"
            )
        with _ISSUED_LOCK:
            issued = _ISSUED.get(id(self))
            if (
                issued is None
                or issued[0]() is not self
                or issued[1] != self.evidence_sha256
            ):
                raise ExecutionCapitalAtRiskError(
                    "capital-at-risk evidence was not canonically issued"
                )
        if _evidence_digest(self) != self.evidence_sha256:
            raise ExecutionCapitalAtRiskError(
                "capital-at-risk evidence identity is invalid"
            )
        snapshot = _VERIFIED_SNAPSHOT(ledger)
        if (
            snapshot.sha256 != self.snapshot_sha256
            or snapshot.event_count != self.event_count
        ):
            raise ExecutionCapitalAtRiskStale(
                "execution ledger changed after capital-at-risk resolution"
            )


_VERIFIED_EXECUTION_VIEW = RealExecutionLedger.verified_execution_view
_VERIFIED_SNAPSHOT = RealExecutionLedger.verified_snapshot
_ISSUED_LOCK = threading.RLock()
_ISSUED: dict[
    int,
    tuple[
        weakref.ReferenceType[ExecutionCapitalAtRiskEvidence],
        str,
    ],
] = {}


_MAX_EXACT_PRECISION = 4096


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ExecutionCapitalAtRiskError("capital value must be a finite Decimal")
    if value.is_zero():
        return "0"
    sign, raw_digits, raw_exponent = value.as_tuple()
    digits = list(raw_digits)
    exponent = int(raw_exponent)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits) or "0"
    prefix = "-" if sign else ""
    return f"{prefix}{coefficient}e{exponent}"


def _bounded_precision(required: int) -> int:
    precision = max(64, required)
    if precision > _MAX_EXACT_PRECISION:
        raise ExecutionCapitalAtRiskUnsupported(
            "execution Decimal scale exceeds exact risk-arithmetic bound"
        )
    return precision


def _add_precision(*values: Decimal) -> int:
    exponents = [int(value.as_tuple().exponent) for value in values]
    minimum_exponent = min(exponents)
    aligned_widths = [
        max(1, len(value.as_tuple().digits))
        + int(value.as_tuple().exponent)
        - minimum_exponent
        for value in values
    ]
    return _bounded_precision(max(aligned_widths) + 2)


def _add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _add_precision(left, right)
        return left + right


def _subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _add_precision(left, right)
        return left - right


def _subtract_nonnegative(left: Decimal, right: Decimal) -> Decimal:
    result = _subtract(left, right)
    if result < 0:
        return Decimal(0)
    return result


def _requested_limit_capital(attempt: ExecutionAttemptReadView) -> Decimal:
    action = attempt.action
    if action.bookmaker_id != "betfair":
        raise ExecutionCapitalAtRiskUnsupported(
            "generic provider stake is not a universal capital-at-risk unit"
        )
    if action.side != "BACK":
        raise ExecutionCapitalAtRiskUnsupported(
            "this conservative floor supports Betfair BACK only; "
            "provider-specific liability must come from canonical order economics"
        )
    return action.requested_stake


def _accepted_capital(attempt: ExecutionAttemptReadView) -> Decimal:
    acknowledgement = attempt.acknowledgement
    if acknowledgement is None:
        raise ExecutionCapitalAtRiskError(
            "accepted/PARTIAL state lacks durable acknowledgement"
        )
    if acknowledgement.status not in {
        AcknowledgementStatus.ACCEPTED,
        AcknowledgementStatus.PARTIAL,
    }:
        raise ExecutionCapitalAtRiskError(
            "accepted/PARTIAL state conflicts with acknowledgement status"
        )
    if (
        acknowledgement.accepted_stake is None
        or acknowledgement.accepted_odds is None
    ):
        raise ExecutionCapitalAtRiskError(
            "accepted/PARTIAL acknowledgement lacks exact stake/odds"
        )
    if attempt.action.side != "BACK":
        raise ExecutionCapitalAtRiskUnsupported(
            "accepted provider liability is unsupported outside Betfair BACK"
        )
    return acknowledgement.accepted_stake


def _attempt_risk(attempt: ExecutionAttemptReadView) -> AttemptCapitalAtRisk:
    action = attempt.action
    requested = _requested_limit_capital(attempt)
    zero = Decimal(0)

    if attempt.state is AttemptState.RESERVED:
        confirmed = zero
        contingent = zero
        maximum = zero
    elif attempt.state in {AttemptState.SUBMITTED, AttemptState.UNKNOWN}:
        confirmed = zero
        # For the supported BACK seam, monetary commitment equals stake and is
        # independent of matched odds. UNKNOWN retains the full requested stake.
        contingent = requested
        maximum = requested
    elif attempt.state in {AttemptState.ACCEPTED, AttemptState.PARTIAL}:
        confirmed = _accepted_capital(attempt)
        acknowledgement = attempt.acknowledgement
        assert acknowledgement is not None
        assert acknowledgement.accepted_stake is not None
        contingent = _subtract_nonnegative(
            action.requested_stake,
            acknowledgement.accepted_stake,
        )
        maximum = _add(confirmed, contingent)
    elif attempt.state in {
        AttemptState.REJECTED,
        AttemptState.RECONCILED_NOT_FOUND,
    }:
        confirmed = zero
        # These are durable ledger facts, not provider-origin release authority.
        # Retain the full possible BACK effect rather than freeing capital.
        contingent = requested
        maximum = requested
    else:  # pragma: no cover - protects future enum widening
        raise ExecutionCapitalAtRiskUnsupported(
            f"unsupported durable attempt state: {attempt.state!r}"
        )

    return AttemptCapitalAtRisk(
        attempt_id=attempt.attempt.attempt_id,
        action_id=action.action_id,
        state=attempt.state,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        side=action.side,
        requested_stake=action.requested_stake,
        requested_odds=action.requested_odds,
        requested_capital_at_limit=requested,
        confirmed_open_capital=confirmed,
        contingent_unknown_capital=contingent,
        confirmed_released_capital=zero,
        max_plausible_capital_at_risk=maximum,
    )


def _sum_capital(values: tuple[Decimal, ...]) -> Decimal:
    total = Decimal(0)
    for value in values:
        total = _add(total, value)
    return total


def _attempt_payload(value: AttemptCapitalAtRisk) -> dict[str, Any]:
    return {
        "attempt_id": value.attempt_id,
        "action_id": value.action_id,
        "state": value.state.value,
        "bookmaker_id": value.bookmaker_id,
        "account_id": value.account_id,
        "event_id": value.event_id,
        "market_id": value.market_id,
        "selection_id": value.selection_id,
        "side": value.side,
        "requested_stake": _decimal_text(value.requested_stake),
        "requested_odds": _decimal_text(value.requested_odds),
        "requested_capital_at_limit": _decimal_text(
            value.requested_capital_at_limit
        ),
        "confirmed_open_capital": _decimal_text(
            value.confirmed_open_capital
        ),
        "contingent_unknown_capital": _decimal_text(
            value.contingent_unknown_capital
        ),
        "confirmed_released_capital": _decimal_text(
            value.confirmed_released_capital
        ),
        "max_plausible_capital_at_risk": _decimal_text(
            value.max_plausible_capital_at_risk
        ),
    }


def _evidence_payload(value: ExecutionCapitalAtRiskEvidence) -> dict[str, Any]:
    return {
        "schema": "autosport.execution_capital_at_risk",
        "schema_version": 1,
        "snapshot_sha256": value.snapshot_sha256,
        "event_count": value.event_count,
        "plan_id": value.plan_id,
        "plan_fingerprint": value.plan_fingerprint,
        "plan_stale": value.plan_stale,
        "attempts": [_attempt_payload(item) for item in value.attempts],
        "confirmed_open_capital": _decimal_text(value.confirmed_open_capital),
        "contingent_unknown_capital": _decimal_text(
            value.contingent_unknown_capital
        ),
        "confirmed_released_capital": _decimal_text(
            value.confirmed_released_capital
        ),
        "max_plausible_capital_at_risk": _decimal_text(
            value.max_plausible_capital_at_risk
        ),
        "execution_authority": value.execution_authority,
        "capital_release_authority": value.capital_release_authority,
        "residual_capacity_authority": value.residual_capacity_authority,
    }


def _evidence_digest(value: ExecutionCapitalAtRiskEvidence) -> str:
    encoded = json.dumps(
        _evidence_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _issue(value: ExecutionCapitalAtRiskEvidence) -> None:
    key = id(value)

    def cleanup(reference: weakref.ReferenceType[ExecutionCapitalAtRiskEvidence]) -> None:
        with _ISSUED_LOCK:
            current = _ISSUED.get(key)
            if current is not None and current[0] is reference:
                _ISSUED.pop(key, None)

    reference = weakref.ref(value, cleanup)
    with _ISSUED_LOCK:
        _ISSUED[key] = (reference, value.evidence_sha256)


def resolve_execution_capital_at_risk(
    ledger: RealExecutionLedger,
    plan_id: str,
) -> ExecutionCapitalAtRiskEvidence:
    """Derive conservative monetary exposure from one verified execution snapshot."""

    if type(ledger) is not RealExecutionLedger:
        raise TypeError("ledger must be exact RealExecutionLedger")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise ValueError("plan_id must be non-empty text")

    view: VerifiedExecutionPlanView = _VERIFIED_EXECUTION_VIEW(ledger, plan_id)
    attempts = tuple(_attempt_risk(item) for item in view.attempts)

    confirmed = _sum_capital(
        tuple(item.confirmed_open_capital for item in attempts)
    )
    contingent = _sum_capital(
        tuple(item.contingent_unknown_capital for item in attempts)
    )
    released = Decimal(0)
    maximum = _sum_capital(
        tuple(item.max_plausible_capital_at_risk for item in attempts)
    )

    provisional = ExecutionCapitalAtRiskEvidence(
        snapshot_sha256=view.snapshot_sha256,
        event_count=view.event_count,
        plan_id=view.plan.plan_id,
        plan_fingerprint=view.plan_fingerprint,
        plan_stale=view.stale,
        attempts=attempts,
        confirmed_open_capital=confirmed,
        contingent_unknown_capital=contingent,
        confirmed_released_capital=released,
        max_plausible_capital_at_risk=maximum,
        evidence_sha256="0" * 64,
    )
    evidence = ExecutionCapitalAtRiskEvidence(
        snapshot_sha256=provisional.snapshot_sha256,
        event_count=provisional.event_count,
        plan_id=provisional.plan_id,
        plan_fingerprint=provisional.plan_fingerprint,
        plan_stale=provisional.plan_stale,
        attempts=provisional.attempts,
        confirmed_open_capital=provisional.confirmed_open_capital,
        contingent_unknown_capital=provisional.contingent_unknown_capital,
        confirmed_released_capital=provisional.confirmed_released_capital,
        max_plausible_capital_at_risk=provisional.max_plausible_capital_at_risk,
        evidence_sha256=_evidence_digest(provisional),
    )

    after = _VERIFIED_SNAPSHOT(ledger)
    if (
        after.sha256 != view.snapshot_sha256
        or after.event_count != view.event_count
    ):
        raise ExecutionCapitalAtRiskStale(
            "execution ledger changed during capital-at-risk resolution"
        )
    _issue(evidence)
    return evidence
