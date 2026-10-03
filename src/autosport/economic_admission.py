from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import FunctionType

from .domain import PaperTicket, TicketLeg
from .economic_goal_provenance import provenance_for
from .economic_goal_store import EconomicGoalStore
from .paper import PaperBook
from .recovery import transaction_history_requires_recovery
from .risk import PaperRiskPolicy, ProposedTicketRiskContext, RiskDecision
from .risk_day_window import ProductDayRiskWindow, ProductDayRiskWindowStore
from .risk_turnover_evidence import PaperDayTurnoverEvidence, PaperDayTurnoverResolver
from .run_registry import RunRegistry, UnresolvedExperimentError
from .run_transaction import RunTransaction
from .workspace_lock import WorkspaceEconomicLock


# The admission critical section is only atomic while the exact reviewed
# WorkspaceEconomicLock executable graph remains installed. Freezing this module's
# globals preserves the class object, not its mutable Python class attributes, so
# witness the lock dispatch before entering the economic mutation boundary.
_WORKSPACE_LOCK_FILE_NAME = WorkspaceEconomicLock.FILE_NAME
_WORKSPACE_LOCK_METHOD_NAMES = (
    "__init__",
    "acquire",
    "release",
    "__enter__",
    "__exit__",
    "_open_lock_handle",
    "_open_new_lock_handle",
    "_validate_existing_lock_path",
    "_validate_open_handle_identity",
    "_require_regular_file",
    "_require_single_link",
    "_lock_handle",
    "_unlock_handle",
)
_WORKSPACE_LOCK_DISPATCH_WITNESSES = tuple(
    (
        name,
        WorkspaceEconomicLock.__dict__[name],
        (
            WorkspaceEconomicLock.__dict__[name].__func__.__code__
            if type(WorkspaceEconomicLock.__dict__[name]) is staticmethod
            else WorkspaceEconomicLock.__dict__[name].__code__
        ),
    )
    for name in _WORKSPACE_LOCK_METHOD_NAMES
)


def _require_workspace_lock_dispatch() -> None:
    if (
        WorkspaceEconomicLock.__dict__.get("FILE_NAME") != _WORKSPACE_LOCK_FILE_NAME
        or WorkspaceEconomicLock.FILE_NAME != _WORKSPACE_LOCK_FILE_NAME
    ):
        raise RuntimeError("workspace economic lock authority changed")
    for name, expected_descriptor, expected_code in _WORKSPACE_LOCK_DISPATCH_WITNESSES:
        current_descriptor = WorkspaceEconomicLock.__dict__.get(name)
        if current_descriptor is not expected_descriptor:
            raise RuntimeError("workspace economic lock executable authority changed")
        current_function = (
            current_descriptor.__func__
            if type(current_descriptor) is staticmethod
            else current_descriptor
        )
        if (
            type(current_function) is not FunctionType
            or current_function.__code__ is not expected_code
        ):
            raise RuntimeError("workspace economic lock executable authority changed")


@dataclass(frozen=True, slots=True)
class PaperAdmissionResult:
    """Result of one risk-evaluate + PAPER ticket-open critical section."""

    risk: RiskDecision
    ticket: PaperTicket | None
    book: PaperBook

    @property
    def admitted(self) -> bool:
        return self.ticket is not None


@dataclass(frozen=True, slots=True)
class _PaperDayTurnoverSnapshot:
    """Read-only pre-lock evidence that must be revalidated under the writer lock."""

    book: PaperBook
    evidence: PaperDayTurnoverEvidence
    window_evidence: ProductDayRiskWindow


def _positive_decimal(value: Decimal | str) -> Decimal:
    if type(value) is Decimal:
        amount = value
    elif type(value) is str:
        try:
            amount = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("stake must be a finite positive decimal") from exc
    else:
        raise ValueError("stake must be a finite positive decimal")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("stake must be a finite positive decimal")
    return amount


def _same_semantic_book_state(expected: PaperBook, observed: PaperBook) -> bool:
    """Compare the complete validated economic state published by one admission."""

    PaperBook._validate_loaded_state(expected)
    PaperBook._validate_loaded_state(observed)
    return (
        observed.initial_bankroll == expected.initial_bankroll
        and observed.balance == expected.balance
        and observed.tickets == expected.tickets
        and observed._lifecycle == expected._lifecycle
        and observed._settlement_times == expected._settlement_times
    )


def _validate_context_binding(
    *,
    context: ProposedTicketRiskContext | None,
    legs: tuple[TicketLeg, ...],
    placed_at: str,
    provider_source_ids: tuple[str, ...],
    provider_accounts: tuple[tuple[str, str], ...],
    bankroll_id: str | None,
    currency: str | None,
) -> None:
    """Bind the risk-reviewed proposal to the exact ticket that will be opened."""

    if context is None:
        return
    if context.legs != legs:
        raise ValueError("risk context legs must match admitted ticket legs")
    if context.proposal_ts is not None and context.proposal_ts != placed_at:
        raise ValueError(
            "risk context proposal_ts must match admitted ticket placed_at"
        )
    if context.provider_accounts != provider_accounts:
        raise ValueError(
            "risk context provider accounts must match admitted ticket provenance"
        )
    if context.bankroll_id != bankroll_id or context.currency != currency:
        raise ValueError(
            "risk context bankroll and currency must match admitted ticket provenance"
        )
    if context.quotes:
        quote_source_ids = tuple(sorted({quote.source_id for quote in context.quotes}))
        if quote_source_ids != provider_source_ids:
            raise ValueError(
                "risk context quote sources must match admitted ticket provider sources"
            )


def _parse_utc_timestamp(value: str) -> datetime | None:
    if type(value) is not str or not value or value != value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _prepare_paper_day_turnover_snapshot(
    *,
    root: Path,
    book_path: Path,
    risk_policy: PaperRiskPolicy,
) -> _PaperDayTurnoverSnapshot | None:
    """Resolve product-issued day turnover before the admission lock is acquired.

    ProductDayRiskWindowStore itself owns the canonical workspace lock while it
    resolves/advances the UTC-day authority. Admission therefore resolves a
    read-only snapshot first and later revalidates every mutable dependency while
    holding its own economic writer lock. Any intervening change disables the
    bounded-day override and falls back to the conservative whole-history policy.
    """

    goal = risk_policy.economic_goal
    if goal is None or not book_path.exists():
        return None
    try:
        goal_store = EconomicGoalStore(root)
        if goal_store.load() != goal:
            return None
        snapshot_book = PaperBook.load(book_path)
        window_store = ProductDayRiskWindowStore(root)
        window = window_store.current()
        if not window.product_clock_authoritative:
            return None
        evidence = PaperDayTurnoverResolver.resolve(
            book=snapshot_book,
            goal_store=goal_store,
            window_store=window_store,
            window_evidence=window,
        )
    except (ArithmeticError, OSError, RuntimeError, TypeError, ValueError):
        return None
    return _PaperDayTurnoverSnapshot(
        book=snapshot_book,
        evidence=evidence,
        window_evidence=window,
    )


def _revalidated_product_day_turnover_room(
    *,
    snapshot: _PaperDayTurnoverSnapshot | None,
    root: Path,
    book: PaperBook,
    risk_policy: PaperRiskPolicy,
    placed_at: str,
    workspace_lock: WorkspaceEconomicLock,
) -> Decimal | None:
    """Return bounded UTC-day room only after canonical under-lock re-resolution."""

    if snapshot is None:
        return None
    goal = risk_policy.economic_goal
    if goal is None:
        return None
    try:
        if not _same_semantic_book_state(snapshot.book, book):
            return None
        durable_goal = EconomicGoalStore(root).load()
        if durable_goal != goal:
            return None

        # Re-resolve the product clock + independent monotonic generation while the
        # exact economic writer lock is already held. Raw risk-day bytes or wall-clock
        # equality are not positive authority and cannot substitute for this check.
        window_store = ProductDayRiskWindowStore(root)
        current_window = ProductDayRiskWindowStore.require_current_under_lock(
            window_store,
            snapshot.window_evidence,
            workspace_lock=workspace_lock,
        )

        evidence = snapshot.evidence
        provenance = provenance_for(goal)
        if (
            evidence.goal_id != goal.goal_id
            or evidence.goal_revision != goal.revision
            or evidence.goal_contract_sha256 != provenance.contract_sha256
            or evidence.bankroll_id != goal.bankroll_id
            or evidence.currency != goal.currency
            or evidence.initial_bankroll != book.initial_bankroll
            or evidence.day_key != current_window.day_key
            or evidence.window_start != current_window.window_start
            or evidence.window_end_exclusive != current_window.window_end_exclusive
            or evidence.window_state_sha256 != current_window.state_sha256
            or evidence.window_authority_generation
            != current_window.authority_generation
            or not current_window.product_clock_authoritative
            or evidence.breached
        ):
            return None

        candidate_time = _parse_utc_timestamp(placed_at)
        window_start = _parse_utc_timestamp(evidence.window_start)
        window_end = _parse_utc_timestamp(evidence.window_end_exclusive)
        if (
            candidate_time is None
            or window_start is None
            or window_end is None
            or not (window_start <= candidate_time < window_end)
        ):
            return None
        room = evidence.residual_headroom
        if type(room) is not Decimal or not room.is_finite() or room < 0:
            return None
        return room
    except (ArithmeticError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _freeze_external_python_function_graph(function: FunctionType) -> FunctionType:
    """Detach one Python function and its same-module helper graph from rebinding."""

    if type(function) is not FunctionType or function.__closure__ is not None:
        raise RuntimeError("canonical risk helper must be a closure-free Python function")
    source_globals = function.__globals__
    frozen_globals: dict[str, object] = dict(source_globals)
    clones: dict[str, FunctionType] = {}
    for name, value in tuple(source_globals.items()):
        if (
            type(value) is FunctionType
            and value.__globals__ is source_globals
            and value.__closure__ is None
        ):
            clone = FunctionType(
                value.__code__,
                frozen_globals,
                name=value.__name__,
                argdefs=value.__defaults__,
            )
            if value.__kwdefaults__ is not None:
                clone.__kwdefaults__ = dict(value.__kwdefaults__)
            clone.__annotations__ = dict(value.__annotations__)
            clone.__qualname__ = value.__qualname__
            clone.__doc__ = value.__doc__
            clones[name] = clone
    frozen_globals.update(clones)
    frozen = FunctionType(
        function.__code__,
        frozen_globals,
        name=function.__name__,
        argdefs=function.__defaults__,
    )
    if function.__kwdefaults__ is not None:
        frozen.__kwdefaults__ = dict(function.__kwdefaults__)
    frozen.__annotations__ = dict(function.__annotations__)
    frozen.__qualname__ = function.__qualname__
    frozen.__doc__ = function.__doc__
    return frozen


# Freeze the exact canonical risk helper descriptors used by the positive
# post-turnover continuation. This mirrors PaperRiskPolicy.evaluate's own helper
# witnesses so a later class/descriptor mutation cannot widen admission authority.
_RISK_BOOK_STATE_DESCRIPTOR = PaperRiskPolicy.__dict__["_book_state"]
_RISK_HISTORY_DESCRIPTOR = PaperRiskPolicy.__dict__["_goal_history_rooms"]
_RISK_QUOTE_DESCRIPTOR = PaperRiskPolicy.__dict__["_quote_risk_decision"]
_RISK_RUIN_DESCRIPTOR = PaperRiskPolicy.__dict__["_risk_of_ruin_evidence_decision"]
_RISK_DERIVED_DESCRIPTOR = PaperRiskPolicy.__dict__["_derived_risk_values"]

_RISK_BOOK_STATE = _RISK_BOOK_STATE_DESCRIPTOR.__func__
_RISK_HISTORY = _RISK_HISTORY_DESCRIPTOR.__func__
_RISK_QUOTE = _RISK_QUOTE_DESCRIPTOR.__func__
_RISK_RUIN = _RISK_RUIN_DESCRIPTOR.__func__
_RISK_DERIVED = _RISK_DERIVED_DESCRIPTOR

_RISK_HISTORY_FROZEN = _freeze_external_python_function_graph(_RISK_HISTORY)
_RISK_QUOTE_FROZEN = _freeze_external_python_function_graph(_RISK_QUOTE)
_RISK_RUIN_FROZEN = _freeze_external_python_function_graph(_RISK_RUIN)
_RISK_DERIVED_FROZEN = _freeze_external_python_function_graph(_RISK_DERIVED)

_ADMISSION_RISK_HELPER_WITNESSES = (
    ("_book_state", _RISK_BOOK_STATE_DESCRIPTOR, _RISK_BOOK_STATE, _RISK_BOOK_STATE.__code__, True),
    ("_goal_history_rooms", _RISK_HISTORY_DESCRIPTOR, _RISK_HISTORY, _RISK_HISTORY.__code__, True),
    ("_quote_risk_decision", _RISK_QUOTE_DESCRIPTOR, _RISK_QUOTE, _RISK_QUOTE.__code__, True),
    ("_risk_of_ruin_evidence_decision", _RISK_RUIN_DESCRIPTOR, _RISK_RUIN, _RISK_RUIN.__code__, True),
    ("_derived_risk_values", _RISK_DERIVED_DESCRIPTOR, _RISK_DERIVED, _RISK_DERIVED.__code__, False),
)


def _resume_after_product_day_turnover(
    *,
    risk_policy: PaperRiskPolicy,
    book: PaperBook,
    amount: Decimal,
    context: ProposedTicketRiskContext,
    pre_evaluation_state: tuple[Decimal, Decimal, Decimal, int] | None,
) -> RiskDecision:
    """Continue canonical risk evaluation after replacing only turnover room."""

    for (
        helper_name,
        expected_descriptor,
        expected_function,
        expected_code,
        descriptor_wrapped,
    ) in _ADMISSION_RISK_HELPER_WITNESSES:
        current_descriptor = PaperRiskPolicy.__dict__.get(helper_name)
        if current_descriptor is not expected_descriptor:
            return RiskDecision(
                False,
                "virtual bankroll risk helper authority is invalid",
            )
        current_function = (
            current_descriptor.__func__
            if descriptor_wrapped
            else current_descriptor
        )
        if (
            current_function is not expected_function
            or current_function.__code__ is not expected_code
        ):
            return RiskDecision(
                False,
                "virtual bankroll risk helper authority is invalid",
            )

    goal = risk_policy.economic_goal
    state = _RISK_BOOK_STATE(PaperRiskPolicy, book)
    if goal is None or state is None or state != pre_evaluation_state:
        return RiskDecision(False, "virtual bankroll changed during risk evaluation")
    initial_bankroll, balance, committed_stake, _ = state

    history_rooms = _RISK_HISTORY_FROZEN(
        PaperRiskPolicy,
        book,
        goal,
        context=context,
    )
    if history_rooms is None:
        return RiskDecision(False, "virtual bankroll risk history is invalid")
    session_room, day_room, drawdown_room, _ = history_rooms
    for room, reason in (
        (session_room, "economic goal conservative session loss limit exceeded"),
        (day_room, "economic goal conservative day loss limit exceeded"),
        (drawdown_room, "economic goal drawdown limit exceeded"),
    ):
        if amount > room:
            return RiskDecision(False, reason)

    quote_decision = _RISK_QUOTE_FROZEN(goal, context)
    if quote_decision is not None:
        return quote_decision

    if goal.max_risk_of_ruin < Decimal("1"):
        ruin_decision = _RISK_RUIN_FROZEN(
            PaperRiskPolicy,
            book,
            amount,
            goal,
            context,
        )
        if ruin_decision is not None:
            return ruin_decision

    derived = _RISK_DERIVED_FROZEN(
        risk_policy,
        initial_bankroll,
        balance,
        committed_stake,
        amount,
    )
    if derived is None:
        return RiskDecision(False, "virtual bankroll state is invalid")
    (
        ticket_limit,
        aggregate_committed,
        committed_limit,
        remaining_balance,
        reserve_limit,
    ) = derived
    if amount > ticket_limit:
        return RiskDecision(False, "ticket exceeds configured bankroll fraction")
    if aggregate_committed > committed_limit:
        return RiskDecision(False, "aggregate committed stake limit exceeded")
    if remaining_balance < reserve_limit:
        return RiskDecision(False, "minimum virtual cash reserve would be violated")
    if _RISK_BOOK_STATE(PaperRiskPolicy, book) != state:
        return RiskDecision(False, "virtual bankroll changed during risk evaluation")
    return RiskDecision(True, "allowed")


def admit_paper_ticket(
    *,
    workspace: str | Path,
    book: PaperBook,
    risk_policy: PaperRiskPolicy,
    stake: Decimal | str,
    legs: tuple[TicketLeg, ...],
    reason: str,
    placed_at: str,
    context: ProposedTicketRiskContext | None = None,
    provider_source_ids: tuple[str, ...] = (),
    provider_accounts: tuple[tuple[str, str], ...] = (),
    bankroll_id: str | None = None,
    currency: str | None = None,
) -> PaperAdmissionResult:
    """Atomically evaluate canonical PAPER risk and open the ticket if allowed.

    This function does not create a second risk, turnover, reservation, or ledger
    authority. It composes PaperRiskPolicy + PaperBook mutation inside the canonical
    WorkspaceEconomicLock. When a current product-issued UTC-day turnover snapshot
    is available, only the policy's conservative whole-history turnover room may be
    replaced; every other canonical risk gate remains authoritative. Incomplete or
    stale day evidence always falls back to the old conservative behavior.

    The lock is intentionally acquired *before* risk evaluation. A contender that
    cannot acquire the lock fails closed through WorkspaceEconomicLock rather than
    evaluating against stale economic state.
    """

    if type(book) is not PaperBook:
        raise TypeError("book must be an exact PaperBook")
    if type(risk_policy) is not PaperRiskPolicy:
        raise TypeError("risk_policy must be an exact PaperRiskPolicy")
    if context is not None and type(context) is not ProposedTicketRiskContext:
        raise TypeError("context must be an exact ProposedTicketRiskContext or None")
    if type(legs) is not tuple or not legs:
        raise ValueError("legs must be a non-empty canonical tuple")

    amount = _positive_decimal(stake)
    _validate_context_binding(
        context=context,
        legs=legs,
        placed_at=placed_at,
        provider_source_ids=provider_source_ids,
        provider_accounts=provider_accounts,
        bankroll_id=bankroll_id,
        currency=currency,
    )

    root = Path(workspace).expanduser().resolve(strict=False)
    book_path = root / "paper_book.json"
    day_turnover_snapshot = _prepare_paper_day_turnover_snapshot(
        root=root,
        book_path=book_path,
        risk_policy=risk_policy,
    )

    _require_workspace_lock_dispatch()
    with WorkspaceEconomicLock(root) as workspace_lock:
        registry_path = root / "run_registry.json"
        registry_missing = False
        try:
            registry_path.lstat()
        except FileNotFoundError:
            registry_missing = True
        else:
            if RunRegistry(registry_path).in_progress():
                raise UnresolvedExperimentError(
                    "Workspace has an unresolved economic run; repair it before PAPER admission."
                )
        if transaction_history_requires_recovery(root):
            raise UnresolvedExperimentError(
                "Workspace has unresolved transaction history; repair it before PAPER admission."
            )
        if registry_missing:
            transaction_root = root / RunTransaction.ROOT_NAME
            try:
                first_transaction = next(transaction_root.iterdir())
            except FileNotFoundError:
                pass
            except StopIteration:
                pass
            else:
                del first_transaction
                raise UnresolvedExperimentError(
                    "Workspace run registry is missing while transaction history exists; "
                    "repair it before PAPER admission."
                )

        if not book_path.exists():
            raise FileNotFoundError(
                "canonical paper_book.json must already exist; "
                "bootstrap/recovery belongs to the product lifecycle"
            )
        canonical_book = PaperBook.load(book_path)

        try:
            _REQUIRE_CURRENT_BINDING(book, book_path)
        except (TypeError, ValueError):
            working_book = canonical_book
        else:
            if not _same_semantic_book_state(canonical_book, book):
                raise ValueError(
                    "supplied current PaperBook does not match canonical durable state"
                )
            working_book = book

        pre_evaluation_state = risk_policy._book_state(working_book)
        decision = risk_policy.evaluate(working_book, amount, context=context)
        if (
            not decision.allowed
            and decision.reason == "economic goal turnover limit exceeded"
            and context is not None
        ):
            turnover_room = _revalidated_product_day_turnover_room(
                snapshot=day_turnover_snapshot,
                root=root,
                book=working_book,
                risk_policy=risk_policy,
                placed_at=placed_at,
                workspace_lock=workspace_lock,
            )
            if turnover_room is not None and amount <= turnover_room:
                decision = _resume_after_product_day_turnover(
                    risk_policy=risk_policy,
                    book=working_book,
                    amount=amount,
                    context=context,
                    pre_evaluation_state=pre_evaluation_state,
                )
        if not decision.allowed:
            return PaperAdmissionResult(
                risk=decision,
                ticket=None,
                book=working_book,
            )

        opened = working_book.open_ticket(
            legs,
            amount,
            reason=reason,
            placed_at=placed_at,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=bankroll_id,
            currency=currency,
        )
        working_book.save(book_path)
        persisted = PaperBook.load(book_path)
        persisted_ticket = persisted.tickets.get(opened.ticket_id)
        if persisted_ticket is None:
            raise RuntimeError(
                "persisted PaperBook lost the ticket opened inside admission"
            )
        if not _same_semantic_book_state(working_book, persisted):
            raise RuntimeError(
                "persisted PaperBook state does not match the admitted mutation"
            )
        result_book = book if working_book is book else persisted
        return PaperAdmissionResult(
            risk=decision,
            ticket=result_book.tickets[persisted_ticket.ticket_id],
            book=result_book,
        )


def _freeze_admission_module_globals() -> dict[str, object]:
    """Detach the complete admission helper graph from mutable module dispatch."""

    source = globals()
    frozen: dict[str, object] = dict(source)
    for name, value in tuple(source.items()):
        if type(value) is not FunctionType or value.__globals__ is not source:
            continue
        clone = FunctionType(
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
    return frozen


from ._paperbook_current_binding_verifier import (
    seal_current_binding_consumer as _seal_current_binding_consumer,
)

_ADMISSION_FROZEN_GLOBALS = _freeze_admission_module_globals()
_FROZEN_ADMIT_PAPER_TICKET = _ADMISSION_FROZEN_GLOBALS["admit_paper_ticket"]
if type(_FROZEN_ADMIT_PAPER_TICKET) is not FunctionType:
    raise RuntimeError("canonical PAPER admission executable is unavailable")
admit_paper_ticket = _seal_current_binding_consumer(_FROZEN_ADMIT_PAPER_TICKET)
del _FROZEN_ADMIT_PAPER_TICKET
del _ADMISSION_FROZEN_GLOBALS
del _seal_current_binding_consumer
