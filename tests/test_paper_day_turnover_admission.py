from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.economic_admission as economic_admission
import autosport.monotonic_workspace_authority as monotonic_module
import autosport.risk as risk_module

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_admission import admit_paper_ticket
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext
from autosport.risk_day_window import ProductDayRiskWindowStore
from autosport.risk_turnover_evidence import PaperDayTurnoverResolver
from autosport.workspace_lock import WorkspaceEconomicLock


def _timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _leg(suffix: str) -> TicketLeg:
    return TicketLeg(
        event_id=f"event-{suffix}",
        market_id=f"market-{suffix}",
        selection_id=f"selection-{suffix}",
        locked_odds=Decimal("2"),
    )


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-paper-day-turnover-admission",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("1"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("0.05"),
        max_risk_of_ruin=Decimal("1"),
        max_concurrent_positions=10,
    )


def _policy(goal: EconomicGoalContract) -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )


def _context(leg: TicketLeg, placed_at: str) -> ProposedTicketRiskContext:
    proposal = datetime.fromisoformat(placed_at.replace("Z", "+00:00"))
    observed = proposal - timedelta(seconds=2)
    quote = MarketEvent(
        event_id=leg.event_id,
        market_id=leg.market_id,
        selection_id=leg.selection_id,
        decimal_odds=leg.locked_odds,
        observed_ts=_timestamp(observed),
        source_id="provider-1",
        sequence=1,
        source_ts=_timestamp(observed - timedelta(seconds=1)),
        ingest_ts=_timestamp(observed + timedelta(seconds=1)),
    )
    return ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(quote,),
        bankroll_id="paper-bankroll",
        currency="USD",
        proposal_ts=placed_at,
    )


def _book_with_settled_turnover(*, placed_at: str, stake: str) -> PaperBook:
    book = PaperBook("100")
    prior = _leg("prior")
    ticket = book.open_ticket(
        [prior],
        Decimal(stake),
        placed_at=placed_at,
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    book.settle(ticket.ticket_id, set(), {ticket.legs[0].quote_key})
    return book


def _admit(tmp_path, book: PaperBook, goal: EconomicGoalContract, placed_at: str):
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("candidate")
    context = _context(candidate, placed_at)
    policy = _policy(goal)
    baseline = policy.evaluate(book, Decimal("0.01"), context=context)
    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(candidate,),
        reason="product-issued day turnover regression",
        placed_at=placed_at,
        context=context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    return baseline, result


def _replacement_code_preserving_freevars(target):
    def forged(*args, **kwargs):
        del args, kwargs
        return None

    return forged.__code__.replace(co_freevars=target.__code__.co_freevars)


def _assert_day_authority_code_mutation_rejected(tmp_path, target) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("day-authority-code")
    context = _context(candidate, _timestamp(now))
    policy = _policy(goal)
    original_code = target.__code__

    try:
        target.__code__ = _replacement_code_preserving_freevars(target)
        with pytest.raises(RuntimeError, match="product day .* executable authority changed"):
            admit_paper_ticket(
                workspace=tmp_path,
                book=book,
                risk_policy=policy,
                stake=Decimal("0.01"),
                legs=(candidate,),
                reason="mutated product-day executable must fail closed",
                placed_at=_timestamp(now),
                context=context,
                provider_source_ids=("provider-1",),
                bankroll_id="paper-bankroll",
                currency="USD",
            )
    finally:
        target.__code__ = original_code

    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert persisted.tickets.keys() == book.tickets.keys()


def test_turnover_resolver_wrapper_code_mutation_cannot_mint_headroom(tmp_path):
    descriptor = PaperDayTurnoverResolver.__dict__["resolve"]
    assert type(descriptor) is classmethod
    _assert_day_authority_code_mutation_rejected(tmp_path, descriptor.__func__)


@pytest.mark.parametrize(
    "method_name",
    (
        "__init__",
        "current",
        "require_current",
        "require_current_under_lock",
        "_current_under_lock",
        "_publish_day",
        "_evidence",
    ),
)
def test_risk_day_wrapper_code_mutation_cannot_mint_headroom(tmp_path, method_name):
    target = ProductDayRiskWindowStore.__dict__[method_name]
    _assert_day_authority_code_mutation_rejected(tmp_path, target)


def _assert_day_authority_dependency_rejected(tmp_path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("day-authority-dependency")
    context = _context(candidate, _timestamp(now))
    policy = _policy(goal)
    admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(candidate,),
        reason="mutated product-day dependency must fail closed",
        placed_at=_timestamp(now),
        context=context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )


def _sealed_wrapper_frozen_globals(function):
    closure = function.__closure__
    assert closure is not None
    freevars = function.__code__.co_freevars
    assert "frozen_globals" in freevars
    return closure[freevars.index("frozen_globals")].cell_contents


def test_turnover_resolver_closure_binding_rebind_cannot_mint_headroom(tmp_path):
    descriptor = PaperDayTurnoverResolver.__dict__["resolve"]
    target = descriptor.__func__
    frozen_globals = _sealed_wrapper_frozen_globals(target)
    original = frozen_globals["_PAPERBOOK_LOAD"]
    try:
        frozen_globals["_PAPERBOOK_LOAD"] = lambda _path: PaperBook("1000000")
        with pytest.raises(
            RuntimeError,
            match="product day turnover resolver dependency authority changed",
        ):
            _assert_day_authority_dependency_rejected(tmp_path)
    finally:
        frozen_globals["_PAPERBOOK_LOAD"] = original


def test_turnover_resolver_frozen_helper_code_mutation_cannot_mint_headroom(tmp_path):
    descriptor = PaperDayTurnoverResolver.__dict__["resolve"]
    target = descriptor.__func__
    frozen_globals = _sealed_wrapper_frozen_globals(target)
    helper = frozen_globals["_exact_sum"]
    original_code = helper.__code__
    try:
        helper.__code__ = _replacement_code_preserving_freevars(helper)
        with pytest.raises(
            RuntimeError,
            match="product day turnover resolver dependency authority changed",
        ):
            _assert_day_authority_dependency_rejected(tmp_path)
    finally:
        helper.__code__ = original_code


def test_risk_day_closure_binding_rebind_cannot_mint_headroom(tmp_path):
    target = ProductDayRiskWindowStore.__dict__["_current_under_lock"]
    frozen_globals = _sealed_wrapper_frozen_globals(target)
    original = frozen_globals["_MONOTONIC_RECOVER"]
    try:
        frozen_globals["_MONOTONIC_RECOVER"] = lambda *args, **kwargs: None
        with pytest.raises(
            RuntimeError,
            match="product day window dependency authority changed",
        ):
            _assert_day_authority_dependency_rejected(tmp_path)
    finally:
        frozen_globals["_MONOTONIC_RECOVER"] = original


def test_risk_day_frozen_helper_code_mutation_cannot_mint_headroom(tmp_path):
    target = ProductDayRiskWindowStore.__dict__["_current_under_lock"]
    frozen_globals = _sealed_wrapper_frozen_globals(target)
    helper = frozen_globals["_clock_utc_instant"]
    original_code = helper.__code__
    try:
        helper.__code__ = _replacement_code_preserving_freevars(helper)
        with pytest.raises(
            RuntimeError,
            match="product day window dependency authority changed",
        ):
            _assert_day_authority_dependency_rejected(tmp_path)
    finally:
        helper.__code__ = original_code


def test_monotonic_constructor_code_mutation_cannot_mint_day_headroom(tmp_path):
    constructor = MonotonicWorkspaceAuthority.__init__
    original_code = constructor.__code__
    try:
        constructor.__code__ = _replacement_code_preserving_freevars(constructor)
        with pytest.raises(
            RuntimeError,
            match="monotonic day authority transition graph changed",
        ):
            _assert_day_authority_dependency_rejected(tmp_path)
    finally:
        constructor.__code__ = original_code


def test_monotonic_transition_helper_rebind_cannot_mint_day_headroom(tmp_path):
    original = MonotonicWorkspaceAuthority._load_bound_history

    def hostile_history(_self):
        raise AssertionError("mutated monotonic history helper executed")

    try:
        MonotonicWorkspaceAuthority._load_bound_history = hostile_history
        with pytest.raises(
            RuntimeError,
            match="monotonic day authority transition graph changed",
        ):
            _assert_day_authority_dependency_rejected(tmp_path)
    finally:
        MonotonicWorkspaceAuthority._load_bound_history = original


def test_monotonic_transition_helper_code_mutation_cannot_mint_day_headroom(tmp_path):
    helper = MonotonicWorkspaceAuthority._load_bound_history
    original_code = helper.__code__
    try:
        helper.__code__ = _replacement_code_preserving_freevars(helper)
        with pytest.raises(
            RuntimeError,
            match="monotonic day authority transition graph changed",
        ):
            _assert_day_authority_dependency_rejected(tmp_path)
    finally:
        helper.__code__ = original_code


def test_monotonic_transition_global_helper_rebind_cannot_mint_day_headroom(tmp_path):
    original = monotonic_module._record_hash
    try:
        monotonic_module._record_hash = lambda _payload: "0" * 64
        with pytest.raises(
            RuntimeError,
            match="monotonic day authority transition graph changed",
        ):
            _assert_day_authority_dependency_rejected(tmp_path)
    finally:
        monotonic_module._record_hash = original


def test_monotonic_transition_global_helper_code_mutation_cannot_mint_day_headroom(
    tmp_path,
):
    helper = monotonic_module._record_hash
    original_code = helper.__code__
    try:
        helper.__code__ = _replacement_code_preserving_freevars(helper)
        with pytest.raises(
            RuntimeError,
            match="monotonic day authority transition graph changed",
        ):
            _assert_day_authority_dependency_rejected(tmp_path)
    finally:
        helper.__code__ = original_code


def _same_day_offset(now: datetime, seconds: int) -> datetime:
    candidate = now + timedelta(seconds=seconds)
    if candidate.date() != now.date():
        candidate = now - timedelta(seconds=seconds)
    assert candidate.date() == now.date()
    return candidate


def test_within_day_backdating_cannot_refresh_stale_quote(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    requested = _same_day_offset(now, -60)
    goal = replace(
        _goal(),
        max_turnover_fraction=Decimal("1"),
        max_quote_age_seconds=Decimal("5"),
    )
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("caller-time-stale-quote")
    context = _context(candidate, _timestamp(requested))
    policy = _policy(goal)

    # The caller-authored timestamp makes this quote look only three seconds old.
    baseline = policy.evaluate(book, Decimal("0.01"), context=context)
    assert baseline.allowed

    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(candidate,),
        reason="product time must govern quote freshness",
        placed_at=_timestamp(requested),
        context=context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    assert result.admitted is False
    assert result.risk.reason in {
        "quote exceeds economic goal maximum age",
        "quote timestamp is after proposal timestamp",
    }
    assert PaperBook.load(tmp_path / "paper_book.json").tickets == {}


def test_positive_economic_admission_persists_product_action_time(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    requested = now - timedelta(seconds=1)
    if requested.date() != now.date():
        pytest.skip("needs one second of current UTC-day history")
    goal = replace(
        _goal(),
        max_turnover_fraction=Decimal("1"),
        max_quote_age_seconds=Decimal("30"),
    )
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("product-action-time")
    context = _context(candidate, _timestamp(requested))
    policy = _policy(goal)

    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(candidate,),
        reason="persist product-owned admission time",
        placed_at=_timestamp(requested),
        context=context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    assert result.admitted is True
    assert result.ticket is not None
    persisted_time = datetime.fromisoformat(
        result.ticket.placed_at.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    assert persisted_time > requested
    assert persisted_time.date() == now.date()


def test_admission_rejects_lock_validator_dependency_rebinding(tmp_path):
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    candidate = _leg("lock-validator-global")
    validator_globals = WorkspaceEconomicLock._validate_open_handle_identity.__globals__
    original_open = validator_globals["_open_read_only_descriptor"]

    def hostile_open(_path) -> int:
        raise AssertionError("mutated lock descriptor opener executed")

    try:
        validator_globals["_open_read_only_descriptor"] = hostile_open
        with pytest.raises(
            RuntimeError,
            match="workspace economic lock dependency authority changed",
        ):
            admit_paper_ticket(
                workspace=tmp_path,
                book=book,
                risk_policy=policy,
                stake=Decimal("1"),
                legs=(candidate,),
                reason="lock validator globals must remain canonical",
                placed_at="2026-10-03T06:30:00Z",
            )
    finally:
        validator_globals["_open_read_only_descriptor"] = original_open

    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert persisted.tickets == {}


@pytest.mark.parametrize(
    "method_name",
    ["resolve", "__truediv__", "lstat", "iterdir", "exists"],
)
def test_admission_rejects_workspace_path_rebinding_before_mutation(
    tmp_path,
    method_name,
):
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    candidate = _leg("workspace-path-dispatch")
    had_own_attribute = method_name in Path.__dict__
    original_local = Path.__dict__.get(method_name)
    hostile_called = False

    def hostile_path_method(*args, **kwargs):
        nonlocal hostile_called
        del args, kwargs
        hostile_called = True
        raise AssertionError("mutated Path method executed")

    try:
        setattr(Path, method_name, hostile_path_method)
        with pytest.raises(
            RuntimeError,
            match="economic admission workspace path authority changed",
        ):
            admit_paper_ticket(
                workspace=tmp_path,
                book=book,
                risk_policy=policy,
                stake=Decimal("1"),
                legs=(candidate,),
                reason="workspace path dispatch must remain canonical",
                placed_at="2026-10-03T06:30:00Z",
            )
    finally:
        if had_own_attribute:
            setattr(Path, method_name, original_local)
        else:
            delattr(Path, method_name)

    assert hostile_called is False
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert persisted.tickets == {}


def test_admission_rejects_workspace_lock_rebinding_before_mutation(tmp_path):
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    candidate = _leg("lock-dispatch")
    original_acquire = WorkspaceEconomicLock.acquire

    def hostile_acquire(_self) -> None:
        raise AssertionError("mutated admission lock acquire executed")

    try:
        WorkspaceEconomicLock.acquire = hostile_acquire
        with pytest.raises(
            RuntimeError,
            match="workspace economic lock executable authority changed",
        ):
            admit_paper_ticket(
                workspace=tmp_path,
                book=book,
                risk_policy=policy,
                stake=Decimal("1"),
                legs=(candidate,),
                reason="lock dispatch must remain canonical",
                placed_at="2026-10-03T06:30:00Z",
            )
    finally:
        WorkspaceEconomicLock.acquire = original_acquire

    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert persisted.tickets == {}


def test_goal_store_constructor_rebinding_cannot_mint_day_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("goal-store-constructor")
    context = _context(candidate, _timestamp(now))
    policy = _policy(goal)

    original_init = EconomicGoalStore.__init__
    hostile_called = False

    def hostile_init(self, workspace):
        nonlocal hostile_called
        hostile_called = True
        original_init(self, workspace)

    try:
        EconomicGoalStore.__init__ = hostile_init
        result = admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=policy,
            stake=Decimal("0.01"),
            legs=(candidate,),
            reason="goal store constructor must remain canonical",
            placed_at=_timestamp(now),
            context=context,
            provider_source_ids=("provider-1",),
            bankroll_id="paper-bankroll",
            currency="USD",
        )
    finally:
        EconomicGoalStore.__init__ = original_init

    assert hostile_called is False
    assert result.admitted is False
    assert result.risk.reason == "economic goal turnover limit exceeded"
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert persisted.tickets.keys() == book.tickets.keys()


def test_product_issued_current_utc_day_releases_old_day_turnover(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")

    baseline, result = _admit(tmp_path, book, goal, _timestamp(now))

    assert baseline.allowed is False
    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is True
    assert result.risk.allowed is True
    assert result.ticket is not None
    assert result.ticket.stake == Decimal("0.01")


def test_current_day_turnover_still_consumes_exact_owner_cap(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(now), stake="5")

    baseline, result = _admit(tmp_path, book, goal, _timestamp(now))

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert result.risk.reason == "economic goal turnover limit exceeded"
    assert len(result.book.tickets) == 1


def test_under_lock_goal_successor_cannot_be_hidden_by_live_load_rebinding(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    goal_store = EconomicGoalStore(tmp_path)
    goal_store.initialize_owner(goal)

    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book_path = tmp_path / "paper_book.json"
    book.save(book_path)
    policy = _policy(goal)
    root = tmp_path.resolve()

    snapshot = economic_admission._prepare_paper_day_turnover_snapshot(
        root=root,
        book_path=book_path,
        risk_policy=policy,
    )
    assert snapshot is not None

    tighter_goal = replace(
        goal,
        revision=goal.revision + 1,
        max_turnover_fraction=Decimal("0.01"),
    )
    goal_store.persist_automatic_successor(tighter_goal)

    original_load = EconomicGoalStore.load
    try:
        EconomicGoalStore.load = lambda self: goal
        with WorkspaceEconomicLock(root) as workspace_lock:
            room = economic_admission._revalidated_product_day_turnover_room(
                snapshot=snapshot,
                root=root,
                book=PaperBook.load(book_path),
                risk_policy=policy,
                placed_at=_timestamp(now),
                workspace_lock=workspace_lock,
            )
    finally:
        EconomicGoalStore.load = original_load

    assert room is None


def test_missing_durable_goal_never_mints_day_turnover_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")

    baseline, result = _admit(tmp_path, book, goal, _timestamp(now))

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert result.risk.reason == "economic goal turnover limit exceeded"
    assert len(result.book.tickets) == 1


def test_baseline_allowed_misdated_ticket_cannot_create_future_day_headroom(
    tmp_path,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    policy = _policy(goal)

    first_leg = _leg("misdated-first")
    first_context = _context(first_leg, _timestamp(old))
    baseline = policy.evaluate(book, Decimal("4"), context=first_context)
    assert baseline.allowed is True

    first = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("4"),
        legs=(first_leg,),
        reason="misdated ticket must not create future daily headroom",
        placed_at=_timestamp(old),
        context=first_context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    assert first.admitted is False
    assert (
        first.risk.reason
        == "economic goal current product day membership unavailable"
    )
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert persisted.tickets == {}

    second_leg = _leg("current-second")
    second_context = _context(second_leg, _timestamp(now))
    second = admit_paper_ticket(
        workspace=tmp_path,
        book=persisted,
        risk_policy=policy,
        stake=Decimal("4"),
        legs=(second_leg,),
        reason="current-day ticket consumes canonical daily headroom",
        placed_at=_timestamp(now),
        context=second_context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    assert second.admitted is True
    final = PaperBook.load(tmp_path / "paper_book.json")
    assert len(final.tickets) == 1
    ticket = next(iter(final.tickets.values()))
    assert ticket.stake == Decimal("4")
    assert ticket.placed_at == _timestamp(now)


def test_candidate_outside_current_product_day_cannot_spend_current_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(
        placed_at=_timestamp(old - timedelta(days=1)),
        stake="50",
    )

    baseline, result = _admit(tmp_path, book, goal, _timestamp(old))

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert result.risk.reason == "economic goal turnover limit exceeded"
    assert len(result.book.tickets) == 1


def test_day_turnover_override_does_not_bypass_local_ticket_limit(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("candidate")
    context = _context(candidate, _timestamp(now))
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.00001"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )

    baseline = policy.evaluate(book, Decimal("0.01"), context=context)
    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(candidate,),
        reason="post-turnover local risk gate",
        placed_at=_timestamp(now),
        context=context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert result.risk.reason == "ticket exceeds configured bankroll fraction"


def test_day_turnover_override_does_not_bypass_quote_freshness(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("candidate")
    stale_quote = MarketEvent(
        event_id=candidate.event_id,
        market_id=candidate.market_id,
        selection_id=candidate.selection_id,
        decimal_odds=candidate.locked_odds,
        observed_ts=_timestamp(now - timedelta(hours=1)),
        source_id="provider-1",
        sequence=1,
        source_ts=_timestamp(now - timedelta(hours=1, seconds=1)),
        ingest_ts=_timestamp(now - timedelta(hours=1) + timedelta(seconds=1)),
    )
    context = ProposedTicketRiskContext(
        legs=(candidate,),
        quotes=(stale_quote,),
        bankroll_id="paper-bankroll",
        currency="USD",
        proposal_ts=_timestamp(now),
    )
    policy = _policy(goal)

    baseline = policy.evaluate(book, Decimal("0.01"), context=context)
    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(candidate,),
        reason="post-turnover quote gate",
        placed_at=_timestamp(now),
        context=context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert "quote" in result.risk.reason



def test_risk_module_timestamp_rebind_cannot_bypass_post_turnover_quote_gate(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("candidate-risk-global-rebind")
    stale_quote = MarketEvent(
        event_id=candidate.event_id,
        market_id=candidate.market_id,
        selection_id=candidate.selection_id,
        decimal_odds=candidate.locked_odds,
        observed_ts=_timestamp(now - timedelta(hours=1)),
        source_id="provider-1",
        sequence=1,
        source_ts=_timestamp(now - timedelta(hours=1, seconds=1)),
        ingest_ts=_timestamp(now - timedelta(hours=1) + timedelta(seconds=1)),
    )
    context = ProposedTicketRiskContext(
        legs=(candidate,),
        quotes=(stale_quote,),
        bankroll_id="paper-bankroll",
        currency="USD",
        proposal_ts=_timestamp(now),
    )
    policy = _policy(goal)
    baseline = policy.evaluate(book, Decimal("0.01"), context=context)
    assert baseline.reason == "economic goal turnover limit exceeded"

    original = risk_module._canonical_context_timestamp
    try:
        risk_module._canonical_context_timestamp = (
            lambda label, value: (_timestamp(now), now)
        )
        result = admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=policy,
            stake=Decimal("0.01"),
            legs=(candidate,),
            reason="risk module timestamp rebind must not widen admission",
            placed_at=_timestamp(now),
            context=context,
            provider_source_ids=("provider-1",),
            bankroll_id="paper-bankroll",
            currency="USD",
        )
    finally:
        risk_module._canonical_context_timestamp = original

    assert result.admitted is False
    assert "quote" in result.risk.reason


def test_serial_admissions_cannot_double_spend_current_day_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    canonical = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    canonical.save(tmp_path / "paper_book.json")
    first_view = PaperBook.load(tmp_path / "paper_book.json")
    stale_second_view = PaperBook.load(tmp_path / "paper_book.json")
    policy = _policy(goal)

    first_leg = _leg("first-current-day")
    first_context = _context(first_leg, _timestamp(now))
    first = admit_paper_ticket(
        workspace=tmp_path,
        book=first_view,
        risk_policy=policy,
        stake=Decimal("3"),
        legs=(first_leg,),
        reason="consume first current-day turnover room",
        placed_at=_timestamp(now),
        context=first_context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    second_leg = _leg("second-current-day")
    second_context = _context(second_leg, _timestamp(now))
    second = admit_paper_ticket(
        workspace=tmp_path,
        book=stale_second_view,
        risk_policy=policy,
        stake=Decimal("3"),
        legs=(second_leg,),
        reason="must not double-spend current-day turnover room",
        placed_at=_timestamp(now),
        context=second_context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    assert first.admitted is True
    assert second.admitted is False
    assert second.risk.reason == "economic goal turnover limit exceeded"
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert len(persisted.tickets) == 2


def test_restart_re_resolves_remaining_current_day_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    policy = _policy(goal)

    first_leg = _leg("restart-first")
    first_context = _context(first_leg, _timestamp(now))
    first = admit_paper_ticket(
        workspace=tmp_path,
        book=PaperBook.load(tmp_path / "paper_book.json"),
        risk_policy=policy,
        stake=Decimal("3"),
        legs=(first_leg,),
        reason="current-day turnover before restart",
        placed_at=_timestamp(now),
        context=first_context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    assert first.admitted is True

    second_leg = _leg("restart-second")
    second_context = _context(second_leg, _timestamp(now))
    second = admit_paper_ticket(
        workspace=tmp_path,
        book=PaperBook.load(tmp_path / "paper_book.json"),
        risk_policy=policy,
        stake=Decimal("2"),
        legs=(second_leg,),
        reason="consume exact residual after restart",
        placed_at=_timestamp(now),
        context=second_context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    assert second.admitted is True

    third_leg = _leg("restart-third")
    third_context = _context(third_leg, _timestamp(now))
    third = admit_paper_ticket(
        workspace=tmp_path,
        book=PaperBook.load(tmp_path / "paper_book.json"),
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(third_leg,),
        reason="cap exhausted after restart",
        placed_at=_timestamp(now),
        context=third_context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )
    assert third.admitted is False
    assert third.risk.reason == "economic goal turnover limit exceeded"


def test_live_module_rebind_cannot_mint_turnover_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(now), stake="5")

    original = economic_admission._revalidated_product_day_turnover_room
    try:
        economic_admission._revalidated_product_day_turnover_room = (
            lambda **kwargs: Decimal("999999")
        )
        baseline, result = _admit(tmp_path, book, goal, _timestamp(now))
    finally:
        economic_admission._revalidated_product_day_turnover_room = original

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert result.risk.reason == "economic goal turnover limit exceeded"


def test_live_resume_helper_rebind_cannot_replace_positive_risk_suffix(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")

    original = economic_admission._resume_after_product_day_turnover
    try:
        def hostile_resume(**kwargs):
            del kwargs
            raise AssertionError("mutable admission resume helper executed")

        economic_admission._resume_after_product_day_turnover = hostile_resume
        baseline, result = _admit(tmp_path, book, goal, _timestamp(now))
    finally:
        economic_admission._resume_after_product_day_turnover = original

    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is True
    assert result.risk.allowed is True
