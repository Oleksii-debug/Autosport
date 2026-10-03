"""Revision-bound binary selection outcome evidence from canonical Betfair settlement.

This module is a read-only projection over #1272.  It does not create a second
settlement store, does not claim provider permanent finality, and does not authorize
training, forecasting, promotion, execution, bankroll mutation, or real-money use.

The projection deliberately distinguishes *bet* outcome from *selection* outcome:
for plain BACK they agree, while for plain LAY they are inverted. Handicap/line facts
and provider void-date facts remain settlement facts only and fail closed here because
betOutcome alone cannot prove an unqualified binary selection result for those cases.
VOIDED/CANCELLED/LAPSED, missing/unknown betOutcome values, and zero settled quantity
also cannot become binary selection labels.

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


_EVIDENCE_TYPE = BetfairBinarySelectionOutcomeEvidence


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
    if getattr(revision, "provider_handicap", None) is not None:
        raise BetfairOutcomeEvidenceError(
            "handicap settlement cannot prove unqualified binary selection outcome"
        )
    if getattr(revision, "provider_voided_date", None) is not None:
        raise BetfairOutcomeEvidenceError(
            "void-dated settlement cannot prove binary selection outcome"
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
    return _EVIDENCE_TYPE(
        evidence_id=_digest(payload),
        **{key: value for key, value in payload.items() if key not in {"schema", "schema_version"}},
    )


_PROJECT = _project
_PROJECT_CODE = getattr(_PROJECT, "__code__", None)
if _PROJECT_CODE is None:
    raise RuntimeError("Betfair settlement outcome projection code is unavailable")


def _build_dispatch_guard():
    store_type = _STORE_TYPE
    revision_type = _REVISION_TYPE
    current = _CURRENT
    current_code = _CURRENT_CODE
    project = _PROJECT
    project_code = _PROJECT_CODE
    evidence_type = _EVIDENCE_TYPE
    settlement_module = _settlement
    module_globals = globals()

    def require_dispatch() -> None:
        installed_current = vars(store_type).get("current")
        installed_project = module_globals.get("_project")
        installed_evidence_type = module_globals.get(
            "BetfairBinarySelectionOutcomeEvidence"
        )
        if (
            settlement_module.BetfairSettlementRevisionStore is not store_type
            or settlement_module.BetfairSettlementRevision is not revision_type
            or installed_current is not current
            or getattr(installed_current, "__code__", None) is not current_code
            or installed_project is not project
            or getattr(installed_project, "__code__", None) is not project_code
            or installed_evidence_type is not evidence_type
        ):
            raise BetfairOutcomeEvidenceError(
                "Betfair settlement outcome authority dispatch changed"
            )

    return require_dispatch


_require_dispatch = _build_dispatch_guard()
del _build_dispatch_guard


def _build_public_resolvers():
    require_dispatch = _require_dispatch
    require_dispatch_code = getattr(require_dispatch, "__code__", None)
    if require_dispatch_code is None:
        raise RuntimeError("Betfair settlement outcome dispatch guard code is unavailable")

    store_type = _STORE_TYPE
    evidence_type = _EVIDENCE_TYPE
    current = _CURRENT
    project = _PROJECT
    error_type = BetfairOutcomeEvidenceError
    module_globals = globals()

    def require_public_dispatch(resolve, require) -> None:
        installed_guard = module_globals.get("_require_dispatch")
        if (
            installed_guard is not require_dispatch
            or getattr(installed_guard, "__code__", None) is not require_dispatch_code
            or module_globals.get("resolve_current_binary_selection_outcome") is not resolve
            or module_globals.get("require_current_binary_selection_outcome") is not require
        ):
            raise error_type("Betfair settlement outcome public dispatch changed")
        require_dispatch()

    def resolve(
        store: store_type,
        *,
        bookmaker_id: str,
        account_id: str,
        external_bet_id: str,
    ) -> BetfairBinarySelectionOutcomeEvidence:
        """Project the exact current verified settlement revision into revocable outcome evidence."""
        require_public_dispatch(resolve, require_current)
        if type(store) is not store_type:
            raise error_type(
                "outcome evidence requires exact canonical settlement store"
            )
        namespace = getattr(store, "__dict__", None)
        if type(namespace) is dict and "current" in namespace:
            raise error_type("settlement current dispatch is instance-shadowed")

        revision = current(
            store,
            bookmaker_id,
            account_id,
            external_bet_id,
        )
        if revision is None:
            raise error_type("current settlement revision is absent")
        evidence = project(revision)

        require_public_dispatch(resolve, require_current)
        current_revision = current(
            store,
            bookmaker_id,
            account_id,
            external_bet_id,
        )
        if (
            current_revision is None
            or current_revision.revision_id != evidence.settlement_revision_id
        ):
            raise error_type(
                "settlement revision changed during outcome evidence projection"
            )
        require_public_dispatch(resolve, require_current)
        return evidence

    def require_current(
        store: store_type,
        evidence: BetfairBinarySelectionOutcomeEvidence,
    ) -> BetfairBinarySelectionOutcomeEvidence:
        """Revalidate revision-bound evidence immediately before downstream use."""
        require_public_dispatch(resolve, require_current)
        if type(evidence) is not evidence_type:
            raise error_type("outcome evidence type is not canonical")
        current_evidence = resolve(
            store,
            bookmaker_id=evidence.bookmaker_id,
            account_id=evidence.account_id,
            external_bet_id=evidence.external_bet_id,
        )
        if current_evidence != evidence:
            raise error_type(
                "outcome evidence was superseded by settlement correction"
            )
        require_public_dispatch(resolve, require_current)
        return current_evidence

    return resolve, require_current


(
    resolve_current_binary_selection_outcome,
    require_current_binary_selection_outcome,
) = _build_public_resolvers()
del _build_public_resolvers

__all__ = [
    "BetfairBinarySelectionOutcomeEvidence",
    "BetfairOutcomeEvidenceError",
    "require_current_binary_selection_outcome",
    "resolve_current_binary_selection_outcome",
]
