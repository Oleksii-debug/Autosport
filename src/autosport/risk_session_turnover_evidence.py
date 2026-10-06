"""Read-only PAPER accepted-turnover evidence for one durable economic session.

This is a #1546 composition consumer of ProductEconomicSessionStore, EconomicGoalStore
and PaperBook. It does not create another session/store/ledger, does not reserve
headroom, and does not authorize provider or real-money execution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, Inexact, InvalidOperation, localcontext
from typing import Final

from .economic_goal_provenance import provenance_for
from .economic_goal_store import EconomicGoalStore
from .economic_session import ProductEconomicSession, ProductEconomicSessionStore
from .paper import PaperBook


_SCHEMA: Final = "autosport.risk.paper-session-turnover-evidence"
_SCHEMA_VERSION: Final = 1
_METRIC_CLASS: Final = "PAPER_ACCEPTED_TURNOVER"
_SCOPE_CLASS: Final = "ECONOMIC_SESSION"
_HEX: Final = frozenset("0123456789abcdef")

_SESSION_CURRENT = ProductEconomicSessionStore.current
_SESSION_CURRENT_CODE = ProductEconomicSessionStore.current.__code__
_GOAL_LOAD = EconomicGoalStore.load
_GOAL_LOAD_CODE = EconomicGoalStore.load.__code__
_PAPER_LOAD_DESCRIPTOR = PaperBook.__dict__["load"]
_PAPER_LOAD_FUNC = _PAPER_LOAD_DESCRIPTOR.__func__
_PAPER_LOAD_CODE = _PAPER_LOAD_FUNC.__code__
_PAPER_LOAD = PaperBook.load
_PAPER_VALIDATE_DESCRIPTOR = PaperBook.__dict__["_validate_loaded_state"]
_PAPER_VALIDATE_FUNC = _PAPER_VALIDATE_DESCRIPTOR.__func__
_PAPER_VALIDATE_CODE = _PAPER_VALIDATE_FUNC.__code__
_PAPER_VALIDATE = PaperBook._validate_loaded_state
_PROVENANCE_FOR = provenance_for
_PROVENANCE_FOR_CODE = provenance_for.__code__


class PaperSessionTurnoverEvidenceError(RuntimeError):
    """Base error for PAPER economic-session turnover evidence."""


class PaperSessionTurnoverEvidenceIncompleteError(PaperSessionTurnoverEvidenceError):
    """Canonical state is insufficient for exact session turnover evidence."""


class PaperSessionTurnoverEvidenceMismatchError(PaperSessionTurnoverEvidenceError):
    """Candidate evidence is not equal to a fresh canonical resolution."""


def _instant(value: object, field: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise PaperSessionTurnoverEvidenceIncompleteError(
            f"{field} must be canonical timestamp text"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperSessionTurnoverEvidenceIncompleteError(
            f"{field} must be valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperSessionTurnoverEvidenceIncompleteError(
            f"{field} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise PaperSessionTurnoverEvidenceError(
            "turnover evidence requires exact finite Decimal values"
        )
    if value == 0:
        return "0e0"
    sign, digits, exponent = value.as_tuple()
    digits = list(digits)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    return f"{'-' if sign else ''}{coefficient}e{exponent}"


def _sha(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PaperSessionTurnoverEvidenceError(
            "turnover evidence payload is outside canonical JSON domain"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _exact_sum(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return Decimal("0")
    if any(type(value) is not Decimal or not value.is_finite() or value < 0 for value in values):
        raise PaperSessionTurnoverEvidenceIncompleteError(
            "turnover constituents must be non-negative finite exact Decimals"
        )
    nonzero = tuple(value for value in values if value != 0)
    if not nonzero:
        return Decimal("0")
    precision = max(
        32,
        max(value.adjusted() for value in nonzero)
        - min(value.as_tuple().exponent for value in nonzero)
        + len(nonzero)
        + 8,
    )
    try:
        with localcontext() as context:
            context.prec = precision
            context.traps[Inexact] = True
            context.traps[InvalidOperation] = True
            result = sum(nonzero, Decimal("0"))
    except DecimalException as exc:
        raise PaperSessionTurnoverEvidenceIncompleteError(
            "turnover sum is not exactly representable"
        ) from exc
    if not result.is_finite():
        raise PaperSessionTurnoverEvidenceIncompleteError("turnover sum is not finite")
    return result


def _exact_product(left: Decimal, right: Decimal) -> Decimal:
    if (
        type(left) is not Decimal
        or type(right) is not Decimal
        or not left.is_finite()
        or not right.is_finite()
        or left < 0
        or right < 0
    ):
        raise PaperSessionTurnoverEvidenceIncompleteError(
            "turnover cap factors must be non-negative finite exact Decimals"
        )
    try:
        with localcontext() as context:
            context.prec = max(32, len(left.as_tuple().digits) + len(right.as_tuple().digits) + 8)
            context.traps[Inexact] = True
            context.traps[InvalidOperation] = True
            result = left * right
    except DecimalException as exc:
        raise PaperSessionTurnoverEvidenceIncompleteError(
            "turnover cap is not exactly representable"
        ) from exc
    if not result.is_finite():
        raise PaperSessionTurnoverEvidenceIncompleteError("turnover cap is not finite")
    return result


def _headroom(cap: Decimal, confirmed: Decimal) -> Decimal:
    if confirmed >= cap:
        return Decimal("0")
    try:
        with localcontext() as context:
            context.prec = max(
                32,
                len(cap.as_tuple().digits) + len(confirmed.as_tuple().digits) + 8,
            )
            context.traps[Inexact] = True
            context.traps[InvalidOperation] = True
            result = cap - confirmed
    except DecimalException as exc:
        raise PaperSessionTurnoverEvidenceError(
            "turnover headroom is not exactly representable"
        ) from exc
    if not result.is_finite() or result < 0:
        raise PaperSessionTurnoverEvidenceError("turnover headroom is invalid")
    return result


@dataclass(frozen=True, slots=True)
class PaperSessionTurnoverEvidence:
    session_id: str
    session_state_sha256: str
    session_authority_generation: int
    session_started_at: str
    goal_id: str
    goal_revision: int
    goal_contract_sha256: str
    bankroll_id: str
    currency: str
    initial_bankroll: Decimal
    confirmed_turnover: Decimal
    turnover_cap: Decimal
    residual_headroom: Decimal
    breached: bool
    constituent_count: int
    constituent_sha256: str
    evidence_sha256: str
    schema: str = _SCHEMA
    schema_version: int = _SCHEMA_VERSION
    metric_class: str = _METRIC_CLASS
    scope_class: str = _SCOPE_CLASS

    def __post_init__(self) -> None:
        text_fields = (
            "session_id", "session_state_sha256", "session_started_at",
            "goal_id", "goal_contract_sha256", "bankroll_id", "currency",
            "constituent_sha256", "evidence_sha256", "schema",
            "metric_class", "scope_class",
        )
        for name in text_fields:
            value = object.__getattribute__(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise PaperSessionTurnoverEvidenceError(
                    f"{name} must be exact non-empty canonical text"
                )
        for name in (
            "session_authority_generation", "goal_revision",
            "constituent_count", "schema_version",
        ):
            value = object.__getattribute__(self, name)
            if type(value) is not int:
                raise PaperSessionTurnoverEvidenceError(
                    f"{name} must use the exact built-in integer type"
                )
        for name in (
            "initial_bankroll", "confirmed_turnover",
            "turnover_cap", "residual_headroom",
        ):
            value = object.__getattribute__(self, name)
            if type(value) is not Decimal or not value.is_finite() or value < 0:
                raise PaperSessionTurnoverEvidenceError(
                    f"{name} must be a non-negative finite exact Decimal"
                )
        if type(self.breached) is not bool:
            raise PaperSessionTurnoverEvidenceError(
                "breached must use the exact built-in boolean type"
            )
        if self.schema != _SCHEMA or self.schema_version != _SCHEMA_VERSION:
            raise PaperSessionTurnoverEvidenceError(
                "session turnover evidence schema identity is invalid"
            )
        if self.metric_class != _METRIC_CLASS or self.scope_class != _SCOPE_CLASS:
            raise PaperSessionTurnoverEvidenceError(
                "session turnover evidence metric/scope identity is invalid"
            )
        if self.session_authority_generation < 1 or self.goal_revision < 1:
            raise PaperSessionTurnoverEvidenceError(
                "session generation and goal revision must be positive"
            )
        if self.constituent_count < 0:
            raise PaperSessionTurnoverEvidenceError(
                "constituent_count must be non-negative"
            )
        if self.initial_bankroll <= 0:
            raise PaperSessionTurnoverEvidenceError(
                "initial_bankroll must be positive"
            )
        _instant(self.session_started_at, "session_started_at")
        for name in (
            "session_state_sha256", "goal_contract_sha256",
            "constituent_sha256", "evidence_sha256",
        ):
            digest = object.__getattribute__(self, name)
            if len(digest) != 64 or any(ch not in _HEX for ch in digest):
                raise PaperSessionTurnoverEvidenceError(
                    f"{name} must be canonical SHA-256"
                )
        expected_breached = self.confirmed_turnover > self.turnover_cap
        if self.breached != expected_breached:
            raise PaperSessionTurnoverEvidenceError(
                "breach flag conflicts with turnover/cap truth"
            )
        if self.residual_headroom != _headroom(
            self.turnover_cap, self.confirmed_turnover
        ):
            raise PaperSessionTurnoverEvidenceError(
                "residual headroom conflicts with turnover/cap truth"
            )

    @property
    def atomic_admission_authority(self) -> bool:
        return False

    @property
    def account_wide_provider_turnover_complete(self) -> bool:
        return False

    @property
    def real_money_execution_authorized(self) -> bool:
        return False


class PaperSessionTurnoverResolver:
    """Resolve PAPER turnover from current durable goal/session/book authorities."""

    @classmethod
    def resolve(
        cls,
        *,
        session_store: ProductEconomicSessionStore,
        session: ProductEconomicSession,
    ) -> PaperSessionTurnoverEvidence:
        if cls is not PaperSessionTurnoverResolver:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "session turnover resolver must be canonical exact class"
            )
        if type(session_store) is not ProductEconomicSessionStore:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "session_store must be canonical ProductEconomicSessionStore"
            )
        if type(session) is not ProductEconomicSession:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "session must be canonical ProductEconomicSession"
            )

        if (
            ProductEconomicSessionStore.current is not _SESSION_CURRENT
            or ProductEconomicSessionStore.current.__code__ is not _SESSION_CURRENT_CODE
            or EconomicGoalStore.load is not _GOAL_LOAD
            or EconomicGoalStore.load.__code__ is not _GOAL_LOAD_CODE
            or type(PaperBook.__dict__.get("load")) is not classmethod
            or PaperBook.__dict__["load"].__func__ is not _PAPER_LOAD_FUNC
            or PaperBook.__dict__["load"].__func__.__code__ is not _PAPER_LOAD_CODE
            or type(PaperBook.__dict__.get("_validate_loaded_state")) is not classmethod
            or PaperBook.__dict__["_validate_loaded_state"].__func__ is not _PAPER_VALIDATE_FUNC
            or PaperBook.__dict__["_validate_loaded_state"].__func__.__code__ is not _PAPER_VALIDATE_CODE
            or provenance_for is not _PROVENANCE_FOR
            or provenance_for.__code__ is not _PROVENANCE_FOR_CODE
        ):
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "session turnover dependency authority changed"
            )

        try:
            current = _SESSION_CURRENT(session_store)
        except Exception as exc:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "economic session cannot be re-resolved"
            ) from exc
        if current != session:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "economic session evidence is stale"
            )

        try:
            goal = _GOAL_LOAD(session_store.goal_store)
            goal_provenance = _PROVENANCE_FOR(goal)
            book = _PAPER_LOAD(session_store.paperbook_path)
            _PAPER_VALIDATE(book)
        except (OSError, ArithmeticError, AttributeError, TypeError, ValueError) as exc:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "durable goal/PaperBook authority cannot be re-resolved"
            ) from exc

        if (
            goal.goal_id != current.goal_id
            or goal.revision != current.goal_revision
            or goal.bankroll_id != current.bankroll_id
            or goal.currency != current.currency
            or goal_provenance.contract_sha256 != current.goal_contract_sha256
        ):
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "economic session no longer matches durable EconomicGoal authority"
            )

        started = _instant(current.started_at, "session.started_at")
        chronology = getattr(book, "_product_day_admissions", None)
        if type(chronology) is not dict or set(chronology) != set(book.tickets):
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "exact session turnover requires product-issued PAPER admission chronology"
            )
        constituents: list[dict[str, str]] = []
        stakes: list[Decimal] = []
        for ticket in book.tickets.values():
            witness = chronology.get(ticket.ticket_id)
            if type(witness) is not tuple or len(witness) != 6:
                raise PaperSessionTurnoverEvidenceIncompleteError(
                    "PAPER admission chronology witness is not canonical"
                )
            admission_ts = witness[0]
            admitted = _instant(admission_ts, "ticket.admission_ts")
            if admitted < started:
                continue
            if ticket.bankroll_id != current.bankroll_id:
                raise PaperSessionTurnoverEvidenceIncompleteError(
                    "session PAPER ticket lacks exact bankroll identity"
                )
            if ticket.currency != current.currency:
                raise PaperSessionTurnoverEvidenceIncompleteError(
                    "session PAPER ticket lacks exact currency identity"
                )
            if type(ticket.stake) is not Decimal or not ticket.stake.is_finite() or ticket.stake <= 0:
                raise PaperSessionTurnoverEvidenceIncompleteError(
                    "session PAPER ticket stake is not canonical"
                )
            stakes.append(ticket.stake)
            constituents.append(
                {
                    "ticket_id": ticket.ticket_id,
                    "stake": _decimal_text(ticket.stake),
                    "admission_ts": admission_ts,
                    "placed_at": ticket.placed_at,
                    "bankroll_id": current.bankroll_id,
                    "currency": current.currency,
                }
            )

        constituents.sort(key=lambda item: item["ticket_id"])
        confirmed = _exact_sum(tuple(stakes))
        cap = _exact_product(book.initial_bankroll, goal.max_turnover_fraction)
        residual = _headroom(cap, confirmed)
        breached = confirmed > cap
        constituent_sha256 = _sha(
            {
                "metric_class": _METRIC_CLASS,
                "scope_class": _SCOPE_CLASS,
                "session_id": current.session_id,
                "session_state_sha256": current.state_sha256,
                "constituents": constituents,
            }
        )
        payload = {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "metric_class": _METRIC_CLASS,
            "scope_class": _SCOPE_CLASS,
            "session_id": current.session_id,
            "session_state_sha256": current.state_sha256,
            "session_authority_generation": current.authority_generation,
            "session_started_at": current.started_at,
            "goal_id": goal.goal_id,
            "goal_revision": goal.revision,
            "goal_contract_sha256": goal_provenance.contract_sha256,
            "bankroll_id": goal.bankroll_id,
            "currency": goal.currency,
            "initial_bankroll": _decimal_text(book.initial_bankroll),
            "confirmed_turnover": _decimal_text(confirmed),
            "turnover_cap": _decimal_text(cap),
            "residual_headroom": _decimal_text(residual),
            "breached": breached,
            "constituent_count": len(constituents),
            "constituent_sha256": constituent_sha256,
        }
        evidence_sha256 = _sha(payload)

        return PaperSessionTurnoverEvidence(
            session_id=current.session_id,
            session_state_sha256=current.state_sha256,
            session_authority_generation=current.authority_generation,
            session_started_at=current.started_at,
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            goal_contract_sha256=goal_provenance.contract_sha256,
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            initial_bankroll=book.initial_bankroll,
            confirmed_turnover=confirmed,
            turnover_cap=cap,
            residual_headroom=residual,
            breached=breached,
            constituent_count=len(constituents),
            constituent_sha256=constituent_sha256,
            evidence_sha256=evidence_sha256,
        )

    @classmethod
    def require_current(
        cls,
        candidate: PaperSessionTurnoverEvidence,
        *,
        session_store: ProductEconomicSessionStore,
        session: ProductEconomicSession,
    ) -> PaperSessionTurnoverEvidence:
        if cls is not PaperSessionTurnoverResolver:
            raise PaperSessionTurnoverEvidenceMismatchError(
                "session turnover resolver must be canonical exact class"
            )
        if type(candidate) is not PaperSessionTurnoverEvidence:
            raise PaperSessionTurnoverEvidenceMismatchError(
                "candidate must be canonical PaperSessionTurnoverEvidence"
            )
        current = PaperSessionTurnoverResolver.resolve(
            session_store=session_store,
            session=session,
        )
        if candidate != current:
            raise PaperSessionTurnoverEvidenceMismatchError(
                "session turnover evidence is not current"
            )
        return current
