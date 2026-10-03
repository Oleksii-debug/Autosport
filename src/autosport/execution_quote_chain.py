"""Fail-closed decision -> submit -> acknowledgement quote-chain projection.

This module is deliberately read-only. It projects facts from one verified
`RealExecutionLedger` snapshot and keeps the intended action, durable submit
boundary, provider-correlation evidence, and acknowledgement economics separate.

Current ledger schema v1 does not persist the identity/digest of the exact
serialized provider instruction in `ATTEMPT_SUBMITTED`. Therefore this
projection can never claim a complete decision -> actual-submit -> accepted
price chain yet. That negative fact is intentional product truth: downstream
slippage/economic/learning consumers must not substitute `ExecutionAction`
requested odds for the actual serialized instruction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .real_execution_ledger import (
    AttemptState,
    RealExecutionLedger,
    _decimal_text,
    _digest,
)


SCHEMA_VERSION = 1

CHAIN_NOT_SUBMITTED = "NOT_SUBMITTED"
CHAIN_SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
CHAIN_SUBMIT_INSTRUCTION_UNBOUND = "SUBMIT_INSTRUCTION_UNBOUND"

ACCEPTED_PRICE_NOT_APPLICABLE = "NOT_APPLICABLE"
ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED = "ACKNOWLEDGED_UNVERIFIED"
ACCEPTED_PRICE_UNKNOWN = "UNKNOWN"


class ExecutionQuoteChainError(RuntimeError):
    """Base error for quote-chain projection failures."""


class ExecutionQuoteChainUnavailable(ExecutionQuoteChainError):
    """Requested plan/attempt cannot be projected from canonical durable facts."""


@dataclass(frozen=True, slots=True)
class ExecutionQuoteChainEvidence:
    """One immutable, snapshot-bound quote-chain diagnostic.

    The class intentionally exposes no caller-settable `chain_complete` or
    `accepted_price_verified` field. With the current ledger schema both are
    properties that remain false. This object is evidence about a missing
    authority boundary, not a new execution or provider authority.
    """

    source_ledger_sha256: str
    source_event_count: int
    plan_id: str
    plan_fingerprint: str
    decision_id: str
    action_id: str
    attempt_id: str
    attempt_state: str
    effect_fingerprint: str

    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str

    decision_quote_id: str
    decision_quote_observed_at: str
    requested_odds: Decimal
    requested_stake: Decimal

    reserved_at: str
    submitted_at: str | None
    submission_instruction_sha256: None
    chain_status: str

    provider_order_ref: str | None
    provider_evidence_id: str | None
    provider_evidence_observed_at: str | None
    provider_evidence_source: str | None
    provider_acknowledgement_sha256: str | None
    acknowledgement_binding_matches: bool

    external_receipt_id: str | None
    acknowledgement_status: str | None
    acknowledged_at: str | None
    acknowledged_odds: Decimal | None
    acknowledged_stake: Decimal | None
    accepted_price_status: str

    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ExecutionQuoteChainError("unsupported quote-chain evidence schema")
        if type(self.source_event_count) is not int or self.source_event_count < 1:
            raise ExecutionQuoteChainError("source_event_count must be positive int")
        if type(self.acknowledgement_binding_matches) is not bool:
            raise ExecutionQuoteChainError(
                "acknowledgement_binding_matches must be bool"
            )
        if self.submission_instruction_sha256 is not None:
            raise ExecutionQuoteChainError(
                "current ledger schema cannot bind submitted instruction identity"
            )
        if self.submitted_at is not None:
            expected_chain_status = CHAIN_SUBMIT_INSTRUCTION_UNBOUND
        elif self.attempt_state == AttemptState.RESERVED.value:
            expected_chain_status = CHAIN_NOT_SUBMITTED
        else:
            expected_chain_status = CHAIN_SUBMISSION_UNKNOWN
        if self.chain_status != expected_chain_status:
            raise ExecutionQuoteChainError("chain_status mismatches durable submit truth")

        has_ack = self.acknowledgement_status is not None
        ack_values = (
            self.external_receipt_id,
            self.acknowledged_at,
        )
        if has_ack != all(value is not None for value in ack_values):
            raise ExecutionQuoteChainError(
                "acknowledgement identity/time must be present with acknowledgement status"
            )
        if not has_ack and (
            self.acknowledged_odds is not None or self.acknowledged_stake is not None
        ):
            raise ExecutionQuoteChainError(
                "unacknowledged attempt cannot claim acknowledgement economics"
            )
        if has_ack and self.acknowledgement_status in {"ACCEPTED", "PARTIAL"}:
            if self.acknowledged_odds is None or self.acknowledged_stake is None:
                raise ExecutionQuoteChainError(
                    "accepted/partial acknowledgement requires acknowledgement economics"
                )
            expected_price_status = ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED
        elif has_ack and self.acknowledgement_status == "REJECTED":
            if self.acknowledged_odds is not None or self.acknowledged_stake is not None:
                raise ExecutionQuoteChainError(
                    "rejected acknowledgement cannot claim acknowledgement economics"
                )
            expected_price_status = ACCEPTED_PRICE_NOT_APPLICABLE
        else:
            expected_price_status = ACCEPTED_PRICE_UNKNOWN
        if self.accepted_price_status != expected_price_status:
            raise ExecutionQuoteChainError(
                "accepted_price_status mismatches acknowledgement truth"
            )

        if self.provider_acknowledgement_sha256 is None:
            if self.acknowledgement_binding_matches:
                raise ExecutionQuoteChainError(
                    "missing provider acknowledgement digest cannot match"
                )
        elif not has_ack and self.acknowledgement_binding_matches:
            raise ExecutionQuoteChainError(
                "provider acknowledgement digest cannot match without acknowledgement"
            )

    @property
    def actual_submitted_instruction_bound(self) -> bool:
        """Whether exact serialized submit identity is durably available."""

        return False

    @property
    def accepted_price_verified(self) -> bool:
        """Whether accepted economics are provider-origin verified end-to-end."""

        return False

    @property
    def chain_complete(self) -> bool:
        """Whether decision -> actual submit -> accepted quote chain is complete."""

        return False

    @property
    def evidence_sha256(self) -> str:
        return _digest(self.to_dict(include_evidence_sha256=False))

    def to_dict(self, *, include_evidence_sha256: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": "autosport.execution_quote_chain_evidence",
            "schema_version": self.schema_version,
            "source_ledger_sha256": self.source_ledger_sha256,
            "source_event_count": self.source_event_count,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "decision_id": self.decision_id,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "attempt_state": self.attempt_state,
            "effect_fingerprint": self.effect_fingerprint,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "decision_quote_id": self.decision_quote_id,
            "decision_quote_observed_at": self.decision_quote_observed_at,
            "requested_odds": _decimal_text(self.requested_odds),
            "requested_stake": _decimal_text(self.requested_stake),
            "reserved_at": self.reserved_at,
            "submitted_at": self.submitted_at,
            "submission_instruction_sha256": self.submission_instruction_sha256,
            "actual_submitted_instruction_bound": self.actual_submitted_instruction_bound,
            "chain_status": self.chain_status,
            "provider_order_ref": self.provider_order_ref,
            "provider_evidence_id": self.provider_evidence_id,
            "provider_evidence_observed_at": self.provider_evidence_observed_at,
            "provider_evidence_source": self.provider_evidence_source,
            "provider_acknowledgement_sha256": self.provider_acknowledgement_sha256,
            "acknowledgement_binding_matches": self.acknowledgement_binding_matches,
            "external_receipt_id": self.external_receipt_id,
            "acknowledgement_status": self.acknowledgement_status,
            "acknowledged_at": self.acknowledged_at,
            "acknowledged_odds": (
                _decimal_text(self.acknowledged_odds)
                if self.acknowledged_odds is not None
                else None
            ),
            "acknowledged_stake": (
                _decimal_text(self.acknowledged_stake)
                if self.acknowledged_stake is not None
                else None
            ),
            "accepted_price_status": self.accepted_price_status,
            "accepted_price_verified": self.accepted_price_verified,
            "chain_complete": self.chain_complete,
        }
        if include_evidence_sha256:
            payload["evidence_sha256"] = _digest(payload)
        return payload


def build_execution_quote_chain_evidence(
    ledger: RealExecutionLedger,
    *,
    plan_id: str,
    attempt_id: str,
) -> ExecutionQuoteChainEvidence:
    """Project one attempt from one verified ledger snapshot.

    The function reads the canonical ledger exactly through
    `verified_execution_view` and never reconstructs a provider request.
    Because current `ATTEMPT_SUBMITTED` stores only `submitted_at`, every
    submitted attempt remains `SUBMIT_INSTRUCTION_UNBOUND` even if an
    acknowledgement happens to repeat the requested odds exactly.
    """

    if type(ledger) is not RealExecutionLedger:
        raise TypeError("ledger must be canonical RealExecutionLedger")
    if type(plan_id) is not str or not plan_id.strip():
        raise ValueError("plan_id must be non-empty text")
    if type(attempt_id) is not str or not attempt_id.strip():
        raise ValueError("attempt_id must be non-empty text")

    try:
        view = ledger.verified_execution_view(plan_id)
    except KeyError as exc:
        raise ExecutionQuoteChainUnavailable("plan is not present in ledger") from exc

    matches = tuple(
        item for item in view.attempts if item.attempt.attempt_id == attempt_id
    )
    if len(matches) != 1:
        raise ExecutionQuoteChainUnavailable(
            "attempt is not uniquely present in requested plan"
        )
    attempt_view = matches[0]
    action = attempt_view.action
    acknowledgement = attempt_view.acknowledgement
    provider = attempt_view.provider_evidence

    if attempt_view.submitted_at is not None:
        chain_status = CHAIN_SUBMIT_INSTRUCTION_UNBOUND
    elif attempt_view.state is AttemptState.RESERVED:
        chain_status = CHAIN_NOT_SUBMITTED
    else:
        # UNKNOWN can originate directly from RESERVED when process recovery or
        # provider uncertainty cannot prove whether the irreversible submit
        # boundary was crossed. RECONCILED_NOT_FOUND remains diagnostic only
        # under the canonical ledger and likewise cannot mint "not submitted".
        chain_status = CHAIN_SUBMISSION_UNKNOWN

    acknowledgement_binding_matches = False
    provider_acknowledgement_sha256: str | None = None
    if provider is not None:
        provider_acknowledgement_sha256 = provider.acknowledgement_sha256
        if (
            provider_acknowledgement_sha256 is not None
            and acknowledgement is not None
        ):
            acknowledgement_binding_matches = (
                provider_acknowledgement_sha256
                == _digest(acknowledgement.to_dict())
            )

    if acknowledgement is None:
        accepted_price_status = ACCEPTED_PRICE_UNKNOWN
        external_receipt_id = None
        acknowledgement_status = None
        acknowledged_at = None
        acknowledged_odds = None
        acknowledged_stake = None
    else:
        external_receipt_id = acknowledgement.external_receipt_id
        acknowledgement_status = acknowledgement.status.value
        acknowledged_at = acknowledgement.acknowledged_at
        acknowledged_odds = acknowledgement.accepted_odds
        acknowledged_stake = acknowledgement.accepted_stake
        if acknowledgement_status in {"ACCEPTED", "PARTIAL"}:
            accepted_price_status = ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED
        else:
            accepted_price_status = ACCEPTED_PRICE_NOT_APPLICABLE

    return ExecutionQuoteChainEvidence(
        source_ledger_sha256=view.snapshot_sha256,
        source_event_count=view.event_count,
        plan_id=view.plan.plan_id,
        plan_fingerprint=view.plan_fingerprint,
        decision_id=view.plan.decision_id,
        action_id=action.action_id,
        attempt_id=attempt_view.attempt.attempt_id,
        attempt_state=attempt_view.state.value,
        effect_fingerprint=attempt_view.attempt.effect_fingerprint,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        side=action.side,
        decision_quote_id=action.quote_id,
        decision_quote_observed_at=action.quote_observed_at,
        requested_odds=action.requested_odds,
        requested_stake=action.requested_stake,
        reserved_at=attempt_view.attempt.reserved_at,
        submitted_at=attempt_view.submitted_at,
        submission_instruction_sha256=None,
        chain_status=chain_status,
        provider_order_ref=attempt_view.provider_order_ref,
        provider_evidence_id=(provider.evidence_id if provider is not None else None),
        provider_evidence_observed_at=(
            provider.observed_at if provider is not None else None
        ),
        provider_evidence_source=(provider.source if provider is not None else None),
        provider_acknowledgement_sha256=provider_acknowledgement_sha256,
        acknowledgement_binding_matches=acknowledgement_binding_matches,
        external_receipt_id=external_receipt_id,
        acknowledgement_status=acknowledgement_status,
        acknowledged_at=acknowledged_at,
        acknowledged_odds=acknowledged_odds,
        acknowledged_stake=acknowledged_stake,
        accepted_price_status=accepted_price_status,
    )
