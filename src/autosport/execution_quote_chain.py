"""Fail-closed decision -> submit -> acknowledgement quote-chain projection.

This module is deliberately read-only. It projects facts from one verified
`RealExecutionLedger` snapshot and keeps the intended action, durable submit
boundary, provider-correlation evidence, and acknowledgement economics separate.

Current main ledger schema v1 stores only `submitted_at` in
`ATTEMPT_SUBMITTED`. The active canonical ACK-trust lineage (#794) adds an
optional exact request digest to the same event/read view and can correlate that
digest with provider evidence. This projection consumes that authority when
present instead of inventing a second writer or serializer.

Even a request hash plus an acknowledgement hash is not, by itself, typed
provider accepted-price provenance. Acknowledgement odds/stake remain
unverified unless the same canonical ledger also contains a provider-origin
verified effect issued by the authenticated readback authority. Full-chain
completion additionally requires exact durable request correlation; provider
outcome truth alone never launders an unbound submit instruction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from types import MappingProxyType
from decimal import Decimal
from operator import attrgetter
from weakref import ReferenceType, ref

from .real_execution_ledger import (
    AttemptState,
    RealExecutionLedger,
    _decimal_text,
    _digest,
    _sha256_text,
)


SCHEMA_VERSION = 3

CHAIN_NOT_SUBMITTED = "NOT_SUBMITTED"
CHAIN_SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
CHAIN_SUBMIT_INSTRUCTION_UNBOUND = "SUBMIT_INSTRUCTION_UNBOUND"
CHAIN_SUBMIT_INSTRUCTION_BOUND = "SUBMIT_INSTRUCTION_BOUND"

ACCEPTED_PRICE_NOT_APPLICABLE = "NOT_APPLICABLE"
ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED = "ACKNOWLEDGED_UNVERIFIED"
ACCEPTED_PRICE_PROVIDER_VERIFIED = "PROVIDER_VERIFIED"
ACCEPTED_PRICE_UNKNOWN = "UNKNOWN"


_CANONICAL_REAL_EXECUTION_LEDGER = RealExecutionLedger
_CANONICAL_VERIFIED_EXECUTION_VIEW = RealExecutionLedger.verified_execution_view
_CANONICAL_VERIFIED_EXECUTION_VIEW_CODE = (
    RealExecutionLedger.verified_execution_view.__code__
)
_CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS = (
    _CANONICAL_VERIFIED_EXECUTION_VIEW.__globals__
)
_CANONICAL_VERIFIED_EXECUTION_PLAN_VIEW_TYPE = (
    _CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS["VerifiedExecutionPlanView"]
)
_CANONICAL_EXECUTION_ATTEMPT_READ_VIEW_TYPE = (
    _CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS["ExecutionAttemptReadView"]
)
_CANONICAL_PROVIDER_EVIDENCE_BINDING_VIEW_TYPE = (
    _CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS["ProviderEvidenceBindingView"]
)
_CANONICAL_VERIFIED_PROVIDER_EFFECT_BINDING_VIEW_TYPE = (
    _CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS["VerifiedProviderEffectBindingView"]
)
_CANONICAL_EXECUTION_ATTEMPT_TYPE = (
    _CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS["ExecutionAttempt"]
)
_CANONICAL_EXECUTION_ACTION_TYPE = (
    _CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS["ExecutionAction"]
)
_CANONICAL_EXTERNAL_ACKNOWLEDGEMENT_TYPE = (
    _CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS["ExternalAcknowledgement"]
)
_CANONICAL_EXECUTION_PLAN_TYPE = (
    _CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS["ExecutionPlan"]
)
_CANONICAL_VERIFIED_EXECUTION_VIEW_TYPE_BINDINGS = (
    ("VerifiedExecutionPlanView", _CANONICAL_VERIFIED_EXECUTION_PLAN_VIEW_TYPE),
    ("ExecutionAttemptReadView", _CANONICAL_EXECUTION_ATTEMPT_READ_VIEW_TYPE),
    ("ProviderEvidenceBindingView", _CANONICAL_PROVIDER_EVIDENCE_BINDING_VIEW_TYPE),
    (
        "VerifiedProviderEffectBindingView",
        _CANONICAL_VERIFIED_PROVIDER_EFFECT_BINDING_VIEW_TYPE,
    ),
    ("ExecutionAttempt", _CANONICAL_EXECUTION_ATTEMPT_TYPE),
    ("ExecutionAction", _CANONICAL_EXECUTION_ACTION_TYPE),
    ("ExternalAcknowledgement", _CANONICAL_EXTERNAL_ACKNOWLEDGEMENT_TYPE),
    ("ExecutionPlan", _CANONICAL_EXECUTION_PLAN_TYPE),
)


class ExecutionQuoteChainError(RuntimeError):
    """Base error for quote-chain projection failures."""


class ExecutionQuoteChainUnavailable(ExecutionQuoteChainError):
    """Requested plan/attempt cannot be projected from canonical durable facts."""


def _build_execution_quote_chain_evidence_meta():
    """Build a metaclass whose seal state is not module/class mutable authority."""

    sealed_classes: dict[type, frozenset[str]] = {}
    protected_names = frozenset(
        {
            "__dataclass_fields__",
            "__init__",
            "__post_init__",
            "assert_projection_issued",
            "submit_instruction_identity_bound",
            "provider_request_correlation_bound",
            "actual_submitted_instruction_bound",
            "acknowledgement_binding_matches",
            "accepted_price_verified",
            "chain_complete",
            "_accepted_price_verified",
            "_chain_complete",
            "evidence_sha256",
            "to_dict",
        }
    )

    class _ExecutionQuoteChainEvidenceMeta(type):
        def __setattr__(cls, name: str, value: object) -> None:
            sealed_fields = sealed_classes.get(cls)
            if sealed_fields is not None and (
                name in protected_names or name in sealed_fields
            ):
                raise TypeError(
                    "quote-chain evidence authority surface is sealed: " + name
                )
            super().__setattr__(name, value)

        def __delattr__(cls, name: str) -> None:
            sealed_fields = sealed_classes.get(cls)
            if sealed_fields is not None and (
                name in protected_names or name in sealed_fields
            ):
                raise TypeError(
                    "quote-chain evidence authority surface is sealed: " + name
                )
            super().__delattr__(name)

        @classmethod
        def seal(mcls, cls: type) -> None:
            sealed_classes[cls] = frozenset(cls.__dataclass_fields__)

    return _ExecutionQuoteChainEvidenceMeta


_ExecutionQuoteChainEvidenceMeta = _build_execution_quote_chain_evidence_meta()
del _build_execution_quote_chain_evidence_meta


def _require_canonical_verified_execution_view_dispatch(
    ledger: RealExecutionLedger,
) -> None:
    """Require the exact canonical ledger read authority used by this projection."""

    if RealExecutionLedger is not _CANONICAL_REAL_EXECUTION_LEDGER:
        raise ExecutionQuoteChainUnavailable(
            "canonical verified execution view authority is unavailable"
        )
    if type(ledger) is not _CANONICAL_REAL_EXECUTION_LEDGER:
        raise TypeError("ledger must be canonical RealExecutionLedger")
    if "verified_execution_view" in getattr(ledger, "__dict__", {}):
        raise ExecutionQuoteChainUnavailable(
            "canonical verified execution view authority is unavailable"
        )
    current = _CANONICAL_REAL_EXECUTION_LEDGER.__dict__.get(
        "verified_execution_view"
    )
    if (
        current is not _CANONICAL_VERIFIED_EXECUTION_VIEW
        or getattr(current, "__code__", None)
        is not _CANONICAL_VERIFIED_EXECUTION_VIEW_CODE
        or getattr(_CANONICAL_VERIFIED_EXECUTION_VIEW, "__globals__", None)
        is not _CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS
        or any(
            _CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS.get(name) is not expected
            for name, expected in _CANONICAL_VERIFIED_EXECUTION_VIEW_TYPE_BINDINGS
        )
    ):
        raise ExecutionQuoteChainUnavailable(
            "canonical verified execution view authority is unavailable"
        )


def _read_canonical_verified_execution_view(
    ledger: RealExecutionLedger,
    plan_id: str,
):
    _require_canonical_verified_execution_view_dispatch(ledger)
    try:
        view = _CANONICAL_VERIFIED_EXECUTION_VIEW(ledger, plan_id)
    finally:
        _require_canonical_verified_execution_view_dispatch(ledger)
    if type(view) is not _CANONICAL_VERIFIED_EXECUTION_PLAN_VIEW_TYPE:
        raise ExecutionQuoteChainUnavailable(
            "canonical verified execution view type authority is unavailable"
        )
    if type(view.plan) is not _CANONICAL_EXECUTION_PLAN_TYPE:
        raise ExecutionQuoteChainUnavailable(
            "canonical execution plan view type authority is unavailable"
        )
    if type(view.attempts) is not tuple:
        raise ExecutionQuoteChainUnavailable(
            "canonical execution attempt collection is unavailable"
        )
    for item in view.attempts:
        if type(item) is not _CANONICAL_EXECUTION_ATTEMPT_READ_VIEW_TYPE:
            raise ExecutionQuoteChainUnavailable(
                "canonical execution attempt read-view type authority is unavailable"
            )
        if type(item.attempt) is not _CANONICAL_EXECUTION_ATTEMPT_TYPE:
            raise ExecutionQuoteChainUnavailable(
                "canonical execution attempt type authority is unavailable"
            )
        if type(item.action) is not _CANONICAL_EXECUTION_ACTION_TYPE:
            raise ExecutionQuoteChainUnavailable(
                "canonical execution action type authority is unavailable"
            )
        if (
            item.provider_evidence is not None
            and type(item.provider_evidence)
            is not _CANONICAL_PROVIDER_EVIDENCE_BINDING_VIEW_TYPE
        ):
            raise ExecutionQuoteChainUnavailable(
                "canonical provider evidence view type authority is unavailable"
            )
        if (
            item.verified_provider_effect is not None
            and type(item.verified_provider_effect)
            is not _CANONICAL_VERIFIED_PROVIDER_EFFECT_BINDING_VIEW_TYPE
        ):
            raise ExecutionQuoteChainUnavailable(
                "canonical verified provider effect view type authority is unavailable"
            )
        if (
            item.acknowledgement is not None
            and type(item.acknowledgement)
            is not _CANONICAL_EXTERNAL_ACKNOWLEDGEMENT_TYPE
        ):
            raise ExecutionQuoteChainUnavailable(
                "canonical acknowledgement view type authority is unavailable"
            )
    return view


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ExecutionQuoteChainEvidence(metaclass=_ExecutionQuoteChainEvidenceMeta):
    """One immutable, snapshot-bound quote-chain diagnostic.

    The class intentionally exposes no caller-settable `chain_complete` or
    `accepted_price_verified` field. Exact request-binding and provider-outcome
    fields may become positive only on a canonical builder-issued projection.
    Provider-origin accepted-price truth and full-chain completion are separate:
    the latter also requires exact durable submit-request correlation. This
    object is not a new execution or provider-write authority.
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
    submission_instruction_sha256: str | None
    chain_status: str

    provider_order_ref: str | None
    provider_evidence_id: str | None
    provider_evidence_observed_at: str | None
    provider_evidence_source: str | None
    provider_request_sha256: str | None
    provider_acknowledgement_sha256: str | None
    _acknowledgement_binding_matches: bool = field(repr=False)

    external_receipt_id: str | None
    acknowledgement_status: str | None
    acknowledged_at: str | None
    acknowledged_odds: Decimal | None
    acknowledged_stake: Decimal | None
    accepted_price_status: str

    provider_outcome_evidence_id: str | None = None
    provider_outcome_observed_at: str | None = None
    provider_outcome_source_payload_sha256: str | None = None
    provider_outcome_receipt_id: str | None = None
    provider_outcome_status: str | None = None
    provider_accepted_odds: Decimal | None = None
    provider_accepted_stake: Decimal | None = None

    schema_version: int = SCHEMA_VERSION
    _accepted_price_verified: bool = field(init=False, repr=False)
    _chain_complete: bool = field(init=False, repr=False)
    _evidence_sha256: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ExecutionQuoteChainError("unsupported quote-chain evidence schema")
        if type(self.source_event_count) is not int or self.source_event_count < 1:
            raise ExecutionQuoteChainError("source_event_count must be positive int")
        try:
            _sha256_text(self.source_ledger_sha256, "source_ledger_sha256")
            _sha256_text(self.plan_fingerprint, "plan_fingerprint")
            _sha256_text(self.effect_fingerprint, "effect_fingerprint")
            if self.submission_instruction_sha256 is not None:
                _sha256_text(
                    self.submission_instruction_sha256,
                    "submission_instruction_sha256",
                )
            if self.provider_request_sha256 is not None:
                _sha256_text(self.provider_request_sha256, "provider_request_sha256")
            if self.provider_acknowledgement_sha256 is not None:
                _sha256_text(
                    self.provider_acknowledgement_sha256,
                    "provider_acknowledgement_sha256",
                )
            if self.provider_outcome_evidence_id is not None:
                _sha256_text(
                    self.provider_outcome_evidence_id,
                    "provider_outcome_evidence_id",
                )
            if self.provider_outcome_source_payload_sha256 is not None:
                _sha256_text(
                    self.provider_outcome_source_payload_sha256,
                    "provider_outcome_source_payload_sha256",
                )
        except ValueError as exc:
            raise ExecutionQuoteChainError(str(exc)) from exc

        if type(self._acknowledgement_binding_matches) is not bool:
            raise ExecutionQuoteChainError(
                "acknowledgement_binding_matches must be bool"
            )

        if self.submitted_at is None:
            if self.submission_instruction_sha256 is not None:
                raise ExecutionQuoteChainError(
                    "unsubmitted attempt cannot bind submitted instruction identity"
                )
            expected_chain_status = (
                CHAIN_NOT_SUBMITTED
                if self.attempt_state == AttemptState.RESERVED.value
                else CHAIN_SUBMISSION_UNKNOWN
            )
        elif self.submission_instruction_sha256 is None:
            expected_chain_status = CHAIN_SUBMIT_INSTRUCTION_UNBOUND
        else:
            expected_chain_status = CHAIN_SUBMIT_INSTRUCTION_BOUND
        if self.chain_status != expected_chain_status:
            raise ExecutionQuoteChainError("chain_status mismatches durable submit truth")

        provider_identity = (
            self.provider_evidence_id,
            self.provider_evidence_observed_at,
            self.provider_evidence_source,
        )
        if any(value is not None for value in provider_identity) and not all(
            value is not None for value in provider_identity
        ):
            raise ExecutionQuoteChainError(
                "provider evidence identity/time/source must be present together"
            )
        if self.provider_request_sha256 is not None:
            if not all(value is not None for value in provider_identity):
                raise ExecutionQuoteChainError(
                    "provider request digest requires provider evidence identity"
                )
            if self.submission_instruction_sha256 is None:
                raise ExecutionQuoteChainError(
                    "provider request digest lacks durable submitted request identity"
                )
            if self.provider_request_sha256 != self.submission_instruction_sha256:
                raise ExecutionQuoteChainError(
                    "provider request digest conflicts with durable submission"
                )

        provider_outcome = (
            self.provider_outcome_evidence_id,
            self.provider_outcome_observed_at,
            self.provider_outcome_source_payload_sha256,
            self.provider_outcome_receipt_id,
            self.provider_outcome_status,
            self.provider_accepted_odds,
            self.provider_accepted_stake,
        )
        has_provider_outcome = all(value is not None for value in provider_outcome)
        if any(value is not None for value in provider_outcome) and not has_provider_outcome:
            raise ExecutionQuoteChainError(
                "provider outcome provenance must be present as one complete typed fact"
            )
        if has_provider_outcome:
            if (
                type(self.provider_outcome_observed_at) is not str
                or not self.provider_outcome_observed_at.strip()
            ):
                raise ExecutionQuoteChainError(
                    "provider outcome observed_at must be non-empty text"
                )
            if (
                type(self.provider_outcome_receipt_id) is not str
                or not self.provider_outcome_receipt_id.strip()
            ):
                raise ExecutionQuoteChainError(
                    "provider outcome receipt must be non-empty text"
                )
            if self.provider_outcome_status not in {"ACCEPTED", "PARTIAL"}:
                raise ExecutionQuoteChainError(
                    "provider outcome status must be ACCEPTED/PARTIAL"
                )
            if type(self.provider_accepted_odds) is not Decimal:
                raise ExecutionQuoteChainError(
                    "provider accepted odds must be exact Decimal"
                )
            if type(self.provider_accepted_stake) is not Decimal:
                raise ExecutionQuoteChainError(
                    "provider accepted stake must be exact Decimal"
                )
            if (
                self.provider_accepted_odds <= Decimal("0")
                or self.provider_accepted_stake <= Decimal("0")
            ):
                raise ExecutionQuoteChainError(
                    "provider accepted economics must be positive"
                )

        has_ack = self.acknowledgement_status is not None
        if not has_ack:
            if self.external_receipt_id is not None or self.acknowledged_at is not None:
                raise ExecutionQuoteChainError(
                    "unacknowledged attempt cannot claim acknowledgement identity/time"
                )
            if self.acknowledged_odds is not None or self.acknowledged_stake is not None:
                raise ExecutionQuoteChainError(
                    "unacknowledged attempt cannot claim acknowledgement economics"
                )
            expected_price_status = (
                ACCEPTED_PRICE_PROVIDER_VERIFIED
                if has_provider_outcome
                else ACCEPTED_PRICE_UNKNOWN
            )
        elif self.acknowledgement_status in {"ACCEPTED", "PARTIAL"}:
            if self.external_receipt_id is None or self.acknowledged_at is None:
                raise ExecutionQuoteChainError(
                    "accepted/partial acknowledgement requires receipt identity/time"
                )
            if self.acknowledged_odds is None or self.acknowledged_stake is None:
                raise ExecutionQuoteChainError(
                    "accepted/partial acknowledgement requires acknowledgement economics"
                )
            if has_provider_outcome:
                if (
                    self.provider_outcome_receipt_id != self.external_receipt_id
                    or self.provider_outcome_status != self.acknowledgement_status
                    or self.provider_outcome_observed_at != self.acknowledged_at
                    or self.provider_accepted_odds != self.acknowledged_odds
                    or self.provider_accepted_stake != self.acknowledged_stake
                ):
                    raise ExecutionQuoteChainError(
                        "provider outcome conflicts with durable acknowledgement"
                    )
                expected_price_status = ACCEPTED_PRICE_PROVIDER_VERIFIED
            else:
                expected_price_status = ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED
        elif self.acknowledgement_status == "REJECTED":
            if self.acknowledged_at is None:
                raise ExecutionQuoteChainError(
                    "rejected acknowledgement requires acknowledgement time"
                )
            if self.acknowledged_odds is not None or self.acknowledged_stake is not None:
                raise ExecutionQuoteChainError(
                    "rejected acknowledgement cannot claim acknowledgement economics"
                )
            if has_provider_outcome:
                raise ExecutionQuoteChainError(
                    "rejected acknowledgement conflicts with positive provider outcome"
                )
            expected_price_status = ACCEPTED_PRICE_NOT_APPLICABLE
        else:
            raise ExecutionQuoteChainError("unsupported acknowledgement status")
        if self.accepted_price_status != expected_price_status:
            raise ExecutionQuoteChainError(
                "accepted_price_status mismatches acknowledgement truth"
            )

        if self.provider_acknowledgement_sha256 is None:
            if self._acknowledgement_binding_matches:
                raise ExecutionQuoteChainError(
                    "missing provider acknowledgement digest cannot match"
                )
        elif not has_ack and self._acknowledgement_binding_matches:
            raise ExecutionQuoteChainError(
                "provider acknowledgement digest cannot match without acknowledgement"
            )

        exact_request_correlation = (
            self.submission_instruction_sha256 is not None
            and self.provider_request_sha256 == self.submission_instruction_sha256
        )
        object.__setattr__(
            self,
            "_accepted_price_verified",
            has_provider_outcome,
        )
        object.__setattr__(
            self,
            "_chain_complete",
            (
                has_provider_outcome
                and has_ack
                and exact_request_correlation
                and self.attempt_state == self.provider_outcome_status
            ),
        )

        # Product issuance is attached only after canonical ledger projection.
        # Caller construction remains structurally valid but has no authority.
        object.__setattr__(self, "_evidence_sha256", "")

    def assert_projection_issued(self) -> None:
        """Verify this object came from the canonical ledger projection builder."""

        raise ExecutionQuoteChainError(
            "quote-chain evidence was not issued by canonical ledger projection"
        )

    @property
    def submit_instruction_identity_bound(self) -> bool:
        """Whether a canonical exact submitted-request digest is durable."""

        return self.submission_instruction_sha256 is not None

    @property
    def provider_request_correlation_bound(self) -> bool:
        """Whether provider evidence binds the same exact durable request digest."""

        return (
            self.submission_instruction_sha256 is not None
            and self.provider_request_sha256 == self.submission_instruction_sha256
        )

    @property
    def actual_submitted_instruction_bound(self) -> bool:
        """Whether provider evidence correlates the exact durable request identity.

        This is not evidence that an external effect occurred and does not
        promote acknowledgement odds into verified accepted-price truth.
        """

        return self.provider_request_correlation_bound

    @property
    def acknowledgement_binding_matches(self) -> bool:
        """Whether provider evidence binds the exact durable acknowledgement."""

        return self._acknowledgement_binding_matches

    # Keep these claim getters out of mutable Python bytecode. The canonical
    # builder computes frozen internal booleans from sealed durable fields;
    # attrgetter descriptors expose them without a replaceable Python fget.
    accepted_price_verified = property(
        attrgetter("_accepted_price_verified"),
        doc="Whether accepted economics are provider-origin verified end-to-end.",
    )
    chain_complete = property(
        attrgetter("_chain_complete"),
        doc="Whether decision -> actual submit -> accepted quote chain is complete.",
    )

    @property
    def evidence_sha256(self) -> str:
        self.assert_projection_issued()
        return self._evidence_sha256

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
            "submit_instruction_identity_bound": (
                self.submission_instruction_sha256 is not None
            ),
            "provider_request_correlation_bound": (
                self.submission_instruction_sha256 is not None
                and self.provider_request_sha256
                == self.submission_instruction_sha256
            ),
            "actual_submitted_instruction_bound": (
                self.submission_instruction_sha256 is not None
                and self.provider_request_sha256
                == self.submission_instruction_sha256
            ),
            "chain_status": self.chain_status,
            "provider_order_ref": self.provider_order_ref,
            "provider_evidence_id": self.provider_evidence_id,
            "provider_evidence_observed_at": self.provider_evidence_observed_at,
            "provider_evidence_source": self.provider_evidence_source,
            "provider_request_sha256": self.provider_request_sha256,
            "provider_acknowledgement_sha256": self.provider_acknowledgement_sha256,
            "acknowledgement_binding_matches": self._acknowledgement_binding_matches,
            "provider_outcome_evidence_id": self.provider_outcome_evidence_id,
            "provider_outcome_observed_at": self.provider_outcome_observed_at,
            "provider_outcome_source_payload_sha256": (
                self.provider_outcome_source_payload_sha256
            ),
            "provider_outcome_receipt_id": self.provider_outcome_receipt_id,
            "provider_outcome_status": self.provider_outcome_status,
            "provider_accepted_odds": (
                _decimal_text(self.provider_accepted_odds)
                if self.provider_accepted_odds is not None
                else None
            ),
            "provider_accepted_stake": (
                _decimal_text(self.provider_accepted_stake)
                if self.provider_accepted_stake is not None
                else None
            ),
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
            payload["evidence_sha256"] = self.evidence_sha256
        return payload



def build_execution_quote_chain_evidence(
    ledger: RealExecutionLedger,
    *,
    plan_id: str,
    attempt_id: str,
) -> ExecutionQuoteChainEvidence:
    """Project one attempt from one verified ledger snapshot.

    The function reads the canonical ledger exactly through
    `verified_execution_view` and never reconstructs provider request bytes
    or provider outcome truth. On current-main views the optional request-digest
    fields are absent and remain unbound. On the canonical #794 successor view,
    the same projection consumes its `submitted_request_sha256` and provider
    `request_sha256` fields without owning or duplicating that writer seam.
    """

    if type(ledger) is not _CANONICAL_REAL_EXECUTION_LEDGER:
        raise TypeError("ledger must be canonical RealExecutionLedger")
    if type(plan_id) is not str or not plan_id.strip():
        raise ValueError("plan_id must be non-empty text")
    if type(attempt_id) is not str or not attempt_id.strip():
        raise ValueError("attempt_id must be non-empty text")

    try:
        view = _read_canonical_verified_execution_view(ledger, plan_id)
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
    verified_effect = attempt_view.verified_provider_effect

    submitted_request_sha256 = getattr(
        attempt_view,
        "submitted_request_sha256",
        None,
    )
    provider_request_sha256 = (
        getattr(provider, "request_sha256", None) if provider is not None else None
    )
    if (
        provider_request_sha256 is not None
        and submitted_request_sha256 is not None
        and provider_request_sha256 != submitted_request_sha256
    ):
        raise ExecutionQuoteChainUnavailable(
            "provider request evidence conflicts with durable submitted request"
        )

    if attempt_view.submitted_at is not None:
        chain_status = (
            CHAIN_SUBMIT_INSTRUCTION_BOUND
            if submitted_request_sha256 is not None
            else CHAIN_SUBMIT_INSTRUCTION_UNBOUND
        )
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

    provider_outcome_evidence_id = None
    provider_outcome_observed_at = None
    provider_outcome_source_payload_sha256 = None
    provider_outcome_receipt_id = None
    provider_outcome_status = None
    provider_accepted_odds = None
    provider_accepted_stake = None
    if verified_effect is not None:
        provider_outcome_evidence_id = verified_effect.evidence_id
        provider_outcome_observed_at = verified_effect.observed_at
        provider_outcome_source_payload_sha256 = (
            verified_effect.source_payload_sha256
        )
        provider_outcome_receipt_id = verified_effect.external_receipt_id
        provider_outcome_status = verified_effect.status.value
        provider_accepted_odds = verified_effect.accepted_odds
        provider_accepted_stake = verified_effect.accepted_stake
        if acknowledgement is not None and (
            acknowledgement.external_receipt_id != provider_outcome_receipt_id
            or acknowledgement.status.value != provider_outcome_status
            or acknowledgement.acknowledged_at != provider_outcome_observed_at
            or acknowledgement.accepted_odds != provider_accepted_odds
            or acknowledgement.accepted_stake != provider_accepted_stake
        ):
            raise ExecutionQuoteChainUnavailable(
                "verified provider outcome conflicts with durable acknowledgement"
            )
        if acknowledgement is not None and acknowledgement.status.value == "REJECTED":
            raise ExecutionQuoteChainUnavailable(
                "positive verified provider outcome conflicts with rejection"
            )
        accepted_price_status = ACCEPTED_PRICE_PROVIDER_VERIFIED

    evidence = ExecutionQuoteChainEvidence(
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
        submission_instruction_sha256=submitted_request_sha256,
        chain_status=chain_status,
        provider_order_ref=attempt_view.provider_order_ref,
        provider_evidence_id=(provider.evidence_id if provider is not None else None),
        provider_evidence_observed_at=(
            provider.observed_at if provider is not None else None
        ),
        provider_evidence_source=(provider.source if provider is not None else None),
        provider_request_sha256=provider_request_sha256,
        provider_acknowledgement_sha256=provider_acknowledgement_sha256,
        _acknowledgement_binding_matches=acknowledgement_binding_matches,
        external_receipt_id=external_receipt_id,
        acknowledgement_status=acknowledgement_status,
        acknowledged_at=acknowledged_at,
        acknowledged_odds=acknowledged_odds,
        acknowledged_stake=acknowledged_stake,
        accepted_price_status=accepted_price_status,
        provider_outcome_evidence_id=provider_outcome_evidence_id,
        provider_outcome_observed_at=provider_outcome_observed_at,
        provider_outcome_source_payload_sha256=(
            provider_outcome_source_payload_sha256
        ),
        provider_outcome_receipt_id=provider_outcome_receipt_id,
        provider_outcome_status=provider_outcome_status,
        provider_accepted_odds=provider_accepted_odds,
        provider_accepted_stake=provider_accepted_stake,
    )
    return evidence


def _install_quote_chain_evidence_authority() -> None:
    """Keep the only issuance registry and mint path outside module globals."""

    module_namespace = globals()
    canonical_evidence_type = ExecutionQuoteChainEvidence
    canonical_evidence_init = ExecutionQuoteChainEvidence.__init__
    canonical_evidence_init_code = canonical_evidence_init.__code__
    canonical_evidence_post_init = ExecutionQuoteChainEvidence.__post_init__
    canonical_evidence_post_init_code = canonical_evidence_post_init.__code__
    raw_builder = build_execution_quote_chain_evidence
    raw_builder_code = raw_builder.__code__
    raw_to_dict = ExecutionQuoteChainEvidence.to_dict
    raw_to_dict_code = raw_to_dict.__code__
    digest = _digest
    unavailable_type = ExecutionQuoteChainUnavailable
    issued_lock = RLock()
    issued_snapshot = MappingProxyType({})

    function_witnesses = (
        (
            "_read_canonical_verified_execution_view",
            _read_canonical_verified_execution_view,
            _read_canonical_verified_execution_view.__code__,
        ),
        (
            "_require_canonical_verified_execution_view_dispatch",
            _require_canonical_verified_execution_view_dispatch,
            _require_canonical_verified_execution_view_dispatch.__code__,
        ),
        ("_digest", _digest, getattr(_digest, "__code__", None)),
        ("_sha256_text", _sha256_text, getattr(_sha256_text, "__code__", None)),
        ("_decimal_text", _decimal_text, getattr(_decimal_text, "__code__", None)),
    )
    identity_witnesses = (
        ("RealExecutionLedger", _CANONICAL_REAL_EXECUTION_LEDGER),
        ("_CANONICAL_REAL_EXECUTION_LEDGER", _CANONICAL_REAL_EXECUTION_LEDGER),
        (
            "_CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS",
            _CANONICAL_VERIFIED_EXECUTION_VIEW_GLOBALS,
        ),
        (
            "_CANONICAL_VERIFIED_EXECUTION_PLAN_VIEW_TYPE",
            _CANONICAL_VERIFIED_EXECUTION_PLAN_VIEW_TYPE,
        ),
        (
            "_CANONICAL_EXECUTION_ATTEMPT_READ_VIEW_TYPE",
            _CANONICAL_EXECUTION_ATTEMPT_READ_VIEW_TYPE,
        ),
        (
            "_CANONICAL_PROVIDER_EVIDENCE_BINDING_VIEW_TYPE",
            _CANONICAL_PROVIDER_EVIDENCE_BINDING_VIEW_TYPE,
        ),
        (
            "_CANONICAL_VERIFIED_PROVIDER_EFFECT_BINDING_VIEW_TYPE",
            _CANONICAL_VERIFIED_PROVIDER_EFFECT_BINDING_VIEW_TYPE,
        ),
        ("_CANONICAL_EXECUTION_ATTEMPT_TYPE", _CANONICAL_EXECUTION_ATTEMPT_TYPE),
        ("_CANONICAL_EXECUTION_ACTION_TYPE", _CANONICAL_EXECUTION_ACTION_TYPE),
        (
            "_CANONICAL_EXTERNAL_ACKNOWLEDGEMENT_TYPE",
            _CANONICAL_EXTERNAL_ACKNOWLEDGEMENT_TYPE,
        ),
        ("_CANONICAL_EXECUTION_PLAN_TYPE", _CANONICAL_EXECUTION_PLAN_TYPE),
        (
            "_CANONICAL_VERIFIED_EXECUTION_VIEW_TYPE_BINDINGS",
            _CANONICAL_VERIFIED_EXECUTION_VIEW_TYPE_BINDINGS,
        ),
        ("AttemptState", AttemptState),
        ("_CANONICAL_VERIFIED_EXECUTION_VIEW", _CANONICAL_VERIFIED_EXECUTION_VIEW),
        (
            "_CANONICAL_VERIFIED_EXECUTION_VIEW_CODE",
            _CANONICAL_VERIFIED_EXECUTION_VIEW_CODE,
        ),
        ("ExecutionQuoteChainError", ExecutionQuoteChainError),
        ("ExecutionQuoteChainUnavailable", ExecutionQuoteChainUnavailable),
    )
    value_witnesses = (
        ("SCHEMA_VERSION", SCHEMA_VERSION),
        ("CHAIN_NOT_SUBMITTED", CHAIN_NOT_SUBMITTED),
        ("CHAIN_SUBMISSION_UNKNOWN", CHAIN_SUBMISSION_UNKNOWN),
        ("CHAIN_SUBMIT_INSTRUCTION_UNBOUND", CHAIN_SUBMIT_INSTRUCTION_UNBOUND),
        ("CHAIN_SUBMIT_INSTRUCTION_BOUND", CHAIN_SUBMIT_INSTRUCTION_BOUND),
        ("ACCEPTED_PRICE_NOT_APPLICABLE", ACCEPTED_PRICE_NOT_APPLICABLE),
        (
            "ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED",
            ACCEPTED_PRICE_ACKNOWLEDGED_UNVERIFIED,
        ),
        ("ACCEPTED_PRICE_PROVIDER_VERIFIED", ACCEPTED_PRICE_PROVIDER_VERIFIED),
        ("ACCEPTED_PRICE_UNKNOWN", ACCEPTED_PRICE_UNKNOWN),
    )

    def _require_projection_dependency_authority() -> None:
        if (
            raw_builder.__code__ is not raw_builder_code
            or raw_to_dict.__code__ is not raw_to_dict_code
            or canonical_evidence_type.__dict__.get("__init__")
            is not canonical_evidence_init
            or canonical_evidence_init.__code__ is not canonical_evidence_init_code
            or canonical_evidence_type.__dict__.get("__post_init__")
            is not canonical_evidence_post_init
            or canonical_evidence_post_init.__code__
            is not canonical_evidence_post_init_code
        ):
            raise unavailable_type(
                "quote-chain projection dependency authority is unavailable"
            )
        for name, expected, expected_code in function_witnesses:
            current = module_namespace.get(name)
            if (
                current is not expected
                or getattr(current, "__code__", None) is not expected_code
            ):
                raise unavailable_type(
                    "quote-chain projection dependency authority is unavailable"
                )
        for name, expected in identity_witnesses:
            if module_namespace.get(name) is not expected:
                raise unavailable_type(
                    "quote-chain projection dependency authority is unavailable"
                )
        for name, expected in value_witnesses:
            if module_namespace.get(name) != expected:
                raise unavailable_type(
                    "quote-chain projection dependency authority is unavailable"
                )

    def _lookup(
        evidence: ExecutionQuoteChainEvidence,
        *,
        require_fingerprint: bool,
    ):
        with issued_lock:
            record = issued_snapshot.get(id(evidence))
        if record is None or record[0]() is not evidence:
            raise ExecutionQuoteChainError(
                "quote-chain evidence was not issued by canonical ledger projection"
            )
        if not require_fingerprint:
            return record
        fingerprint = record[1]
        issued_payload = record[2]
        if (
            type(fingerprint) is not str
            or not fingerprint
            or issued_payload is None
        ):
            raise ExecutionQuoteChainError(
                "quote-chain evidence issuance is incomplete"
            )
        _require_projection_dependency_authority()
        try:
            current = digest(
                raw_to_dict(evidence, include_evidence_sha256=False)
            )
        except Exception as exc:
            raise ExecutionQuoteChainError(
                "quote-chain evidence is no longer canonical"
            ) from exc
        finally:
            _require_projection_dependency_authority()
        if (
            fingerprint != current
            or evidence._evidence_sha256 != current
        ):
            raise ExecutionQuoteChainError(
                "quote-chain evidence was not issued by canonical ledger projection"
            )
        return record

    def assert_projection_issued(self: ExecutionQuoteChainEvidence) -> None:
        _lookup(self, require_fingerprint=True)

    def _issued_payload(
        self: ExecutionQuoteChainEvidence,
    ) -> tuple[dict[str, object], str]:
        record = _lookup(self, require_fingerprint=True)
        fingerprint = record[1]
        issued_payload = record[2]
        if type(fingerprint) is not str or issued_payload is None:
            raise ExecutionQuoteChainError(
                "quote-chain evidence issuance is incomplete"
            )
        return dict(issued_payload), fingerprint

    def submit_instruction_identity_bound(
        self: ExecutionQuoteChainEvidence,
    ) -> bool:
        payload, _fingerprint = _issued_payload(self)
        return bool(payload["submit_instruction_identity_bound"])

    def provider_request_correlation_bound(
        self: ExecutionQuoteChainEvidence,
    ) -> bool:
        payload, _fingerprint = _issued_payload(self)
        return bool(payload["provider_request_correlation_bound"])

    def actual_submitted_instruction_bound(
        self: ExecutionQuoteChainEvidence,
    ) -> bool:
        payload, _fingerprint = _issued_payload(self)
        return bool(payload["actual_submitted_instruction_bound"])

    def acknowledgement_binding_matches(
        self: ExecutionQuoteChainEvidence,
    ) -> bool:
        payload, _fingerprint = _issued_payload(self)
        return bool(payload["acknowledgement_binding_matches"])

    def evidence_sha256(self: ExecutionQuoteChainEvidence) -> str:
        _payload, fingerprint = _issued_payload(self)
        return fingerprint

    def to_dict(
        self: ExecutionQuoteChainEvidence,
        *,
        include_evidence_sha256: bool = True,
    ) -> dict[str, object]:
        payload, fingerprint = _issued_payload(self)
        if include_evidence_sha256:
            payload["evidence_sha256"] = fingerprint
        return payload

    def issue(
        ledger: RealExecutionLedger,
        *,
        plan_id: str,
        attempt_id: str,
    ) -> ExecutionQuoteChainEvidence:
        nonlocal issued_snapshot
        _require_projection_dependency_authority()
        if module_namespace.get("ExecutionQuoteChainEvidence") is not canonical_evidence_type:
            raise ExecutionQuoteChainError(
                "quote-chain evidence class authority is unavailable"
            )
        try:
            evidence = raw_builder(
                ledger,
                plan_id=plan_id,
                attempt_id=attempt_id,
            )
        finally:
            _require_projection_dependency_authority()
        if (
            module_namespace.get("ExecutionQuoteChainEvidence") is not canonical_evidence_type
            or type(evidence) is not canonical_evidence_type
        ):
            raise ExecutionQuoteChainError(
                "quote-chain evidence class authority is unavailable"
            )
        identity = id(evidence)

        def discard(reference: ReferenceType[ExecutionQuoteChainEvidence]) -> None:
            nonlocal issued_snapshot
            with issued_lock:
                current = issued_snapshot.get(identity)
                if current is None or current[0] is not reference:
                    return
                updated = dict(issued_snapshot)
                updated.pop(identity, None)
                issued_snapshot = MappingProxyType(updated)

        reference = ref(evidence, discard)
        with issued_lock:
            updated = dict(issued_snapshot)
            updated[identity] = (reference, None, None)
            issued_snapshot = MappingProxyType(updated)
        try:
            _require_projection_dependency_authority()
            try:
                canonical_payload = raw_to_dict(
                    evidence,
                    include_evidence_sha256=False,
                )
                fingerprint = digest(canonical_payload)
                issued_payload = MappingProxyType(dict(canonical_payload))
            finally:
                _require_projection_dependency_authority()
            object.__setattr__(evidence, "_evidence_sha256", fingerprint)
            with issued_lock:
                current = issued_snapshot.get(identity)
                if current is None or current[0] is not reference:
                    raise ExecutionQuoteChainError(
                        "quote-chain evidence issuance disappeared"
                    )
                updated = dict(issued_snapshot)
                updated[identity] = (reference, fingerprint, issued_payload)
                issued_snapshot = MappingProxyType(updated)
            assert_projection_issued(evidence)
            return evidence
        except Exception:
            with issued_lock:
                current = issued_snapshot.get(identity)
                if current is not None and current[0] is reference:
                    updated = dict(issued_snapshot)
                    updated.pop(identity, None)
                    issued_snapshot = MappingProxyType(updated)
            raise

    ExecutionQuoteChainEvidence.assert_projection_issued = assert_projection_issued
    ExecutionQuoteChainEvidence.submit_instruction_identity_bound = property(
        submit_instruction_identity_bound
    )
    ExecutionQuoteChainEvidence.provider_request_correlation_bound = property(
        provider_request_correlation_bound
    )
    ExecutionQuoteChainEvidence.actual_submitted_instruction_bound = property(
        actual_submitted_instruction_bound
    )
    ExecutionQuoteChainEvidence.acknowledgement_binding_matches = property(
        acknowledgement_binding_matches
    )
    ExecutionQuoteChainEvidence.evidence_sha256 = property(evidence_sha256)
    ExecutionQuoteChainEvidence.to_dict = to_dict
    globals()["build_execution_quote_chain_evidence"] = issue
    _ExecutionQuoteChainEvidenceMeta.seal(ExecutionQuoteChainEvidence)


_install_quote_chain_evidence_authority()
del _install_quote_chain_evidence_authority
