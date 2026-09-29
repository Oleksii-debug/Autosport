"""Revision-bound binary selection outcome evidence from canonical Betfair settlement.

This module is a read-only projection over #1272.  It does not create a second
settlement store, does not claim provider permanent finality, and does not authorize
training, forecasting, promotion, execution, bankroll mutation, or real-money use.

The projection deliberately distinguishes *bet* outcome from *selection* outcome:
for BACK they agree, while for LAY they are inverted.  VOIDED/CANCELLED/LAPSED,
missing/unknown betOutcome values, and zero settled quantity cannot become binary
selection labels.

Evidence is bound to one exact current settlement revision.  Because #1272 explicitly
keeps permanent_final=false, downstream users must re-resolve the store and require the
same revision id immediately before treating this observation as current outcome truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from . import betfair_settlement_revisions as _settlement


_SCHEMA = "autosport.betfair_binary_selection_outcome_evidence"
_SCHEMA_VERSION = 1
_ALLOWED_BET_OUTCOMES = frozenset({"WON", "LOST"})
_ALLOWED_SIDES = frozenset({"BACK", "LAY"})

_STORE_TYPE = _settlement.BetfairSettlementRevisionStore
_REVISION_TYPE = _settlement.BetfairSettlementRevision
_CURRENT = vars(_STORE_TYPE).get("current")
if not callable(_CURRENT):
    raise RuntimeError("Betfair settlement current-revision authority is unavailable")
_CURRENT_CODE = getattr(_CURRENT, "__code__", None)
if _CURRENT_CODE is None:
    raise RuntimeError("Betfair settlement current-revision code is unavailable")


class BetfairOutcomeEvidenceError(RuntimeError):
    pass


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairOutcomeEvidenceError("outcome evidence is not canonical JSON") from exc


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _require_dispatch() -> None:
    current = vars(_STORE_TYPE).get("current")
    if (
        _settlement.BetfairSettlementRevisionStore is not _STORE_TYPE
        or _settlement.BetfairSettlementRevision is not _REVISION_TYPE
        or current is not _CURRENT
        or getattr(current, "__code__", None) is not _CURRENT_CODE
    ):
        raise BetfairOutcomeEvidenceError(
            "Betfair settlement outcome authority dispatch changed"
        )


@dataclass(frozen=True, slots=True)
class BetfairBinarySelectionOutcomeEvidence:
    evidence_id: str
    bookmaker_id: str
    account_id: str
    external_bet_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    bet_outcome: str
    selection_won: bool
    target_value: int
    settled_at: str
    available_at: str
    settlement_revision_id: str
    settlement_revision_number: int
    settlement_content_sha256: str
    source_payload_sha256: str
    capture_evidence_sha256: str

    @property
    def permanent_final(self) -> bool:
        return False

    @property
    def training_label_authorized(self) -> bool:
        return False

    def semantic_payload(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "external_bet_id": self.external_bet_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "bet_outcome": self.bet_outcome,
            "selection_won": self.selection_won,
            "target_value": self.target_value,
            "settled_at": self.settled_at,
            "available_at": self.available_at,
            "settlement_revision_id": self.settlement_revision_id,
            "settlement_revision_number": self.settlement_revision_number,
            "settlement_content_sha256": self.settlement_content_sha256,
            "source_payload_sha256": self.source_payload_sha256,
            "capture_evidence_sha256": self.capture_evidence_sha256,
        }

    def __post_init__(self) -> None:
        if type(self.selection_won) is not bool:
            raise BetfairOutcomeEvidenceError("selection_won must be bool")
        if type(self.target_value) is not int or self.target_value not in (0, 1):
            raise BetfairOutcomeEvidenceError("target_value must be exact binary int")
        if self.target_value != int(self.selection_won):
            raise BetfairOutcomeEvidenceError("binary target disagrees with selection outcome")
        if self.side not in _ALLOWED_SIDES:
            raise BetfairOutcomeEvidenceError("unsupported Betfair side")
        if self.bet_outcome not in _ALLOWED_BET_OUTCOMES:
            raise BetfairOutcomeEvidenceError("unsupported Betfair betOutcome")
        expected_selection_won = (
            self.bet_outcome == "WON"
            if self.side == "BACK"
            else self.bet_outcome == "LOST"
        )
        if self.selection_won is not expected_selection_won:
            raise BetfairOutcomeEvidenceError("bet/selection outcome mapping mismatch")
        if self.evidence_id != _digest(self.semantic_payload()):
            raise BetfairOutcomeEvidenceError("outcome evidence identity mismatch")


def _project(revision: object) -> BetfairBinarySelectionOutcomeEvidence:
    if type(revision) is not _REVISION_TYPE:
        raise BetfairOutcomeEvidenceError(
            "outcome evidence requires exact canonical settlement revision"
        )
    if revision.provider_status != "SETTLED":
        raise BetfairOutcomeEvidenceError(
            "only SETTLED provider status can produce binary selection outcome evidence"
        )
    if revision.side not in _ALLOWED_SIDES:
        raise BetfairOutcomeEvidenceError("settlement side is not BACK or LAY")
    bet_outcome = getattr(revision, "bet_outcome", None)
    if bet_outcome not in _ALLOWED_BET_OUTCOMES:
        raise BetfairOutcomeEvidenceError(
            "settlement lacks supported WON/LOST betOutcome"
        )
    if revision.size_settled <= 0:
        raise BetfairOutcomeEvidenceError(
            "zero settled quantity cannot issue selection outcome evidence"
        )

    selection_won = (
        bet_outcome == "WON" if revision.side == "BACK" else bet_outcome == "LOST"
    )
    payload = {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "bookmaker_id": revision.bookmaker_id,
        "account_id": revision.account_id,
        "external_bet_id": revision.external_bet_id,
        "event_id": revision.event_id,
        "market_id": revision.market_id,
        "selection_id": revision.selection_id,
        "side": revision.side,
        "bet_outcome": bet_outcome,
        "selection_won": selection_won,
        "target_value": int(selection_won),
        "settled_at": revision.settled_date,
        "available_at": revision.available_at,
        "settlement_revision_id": revision.revision_id,
        "settlement_revision_number": revision.revision_number,
        "settlement_content_sha256": revision.content_sha256,
        "source_payload_sha256": revision.source_payload_sha256,
        "capture_evidence_sha256": revision.capture_evidence_sha256,
    }
    return BetfairBinarySelectionOutcomeEvidence(
        evidence_id=_digest(payload),
        **{key: value for key, value in payload.items() if key not in {"schema", "schema_version"}},
    )


def resolve_current_binary_selection_outcome(
    store: _STORE_TYPE,
    *,
    bookmaker_id: str,
    account_id: str,
    external_bet_id: str,
) -> BetfairBinarySelectionOutcomeEvidence:
    """Project the exact current verified settlement revision into revocable outcome evidence."""
    _require_dispatch()
    if type(store) is not _STORE_TYPE:
        raise BetfairOutcomeEvidenceError(
            "outcome evidence requires exact canonical settlement store"
        )
    namespace = getattr(store, "__dict__", None)
    if type(namespace) is dict and "current" in namespace:
        raise BetfairOutcomeEvidenceError("settlement current dispatch is instance-shadowed")

    revision = _CURRENT(
        store,
        bookmaker_id,
        account_id,
        external_bet_id,
    )
    if revision is None:
        raise BetfairOutcomeEvidenceError("current settlement revision is absent")
    evidence = _project(revision)

    _require_dispatch()
    current = _CURRENT(
        store,
        bookmaker_id,
        account_id,
        external_bet_id,
    )
    if current is None or current.revision_id != evidence.settlement_revision_id:
        raise BetfairOutcomeEvidenceError(
            "settlement revision changed during outcome evidence projection"
        )
    _require_dispatch()
    return evidence


def require_current_binary_selection_outcome(
    store: _STORE_TYPE,
    evidence: BetfairBinarySelectionOutcomeEvidence,
) -> BetfairBinarySelectionOutcomeEvidence:
    """Revalidate revision-bound evidence immediately before downstream use."""
    if type(evidence) is not BetfairBinarySelectionOutcomeEvidence:
        raise BetfairOutcomeEvidenceError("outcome evidence type is not canonical")
    current = resolve_current_binary_selection_outcome(
        store,
        bookmaker_id=evidence.bookmaker_id,
        account_id=evidence.account_id,
        external_bet_id=evidence.external_bet_id,
    )
    if current != evidence:
        raise BetfairOutcomeEvidenceError(
            "outcome evidence was superseded by settlement correction"
        )
    return current


__all__ = [
    "BetfairBinarySelectionOutcomeEvidence",
    "BetfairOutcomeEvidenceError",
    "require_current_binary_selection_outcome",
    "resolve_current_binary_selection_outcome",
]
