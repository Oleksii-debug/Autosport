from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.economic_admission as economic_admission
import autosport.monotonic_workspace_authority as monotonic_module
import autosport.recovery as recovery_module
import autosport.risk as risk_module

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_admission import admit_paper_ticket
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext, RiskOfRuinEvidence
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


def _closure_cell(function, name):
    closure = function.__closure__
    assert closure is not None
    freevars = function.__code__.co_freevars
    assert name in freevars
    return closure[freevars.index(name)]


def _admission_current_binding_consumer():
    inner = _closure_cell(admit_paper_ticket, "expected_function").cell_contents
    assert callable(inner)
    return inner


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


def _assert_risk_helper_mutation_rejected(tmp_path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    goal = replace(
        _goal(),
        max_turnover_fraction=Decimal("1"),
        max_quote_age_seconds=Decimal("30"),
    )
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("risk-helper-authority")
    context = _context(candidate, _timestamp(now))

    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=_policy(goal),
        stake=Decimal("0.01"),
        legs=(candidate,),
        reason="mutated detached risk helper must fail closed",
        placed_at=_timestamp(now),
        context=context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    assert result.admitted is False
    assert result.risk.reason == "virtual bankroll risk helper authority is invalid"
    assert PaperBook.load(tmp_path / "paper_book.json").tickets == {}


def test_detached_quote_helper_code_mutation_cannot_mint_admission(tmp_path):
    target = economic_admission._RISK_QUOTE_FROZEN
    original_code = target.__code__
    try:
        target.__code__ = _replacement_code_preserving_freevars(target)
        _assert_risk_helper_mutation_rejected(tmp_path)
    finally:
        target.__code__ = original_code


def test_detached_quote_helper_frozen_global_rebind_cannot_mint_admission(tmp_path):
    target = economic_admission._RISK_QUOTE_FROZEN
    namespace = target.__globals__
    original = namespace["_canonical_context_timestamp"]
    try:
        namespace["_canonical_context_timestamp"] = lambda _name, value: (value, value)
        _assert_risk_helper_mutation_rejected(tmp_path)
    finally:
        namespace["_canonical_context_timestamp"] = original


def test_transitive_decimal_context_code_mutation_cannot_mint_admission(tmp_path):
    descriptor = PaperRiskPolicy.__dict__["_decimal_context"]
    assert type(descriptor) is staticmethod
    target = descriptor.__func__
    original_code = target.__code__
    try:
        target.__code__ = _replacement_code_preserving_freevars(target)
        _assert_risk_helper_mutation_rejected(tmp_path)
    finally:
        target.__code__ = original_code


def test_turnover_override_cannot_use_caller_time_to_refresh_stale_quote(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    requested = _same_day_offset(now, -60)
    old = now - timedelta(days=2)
    goal = replace(_goal(), max_quote_age_seconds=Decimal("5"))
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("override-stale-quote")
    context = _context(candidate, _timestamp(requested))
    policy = _policy(goal)

    baseline = policy.evaluate(book, Decimal("0.01"), context=context)
    assert baseline.allowed is False
    assert baseline.reason == "economic goal turnover limit exceeded"

    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(candidate,),
        reason="bounded-day override must use product quote time",
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
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert tuple(persisted.tickets) == tuple(book.tickets)


def test_product_action_time_preserves_provenance_bound_proposal_context(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    requested = now - timedelta(seconds=1)
    if requested.date() != now.date():
        pytest.skip("needs one second of current UTC-day history")

    goal = replace(
        _goal(),
        max_turnover_fraction=Decimal("1"),
        max_quote_age_seconds=Decimal("30"),
        max_risk_of_ruin=Decimal("0.5"),
    )
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("product-action-provenance")
    base_context = _context(candidate, _timestamp(requested))
    policy = _policy(goal)
    portfolio_sha256 = policy.risk_of_ruin_portfolio_sha256(book)
    candidate_sha256 = policy.risk_of_ruin_candidate_sha256(base_context)
    assert portfolio_sha256 is not None
    assert candidate_sha256 is not None
    evidence = RiskOfRuinEvidence(
        evidence_id="product-action-time-ruin-evidence",
        research_protocol_sha256="a" * 64,
        reproducibility_bundle_sha256="b" * 64,
        producer_identity="test-risk-model-source",
        causal_cutoff=_timestamp(requested - timedelta(seconds=2)),
        evaluated_at=_timestamp(requested - timedelta(seconds=1)),
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256=portfolio_sha256,
        candidate_sha256=candidate_sha256,
        evaluated_stake=Decimal("0.01"),
        upper_bound=Decimal("0.1"),
    )
    context = replace(base_context, risk_of_ruin_evidence=evidence)

    baseline = policy.evaluate(book, Decimal("0.01"), context=context)
    assert baseline.allowed is True

    result = admit_paper_ticket(
        workspace=tmp_path,
        book=book,
        risk_policy=policy,
        stake=Decimal("0.01"),
        legs=(candidate,),
        reason="proposal provenance and action time have distinct authority",
        placed_at=_timestamp(requested),
        context=context,
        provider_source_ids=("provider-1",),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    assert result.admitted is True
    assert result.ticket is not None
    assert context.proposal_ts == _timestamp(requested)
    assert policy.risk_of_ruin_candidate_sha256(context) == candidate_sha256
    persisted_time = datetime.fromisoformat(
        result.ticket.placed_at.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    assert persisted_time > requested


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
            authority = (
                economic_admission._revalidated_product_day_admission_authority(
                    snapshot=snapshot,
                    root=root,
                    book=PaperBook.load(book_path),
                    risk_policy=policy,
                    placed_at=_timestamp(now),
                    workspace_lock=workspace_lock,
                )
            )
    finally:
        EconomicGoalStore.load = original_load

    assert authority is None


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


def _risk_decision_descriptor_case(tmp_path, *, goal):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("risk-decision-descriptor")
    context = _context(candidate, _timestamp(now))
    return now, book, candidate, context, _policy(goal)


def test_day_authority_descriptor_witness_inventory_is_complete():
    expected = {
        economic_admission._PaperDayTurnoverSnapshot: tuple(
            economic_admission._PaperDayTurnoverSnapshot.__dataclass_fields__
        ),
        economic_admission._ProductDayAdmissionAuthority: tuple(
            economic_admission._ProductDayAdmissionAuthority.__dataclass_fields__
        ),
        economic_admission.PaperDayTurnoverEvidence: tuple(
            economic_admission.PaperDayTurnoverEvidence.__dataclass_fields__
        ),
        economic_admission.ProductDayRiskWindow: tuple(
            economic_admission.ProductDayRiskWindow.__dataclass_fields__
        ),
    }
    observed = {
        owner: tuple(name for name, _descriptor in witnesses)
        for owner, witnesses in (
            economic_admission._DAY_AUTHORITY_FIELD_DESCRIPTOR_WITNESSES
        )
    }

    assert observed == expected


@pytest.mark.parametrize(
    ("owner_name", "field_name"),
    (
        ("_PaperDayTurnoverSnapshot", "evidence"),
        ("_ProductDayAdmissionAuthority", "turnover_room"),
        ("PaperDayTurnoverEvidence", "residual_headroom"),
        ("PaperDayTurnoverEvidence", "breached"),
        ("ProductDayRiskWindow", "state_sha256"),
        ("ProductDayRiskWindow", "product_clock_authoritative"),
    ),
)
def test_day_authority_descriptor_rebind_cannot_mint_headroom(
    tmp_path,
    owner_name,
    field_name,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg(f"day-descriptor-{owner_name}-{field_name}")
    context = _context(candidate, _timestamp(now))
    policy = _policy(goal)
    owner = getattr(economic_admission, owner_name)
    original = owner.__dict__[field_name]
    hostile_called = False

    def forged_field(_self):
        nonlocal hostile_called
        hostile_called = True
        if field_name in {"turnover_room", "residual_headroom"}:
            return Decimal("999999")
        if field_name in {"breached", "product_clock_authoritative"}:
            return False if field_name == "breached" else True
        return "0" * 64

    try:
        setattr(owner, field_name, property(forged_field))
        with pytest.raises(
            RuntimeError,
            match="economic admission day authority data descriptor changed",
        ):
            admit_paper_ticket(
                workspace=tmp_path,
                book=book,
                risk_policy=policy,
                stake=Decimal("0.01"),
                legs=(candidate,),
                reason="day authority descriptor mutation must fail closed",
                placed_at=_timestamp(now),
                context=context,
                provider_source_ids=("provider-1",),
                bankroll_id="paper-bankroll",
                currency="USD",
            )
    finally:
        setattr(owner, field_name, original)

    assert hostile_called is False
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_risk_decision_allowed_descriptor_rebind_cannot_promote_rejection(tmp_path):
    goal = replace(
        _goal(),
        max_stake_amount=Decimal("0.001"),
    )
    now, book, candidate, context, policy = _risk_decision_descriptor_case(
        tmp_path,
        goal=goal,
    )
    original = risk_module.RiskDecision.__dict__["allowed"]
    hostile_called = False

    def forged_allowed(_self):
        nonlocal hostile_called
        hostile_called = True
        return True

    try:
        risk_module.RiskDecision.allowed = property(forged_allowed)
        with pytest.raises(
            RuntimeError,
            match="economic admission risk decision authority changed",
        ):
            admit_paper_ticket(
                workspace=tmp_path,
                book=book,
                risk_policy=policy,
                stake=Decimal("0.01"),
                legs=(candidate,),
                reason="risk decision allowed descriptor must not promote rejection",
                placed_at=_timestamp(now),
                context=context,
                provider_source_ids=("provider-1",),
                bankroll_id="paper-bankroll",
                currency="USD",
            )
    finally:
        risk_module.RiskDecision.allowed = original

    assert hostile_called is False
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_risk_decision_reason_descriptor_rebind_cannot_trigger_turnover_resume(tmp_path):
    goal = replace(
        _goal(),
        blocked_providers=frozenset({"provider-1"}),
    )
    now, book, candidate, context, policy = _risk_decision_descriptor_case(
        tmp_path,
        goal=goal,
    )
    original = risk_module.RiskDecision.__dict__["reason"]
    hostile_called = False

    def forged_reason(_self):
        nonlocal hostile_called
        hostile_called = True
        return "economic goal turnover limit exceeded"

    try:
        risk_module.RiskDecision.reason = property(forged_reason)
        with pytest.raises(
            RuntimeError,
            match="economic admission risk decision authority changed",
        ):
            admit_paper_ticket(
                workspace=tmp_path,
                book=book,
                risk_policy=policy,
                stake=Decimal("0.01"),
                legs=(candidate,),
                reason="risk decision reason must not reclassify provider rejection",
                placed_at=_timestamp(now),
                context=context,
                provider_source_ids=("provider-1",),
                bankroll_id="paper-bankroll",
                currency="USD",
            )
    finally:
        risk_module.RiskDecision.reason = original

    assert hostile_called is False
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_proposal_market_descriptor_witness_inventory_is_complete():
    expected = {
        risk_module.ProposedTicketRiskContext: tuple(
            risk_module.ProposedTicketRiskContext.__dataclass_fields__
        ),
        risk_module.RiskOfRuinEvidence: tuple(
            risk_module.RiskOfRuinEvidence.__dataclass_fields__
        ),
        economic_admission.TicketLeg: tuple(
            economic_admission.TicketLeg.__dataclass_fields__
        ),
        economic_admission.MarketEvent: tuple(
            economic_admission.MarketEvent.__dataclass_fields__
        ),
    }
    observed = {
        risk_module.ProposedTicketRiskContext: tuple(
            name
            for name, _descriptor in (
                economic_admission._PROPOSED_CONTEXT_FIELD_DESCRIPTOR_WITNESSES
            )
        ),
        risk_module.RiskOfRuinEvidence: tuple(
            name
            for name, _descriptor in (
                economic_admission._RISK_OF_RUIN_EVIDENCE_FIELD_DESCRIPTOR_WITNESSES
            )
        ),
        economic_admission.TicketLeg: tuple(
            name
            for name, _descriptor in (
                economic_admission._TICKET_LEG_FIELD_DESCRIPTOR_WITNESSES
            )
        ),
        economic_admission.MarketEvent: tuple(
            name
            for name, _descriptor in (
                economic_admission._MARKET_EVENT_FIELD_DESCRIPTOR_WITNESSES
            )
        ),
    }

    assert observed == expected


@pytest.mark.parametrize(
    ("owner", "field_name", "forged_value"),
    (
        (risk_module.ProposedTicketRiskContext, "proposal_ts", "2099-01-01T00:00:00Z"),
        (risk_module.ProposedTicketRiskContext, "provider_accounts", ()),
        (risk_module.RiskOfRuinEvidence, "upper_bound", Decimal("0")),
        (
            risk_module.RiskOfRuinEvidence,
            "base_portfolio_sha256",
            "0" * 64,
        ),
        (
            risk_module.RiskOfRuinEvidence,
            "candidate_sha256",
            "0" * 64,
        ),
        (
            risk_module.RiskOfRuinEvidence,
            "evaluated_stake",
            Decimal("0.01"),
        ),
        (economic_admission.TicketLeg, "locked_odds", Decimal("999")),
        (economic_admission.MarketEvent, "source_id", "trusted-forged-provider"),
        (economic_admission.MarketEvent, "decimal_odds", Decimal("999")),
        (economic_admission.MarketEvent, "observed_ts", "2099-01-01T00:00:00Z"),
    ),
)
def test_proposal_market_descriptor_rebind_fails_closed(
    tmp_path,
    owner,
    field_name,
    forged_value,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg(f"proposal-descriptor-{field_name}")
    context = _context(candidate, _timestamp(now))
    policy = _policy(goal)
    original = owner.__dict__[field_name]
    hostile_called = False

    def forged_field(_self):
        nonlocal hostile_called
        hostile_called = True
        return forged_value

    try:
        setattr(owner, field_name, property(forged_field))
        result = admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=policy,
            stake=Decimal("0.01"),
            legs=(candidate,),
            reason="proposal market descriptor mutation must fail closed",
            placed_at=_timestamp(now),
            context=context,
            provider_source_ids=("provider-1",),
            bankroll_id="paper-bankroll",
            currency="USD",
        )
    finally:
        setattr(owner, field_name, original)

    assert hostile_called is False
    assert result.admitted is False
    assert result.risk.reason == "virtual bankroll risk helper authority is invalid"
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_paper_ticket_descriptor_witness_inventory_is_complete():
    observed = tuple(
        name
        for name, _descriptor in (
            economic_admission._PAPER_TICKET_FIELD_DESCRIPTOR_WITNESSES
        )
    )
    assert observed == tuple(economic_admission.PaperTicket.__dataclass_fields__)


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    (
        ("stake", Decimal("0")),
        ("status", "forged-open"),
        ("provider_source_ids", ()),
        ("provider_accounts", ()),
        ("bankroll_id", "forged-bankroll"),
        ("currency", "EUR"),
    ),
)
def test_paper_ticket_descriptor_rebind_cannot_understate_economic_history(
    tmp_path,
    field_name,
    forged_value,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg(f"paper-ticket-descriptor-{field_name}")
    context = _context(candidate, _timestamp(now))
    policy = _policy(goal)
    owner = economic_admission.PaperTicket
    original = owner.__dict__[field_name]
    hostile_called = False

    def forged_field(_self):
        nonlocal hostile_called
        hostile_called = True
        return forged_value

    try:
        setattr(owner, field_name, property(forged_field))
        result = admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=policy,
            stake=Decimal("0.01"),
            legs=(candidate,),
            reason="PaperTicket descriptor mutation must fail closed",
            placed_at=_timestamp(now),
            context=context,
            provider_source_ids=("provider-1",),
            bankroll_id="paper-bankroll",
            currency="USD",
        )
    finally:
        setattr(owner, field_name, original)

    assert hostile_called is False
    assert result.admitted is False
    assert result.risk.reason == "virtual bankroll risk helper authority is invalid"
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_paperbook_instance_state_class_witness_inventory_is_complete():
    observed = tuple(
        name
        for name, _present, _value in (
            economic_admission._PAPERBOOK_INSTANCE_STATE_CLASS_WITNESSES
        )
    )
    assert observed == (
        "initial_bankroll",
        "balance",
        "tickets",
        "_lifecycle",
        "_settlement_times",
    )
    assert all(
        present is False and value is None
        for _name, present, value in (
            economic_admission._PAPERBOOK_INSTANCE_STATE_CLASS_WITNESSES
        )
    )


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    (
        ("initial_bankroll", Decimal("1000000")),
        ("balance", Decimal("1000000")),
        ("tickets", {}),
        ("_lifecycle", []),
        ("_settlement_times", {}),
    ),
)
def test_paperbook_class_shadow_cannot_forge_economic_state(
    tmp_path,
    field_name,
    forged_value,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg(f"paperbook-shadow-{field_name}")
    context = _context(candidate, _timestamp(now))
    policy = _policy(goal)
    assert field_name not in PaperBook.__dict__
    hostile_called = False

    def forged_field(_self):
        nonlocal hostile_called
        hostile_called = True
        return forged_value

    try:
        setattr(PaperBook, field_name, property(forged_field))
        with pytest.raises(
            RuntimeError,
            match="economic admission PaperBook state descriptor authority changed",
        ):
            admit_paper_ticket(
                workspace=tmp_path,
                book=book,
                risk_policy=policy,
                stake=Decimal("0.01"),
                legs=(candidate,),
                reason="PaperBook class shadow must fail closed",
                placed_at=_timestamp(now),
                context=context,
                provider_source_ids=("provider-1",),
                bankroll_id="paper-bankroll",
                currency="USD",
            )
    finally:
        delattr(PaperBook, field_name)

    assert hostile_called is False
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_authority_instance_state_class_witness_inventory_is_complete():
    expected = {
        economic_admission.WorkspaceEconomicLock: (
            "workspace",
            "path",
            "_handle",
        ),
        economic_admission.ProductDayRiskWindowStore: (
            "workspace",
            "state_path",
            "_clock",
            "_authority",
        ),
        economic_admission.EconomicGoalStore: ("workspace", "path"),
        economic_admission.RunRegistry: ("path",),
        economic_admission.RunTransaction: (
            "workspace",
            "run_id",
            "root",
            "manifest_path",
            "run_ledger_path",
            "staged_book_path",
            "staged_ledger_path",
            "staged_summary_path",
            "_identity",
        ),
        economic_admission.MonotonicWorkspaceAuthority: (
            "workspace",
            "domain",
            "key",
            "authority_root",
            "workspace_binding",
            "workspace_instance_id",
            "authority_root_selection",
            "authority_root_binding_path",
            "workspace_binding_path",
            "namespace_sha256",
            "authority_root_activation_path",
            "journal_dir",
            "records_dir",
            "namespace_marker_path",
        ),
    }
    observed = {
        economic_admission.WorkspaceEconomicLock: tuple(
            name
            for name, _present, _value in (
                economic_admission._WORKSPACE_LOCK_STATE_WITNESSES
            )
        ),
        economic_admission.ProductDayRiskWindowStore: tuple(
            name
            for name, _present, _value in (
                economic_admission._PRODUCT_DAY_STORE_STATE_WITNESSES
            )
        ),
        economic_admission.EconomicGoalStore: tuple(
            name
            for name, _present, _value in (
                economic_admission._ECONOMIC_GOAL_STORE_STATE_WITNESSES
            )
        ),
        economic_admission.RunRegistry: tuple(
            name
            for name, _present, _value in (
                economic_admission._RUN_REGISTRY_STATE_WITNESSES
            )
        ),
        economic_admission.RunTransaction: tuple(
            name
            for name, _present, _value in (
                economic_admission._RUN_TRANSACTION_STATE_WITNESSES
            )
        ),
        economic_admission.MonotonicWorkspaceAuthority: tuple(
            name
            for name, _present, _value in (
                economic_admission._MONOTONIC_AUTHORITY_STATE_WITNESSES
            )
        ),
    }

    assert observed == expected
    for witnesses in (
        economic_admission._WORKSPACE_LOCK_STATE_WITNESSES,
        economic_admission._PRODUCT_DAY_STORE_STATE_WITNESSES,
        economic_admission._ECONOMIC_GOAL_STORE_STATE_WITNESSES,
        economic_admission._RUN_REGISTRY_STATE_WITNESSES,
        economic_admission._RUN_TRANSACTION_STATE_WITNESSES,
        economic_admission._MONOTONIC_AUTHORITY_STATE_WITNESSES,
    ):
        assert all(
            present is False and value is None
            for _name, present, value in witnesses
        )


@pytest.mark.parametrize(
    ("owner", "field_name", "error"),
    (
        (
            economic_admission.WorkspaceEconomicLock,
            "path",
            "workspace economic lock state authority changed",
        ),
        (
            economic_admission.ProductDayRiskWindowStore,
            "_clock",
            "product day store state authority changed",
        ),
        (
            economic_admission.MonotonicWorkspaceAuthority,
            "workspace_instance_id",
            "monotonic day authority state changed",
        ),
        (
            economic_admission.EconomicGoalStore,
            "path",
            "economic goal store state authority changed",
        ),
        (
            economic_admission.RunRegistry,
            "path",
            "economic admission run registry state authority changed",
        ),
        (
            economic_admission.RunTransaction,
            "root",
            "economic admission transaction state authority changed",
        ),
    ),
)
def test_authority_instance_state_class_shadow_fails_closed_before_read(
    tmp_path,
    owner,
    field_name,
    error,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg(f"authority-shadow-{field_name}")
    context = _context(candidate, _timestamp(now))
    policy = _policy(goal)
    assert field_name not in owner.__dict__
    hostile_called = False

    def forged_state(_self):
        nonlocal hostile_called
        hostile_called = True
        return object()

    try:
        type.__setattr__(owner, field_name, property(forged_state))
        with pytest.raises(RuntimeError, match=error):
            admit_paper_ticket(
                workspace=tmp_path,
                book=book,
                risk_policy=policy,
                stake=Decimal("0.01"),
                legs=(candidate,),
                reason="authority instance-state class shadow must fail closed",
                placed_at=_timestamp(now),
                context=context,
                provider_source_ids=("provider-1",),
                bankroll_id="paper-bankroll",
                currency="USD",
            )
    finally:
        if field_name in owner.__dict__:
            type.__delattr__(owner, field_name)

    assert hostile_called is False
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_carrier_executable_witness_inventory_is_complete():
    observed = {
        economic_admission.RiskDecision: tuple(
            name
            for name, _descriptor, _function, _code in (
                economic_admission._RISK_DECISION_EXECUTABLE_WITNESSES
            )
        ),
        economic_admission.ProposedTicketRiskContext: tuple(
            name
            for name, _descriptor, _function, _code in (
                economic_admission._PROPOSED_CONTEXT_EXECUTABLE_WITNESSES
            )
        ),
        economic_admission.TicketLeg: tuple(
            name
            for name, _descriptor, _function, _code in (
                economic_admission._TICKET_LEG_EXECUTABLE_WITNESSES
            )
        ),
        economic_admission.MarketEvent: tuple(
            name
            for name, _descriptor, _function, _code in (
                economic_admission._MARKET_EVENT_EXECUTABLE_WITNESSES
            )
        ),
        economic_admission.EconomicGoalContract: tuple(
            name
            for name, _descriptor, _function, _code in (
                economic_admission._ECONOMIC_GOAL_EXECUTABLE_WITNESSES
            )
        ),
        economic_admission.PaperTicket: tuple(
            name
            for name, _descriptor, _function, _code in (
                economic_admission._PAPER_TICKET_EXECUTABLE_WITNESSES
            )
        ),
    }
    assert observed == {
        economic_admission.RiskDecision: ("__init__",),
        economic_admission.ProposedTicketRiskContext: ("__init__", "__post_init__"),
        economic_admission.TicketLeg: ("__eq__", "quote_key"),
        economic_admission.MarketEvent: (
            "__eq__",
            "quote_key",
            "to_dict",
            "from_dict",
        ),
        economic_admission.EconomicGoalContract: (
            "__init__",
            "__post_init__",
            "__eq__",
        ),
        economic_admission.PaperTicket: ("__init__", "__eq__"),
    }

    day_observed = {
        owner: tuple(
            name for name, _descriptor, _function, _code in witnesses
        )
        for owner, witnesses in (
            economic_admission._DAY_AUTHORITY_EXECUTABLE_DESCRIPTOR_WITNESSES
        )
    }
    assert day_observed == {
        economic_admission._PaperDayTurnoverSnapshot: ("__init__",),
        economic_admission._ProductDayAdmissionAuthority: ("__init__",),
        economic_admission.PaperDayTurnoverEvidence: ("__init__", "__post_init__"),
        economic_admission.ProductDayRiskWindow: (
            "__init__",
            "__post_init__",
            "__eq__",
        ),
    }


@pytest.mark.parametrize(
    ("owner", "descriptor_name", "outcome"),
    (
        (economic_admission.RiskDecision, "__init__", "risk-decision-error"),
        (
            economic_admission.ProposedTicketRiskContext,
            "__post_init__",
            "risk-deny",
        ),
        (economic_admission.TicketLeg, "quote_key", "risk-deny"),
        (economic_admission.MarketEvent, "from_dict", "risk-deny"),
        (economic_admission.EconomicGoalContract, "__eq__", "risk-deny"),
        (economic_admission.PaperTicket, "__eq__", "risk-deny"),
        (
            economic_admission._ProductDayAdmissionAuthority,
            "__init__",
            "day-error",
        ),
        (
            economic_admission.PaperDayTurnoverEvidence,
            "__init__",
            "day-error",
        ),
        (
            economic_admission.ProductDayRiskWindow,
            "__eq__",
            "day-error",
        ),
    ),
)
def test_carrier_executable_descriptor_rebind_fails_closed_before_dispatch(
    tmp_path,
    owner,
    descriptor_name,
    outcome,
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg(f"carrier-executable-{descriptor_name}")
    context = _context(candidate, _timestamp(now))
    policy = _policy(goal)
    original = owner.__dict__[descriptor_name]
    hostile_called = False

    def forged(*args, **kwargs):
        nonlocal hostile_called
        del args, kwargs
        hostile_called = True
        return True

    if type(original) is property:
        replacement = property(forged)
    elif type(original) is classmethod:
        replacement = classmethod(forged)
    elif type(original) is staticmethod:
        replacement = staticmethod(forged)
    else:
        replacement = forged

    try:
        setattr(owner, descriptor_name, replacement)
        if outcome == "risk-decision-error":
            with pytest.raises(
                RuntimeError,
                match="economic admission risk decision executable authority changed",
            ):
                admit_paper_ticket(
                    workspace=tmp_path,
                    book=book,
                    risk_policy=policy,
                    stake=Decimal("0.01"),
                    legs=(candidate,),
                    reason="mutated carrier executable must fail closed",
                    placed_at=_timestamp(now),
                    context=context,
                    provider_source_ids=("provider-1",),
                    bankroll_id="paper-bankroll",
                    currency="USD",
                )
        elif outcome == "day-error":
            with pytest.raises(
                RuntimeError,
                match="economic admission day authority executable descriptor changed",
            ):
                admit_paper_ticket(
                    workspace=tmp_path,
                    book=book,
                    risk_policy=policy,
                    stake=Decimal("0.01"),
                    legs=(candidate,),
                    reason="mutated day carrier executable must fail closed",
                    placed_at=_timestamp(now),
                    context=context,
                    provider_source_ids=("provider-1",),
                    bankroll_id="paper-bankroll",
                    currency="USD",
                )
        else:
            result = admit_paper_ticket(
                workspace=tmp_path,
                book=book,
                risk_policy=policy,
                stake=Decimal("0.01"),
                legs=(candidate,),
                reason="mutated risk carrier executable must fail closed",
                placed_at=_timestamp(now),
                context=context,
                provider_source_ids=("provider-1",),
                bankroll_id="paper-bankroll",
                currency="USD",
            )
            assert result.admitted is False
            assert result.risk.reason == "virtual bankroll risk helper authority is invalid"
    finally:
        setattr(owner, descriptor_name, original)

    assert hostile_called is False
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_risk_policy_limit_descriptor_rebind_cannot_widen_ticket_cap(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("policy-descriptor")
    context = _context(candidate, _timestamp(now))
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.00001"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )
    original = PaperRiskPolicy.__dict__["max_ticket_fraction"]
    hostile_called = False

    def widened(_self):
        nonlocal hostile_called
        hostile_called = True
        return Decimal("1")

    try:
        PaperRiskPolicy.max_ticket_fraction = property(widened)
        result = admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=policy,
            stake=Decimal("0.01"),
            legs=(candidate,),
            reason="policy descriptor replacement must fail closed",
            placed_at=_timestamp(now),
            context=context,
            provider_source_ids=("provider-1",),
            bankroll_id="paper-bankroll",
            currency="USD",
        )
    finally:
        PaperRiskPolicy.max_ticket_fraction = original

    assert hostile_called is False
    assert result.admitted is False
    assert result.risk.reason == "virtual bankroll risk helper authority is invalid"
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_goal_emergency_stop_descriptor_rebind_cannot_disable_stop(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = replace(_goal(), emergency_stop=True)
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("goal-descriptor")
    context = _context(candidate, _timestamp(now))
    policy = _policy(goal)
    original = EconomicGoalContract.__dict__["emergency_stop"]
    hostile_called = False

    def disabled(_self):
        nonlocal hostile_called
        hostile_called = True
        return False

    try:
        EconomicGoalContract.emergency_stop = property(disabled)
        result = admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=policy,
            stake=Decimal("0.01"),
            legs=(candidate,),
            reason="goal descriptor replacement must fail closed",
            placed_at=_timestamp(now),
            context=context,
            provider_source_ids=("provider-1",),
            bankroll_id="paper-bankroll",
            currency="USD",
        )
    finally:
        EconomicGoalContract.emergency_stop = original

    assert hostile_called is False
    assert result.admitted is False
    assert result.risk.reason == "virtual bankroll risk helper authority is invalid"
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_risk_evaluate_class_rebind_cannot_bypass_local_risk_gates(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("candidate-evaluate-rebind")
    context = _context(candidate, _timestamp(now))
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.00001"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )
    hostile_called = False
    original = PaperRiskPolicy.evaluate

    def hostile_evaluate(self, book, stake, *, context=None):
        nonlocal hostile_called
        del self, book, stake, context
        hostile_called = True
        return risk_module.RiskDecision(True, "allowed")

    try:
        PaperRiskPolicy.evaluate = hostile_evaluate
        result = admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=policy,
            stake=Decimal("0.01"),
            legs=(candidate,),
            reason="class-rebound evaluate must not become risk authority",
            placed_at=_timestamp(now),
            context=context,
            provider_source_ids=("provider-1",),
            bankroll_id="paper-bankroll",
            currency="USD",
        )
    finally:
        PaperRiskPolicy.evaluate = original

    assert hostile_called is False
    assert result.admitted is False
    assert result.risk.reason == "virtual bankroll risk helper authority is invalid"
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_risk_evaluate_code_replacement_cannot_bypass_local_risk_gates(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("candidate-evaluate-code")
    context = _context(candidate, _timestamp(now))
    policy = _policy(goal)
    target = PaperRiskPolicy.evaluate
    original_code = target.__code__

    try:
        target.__code__ = _replacement_code_preserving_freevars(target)
        result = admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=policy,
            stake=Decimal("0.01"),
            legs=(candidate,),
            reason="mutated evaluate code must not become risk authority",
            placed_at=_timestamp(now),
            context=context,
            provider_source_ids=("provider-1",),
            bankroll_id="paper-bankroll",
            currency="USD",
        )
    finally:
        target.__code__ = original_code

    assert result.admitted is False
    assert result.risk.reason == "virtual bankroll risk helper authority is invalid"
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


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
    hostile_called = False

    original = economic_admission._revalidated_product_day_admission_authority

    def hostile_authority(**kwargs):
        nonlocal hostile_called
        del kwargs
        hostile_called = True
        return economic_admission._ProductDayAdmissionAuthority(
            turnover_room=Decimal("999999"),
            admission_ts=_timestamp(now),
        )

    try:
        economic_admission._revalidated_product_day_admission_authority = (
            hostile_authority
        )
        baseline, result = _admit(tmp_path, book, goal, _timestamp(now))
    finally:
        economic_admission._revalidated_product_day_admission_authority = original

    assert hostile_called is False
    assert baseline.reason == "economic goal turnover limit exceeded"
    assert result.admitted is False
    assert result.risk.reason == "economic goal turnover limit exceeded"


def test_admission_closure_mapping_is_read_only(tmp_path):
    del tmp_path
    consumer = _admission_current_binding_consumer()
    inner_globals = _closure_cell(consumer, "inner_globals").cell_contents

    with pytest.raises(TypeError):
        inner_globals["_revalidated_product_day_admission_authority"] = (
            lambda **kwargs: None
        )


def test_admission_closure_cell_replacement_cannot_mint_turnover_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(now), stake="5")
    book.save(tmp_path / "paper_book.json")

    consumer = _admission_current_binding_consumer()
    globals_cell = _closure_cell(consumer, "inner_globals")
    original_globals = globals_cell.cell_contents
    hostile_globals = dict(original_globals)
    hostile_called = False

    def hostile_authority(**kwargs):
        nonlocal hostile_called
        del kwargs
        hostile_called = True
        return economic_admission._ProductDayAdmissionAuthority(
            turnover_room=Decimal("999999"),
            admission_ts=_timestamp(now),
        )

    hostile_globals["_revalidated_product_day_admission_authority"] = (
        hostile_authority
    )
    globals_cell.cell_contents = hostile_globals
    try:
        with pytest.raises(
            RuntimeError,
            match="PAPER admission sealed consumer authority changed",
        ):
            _admit(tmp_path, book, goal, _timestamp(now))
    finally:
        globals_cell.cell_contents = original_globals

    assert hostile_called is False
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_admission_closure_helper_code_replacement_cannot_mint_headroom(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(now), stake="5")
    book.save(tmp_path / "paper_book.json")

    consumer = _admission_current_binding_consumer()
    inner_globals = _closure_cell(consumer, "inner_globals").cell_contents
    target = inner_globals["_revalidated_product_day_admission_authority"]
    original_code = target.__code__

    try:
        target.__code__ = _replacement_code_preserving_freevars(target)
        with pytest.raises(
            RuntimeError,
            match="PAPER admission sealed consumer authority changed",
        ):
            _admit(tmp_path, book, goal, _timestamp(now))
    finally:
        target.__code__ = original_code

    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def _assert_recovery_gate_tamper_rejected(tmp_path, match):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")

    with pytest.raises(RuntimeError, match=match):
        _admit(tmp_path, book, goal, _timestamp(now))

    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert set(persisted.tickets) == set(book.tickets)


def test_run_registry_gate_rebind_cannot_bypass_unresolved_run_blocker(tmp_path):
    original = economic_admission.RunRegistry.in_progress
    try:
        economic_admission.RunRegistry.in_progress = lambda self: ()
        _assert_recovery_gate_tamper_rejected(
            tmp_path,
            "economic admission run registry gate authority changed",
        )
    finally:
        economic_admission.RunRegistry.in_progress = original


def test_run_transaction_root_rebind_cannot_hide_recovery_history(tmp_path):
    original = economic_admission.RunTransaction.ROOT_NAME
    try:
        economic_admission.RunTransaction.ROOT_NAME = ".forged-transactions"
        _assert_recovery_gate_tamper_rejected(
            tmp_path,
            "economic admission recovery gate authority changed",
        )
    finally:
        economic_admission.RunTransaction.ROOT_NAME = original


def test_recovery_helper_code_replacement_cannot_hide_transaction_history(tmp_path):
    target = recovery_module._lstat_or_none
    original_code = target.__code__
    try:
        target.__code__ = _replacement_code_preserving_freevars(target)
        _assert_recovery_gate_tamper_rejected(
            tmp_path,
            "economic admission recovery gate authority changed",
        )
    finally:
        target.__code__ = original_code


def _paperbook_mutation_gate_case(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old = now - timedelta(days=2)
    goal = _goal()
    EconomicGoalStore(tmp_path).initialize_owner(goal)
    book = _book_with_settled_turnover(placed_at=_timestamp(old), stake="50")
    book.save(tmp_path / "paper_book.json")
    candidate = _leg("paperbook-mutation-gate")
    context = _context(candidate, _timestamp(now))
    policy = _policy(goal)
    return now, book, candidate, context, policy


def _assert_paperbook_mutation_gate_tamper_rejected(
    tmp_path,
    *,
    now,
    book,
    candidate,
    context,
    policy,
    match,
):
    with pytest.raises(RuntimeError, match=match):
        admit_paper_ticket(
            workspace=tmp_path,
            book=book,
            risk_policy=policy,
            stake=Decimal("0.01"),
            legs=(candidate,),
            reason="mutated PaperBook dispatch must fail closed",
            placed_at=_timestamp(now),
            context=context,
            provider_source_ids=("provider-1",),
            bankroll_id="paper-bankroll",
            currency="USD",
        )


def test_paperbook_open_ticket_rebind_cannot_inflate_approved_stake(tmp_path):
    now, book, candidate, context, policy = _paperbook_mutation_gate_case(tmp_path)
    original = PaperBook.__dict__["open_ticket"]
    hostile_called = False

    def hostile_open(self, legs, stake, *args, **kwargs):
        nonlocal hostile_called
        hostile_called = True
        del stake
        return original(
            self,
            legs,
            Decimal("50"),
            *args,
            **kwargs,
        )

    try:
        PaperBook.open_ticket = hostile_open
        _assert_paperbook_mutation_gate_tamper_rejected(
            tmp_path,
            now=now,
            book=book,
            candidate=candidate,
            context=context,
            policy=policy,
            match="economic admission PaperBook mutation authority changed",
        )
    finally:
        PaperBook.open_ticket = original

    assert hostile_called is False
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert len(persisted.tickets) == 1


def test_paperbook_save_rebind_cannot_redirect_approved_mutation(tmp_path):
    now, book, candidate, context, policy = _paperbook_mutation_gate_case(tmp_path)
    original = PaperBook.__dict__["save"]
    hostile_called = False

    def hostile_save(self, path):
        nonlocal hostile_called
        hostile_called = True
        del self, path
        raise AssertionError("replacement save executed")

    try:
        PaperBook.save = hostile_save
        _assert_paperbook_mutation_gate_tamper_rejected(
            tmp_path,
            now=now,
            book=book,
            candidate=candidate,
            context=context,
            policy=policy,
            match="economic admission PaperBook mutation authority changed",
        )
    finally:
        PaperBook.save = original

    assert hostile_called is False
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert len(persisted.tickets) == 1


def test_paperbook_load_rebind_cannot_substitute_economic_state(tmp_path):
    now, book, candidate, context, policy = _paperbook_mutation_gate_case(tmp_path)
    original = PaperBook.__dict__["load"]
    hostile_called = False

    def hostile_load(cls, path):
        nonlocal hostile_called
        hostile_called = True
        del cls, path
        return PaperBook("1000000")

    try:
        PaperBook.load = classmethod(hostile_load)
        _assert_paperbook_mutation_gate_tamper_rejected(
            tmp_path,
            now=now,
            book=book,
            candidate=candidate,
            context=context,
            policy=policy,
            match="economic admission PaperBook mutation authority changed",
        )
    finally:
        PaperBook.load = original

    assert hostile_called is False
    persisted = PaperBook.load(tmp_path / "paper_book.json")
    assert len(persisted.tickets) == 1


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
