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
from pathlib import Path
from types import FunctionType
from typing import Final

from .economic_goal_provenance import provenance_for
from .economic_goal_store import EconomicGoalStore
from .economic_session import ProductEconomicSession, ProductEconomicSessionStore
from .paper import PaperBook
from .risk_day_window import ProductDayRiskWindow, ProductDayRiskWindowStore
from ._paperbook_current_binding_verifier import (
    seal_current_binding_consumer as _seal_current_binding_consumer,
)


_SCHEMA: Final = "autosport.risk.paper-day-turnover-evidence"
_SCHEMA_VERSION: Final = 1
_METRIC_CLASS: Final = "PAPER_ACCEPTED_TURNOVER"
_SCOPE_CLASS: Final = "UTC_DAY"

_TURNOVER_PATH_EXPANDUSER = Path.expanduser
_TURNOVER_PATH_EXPANDUSER_CODE = getattr(_TURNOVER_PATH_EXPANDUSER, "__code__", None)
_TURNOVER_PATH_RESOLVE = Path.resolve
_TURNOVER_PATH_RESOLVE_CODE = getattr(_TURNOVER_PATH_RESOLVE, "__code__", None)
_TURNOVER_PATH_TRUEDIV = Path.__truediv__
_TURNOVER_PATH_TRUEDIV_CODE = getattr(_TURNOVER_PATH_TRUEDIV, "__code__", None)
_TURNOVER_CANONICAL_PATH_TYPE = type(Path())


def _require_turnover_path_dispatch() -> None:
    for current, expected, expected_code in (
        (
            Path.expanduser,
            _TURNOVER_PATH_EXPANDUSER,
            _TURNOVER_PATH_EXPANDUSER_CODE,
        ),
        (Path.resolve, _TURNOVER_PATH_RESOLVE, _TURNOVER_PATH_RESOLVE_CODE),
        (Path.__truediv__, _TURNOVER_PATH_TRUEDIV, _TURNOVER_PATH_TRUEDIV_CODE),
    ):
        if (
            current is not expected
            or getattr(current, "__code__", None) is not expected_code
        ):
            raise PaperDayTurnoverEvidenceIncompleteError(
                "turnover workspace path authority changed"
            )


def _require_current_paper_book_binding(book: PaperBook, book_path) -> None:
    """Consume only the existing sealed PaperBook generation/path authority."""

    _REQUIRE_CURRENT_BINDING(book, book_path)


_PAPERBOOK_REQUIRE_CURRENT = _seal_current_binding_consumer(
    _require_current_paper_book_binding
)
del _require_current_paper_book_binding
del _seal_current_binding_consumer


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


class _PaperDayTurnoverResolverMeta(type):
    """Seal positive evidence resolver entrypoint descriptors."""

    def __setattr__(cls, name: str, value: object) -> None:
        if name in {"resolve", "require_current"} and name in cls.__dict__:
            raise TypeError("canonical PaperDayTurnoverResolver authority method is sealed")
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        if name in {"resolve", "require_current"} and name in cls.__dict__:
            raise TypeError("canonical PaperDayTurnoverResolver authority method is sealed")
        super().__delattr__(name)


class PaperDayTurnoverResolver(metaclass=_PaperDayTurnoverResolverMeta):
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
        if cls is not PaperDayTurnoverResolver:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "turnover resolver must be the canonical exact resolver class"
            )
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
        _require_turnover_path_dispatch()
        if (
            type(goal_store.workspace) is not _TURNOVER_CANONICAL_PATH_TYPE
            or type(window_store.workspace) is not _TURNOVER_CANONICAL_PATH_TYPE
            or type(goal_store.path) is not _TURNOVER_CANONICAL_PATH_TYPE
            or type(window_store.state_path) is not _TURNOVER_CANONICAL_PATH_TYPE
        ):
            raise PaperDayTurnoverEvidenceIncompleteError(
                "turnover workspace paths must use the canonical Path type"
            )
        goal_workspace = _TURNOVER_PATH_RESOLVE(
            _TURNOVER_PATH_EXPANDUSER(goal_store.workspace),
            strict=False,
        )
        window_workspace = _TURNOVER_PATH_RESOLVE(
            _TURNOVER_PATH_EXPANDUSER(window_store.workspace),
            strict=False,
        )
        if (
            type(goal_workspace) is not _TURNOVER_CANONICAL_PATH_TYPE
            or type(window_workspace) is not _TURNOVER_CANONICAL_PATH_TYPE
            or goal_workspace != window_workspace
        ):
            raise PaperDayTurnoverEvidenceIncompleteError(
                "economic goal and risk day authority must share one canonical workspace"
            )
        expected_goal_path = _TURNOVER_PATH_TRUEDIV(
            goal_workspace,
            EconomicGoalStore.FILE_NAME,
        )
        if goal_store.path != expected_goal_path:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "economic goal store path is not canonical for its workspace"
            )
        expected_day_path = _TURNOVER_PATH_TRUEDIV(
            _TURNOVER_PATH_TRUEDIV(window_workspace, ".autosport"),
            "risk_day_window.json",
        )
        if window_store.state_path != expected_day_path:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "risk day store path is not canonical for its workspace"
            )

        book_path = _TURNOVER_PATH_TRUEDIV(goal_workspace, "paper_book.json")
        try:
            # Positive turnover evidence is a projection of the current durable
            # workspace PaperBook generation, never a caller-selected in-memory
            # history. Require the supplied object to carry the existing sealed
            # path/generation binding, then load that exact durable authority and
            # derive every monetary constituent from the loaded snapshot.
            _PAPERBOOK_REQUIRE_CURRENT(book, book_path)
            durable_book = _PAPERBOOK_LOAD(book_path)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "PaperBook is not current durable workspace authority"
            ) from exc
        book = durable_book

        try:
            # Exact-type checks above make direct class dispatch authoritative here.
            # Do not let a mutable exact store instance shadow load and mint a
            # different goal contract for turnover evidence.
            goal = _ECONOMIC_GOAL_LOAD(goal_store)
            goal_provenance = provenance_for(goal)
        except (OSError, TypeError, ValueError) as exc:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "durable EconomicGoal authority cannot be re-resolved"
            ) from exc

        try:
            _PAPERBOOK_VALIDATE_LOADED_STATE(book)
        except (ArithmeticError, AttributeError, TypeError, ValueError) as exc:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "PaperBook lifecycle is not canonical"
            ) from exc

        try:
            # Bypass mutable instance dispatch for the same reason as EconomicGoalStore
            # above. A caller-owned require_current attribute is not product day
            # authority even when the container itself has the exact store type.
            current_window = _RISK_DAY_REQUIRE_CURRENT(
                window_store,
                window_evidence,
            )
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

        admissions = getattr(book, "_product_day_admissions", None)
        if type(admissions) is not dict:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "PaperBook lacks canonical product-day admission chronology"
            )

        constituents: list[dict[str, str]] = []
        stakes: list[Decimal] = []
        for ticket in book.tickets.values():
            # Day membership is never inferred from caller-selectable placed_at.
            # Every ticket participating in a positive bounded-day projection must
            # carry durable chronology written by the canonical admission path.
            # Legacy/direct tickets without that witness make the projection
            # incomplete and therefore fall back to whole-history turnover.
            if ticket.bankroll_id != goal.bankroll_id:
                raise PaperDayTurnoverEvidenceIncompleteError(
                    "PAPER ticket lacks exact EconomicGoal bankroll identity"
                )
            if ticket.currency != goal.currency:
                raise PaperDayTurnoverEvidenceIncompleteError(
                    "PAPER ticket lacks exact EconomicGoal currency identity"
                )
            witness = admissions.get(ticket.ticket_id)
            if type(witness) is not tuple or len(witness) != 6:
                raise PaperDayTurnoverEvidenceIncompleteError(
                    "PAPER ticket lacks product-issued UTC-day admission chronology"
                )
            (
                admission_ts,
                admission_day_key,
                admission_window_start,
                admission_window_end,
                admission_state_sha256,
                admission_generation,
            ) = witness
            admission_time = _parse_timestamp(
                admission_ts,
                "ticket.product_day_admission_ts",
            )
            admission_start = _parse_timestamp(
                admission_window_start,
                "ticket.product_day_window_start",
            )
            admission_end = _parse_timestamp(
                admission_window_end,
                "ticket.product_day_window_end_exclusive",
            )
            if not (admission_start <= admission_time < admission_end):
                raise PaperDayTurnoverEvidenceIncompleteError(
                    "PAPER product-day admission chronology is internally inconsistent"
                )

            # A fully earlier canonical UTC-day witness is safely historical. Any
            # future or overlapping-but-different window is ambiguous and cannot
            # create positive current-day headroom.
            if admission_end <= start:
                try:
                    _RISK_DAY_REQUIRE_COMMITTED_WINDOW(
                        window_store,
                        day_key=admission_day_key,
                        window_start=admission_window_start,
                        window_end_exclusive=admission_window_end,
                        state_sha256=admission_state_sha256,
                        authority_generation=admission_generation,
                    )
                except Exception as exc:
                    raise PaperDayTurnoverEvidenceIncompleteError(
                        "historical PAPER product-day admission authority cannot be re-resolved"
                    ) from exc
                continue
            if admission_start != start or admission_end != end:
                raise PaperDayTurnoverEvidenceIncompleteError(
                    "PAPER product-day admission window does not match current authority"
                )
            if (
                admission_day_key != current_window.day_key
                or admission_state_sha256 != current_window.state_sha256
                or admission_generation != current_window.authority_generation
            ):
                raise PaperDayTurnoverEvidenceIncompleteError(
                    "PAPER product-day admission generation does not match current authority"
                )
            stakes.append(ticket.stake)
            constituents.append(
                {
                    "ticket_id": ticket.ticket_id,
                    "stake": _decimal_text(ticket.stake),
                    "admission_ts": admission_ts,
                    "admission_day_key": admission_day_key,
                    "admission_window_state_sha256": admission_state_sha256,
                    "admission_window_authority_generation": str(
                        admission_generation
                    ),
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
        if cls is not PaperDayTurnoverResolver:
            raise PaperDayTurnoverEvidenceMismatchError(
                "turnover resolver must be the canonical exact resolver class"
            )
        if type(candidate) is not PaperDayTurnoverEvidence:
            raise PaperDayTurnoverEvidenceMismatchError(
                "candidate must be canonical PaperDayTurnoverEvidence"
            )
        current = _RISK_TURNOVER_RESOLVE_BOUND(
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


# Positive evidence resolution consumes existing canonical authorities through exact
# captured callables. Their implementations remain owned by their source modules.
_ECONOMIC_GOAL_LOAD = EconomicGoalStore.load
_PAPERBOOK_VALIDATE_LOADED_STATE = PaperBook._validate_loaded_state
_PAPERBOOK_LOAD = PaperBook.load
_RISK_DAY_REQUIRE_CURRENT = ProductDayRiskWindowStore.require_current
_RISK_DAY_REQUIRE_COMMITTED_WINDOW = ProductDayRiskWindowStore.require_committed_window


def _freeze_turnover_module_globals() -> dict[str, object]:
    source = globals()
    frozen: dict[str, object] = dict(source)
    function_type = FunctionType
    for name, value in tuple(source.items()):
        if type(value) is not function_type or value.__globals__ is not source:
            continue
        clone = function_type(
            value.__code__,
            frozen,
            name=value.__name__,
            argdefs=value.__defaults__,
            closure=value.__closure__,
        )
        if value.__kwdefaults__ is not None:
            clone.__kwdefaults__ = dict(value.__kwdefaults__)
        clone.__qualname__ = value.__qualname__
        clone.__doc__ = value.__doc__
        clone.__annotations__ = dict(value.__annotations__)
        frozen[name] = clone
    frozen["_ECONOMIC_GOAL_LOAD"] = _ECONOMIC_GOAL_LOAD
    frozen["_PAPERBOOK_VALIDATE_LOADED_STATE"] = _PAPERBOOK_VALIDATE_LOADED_STATE
    frozen["_PAPERBOOK_LOAD"] = _PAPERBOOK_LOAD
    frozen["_PAPERBOOK_REQUIRE_CURRENT"] = _PAPERBOOK_REQUIRE_CURRENT
    frozen["_RISK_DAY_REQUIRE_CURRENT"] = _RISK_DAY_REQUIRE_CURRENT
    frozen["_RISK_DAY_REQUIRE_COMMITTED_WINDOW"] = (
        _RISK_DAY_REQUIRE_COMMITTED_WINDOW
    )
    if "_RISK_TURNOVER_RESOLVE_BOUND" in source:
        frozen["_RISK_TURNOVER_RESOLVE_BOUND"] = source[
            "_RISK_TURNOVER_RESOLVE_BOUND"
        ]
    return frozen


def _seal_turnover_resolver_method(
    function: FunctionType,
    frozen_globals: dict[str, object],
) -> FunctionType:
    if type(function) is not FunctionType:
        raise TypeError("turnover resolver authority method must be a Python function")
    function_type = FunctionType
    code = function.__code__
    defaults = function.__defaults__
    kwdefaults = (
        None if function.__kwdefaults__ is None else dict(function.__kwdefaults__)
    )
    closure = function.__closure__
    name = function.__name__
    qualname = function.__qualname__
    doc = function.__doc__
    annotations = dict(function.__annotations__)

    def sealed(*args, **kwargs):
        if type(function) is not function_type or function.__code__ is not code:
            raise PaperDayTurnoverEvidenceIncompleteError(
                "canonical turnover resolver executable authority changed"
            )
        delegate = function_type(
            code,
            frozen_globals,
            name=name,
            argdefs=defaults,
            closure=closure,
        )
        if kwdefaults is not None:
            delegate.__kwdefaults__ = dict(kwdefaults)
        return delegate(*args, **kwargs)

    sealed.__name__ = name
    sealed.__qualname__ = qualname
    sealed.__doc__ = doc
    sealed.__annotations__ = annotations
    return sealed


_raw_resolve_descriptor = PaperDayTurnoverResolver.__dict__["resolve"]
if type(_raw_resolve_descriptor) is not classmethod:
    raise RuntimeError("canonical turnover resolve classmethod is unavailable")
_resolve_globals = _freeze_turnover_module_globals()
type.__setattr__(
    PaperDayTurnoverResolver,
    "resolve",
    classmethod(
        _seal_turnover_resolver_method(
            _raw_resolve_descriptor.__func__,
            _resolve_globals,
        )
    ),
)
_RISK_TURNOVER_RESOLVE_BOUND = PaperDayTurnoverResolver.resolve

_raw_require_descriptor = PaperDayTurnoverResolver.__dict__["require_current"]
if type(_raw_require_descriptor) is not classmethod:
    raise RuntimeError("canonical turnover require_current classmethod is unavailable")
_require_globals = _freeze_turnover_module_globals()
type.__setattr__(
    PaperDayTurnoverResolver,
    "require_current",
    classmethod(
        _seal_turnover_resolver_method(
            _raw_require_descriptor.__func__,
            _require_globals,
        )
    ),
)

del _raw_resolve_descriptor
del _resolve_globals
del _raw_require_descriptor
del _require_globals
del _RISK_TURNOVER_RESOLVE_BOUND


_SESSION_SCHEMA: Final = "autosport.risk.paper-session-turnover-evidence"
_SESSION_SCHEMA_VERSION: Final = 1
_SESSION_SCOPE_CLASS: Final = "ECONOMIC_SESSION"


class PaperSessionTurnoverEvidenceError(RuntimeError):
    """Base error for PAPER economic-session turnover evidence."""


class PaperSessionTurnoverEvidenceIncompleteError(PaperSessionTurnoverEvidenceError):
    """Canonical PAPER/session state is insufficient for exact session turnover."""


class PaperSessionTurnoverEvidenceMismatchError(PaperSessionTurnoverEvidenceError):
    """Caller evidence does not equal a fresh canonical session projection."""


@dataclass(frozen=True, slots=True)
class PaperSessionTurnoverEvidence:
    """Immutable PAPER accepted-stake turnover evidence for one economic session."""

    goal_id: str
    goal_revision: int
    goal_contract_sha256: str
    bankroll_id: str
    currency: str
    session_id: str
    session_started_at: str
    session_state_sha256: str
    session_authority_generation: int
    initial_bankroll: Decimal
    confirmed_turnover: Decimal
    turnover_cap: Decimal
    residual_headroom: Decimal
    breached: bool
    constituent_count: int
    constituent_sha256: str
    evidence_sha256: str
    schema: str = _SESSION_SCHEMA
    schema_version: int = _SESSION_SCHEMA_VERSION
    metric_class: str = _METRIC_CLASS
    scope_class: str = _SESSION_SCOPE_CLASS

    def __post_init__(self) -> None:
        if (
            self.schema != _SESSION_SCHEMA
            or type(self.schema_version) is not int
            or self.schema_version != _SESSION_SCHEMA_VERSION
            or self.metric_class != _METRIC_CLASS
            or self.scope_class != _SESSION_SCOPE_CLASS
        ):
            raise PaperSessionTurnoverEvidenceError(
                "session-turnover evidence schema identity is invalid"
            )
        for name in (
            "goal_id",
            "goal_contract_sha256",
            "bankroll_id",
            "currency",
            "session_id",
            "session_started_at",
            "session_state_sha256",
            "constituent_sha256",
            "evidence_sha256",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise PaperSessionTurnoverEvidenceError(
                    f"{name} must be non-empty canonical text"
                )
        for name in ("goal_revision", "session_authority_generation", "constituent_count"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "constituent_count" else 1):
                raise PaperSessionTurnoverEvidenceError(
                    f"{name} is outside the canonical integer domain"
                )
        for name in (
            "initial_bankroll",
            "confirmed_turnover",
            "turnover_cap",
            "residual_headroom",
        ):
            value = getattr(self, name)
            if type(value) is not Decimal or not value.is_finite() or value < 0:
                raise PaperSessionTurnoverEvidenceError(
                    f"{name} must be a non-negative finite exact Decimal"
                )
        if self.initial_bankroll <= 0:
            raise PaperSessionTurnoverEvidenceError("initial_bankroll must be positive")
        if type(self.breached) is not bool:
            raise PaperSessionTurnoverEvidenceError("breached must be exact boolean")
        expected_breached = self.confirmed_turnover > self.turnover_cap
        expected_room = (
            Decimal("0")
            if expected_breached
            else _exact_nonnegative_difference(self.turnover_cap, self.confirmed_turnover)
        )
        if self.breached != expected_breached or self.residual_headroom != expected_room:
            raise PaperSessionTurnoverEvidenceError(
                "session-turnover room conflicts with canonical turnover/cap truth"
            )
        for name in (
            "goal_contract_sha256",
            "session_state_sha256",
            "constituent_sha256",
            "evidence_sha256",
        ):
            digest = getattr(self, name)
            if (
                len(digest) != 64
                or digest != digest.lower()
                or any(ch not in "0123456789abcdef" for ch in digest)
            ):
                raise PaperSessionTurnoverEvidenceError(
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


class _PaperSessionTurnoverResolverMeta(type):
    def __setattr__(cls, name: str, value: object) -> None:
        if name in {"resolve", "require_current"} and name in cls.__dict__:
            raise TypeError("canonical PaperSessionTurnoverResolver authority method is sealed")
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        if name in {"resolve", "require_current"} and name in cls.__dict__:
            raise TypeError("canonical PaperSessionTurnoverResolver authority method is sealed")
        super().__delattr__(name)


class PaperSessionTurnoverResolver(metaclass=_PaperSessionTurnoverResolverMeta):
    """Project durable PAPER accepted stake onto the current economic session."""

    @classmethod
    def resolve(
        cls,
        *,
        book: PaperBook,
        goal_store: EconomicGoalStore,
        session_store: ProductEconomicSessionStore,
        session_evidence: ProductEconomicSession,
    ) -> PaperSessionTurnoverEvidence:
        if cls is not PaperSessionTurnoverResolver:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "session turnover resolver must be canonical exact class"
            )
        if type(book) is not PaperBook:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "book must be canonical PaperBook"
            )
        if type(goal_store) is not EconomicGoalStore:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "goal_store must be canonical EconomicGoalStore"
            )
        if type(session_store) is not ProductEconomicSessionStore:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "session_store must be canonical ProductEconomicSessionStore"
            )
        if type(session_evidence) is not ProductEconomicSession:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "session_evidence must be canonical ProductEconomicSession"
            )
        _require_turnover_path_dispatch()
        try:
            goal_workspace = _TURNOVER_PATH_RESOLVE(
                _TURNOVER_PATH_EXPANDUSER(goal_store.workspace),
                strict=False,
            )
            session_workspace = _TURNOVER_PATH_RESOLVE(
                _TURNOVER_PATH_EXPANDUSER(session_store.workspace),
                strict=False,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "session turnover workspace cannot be resolved canonically"
            ) from exc
        if goal_workspace != session_workspace:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "economic goal and economic session must share one workspace"
            )
        book_path = _TURNOVER_PATH_TRUEDIV(goal_workspace, "paper_book.json")
        try:
            _PAPERBOOK_REQUIRE_CURRENT(book, book_path)
            durable_book = _PAPERBOOK_LOAD(book_path)
            _PAPERBOOK_VALIDATE_LOADED_STATE(durable_book)
            goal = _ECONOMIC_GOAL_LOAD(goal_store)
            provenance = provenance_for(goal)
            current_session = ProductEconomicSessionStore.require_current(
                session_store,
                session_evidence,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "session turnover dependencies cannot be re-resolved"
            ) from exc

        if (
            current_session.goal_id != goal.goal_id
            or current_session.goal_revision != goal.revision
            or current_session.goal_contract_sha256 != provenance.contract_sha256
            or current_session.bankroll_id != goal.bankroll_id
            or current_session.currency != goal.currency
        ):
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "economic session identity conflicts with current EconomicGoal"
            )
        start = _parse_timestamp(current_session.started_at, "session_started_at")
        admissions = getattr(durable_book, "_product_day_admissions", None)
        if type(admissions) is not dict:
            raise PaperSessionTurnoverEvidenceIncompleteError(
                "PaperBook lacks canonical product admission chronology"
            )

        constituents: list[dict[str, str]] = []
        stakes: list[Decimal] = []
        for ticket in durable_book.tickets.values():
            if ticket.bankroll_id != goal.bankroll_id or ticket.currency != goal.currency:
                raise PaperSessionTurnoverEvidenceIncompleteError(
                    "PAPER ticket scope conflicts with EconomicGoal"
                )
            witness = admissions.get(ticket.ticket_id)
            if type(witness) is not tuple or len(witness) != 6:
                raise PaperSessionTurnoverEvidenceIncompleteError(
                    "PAPER ticket lacks product-issued admission chronology"
                )
            admission_ts = witness[0]
            admission_time = _parse_timestamp(
                admission_ts,
                "ticket.product_day_admission_ts",
            )
            if admission_time < start:
                continue
            stakes.append(ticket.stake)
            constituents.append(
                {
                    "ticket_id": ticket.ticket_id,
                    "stake": _decimal_text(ticket.stake),
                    "admission_ts": admission_ts,
                    "session_id": current_session.session_id,
                    "session_state_sha256": current_session.state_sha256,
                    "session_authority_generation": str(
                        current_session.authority_generation
                    ),
                    "bankroll_id": goal.bankroll_id,
                    "currency": goal.currency,
                }
            )

        constituents.sort(key=lambda item: item["ticket_id"])
        confirmed = _exact_sum(tuple(stakes))
        cap = _exact_product(durable_book.initial_bankroll, goal.max_turnover_fraction)
        breached = confirmed > cap
        residual = (
            Decimal("0")
            if breached
            else _exact_nonnegative_difference(cap, confirmed)
        )
        constituent_sha256 = _canonical_json_sha256(
            {
                "metric_class": _METRIC_CLASS,
                "scope_class": _SESSION_SCOPE_CLASS,
                "session_id": current_session.session_id,
                "constituents": constituents,
            }
        )
        payload = {
            "schema": _SESSION_SCHEMA,
            "schema_version": _SESSION_SCHEMA_VERSION,
            "metric_class": _METRIC_CLASS,
            "scope_class": _SESSION_SCOPE_CLASS,
            "goal_id": goal.goal_id,
            "goal_revision": goal.revision,
            "goal_contract_sha256": provenance.contract_sha256,
            "bankroll_id": goal.bankroll_id,
            "currency": goal.currency,
            "session_id": current_session.session_id,
            "session_started_at": current_session.started_at,
            "session_state_sha256": current_session.state_sha256,
            "session_authority_generation": current_session.authority_generation,
            "initial_bankroll": _decimal_text(durable_book.initial_bankroll),
            "confirmed_turnover": _decimal_text(confirmed),
            "turnover_cap": _decimal_text(cap),
            "residual_headroom": _decimal_text(residual),
            "breached": breached,
            "constituent_count": len(constituents),
            "constituent_sha256": constituent_sha256,
        }
        evidence_sha256 = _canonical_json_sha256(payload)
        return PaperSessionTurnoverEvidence(
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            goal_contract_sha256=provenance.contract_sha256,
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            session_id=current_session.session_id,
            session_started_at=current_session.started_at,
            session_state_sha256=current_session.state_sha256,
            session_authority_generation=current_session.authority_generation,
            initial_bankroll=durable_book.initial_bankroll,
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
        book: PaperBook,
        goal_store: EconomicGoalStore,
        session_store: ProductEconomicSessionStore,
        session_evidence: ProductEconomicSession,
    ) -> PaperSessionTurnoverEvidence:
        if cls is not PaperSessionTurnoverResolver:
            raise PaperSessionTurnoverEvidenceMismatchError(
                "session turnover resolver must be canonical exact class"
            )
        if type(candidate) is not PaperSessionTurnoverEvidence:
            raise PaperSessionTurnoverEvidenceMismatchError(
                "candidate must be canonical PaperSessionTurnoverEvidence"
            )
        current = cls.resolve(
            book=book,
            goal_store=goal_store,
            session_store=session_store,
            session_evidence=session_evidence,
        )
        if candidate != current:
            raise PaperSessionTurnoverEvidenceMismatchError(
                "session turnover evidence does not match current canonical PAPER state"
            )
        return current
