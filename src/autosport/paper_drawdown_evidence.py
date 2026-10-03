from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    Underflow,
    localcontext,
)
from pathlib import Path
from types import FunctionType

from .domain import TicketStatus
from .economic_goal_provenance import provenance_for
from .economic_goal_store import EconomicGoalStore
from .paper import PaperBook


DRAW_DOWN_EVIDENCE_SCHEMA = "autosport.paper-realized-drawdown-evidence.v1"
DRAW_DOWN_PATH_SCHEMA = "autosport.paper-realized-equity-path.v1"
DRAW_DOWN_SOURCE_SCHEMA = "autosport.paper-drawdown-source-state.v1"
DRAW_DOWN_METRIC_CLASS = "REALIZED_SETTLED_EQUITY_DRAWDOWN"
DRAW_DOWN_SCOPE = "PAPER_CURRENT_CANONICAL_HISTORY"


class PaperDrawdownEvidenceError(ValueError):
    """Base failure for product-issued PAPER drawdown evidence."""


class PaperDrawdownEvidenceMismatchError(PaperDrawdownEvidenceError):
    """Raised when supplied evidence is not the current canonical result."""


def _exact_shape_sum(values: tuple[Decimal, ...]) -> Decimal:
    """Add canonical evidence decimals without ambient Decimal-context rounding."""

    if not values:
        return Decimal("0")
    min_exponent: int | None = None
    max_adjusted: int | None = None
    nonzero_count = 0
    for value in values:
        if type(value) is not Decimal or not value.is_finite():
            raise PaperDrawdownEvidenceError(
                "drawdown evidence transition requires finite exact Decimals"
            )
        if value.is_zero():
            continue
        parts = value.copy_abs().as_tuple()
        exponent = int(parts.exponent)
        adjusted = exponent + len(parts.digits) - 1
        min_exponent = exponent if min_exponent is None else min(min_exponent, exponent)
        max_adjusted = adjusted if max_adjusted is None else max(max_adjusted, adjusted)
        nonzero_count += 1
    if nonzero_count == 0:
        return Decimal("0")
    if min_exponent is None or max_adjusted is None:
        raise PaperDrawdownEvidenceError(
            "drawdown evidence transition arithmetic is invalid"
        )
    precision = max_adjusted - min_exponent + 2 + len(str(nonzero_count))
    context = Context(
        prec=max(1, precision),
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
    )
    context.traps[Inexact] = True
    context.traps[InvalidOperation] = True
    context.traps[Overflow] = True
    context.traps[Underflow] = True
    context.clear_flags()
    try:
        with localcontext(context):
            return sum(values, Decimal("0"))
    except ArithmeticError as exc:
        raise PaperDrawdownEvidenceError(
            "drawdown evidence transition arithmetic is invalid"
        ) from exc


def _shape_drawdown_fraction(drawdown: Decimal, peak: Decimal) -> Decimal:
    """Match the resolver's canonical rounded ratio semantics for shape checks."""

    if (
        type(drawdown) is not Decimal
        or not drawdown.is_finite()
        or drawdown < 0
        or type(peak) is not Decimal
        or not peak.is_finite()
        or peak <= 0
    ):
        raise PaperDrawdownEvidenceError(
            "drawdown evidence fraction arithmetic is invalid"
        )
    context = Context(
        prec=28,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
    )
    context.traps[Inexact] = False
    context.traps[InvalidOperation] = True
    context.traps[Overflow] = True
    context.traps[Underflow] = True
    context.clear_flags()
    try:
        with localcontext(context):
            return drawdown / peak
    except ArithmeticError as exc:
        raise PaperDrawdownEvidenceError(
            "drawdown evidence fraction arithmetic is invalid"
        ) from exc


@dataclass(frozen=True, slots=True)
class PaperRealizedEquityPoint:
    sequence: int
    point_id: str
    action: str
    ticket_id: str | None
    equity: Decimal
    realized_delta: Decimal
    event_time: str | None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 0:
            raise PaperDrawdownEvidenceError("equity point sequence must be non-negative")
        if type(self.point_id) is not str or not self.point_id:
            raise PaperDrawdownEvidenceError("equity point identity is required")
        if self.action not in {"initial", "open", "settle"}:
            raise PaperDrawdownEvidenceError("equity point action is invalid")
        if self.action == "initial":
            if self.ticket_id is not None or self.event_time is not None:
                raise PaperDrawdownEvidenceError(
                    "initial equity point cannot carry ticket/time identity"
                )
        elif type(self.ticket_id) is not str or not self.ticket_id:
            raise PaperDrawdownEvidenceError("lifecycle equity point requires ticket_id")
        for name, value in (
            ("equity", self.equity),
            ("realized_delta", self.realized_delta),
        ):
            if type(value) is not Decimal or not value.is_finite():
                raise PaperDrawdownEvidenceError(
                    f"equity point {name} must be a finite exact Decimal"
                )
        if self.equity < 0:
            raise PaperDrawdownEvidenceError("equity point cannot be negative")
        if self.action in {"initial", "open"} and self.realized_delta != Decimal("0"):
            raise PaperDrawdownEvidenceError(
                "initial/open equity point cannot carry realized P&L"
            )
        if self.event_time is not None and (
            type(self.event_time) is not str or not self.event_time
        ):
            raise PaperDrawdownEvidenceError(
                "equity point event_time must be non-empty or None"
            )


@dataclass(frozen=True, slots=True)
class PaperRealizedDrawdownEvidence:
    """Product drawdown projection over one canonical realized-equity path.

    The peak/trough identity fields identify the episode with maximum absolute
    drawdown amount. historical_max_drawdown_fraction is independently the
    worst fractional drawdown across all historical peaks.
    """

    schema: str
    scope: str
    metric_class: str
    goal_id: str
    goal_revision: int
    goal_contract_sha256: str
    bankroll_id: str
    currency: str
    source_state_sha256: str
    path_sha256: str
    evidence_sha256: str
    initial_equity: Decimal
    current_equity: Decimal
    peak_equity: Decimal
    minimum_equity: Decimal
    historical_max_drawdown_amount: Decimal
    historical_max_drawdown_fraction: Decimal
    historical_max_drawdown_peak_id: str | None
    historical_max_drawdown_trough_id: str | None
    current_drawdown_amount: Decimal
    recovered_to_peak: bool
    open_position_count: int
    settlement_availability_complete: bool
    as_known_at_supported: bool
    points: tuple[PaperRealizedEquityPoint, ...]

    def __post_init__(self) -> None:
        if self.schema != DRAW_DOWN_EVIDENCE_SCHEMA:
            raise PaperDrawdownEvidenceError("drawdown evidence schema is invalid")
        if self.scope != DRAW_DOWN_SCOPE or self.metric_class != DRAW_DOWN_METRIC_CLASS:
            raise PaperDrawdownEvidenceError("drawdown evidence scope/metric is invalid")
        if type(self.goal_id) is not str or not self.goal_id:
            raise PaperDrawdownEvidenceError("drawdown evidence goal_id is required")
        if type(self.goal_revision) is not int or self.goal_revision < 1:
            raise PaperDrawdownEvidenceError("drawdown evidence goal revision is invalid")
        if type(self.bankroll_id) is not str or not self.bankroll_id:
            raise PaperDrawdownEvidenceError("drawdown evidence bankroll_id is required")
        if (
            type(self.currency) is not str
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or self.currency != self.currency.upper()
        ):
            raise PaperDrawdownEvidenceError("drawdown evidence currency is invalid")
        for name in (
            "goal_contract_sha256",
            "source_state_sha256",
            "path_sha256",
            "evidence_sha256",
        ):
            value = getattr(self, name)
            if (
                type(value) is not str
                or len(value) != 64
                or value != value.lower()
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise PaperDrawdownEvidenceError(
                    f"drawdown evidence {name} must be lowercase SHA-256"
                )
        values = (
            self.initial_equity,
            self.current_equity,
            self.peak_equity,
            self.minimum_equity,
            self.historical_max_drawdown_amount,
            self.historical_max_drawdown_fraction,
            self.current_drawdown_amount,
        )
        if any(
            type(value) is not Decimal or not value.is_finite() or value < 0
            for value in values
        ):
            raise PaperDrawdownEvidenceError(
                "drawdown evidence monetary metrics must be non-negative finite Decimals"
            )
        if self.initial_equity <= 0 or self.peak_equity <= 0:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence initial/peak equity must be positive"
            )
        if self.peak_equity < self.initial_equity:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence peak cannot be below initial equity"
            )
        if self.current_equity > self.peak_equity:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence current equity cannot exceed peak"
            )
        if (
            self.minimum_equity > self.current_equity
            or self.minimum_equity > self.initial_equity
        ):
            raise PaperDrawdownEvidenceError("drawdown evidence minimum equity is invalid")
        if self.historical_max_drawdown_fraction > Decimal("1"):
            raise PaperDrawdownEvidenceError(
                "drawdown evidence historical fraction cannot exceed one"
            )
        if self.historical_max_drawdown_amount == 0:
            if (
                self.historical_max_drawdown_fraction != 0
                or self.historical_max_drawdown_peak_id is not None
                or self.historical_max_drawdown_trough_id is not None
            ):
                raise PaperDrawdownEvidenceError(
                    "zero historical drawdown cannot carry loss episode evidence"
                )
        elif (
            self.historical_max_drawdown_fraction <= 0
            or self.historical_max_drawdown_peak_id is None
            or self.historical_max_drawdown_trough_id is None
        ):
            raise PaperDrawdownEvidenceError(
                "positive historical drawdown requires fraction and episode identity"
            )
        if (
            type(self.recovered_to_peak) is not bool
            or self.recovered_to_peak != (self.current_equity == self.peak_equity)
        ):
            raise PaperDrawdownEvidenceError(
                "drawdown evidence recovery flag is inconsistent"
            )
        if type(self.settlement_availability_complete) is not bool:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence settlement availability flag is invalid"
            )
        if type(self.open_position_count) is not int or self.open_position_count < 0:
            raise PaperDrawdownEvidenceError("drawdown evidence open count is invalid")
        if self.as_known_at_supported is not False:
            raise PaperDrawdownEvidenceError(
                "current-history drawdown evidence cannot claim AS_KNOWN_AT authority"
            )
        if type(self.points) is not tuple or not self.points:
            raise PaperDrawdownEvidenceError("drawdown evidence requires a non-empty path")
        if any(type(point) is not PaperRealizedEquityPoint for point in self.points):
            raise PaperDrawdownEvidenceError(
                "drawdown evidence path requires canonical equity points"
            )
        if tuple(point.sequence for point in self.points) != tuple(range(len(self.points))):
            raise PaperDrawdownEvidenceError(
                "drawdown evidence points must use contiguous canonical sequence"
            )
        if self.points[0].action != "initial":
            raise PaperDrawdownEvidenceError("drawdown evidence path must begin at initial equity")
        if self.points[0].equity != self.initial_equity:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence initial point does not match initial equity"
            )
        previous_point = self.points[0]
        for point in self.points[1:]:
            expected_equity = _exact_shape_sum(
                (previous_point.equity, point.realized_delta)
            )
            if point.equity != expected_equity:
                raise PaperDrawdownEvidenceError(
                    "drawdown evidence equity transition does not match realized delta"
                )
            previous_point = point
        if self.points[-1].equity != self.current_equity:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence final point does not match current equity"
            )
        if len({point.point_id for point in self.points}) != len(self.points):
            raise PaperDrawdownEvidenceError(
                "drawdown evidence point identities must be unique"
            )

        opened_ticket_ids: set[str] = set()
        settled_ticket_ids: set[str] = set()
        settlement_times_complete = True
        for point in self.points[1:]:
            if point.action == "initial":
                raise PaperDrawdownEvidenceError(
                    "drawdown evidence initial action may appear only at path origin"
                )
            ticket_id = point.ticket_id
            if type(ticket_id) is not str or not ticket_id:
                raise PaperDrawdownEvidenceError(
                    "drawdown evidence lifecycle point requires canonical ticket identity"
                )
            if point.action == "open":
                if ticket_id in opened_ticket_ids:
                    raise PaperDrawdownEvidenceError(
                        "drawdown evidence ticket may open only once"
                    )
                opened_ticket_ids.add(ticket_id)
                continue
            if (
                ticket_id not in opened_ticket_ids
                or ticket_id in settled_ticket_ids
            ):
                raise PaperDrawdownEvidenceError(
                    "drawdown evidence settlement must follow one canonical open"
                )
            settled_ticket_ids.add(ticket_id)
            settlement_times_complete = (
                settlement_times_complete and point.event_time is not None
            )

        if self.open_position_count != len(opened_ticket_ids - settled_ticket_ids):
            raise PaperDrawdownEvidenceError(
                "drawdown evidence open count does not match the path"
            )
        if self.settlement_availability_complete != settlement_times_complete:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence settlement availability does not match the path"
            )
        if self.current_drawdown_amount > self.historical_max_drawdown_amount:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence current drawdown exceeds historical maximum"
            )
        if (self.current_drawdown_amount == 0) != self.recovered_to_peak:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence current drawdown is inconsistent with recovery"
            )

        point_ids = {point.point_id for point in self.points}
        if self.historical_max_drawdown_amount > 0:
            peak_id = self.historical_max_drawdown_peak_id
            trough_id = self.historical_max_drawdown_trough_id
            if (
                type(peak_id) is not str
                or not peak_id
                or type(trough_id) is not str
                or not trough_id
                or peak_id not in point_ids
                or trough_id not in point_ids
            ):
                raise PaperDrawdownEvidenceError(
                    "drawdown evidence loss episode identity is not in the path"
                )
            point_indexes = {
                point.point_id: index for index, point in enumerate(self.points)
            }
            if point_indexes[peak_id] >= point_indexes[trough_id]:
                raise PaperDrawdownEvidenceError(
                    "drawdown evidence loss episode order is invalid"
                )

        path_running_peak = self.points[0]
        path_maximum_drawdown = Decimal("0")
        path_maximum_fraction = Decimal("0")
        path_maximum_peak_id: str | None = None
        path_maximum_trough_id: str | None = None
        for point in self.points[1:]:
            if point.equity > path_running_peak.equity:
                path_running_peak = point
                continue
            drawdown = _exact_shape_sum(
                (path_running_peak.equity, point.equity.copy_negate())
            )
            drawdown_fraction = _shape_drawdown_fraction(
                drawdown,
                path_running_peak.equity,
            )
            if drawdown_fraction > path_maximum_fraction:
                path_maximum_fraction = drawdown_fraction
            if drawdown > path_maximum_drawdown:
                path_maximum_drawdown = drawdown
                path_maximum_peak_id = path_running_peak.point_id
                path_maximum_trough_id = point.point_id

        path_current_drawdown = _exact_shape_sum(
            (path_running_peak.equity, self.points[-1].equity.copy_negate())
        )
        if self.current_drawdown_amount != path_current_drawdown:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence current drawdown does not match the path"
            )
        if (
            self.historical_max_drawdown_amount != path_maximum_drawdown
            or self.historical_max_drawdown_peak_id != path_maximum_peak_id
            or self.historical_max_drawdown_trough_id != path_maximum_trough_id
        ):
            raise PaperDrawdownEvidenceError(
                "drawdown evidence historical maximum drawdown does not match the path"
            )
        if self.historical_max_drawdown_fraction != path_maximum_fraction:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence historical fraction does not match the path"
            )

        if min(point.equity for point in self.points) != self.minimum_equity:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence minimum does not match the path"
            )
        if max(point.equity for point in self.points) != self.peak_equity:
            raise PaperDrawdownEvidenceError(
                "drawdown evidence peak does not match the path"
            )


def _make_resolver() -> FunctionType:
    error_type = PaperDrawdownEvidenceError
    evidence_schema = DRAW_DOWN_EVIDENCE_SCHEMA
    path_schema = DRAW_DOWN_PATH_SCHEMA
    source_schema = DRAW_DOWN_SOURCE_SCHEMA
    metric_class = DRAW_DOWN_METRIC_CLASS
    scope = DRAW_DOWN_SCOPE
    path_type = Path
    path_new = Path.__new__
    path_init = Path.__init__
    path_expanduser = Path.expanduser
    path_resolve = Path.resolve
    path_truediv = Path.__truediv__
    canonical_path_type = type(Path())
    goal_store_type = EconomicGoalStore
    goal_store_new = EconomicGoalStore.__new__
    goal_store_init = EconomicGoalStore.__init__
    goal_store_file_name = EconomicGoalStore.FILE_NAME
    goal_load = EconomicGoalStore.load
    goal_load_code = goal_load.__code__
    goal_provenance = provenance_for
    goal_provenance_code = goal_provenance.__code__
    book_type = PaperBook
    book_load = book_type.load
    book_load_function = book_load.__func__
    book_load_code = book_load_function.__code__
    book_load_owner = book_load.__self__
    book_load_bytes = book_type.load_bytes
    book_load_bytes_function = book_load_bytes.__func__
    book_load_bytes_code = book_load_bytes_function.__code__
    book_load_bytes_owner = book_load_bytes.__self__
    book_from_raw = book_type._from_raw_snapshot
    book_from_raw_function = book_from_raw.__func__
    book_from_raw_code = book_from_raw_function.__code__
    book_from_raw_owner = book_from_raw.__self__
    goal_parser = goal_load.__globals__.get("economic_goal_from_json")
    if type(goal_parser) is not FunctionType:
        raise RuntimeError("economic-goal parser authority is unavailable")
    goal_parser_code = goal_parser.__code__
    goal_strict_json = goal_parser.__globals__.get("strict_json_loads")
    goal_from_payload = goal_parser.__globals__.get("economic_goal_from_payload")
    if type(goal_strict_json) is not FunctionType or type(goal_from_payload) is not FunctionType:
        raise RuntimeError("economic-goal parser dependency authority is unavailable")
    goal_strict_json_code = goal_strict_json.__code__
    goal_from_payload_code = goal_from_payload.__code__
    exact_ticket_status = TicketStatus
    exact_point_type = PaperRealizedEquityPoint
    exact_evidence_type = PaperRealizedDrawdownEvidence
    sha256 = hashlib.sha256
    json_dumps = json.dumps
    decimal_type = Decimal
    decimal_context_type = Context
    local_context = localcontext
    inexact_signal = Inexact
    invalid_signal = InvalidOperation
    overflow_signal = Overflow
    underflow_signal = Underflow
    round_half_even = ROUND_HALF_EVEN

    def canonical_sha256(payload: object) -> str:
        encoded = json_dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def exact_context() -> Context:
        context = decimal_context_type(
            prec=28,
            rounding=round_half_even,
            Emin=-999999,
            Emax=999999,
        )
        context.traps[inexact_signal] = True
        context.traps[invalid_signal] = True
        context.traps[overflow_signal] = True
        context.traps[underflow_signal] = True
        context.clear_flags()
        return context

    def exact_sum(values: tuple[Decimal, ...]) -> Decimal:
        if not values:
            return decimal_type("0")
        min_exponent: int | None = None
        max_adjusted: int | None = None
        nonzero_count = 0
        for value in values:
            if type(value) is not decimal_type or not value.is_finite():
                raise error_type(
                    "drawdown arithmetic requires finite exact Decimals"
                )
            if value.is_zero():
                continue
            parts = value.copy_abs().as_tuple()
            exponent = int(parts.exponent)
            adjusted = exponent + len(parts.digits) - 1
            min_exponent = exponent if min_exponent is None else min(min_exponent, exponent)
            max_adjusted = adjusted if max_adjusted is None else max(max_adjusted, adjusted)
            nonzero_count += 1
        if nonzero_count == 0:
            return decimal_type("0")
        assert min_exponent is not None and max_adjusted is not None
        precision = max_adjusted - min_exponent + 2 + len(str(nonzero_count))
        context = decimal_context_type(
            prec=max(1, precision),
            rounding=round_half_even,
            Emin=-999999,
            Emax=999999,
        )
        context.traps[inexact_signal] = True
        context.traps[invalid_signal] = True
        context.traps[overflow_signal] = True
        context.traps[underflow_signal] = True
        context.clear_flags()
        with local_context(context):
            return sum(values, decimal_type("0"))

    def resolver(workspace: str | Path) -> PaperRealizedDrawdownEvidence:
        if (
            goal_load.__code__ is not goal_load_code
            or goal_load.__globals__.get("economic_goal_from_json") is not goal_parser
            or goal_parser.__code__ is not goal_parser_code
            or goal_parser.__globals__.get("strict_json_loads") is not goal_strict_json
            or goal_strict_json.__code__ is not goal_strict_json_code
            or goal_parser.__globals__.get("economic_goal_from_payload") is not goal_from_payload
            or goal_from_payload.__code__ is not goal_from_payload_code
            or goal_provenance.__code__ is not goal_provenance_code
            or book_load.__func__ is not book_load_function
            or book_load_function.__code__ is not book_load_code
            or book_load.__self__ is not book_load_owner
            or book_type.load_bytes.__func__ is not book_load_bytes_function
            or book_load_bytes_function.__code__ is not book_load_bytes_code
            or book_type.load_bytes.__self__ is not book_load_bytes_owner
            or book_type._from_raw_snapshot.__func__ is not book_from_raw_function
            or book_from_raw_function.__code__ is not book_from_raw_code
            or book_type._from_raw_snapshot.__self__ is not book_from_raw_owner
        ):
            raise error_type(
                "drawdown durable source resolver authority changed"
            )
        if (
            path_type.__new__ is not path_new
            or path_type.__init__ is not path_init
            or path_type.expanduser is not path_expanduser
            or path_type.resolve is not path_resolve
            or path_type.__truediv__ is not path_truediv
        ):
            raise error_type(
                "drawdown workspace path authority changed"
            )
        workspace_path = path_type(workspace)
        if type(workspace_path) is not canonical_path_type:
            raise error_type(
                "drawdown workspace path type is not canonical"
            )
        expanded = path_expanduser(workspace_path)
        if type(expanded) is not canonical_path_type:
            raise error_type(
                "drawdown expanded workspace path type is not canonical"
            )
        root = path_resolve(expanded, strict=False)
        if type(root) is not canonical_path_type:
            raise error_type(
                "drawdown resolved workspace path type is not canonical"
            )
        book_path = path_truediv(root, "paper_book.json")
        if type(book_path) is not canonical_path_type:
            raise error_type(
                "drawdown book path type is not canonical"
            )
        if (
            goal_store_type.__new__ is not goal_store_new
            or goal_store_type.__init__ is not goal_store_init
            or goal_store_type.FILE_NAME != goal_store_file_name
        ):
            raise error_type(
                "drawdown economic-goal store authority changed"
            )
        goal_store = goal_store_type(root)
        if type(goal_store) is not goal_store_type:
            raise error_type(
                "drawdown economic-goal store type is not canonical"
            )
        try:
            goal = goal_load(goal_store)
            book = book_load(book_path)
            provenance = goal_provenance(goal)
        except (ArithmeticError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise error_type(
                "canonical PAPER drawdown source cannot be resolved"
            ) from exc
        if (
            goal_load.__globals__.get("economic_goal_from_json") is not goal_parser
            or goal_parser.__code__ is not goal_parser_code
            or goal_parser.__globals__.get("strict_json_loads") is not goal_strict_json
            or goal_strict_json.__code__ is not goal_strict_json_code
            or goal_parser.__globals__.get("economic_goal_from_payload") is not goal_from_payload
            or goal_from_payload.__code__ is not goal_from_payload_code
            or book_type.load_bytes.__func__ is not book_load_bytes_function
            or book_load_bytes_function.__code__ is not book_load_bytes_code
            or book_type.load_bytes.__self__ is not book_load_bytes_owner
            or book_type._from_raw_snapshot.__func__ is not book_from_raw_function
            or book_from_raw_function.__code__ is not book_from_raw_code
            or book_type._from_raw_snapshot.__self__ is not book_from_raw_owner
        ):
            raise error_type(
                "drawdown durable source resolver authority changed during load"
            )

        for ticket in book.tickets.values():
            if (
                ticket.bankroll_id != goal.bankroll_id
                or ticket.currency != goal.currency
            ):
                raise error_type(
                    "PAPER drawdown source ticket denomination is not bound to the durable goal"
                )

        source_tickets: list[dict[str, object]] = []
        for ticket_id, ticket in book.tickets.items():
            source_tickets.append(
                {
                    "ticket_id": ticket_id,
                    "stake": str(ticket.stake),
                    "payout": str(ticket.payout),
                    "status": ticket.status.value,
                    "placed_at": ticket.placed_at,
                    "settled_at": ticket.settled_at,
                    "strategy_reason": ticket.strategy_reason,
                    "provider_source_ids": list(ticket.provider_source_ids),
                    "provider_accounts": [
                        [source_id, account_id]
                        for source_id, account_id in ticket.provider_accounts
                    ],
                    "bankroll_id": ticket.bankroll_id,
                    "currency": ticket.currency,
                    "legs": [
                        {
                            "event_id": leg.event_id,
                            "market_id": leg.market_id,
                            "selection_id": leg.selection_id,
                            "locked_odds": str(leg.locked_odds),
                            "sport": leg.sport,
                            "exchange_side": leg.exchange_side,
                        }
                        for leg in ticket.legs
                    ],
                }
            )

        source_lifecycle: list[dict[str, object]] = []
        for index, raw_entry in enumerate(book._lifecycle):
            action, ticket_id, winners, voids = raw_entry
            source_lifecycle.append(
                {
                    "index": index,
                    "action": action,
                    "ticket_id": ticket_id,
                    "winning_quote_keys": list(winners),
                    "void_quote_keys": list(voids),
                    "settled_at": (
                        book._settlement_times[ticket_id]
                        if action == "settle"
                        else None
                    ),
                }
            )
        source_state_sha256 = canonical_sha256(
            {
                "schema": source_schema,
                "goal_contract_sha256": provenance.contract_sha256,
                "initial_bankroll": str(book.initial_bankroll),
                "balance": str(book.balance),
                "tickets": source_tickets,
                "lifecycle": source_lifecycle,
            }
        )

        points: list[PaperRealizedEquityPoint] = [
            exact_point_type(
                sequence=0,
                point_id="paper-initial-equity",
                action="initial",
                ticket_id=None,
                equity=book.initial_bankroll,
                realized_delta=decimal_type("0"),
                event_time=None,
            )
        ]
        equity = book.initial_bankroll
        running_peak = equity
        running_peak_id = "paper-initial-equity"
        minimum_equity = equity
        maximum_drawdown = decimal_type("0")
        maximum_fraction = decimal_type("0")
        maximum_peak_id: str | None = None
        maximum_trough_id: str | None = None
        settlement_availability_complete = True

        for lifecycle_index, raw_entry in enumerate(book._lifecycle):
            action, ticket_id, _winners, _voids = raw_entry
            ticket = book.tickets[ticket_id]
            if action == "open":
                delta = decimal_type("0")
                event_time = ticket.placed_at
            else:
                delta = exact_sum((ticket.payout, -ticket.stake))
                equity = exact_sum((equity, delta))
                event_time = book._settlement_times[ticket_id]
                settlement_availability_complete = (
                    settlement_availability_complete and event_time is not None
                )

            point_id = f"paper-lifecycle:{lifecycle_index}:{action}:{ticket_id}"
            point = exact_point_type(
                sequence=len(points),
                point_id=point_id,
                action=action,
                ticket_id=ticket_id,
                equity=equity,
                realized_delta=delta,
                event_time=event_time,
            )
            points.append(point)
            if equity < minimum_equity:
                minimum_equity = equity
            if equity > running_peak:
                running_peak = equity
                running_peak_id = point_id
                continue
            with local_context(exact_context()):
                drawdown = running_peak - equity
            ratio_context = exact_context()
            ratio_context.traps[inexact_signal] = False
            with local_context(ratio_context):
                drawdown_fraction = drawdown / running_peak
            if drawdown_fraction > maximum_fraction:
                maximum_fraction = drawdown_fraction
            if drawdown > maximum_drawdown:
                maximum_drawdown = drawdown
                maximum_peak_id = running_peak_id
                maximum_trough_id = point_id

        open_position_count = sum(
            1 for ticket in book.tickets.values()
            if ticket.status is exact_ticket_status.OPEN
        )
        committed = exact_sum(
            tuple(
                ticket.stake
                for ticket in book.tickets.values()
                if ticket.status is exact_ticket_status.OPEN
            )
        )
        expected_current_equity = exact_sum((book.balance, committed))
        current_drawdown = exact_sum((running_peak, -equity))
        if equity != expected_current_equity:
            raise error_type(
                "realized-settled equity path is inconsistent with canonical PaperBook"
            )
        if equity < 0 or current_drawdown < 0:
            raise error_type(
                "realized-settled equity path contains invalid negative state"
            )

        path_payload = {
            "schema": path_schema,
            "bankroll_id": goal.bankroll_id,
            "currency": goal.currency,
            "goal_contract_sha256": provenance.contract_sha256,
            "points": [
                {
                    "sequence": point.sequence,
                    "point_id": point.point_id,
                    "action": point.action,
                    "ticket_id": point.ticket_id,
                    "equity": str(point.equity),
                    "realized_delta": str(point.realized_delta),
                    "event_time": point.event_time,
                }
                for point in points
            ],
        }
        path_sha256 = canonical_sha256(path_payload)

        evidence_payload = {
            "schema": evidence_schema,
            "scope": scope,
            "metric_class": metric_class,
            "goal_id": goal.goal_id,
            "goal_revision": goal.revision,
            "goal_contract_sha256": provenance.contract_sha256,
            "bankroll_id": goal.bankroll_id,
            "currency": goal.currency,
            "source_state_sha256": source_state_sha256,
            "path_sha256": path_sha256,
            "initial_equity": str(book.initial_bankroll),
            "current_equity": str(equity),
            "peak_equity": str(running_peak),
            "minimum_equity": str(minimum_equity),
            "historical_max_drawdown_amount": str(maximum_drawdown),
            "historical_max_drawdown_fraction": str(maximum_fraction),
            "historical_max_drawdown_peak_id": maximum_peak_id,
            "historical_max_drawdown_trough_id": maximum_trough_id,
            "current_drawdown_amount": str(current_drawdown),
            "recovered_to_peak": equity >= running_peak,
            "open_position_count": open_position_count,
            "settlement_availability_complete": settlement_availability_complete,
            "as_known_at_supported": False,
        }
        evidence_sha256 = canonical_sha256(evidence_payload)
        return exact_evidence_type(
            schema=evidence_schema,
            scope=scope,
            metric_class=metric_class,
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            goal_contract_sha256=provenance.contract_sha256,
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            source_state_sha256=source_state_sha256,
            path_sha256=path_sha256,
            evidence_sha256=evidence_sha256,
            initial_equity=book.initial_bankroll,
            current_equity=equity,
            peak_equity=running_peak,
            minimum_equity=minimum_equity,
            historical_max_drawdown_amount=maximum_drawdown,
            historical_max_drawdown_fraction=maximum_fraction,
            historical_max_drawdown_peak_id=maximum_peak_id,
            historical_max_drawdown_trough_id=maximum_trough_id,
            current_drawdown_amount=current_drawdown,
            recovered_to_peak=equity >= running_peak,
            open_position_count=open_position_count,
            settlement_availability_complete=settlement_availability_complete,
            as_known_at_supported=False,
            points=tuple(points),
        )

    return resolver


def _freeze_resolver(function: FunctionType) -> FunctionType:
    error_type = PaperDrawdownEvidenceError
    if type(function) is not FunctionType:
        raise TypeError("drawdown resolver must be a Python function")
    function_type = FunctionType
    exact_type = type
    expected_code = function.__code__
    expected_closure = function.__closure__
    expected_closure_values = (
        None
        if expected_closure is None
        else tuple(cell.cell_contents for cell in expected_closure)
    )
    expected_closure_function_codes = (
        None
        if expected_closure_values is None
        else tuple(
            value.__code__ if type(value) is function_type else None
            for value in expected_closure_values
        )
    )

    def frozen(workspace: str | Path) -> PaperRealizedDrawdownEvidence:
        if (
            exact_type(function) is not function_type
            or function.__code__ is not expected_code
            or function.__closure__ is not expected_closure
        ):
            raise error_type(
                "drawdown resolver executable authority changed"
            )
        if expected_closure is not None:
            assert expected_closure_values is not None
            assert expected_closure_function_codes is not None
            for cell, expected, expected_function_code in zip(
                expected_closure,
                expected_closure_values,
                expected_closure_function_codes,
            ):
                current = cell.cell_contents
                if current is not expected:
                    raise error_type(
                        "drawdown resolver dependency authority changed"
                    )
                if (
                    expected_function_code is not None
                    and (
                        type(current) is not function_type
                        or current.__code__ is not expected_function_code
                    )
                ):
                    raise error_type(
                        "drawdown resolver dependency executable authority changed"
                    )
        return function(workspace)

    return frozen


resolve_paper_drawdown_evidence = _freeze_resolver(_make_resolver())


def _make_require_current(resolver: FunctionType) -> FunctionType:
    expected_code = resolver.__code__
    exact_evidence_type = PaperRealizedDrawdownEvidence
    function_type = FunctionType
    exact_type = type

    def require_current(
        workspace: str | Path,
        candidate: PaperRealizedDrawdownEvidence,
    ) -> PaperRealizedDrawdownEvidence:
        if type(candidate) is not exact_evidence_type:
            raise PaperDrawdownEvidenceMismatchError(
                "candidate must be canonical PaperRealizedDrawdownEvidence"
            )
        if exact_type(resolver) is not function_type or resolver.__code__ is not expected_code:
            raise PaperDrawdownEvidenceMismatchError(
                "drawdown resolver executable authority changed"
            )
        current = resolver(workspace)
        if current != candidate:
            raise PaperDrawdownEvidenceMismatchError(
                "drawdown evidence is not current canonical product authority"
            )
        return current

    return require_current


require_current_paper_drawdown_evidence = _make_require_current(
    resolve_paper_drawdown_evidence
)

del _make_require_current
del _make_resolver
