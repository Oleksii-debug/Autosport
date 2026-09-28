"""Read-only product-issued PAPER turnover evidence for one authoritative UTC day.

This module derives turnover from canonical PaperBook lifecycle state and the
rollback-resistant UTC day authority. It does not mutate PaperBook, grant atomic
admission, create reservations, infer provider-account activity, or authorize
real-money execution.
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
from .paper import PaperBook
from .risk_day_window import ProductDayRiskWindow, ProductDayRiskWindowStore


_SCHEMA: Final = "autosport.risk.paper-day-turnover-evidence"
_SCHEMA_VERSION: Final = 1
_METRIC_CLASS: Final = "PAPER_ACCEPTED_TURNOVER"
_SCOPE_CLASS: Final = "UTC_DAY"


class PaperDayTurnoverEvidenceError(RuntimeError):
    """Base error for PAPER day-turnover evidence derivation or verification."""


class PaperDayTurnoverEvidenceIncompleteError(PaperDayTurnoverEvidenceError):
    """Canonical PAPER state is insufficient for exact scoped turnover evidence."""


class PaperDayTurnoverEvidenceMismatchError(PaperDayTurnoverEvidenceError):
    """Caller-supplied evidence does not equal a fresh canonical re-resolution."""


def _canonical_json_sha256(payload: object) -> str:
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PaperDayTurnoverEvidenceError(
            "turnover evidence payload is outside canonical JSON domain"
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PaperDayTurnoverEvidenceError(
            "turnover evidence requires finite exact Decimal values"
        )
    if value == 0:
        return "0e0"
    sign, digits, exponent = value.as_tuple()
    canonical_digits = list(digits)
    canonical_exponent = exponent
    while canonical_digits and canonical_digits[-1] == 0:
        canonical_digits.pop()
        canonical_exponent += 1
    coefficient = "".join(str(digit) for digit in canonical_digits)
    prefix = "-" if sign else ""
    return f"{prefix}{coefficient}e{canonical_exponent}"


def _parse_timestamp(value: object, field_name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise PaperDayTurnoverEvidenceIncompleteError(
            f"{field_name} must be a non-empty canonical timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperDayTurnoverEvidenceIncompleteError(
            f"{field_name} must be valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperDayTurnoverEvidenceIncompleteError(
            f"{field_name} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def _exact_sum(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return Decimal("0")
    if any(
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < 0
        for value in values
    ):
        raise PaperDayTurnoverEvidenceIncompleteError(
            "turnover constituents must be non-negative finite exact Decimals"
        )
    nonzero = tuple(value for value in values if value != 0)
    if not nonzero:
        return Decimal("0")
    max_adjusted = max(value.adjusted() for value in nonzero)
    min_exponent = min(value.as_tuple().exponent for value in nonzero)
    precision = max(32, max_adjusted - min_exponent + len(nonzero) + 8)
    try:
        with localcontext() as context:
            context.prec = precision
            context.traps[Inexact] = True
            context.traps[InvalidOperation] = True
            result = sum(nonzero, Decimal("0"))
    except DecimalException as exc:
        raise PaperDayTurnoverEvidenceIncompleteError(
            "turnover constituent sum is not exactly representable"
        ) from exc
    if not result.is_finite():
        raise PaperDayTurnoverEvidenceIncompleteError(
            "turnover constituent sum is not finite"
        )
    return result


def _exact_nonnegative_difference(left: Decimal, right: Decimal) -> Decimal:
    if (
        not isinstance(left, Decimal)
        or not left.is_finite()
        or not isinstance(right, Decimal)
        or not right.is_finite()
        or left < 0
        or right < 0
        or right > left
    ):
        raise PaperDayTurnoverEvidenceError(
            "turnover difference requires finite non-negative ordered Decimals"
        )
    if left == right:
        return Decimal("0")
    nonzero = tuple(value for value in (left, right) if value != 0)
    max_adjusted = max(value.adjusted() for value in nonzero)
    min_exponent = min(value.as_tuple().exponent for value in nonzero)
    precision = max(32, max_adjusted - min_exponent + 8)
    try:
        with localcontext() as context:
            context.prec = precision
            context.traps[Inexact] = True
            context.traps[InvalidOperation] = True
            result = left - right
    except DecimalException as exc:
        raise PaperDayTurnoverEvidenceError(
            "turnover difference is not exactly representable"
        ) from exc
    if not result.is_finite() or result < 0:
        raise PaperDayTurnoverEvidenceError(
            "turnover difference is outside canonical domain"
        )
    return result


def _exact_product(left: Decimal, right: Decimal) -> Decimal:
    if (
        not isinstance(left, Decimal)
        or not left.is_finite()
        or not isinstance(right, Decimal)
        or not right.is_finite()
        or left < 0
        or right < 0
    ):
        raise PaperDayTurnoverEvidenceIncompleteError(
            "turnover cap factors must be non-negative finite exact Decimals"
        )
    precision = max(
        32,
        len(left.as_tuple().digits) + len(right.as_tuple().digits) + 8,
    )
    try:
        with localcontext() as context:
            context.prec = precision
            context.traps[Inexact] = True
            context.traps[InvalidOperation] = True
            result = left * right
    except DecimalException as exc:
        raise PaperDayTurnoverEvidenceIncompleteError(
            "turnover cap is not exactly representable"
        ) from exc
    if not result.is_finite():
        raise PaperDayTurnoverEvidenceIncompleteError(
            "turnover cap is not finite"
        )
    return result


@dataclass(frozen=True, slots=True)
class PaperDayTurnoverEvidence:
    """Immutable, re-resolvable PAPER accepted-stake turnover evidence."""

    goal_id: str
    goal_revision: int
    goal_contract_sha256: str
    bankroll_id: str
    currency: str
    day_key: str
    window_start: str
    window_end_exclusive: str
    window_state_sha256: str
    window_authority_generation: int
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
        for name in (
            "goal_id",
            "goal_contract_sha256",
            "bankroll_id",
            "currency",
            "day_key",
            "window_start",
            "window_end_exclusive",
            "window_state_sha256",
            "constituent_sha256",
            "evidence_sha256",
            "schema",
            "metric_class",
            "scope_class",
        ):
            if type(object.__getattribute__(self, name)) is not str:
                raise PaperDayTurnoverEvidenceError(
                    f"{name} must use the exact built-in string type"
                )
        for name in (
            "goal_revision",
            "window_authority_generation",
            "constituent_count",
            "schema_version",
        ):
            if type(object.__getattribute__(self, name)) is not int:
                raise PaperDayTurnoverEvidenceError(
                    f"{name} must use the exact built-in integer type"
                )
        for name in (
            "initial_bankroll",
            "confirmed_turnover",
            "turnover_cap",
            "residual_headroom",
        ):
            if type(object.__getattribute__(self, name)) is not Decimal:
                raise PaperDayTurnoverEvidenceError(
                    f"{name} must use the exact Decimal type"
                )
        if type(object.__getattribute__(self, "breached")) is not bool:
            raise PaperDayTurnoverEvidenceError(
                "breached must use the exact built-in boolean type"
            )

        if self.schema != _SCHEMA or self.schema_version != _SCHEMA_VERSION:
            raise PaperDayTurnoverEvidenceError(
                "turnover evidence schema identity is invalid"
            )
        if self.metric_class != _METRIC_CLASS or self.scope_class != _SCOPE_CLASS:
            raise PaperDayTurnoverEvidenceError(
                "turnover evidence metric/scope identity is invalid"
            )
        for name, value in (
            ("goal_id", self.goal_id),
            ("bankroll_id", self.bankroll_id),
            ("currency", self.currency),
            ("day_key", self.day_key),
            ("window_start", self.window_start),
            ("window_end_exclusive", self.window_end_exclusive),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise PaperDayTurnoverEvidenceError(
                    f"{name} must be a non-empty canonical string"
                )
        if (
            isinstance(self.goal_revision, bool)
            or not isinstance(self.goal_revision, int)
            or self.goal_revision <= 0
        ):
            raise PaperDayTurnoverEvidenceError(
                "goal_revision must be a positive integer"
            )
        if (
            isinstance(self.window_authority_generation, bool)
            or not isinstance(self.window_authority_generation, int)
            or self.window_authority_generation <= 0
        ):
            raise PaperDayTurnoverEvidenceError(
                "window_authority_generation must be positive"
            )
        if (
            isinstance(self.constituent_count, bool)
            or not isinstance(self.constituent_count, int)
            or self.constituent_count < 0
        ):
            raise PaperDayTurnoverEvidenceError(
                "constituent_count must be a non-negative integer"
            )
        for name, value in (
            ("initial_bankroll", self.initial_bankroll),
            ("confirmed_turnover", self.confirmed_turnover),
            ("turnover_cap", self.turnover_cap),
            ("residual_headroom", self.residual_headroom),
        ):
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value < 0
            ):
                raise PaperDayTurnoverEvidenceError(
                    f"{name} must be a non-negative finite exact Decimal"
                )
        if self.initial_bankroll <= 0:
            raise PaperDayTurnoverEvidenceError(
                "initial_bankroll must be positive"
            )
        if not isinstance(self.breached, bool):
            raise PaperDayTurnoverEvidenceError("breached must be boolean")
        expected_breached = self.confirmed_turnover > self.turnover_cap
        expected_headroom = (
            Decimal("0")
            if expected_breached
            else _exact_nonnegative_difference(
                self.turnover_cap,
                self.confirmed_turnover,
            )
        )
        if self.breached != expected_breached:
            raise PaperDayTurnoverEvidenceError(
                "breach flag conflicts with turnover/cap truth"
            )
        if self.residual_headroom != expected_headroom:
            raise PaperDayTurnoverEvidenceError(
                "residual headroom conflicts with turnover/cap truth"
            )
        for name, digest in (
            ("goal_contract_sha256", self.goal_contract_sha256),
            ("window_state_sha256", self.window_state_sha256),
            ("constituent_sha256", self.constituent_sha256),
            ("evidence_sha256", self.evidence_sha256),
        ):
            if (
                type(digest) is not str
                or len(digest) != 64
                or digest != digest.lower()
                or any(ch not in "0123456789abcdef" for ch in digest)
            ):
                raise PaperDayTurnoverEvidenceError(
                    f"{name} must be canonical SHA-256"
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


class PaperDayTurnoverResolver:
    """Derive and re-resolve read-only PAPER UTC-day turnover evidence."""

    @classmethod
    def resolve(
        cls,
        *,
        book: PaperBook,
        goal_store: EconomicGoalStore,
        window_store: ProductDayRiskWindowStore,
        window_evidence: ProductDayRiskWindow,
    ) -> PaperDayTurnoverEvidence:
        if type(book) is not PaperBook:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "book must be canonical PaperBook"
            )
        if type(goal_store) is not EconomicGoalStore:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "goal_store must be canonical EconomicGoalStore"
            )
        if type(window_store) is not ProductDayRiskWindowStore:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "window_store must be canonical ProductDayRiskWindowStore"
            )
        if type(window_evidence) is not ProductDayRiskWindow:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "window_evidence must be canonical ProductDayRiskWindow"
            )
        if (
            goal_store.workspace.expanduser().resolve(strict=False)
            != window_store.workspace
        ):
            raise PaperDayTurnoverEvidenceIncompleteError(
                "economic goal and risk day authority must share one canonical workspace"
            )

        try:
            goal = goal_store.load()
            goal_provenance = provenance_for(goal)
        except (OSError, TypeError, ValueError) as exc:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "durable EconomicGoal authority cannot be re-resolved"
            ) from exc

        try:
            PaperBook._validate_loaded_state(book)
        except (ArithmeticError, AttributeError, TypeError, ValueError) as exc:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "PaperBook lifecycle is not canonical"
            ) from exc

        try:
            current_window = window_store.require_current(window_evidence)
        except Exception as exc:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "UTC day-window evidence is not current product authority"
            ) from exc

        start = _parse_timestamp(current_window.window_start, "window_start")
        end = _parse_timestamp(
            current_window.window_end_exclusive,
            "window_end_exclusive",
        )
        if not start < end:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "UTC day window must have positive duration"
            )

        constituents: list[dict[str, str]] = []
        stakes: list[Decimal] = []
        for ticket in book.tickets.values():
            placed_at = _parse_timestamp(ticket.placed_at, "ticket.placed_at")
            if not (start <= placed_at < end):
                continue
            if ticket.bankroll_id != goal.bankroll_id:
                raise PaperDayTurnoverEvidenceIncompleteError(
                    "current-day PAPER ticket lacks exact EconomicGoal bankroll identity"
                )
            if ticket.currency != goal.currency:
                raise PaperDayTurnoverEvidenceIncompleteError(
                    "current-day PAPER ticket lacks exact EconomicGoal currency identity"
                )
            stakes.append(ticket.stake)
            constituents.append(
                {
                    "ticket_id": ticket.ticket_id,
                    "stake": _decimal_text(ticket.stake),
                    "placed_at": ticket.placed_at,
                    "bankroll_id": goal.bankroll_id,
                    "currency": goal.currency,
                }
            )

        constituents.sort(key=lambda item: item["ticket_id"])
        confirmed = _exact_sum(tuple(stakes))
        cap = _exact_product(book.initial_bankroll, goal.max_turnover_fraction)
        breached = confirmed > cap
        residual = (
            Decimal("0")
            if breached
            else _exact_nonnegative_difference(cap, confirmed)
        )

        constituent_sha256 = _canonical_json_sha256(
            {
                "metric_class": _METRIC_CLASS,
                "scope_class": _SCOPE_CLASS,
                "day_key": current_window.day_key,
                "constituents": constituents,
            }
        )

        payload = {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "metric_class": _METRIC_CLASS,
            "scope_class": _SCOPE_CLASS,
            "goal_id": goal.goal_id,
            "goal_revision": goal.revision,
            "goal_contract_sha256": goal_provenance.contract_sha256,
            "bankroll_id": goal.bankroll_id,
            "currency": goal.currency,
            "day_key": current_window.day_key,
            "window_start": current_window.window_start,
            "window_end_exclusive": current_window.window_end_exclusive,
            "window_state_sha256": current_window.state_sha256,
            "window_authority_generation": current_window.authority_generation,
            "initial_bankroll": _decimal_text(book.initial_bankroll),
            "confirmed_turnover": _decimal_text(confirmed),
            "turnover_cap": _decimal_text(cap),
            "residual_headroom": _decimal_text(residual),
            "breached": breached,
            "constituent_count": len(constituents),
            "constituent_sha256": constituent_sha256,
        }
        evidence_sha256 = _canonical_json_sha256(payload)

        return PaperDayTurnoverEvidence(
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            goal_contract_sha256=goal_provenance.contract_sha256,
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            day_key=current_window.day_key,
            window_start=current_window.window_start,
            window_end_exclusive=current_window.window_end_exclusive,
            window_state_sha256=current_window.state_sha256,
            window_authority_generation=current_window.authority_generation,
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
        candidate: PaperDayTurnoverEvidence,
        *,
        book: PaperBook,
        goal_store: EconomicGoalStore,
        window_store: ProductDayRiskWindowStore,
        window_evidence: ProductDayRiskWindow,
    ) -> PaperDayTurnoverEvidence:
        if type(candidate) is not PaperDayTurnoverEvidence:
            raise PaperDayTurnoverEvidenceMismatchError(
                "candidate must be canonical PaperDayTurnoverEvidence"
            )
        current = cls.resolve(
            book=book,
            goal_store=goal_store,
            window_store=window_store,
            window_evidence=window_evidence,
        )
        if candidate != current:
            raise PaperDayTurnoverEvidenceMismatchError(
                "turnover evidence does not match current canonical PAPER state"
            )
        return current
