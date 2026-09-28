from __future__ import annotations

from decimal import Decimal
from types import FunctionType

import pytest

from autosport.domain import MarketEvent, TicketLeg, TicketStatus
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext, RiskDecision


def _noop_validator(cls, book) -> None:
    del cls, book


def _noop_graph_check(*args, **kwargs) -> None:
    del args, kwargs


def _descriptor_in_mro(owner: type, name: str) -> object:
    for candidate in owner.__mro__:
        namespace = vars(candidate)
        if name in namespace:
            return namespace[name]
    raise AssertionError(f"missing canonical descriptor: {name}")


def test_public_risk_wrapper_cannot_retarget_reachable_generation_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested public risk specs must not expose mutable generation admission helpers."""

    book = PaperBook("100")
    # Empty opening/causal history is still product-issued and therefore passes the
    # outer private-authority guard. Only canonical loaded-state validation can reject
    # this impossible balance.
    book.balance = Decimal("999")

    monkeypatch.setattr(
        PaperBook,
        "_validate_loaded_state",
        classmethod(_noop_validator),
    )

    # The owner-facing root seal intentionally publishes a zero-state subclass facade;
    # private read roots remain inherited from the already-composed canonical base.
    # Resolve the actual descriptor through the MRO so this falsifier cannot turn RED
    # merely because the root facade moved the descriptor off vars(PaperRiskPolicy).
    descriptor = _descriptor_in_mro(PaperRiskPolicy, "_book_state")
    assert type(descriptor) is classmethod
    public_wrapper = descriptor.__func__
    assert type(public_wrapper) is FunctionType

    # The private-authority wrapper stores the prior generation wrapper as a delegate
    # spec. Its global-bindings tuple currently exposes the live inner guard function,
    # whose __globals__ is the generation guard's mutable authority mapping.
    nested_spec = public_wrapper.__globals__["_BOOK_STATE_SPEC"]
    assert type(nested_spec) is tuple and len(nested_spec) == 6
    nested_global_bindings = nested_spec[5]
    assert type(nested_global_bindings) is tuple
    nested_globals = dict(nested_global_bindings)
    inner_guard = nested_globals["_guarded_risk_call"]
    assert type(inner_guard) is FunctionType

    generation_globals = inner_guard.__globals__
    monkeypatch.setitem(
        generation_globals,
        "_FROZEN_VALIDATE_LOADED_STATE",
        _noop_validator,
    )
    monkeypatch.setitem(
        generation_globals,
        "_FROZEN_REQUIRE_CLASS_GRAPH",
        _noop_graph_check,
    )

    # Retargeting helpers reachable from a public risk callable must never turn
    # structurally impossible caller-authored economics into a positive risk state.
    assert PaperRiskPolicy._book_state(book) is None


def test_sealed_owner_root_cannot_retarget_wrapper_global_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Class-level sealing must also protect the executable helper dispatch beneath it."""

    book = PaperBook("100")
    policy = PaperRiskPolicy()
    root = vars(PaperRiskPolicy)["evaluate"]
    assert type(root) is FunctionType
    hostile_executed = False

    def hostile_private_guard(*args, **kwargs):
        nonlocal hostile_executed
        del args, kwargs
        hostile_executed = True
        return RiskDecision(True, "hostile wrapper-global bypass")

    # Adding/replacing a global is deliberately used rather than replacing the sealed
    # class attribute. A real root seal must not leave its positive decision dispatch
    # controlled by the mutable __globals__ dictionary of the exported Python function.
    monkeypatch.setitem(root.__globals__, "_guarded_private_call", hostile_private_guard)

    decision = policy.evaluate(book, Decimal("99"))
    assert hostile_executed is False
    assert decision.allowed is False


_TS = "2026-09-27T21:40:00+00:00"


def _reconstruct_spec(spec: tuple[object, ...]) -> FunctionType:
    code, name, defaults, kwdefaults, closure, global_bindings = spec
    assert type(global_bindings) is tuple
    function = FunctionType(
        code,
        dict(global_bindings),
        name=name,
        argdefs=defaults,
        closure=closure,
    )
    if kwdefaults is not None:
        function.__kwdefaults__ = dict(kwdefaults)
    return function


def _generation_race_leg() -> TicketLeg:
    return TicketLeg(
        "risk-reconstruct-event",
        "risk-reconstruct-market",
        "concurrent-writer",
        Decimal("2"),
        sport="soccer",
        exchange_side="back",
    )


class _PublicationTriggerDecimal(Decimal):
    """Exact Decimal value that runs one test callback on right-side multiplication."""

    def __new__(cls, value: str, trigger):
        instance = super().__new__(cls, value)
        instance._trigger = trigger
        instance._triggered = False
        return instance

    def __rmul__(self, other):
        if not self._triggered:
            self._triggered = True
            self._trigger()
        return Decimal(other) * Decimal(self)


def test_reconstructible_nested_evaluate_spec_cannot_bypass_decision_generation_scope(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nested exported spec must not reconstruct a pre-admission positive authority."""

    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-risk-reconstruct-authority"),
    )
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.save(path)
    evaluated = PaperBook.load(path)
    writer = PaperBook.load(path)

    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )

    public_root = vars(PaperRiskPolicy)["evaluate"]
    assert type(public_root) is FunctionType
    private_spec = public_root.__globals__["_EVALUATE_SPEC"]
    assert type(private_spec) is tuple and len(private_spec) == 6
    generation_globals = dict(private_spec[5])
    nested_spec = generation_globals["_EVALUATE_SPEC"]
    assert type(nested_spec) is tuple and len(nested_spec) == 6
    reconstructed = _reconstruct_spec(nested_spec)

    writer_published = False

    def interleave_publication() -> None:
        nonlocal writer_published
        writer.open_ticket(
            [_generation_race_leg()],
            "95",
            placed_at=_TS,
        )
        writer.save(path)
        writer_published = True

    object.__setattr__(
        policy,
        "minimum_cash_reserve_fraction",
        _PublicationTriggerDecimal("0", interleave_publication),
    )

    decision = reconstructed(policy, evaluated, Decimal("10"))

    assert writer_published is True
    assert decision.allowed is False


def test_closure_reachable_nested_evaluate_spec_cannot_bypass_generation_scope(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closure-retained specs must be no weaker than the installed public risk root."""

    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-risk-closure-reconstruct-authority"),
    )
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.save(path)
    evaluated = PaperBook.load(path)
    writer = PaperBook.load(path)

    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )

    public_root = vars(PaperRiskPolicy)["evaluate"]
    assert type(public_root) is FunctionType
    outer_spec = None
    for cell in public_root.__closure__ or ():
        try:
            candidate = cell.cell_contents
        except ValueError:
            continue
        if (
            type(candidate) is tuple
            and len(candidate) == 6
            and type(candidate[5]) is tuple
        ):
            globals_snapshot = dict(candidate[5])
            nested = globals_snapshot.get("_EVALUATE_SPEC")
            if type(nested) is tuple and len(nested) == 6:
                outer_spec = nested
                break
    assert outer_spec is not None
    reconstructed = _reconstruct_spec(outer_spec)

    writer_published = False

    def interleave_publication() -> None:
        nonlocal writer_published
        writer.open_ticket(
            [_generation_race_leg()],
            "95",
            placed_at=_TS,
        )
        writer.save(path)
        writer_published = True

    object.__setattr__(
        policy,
        "minimum_cash_reserve_fraction",
        _PublicationTriggerDecimal("0", interleave_publication),
    )

    decision = reconstructed(policy, evaluated, Decimal("10"))

    assert writer_published is True
    assert decision.allowed is False


def test_closure_reachable_goal_stake_spec_rejects_generation_change(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-stake-closure-reconstruct-authority"),
    )
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.save(path)
    evaluated = PaperBook.load(path)
    writer = PaperBook.load(path)

    goal = EconomicGoalContract(
        goal_id="risk-reconstruct-goal",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("1"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("1000"),
        max_risk_of_ruin=Decimal("1"),
        max_concurrent_positions=10,
    )
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )

    public_root = vars(PaperRiskPolicy)["derive_goal_stake"]
    assert type(public_root) is FunctionType
    nested_spec = None
    for cell in public_root.__closure__ or ():
        try:
            candidate = cell.cell_contents
        except ValueError:
            continue
        if (
            type(candidate) is tuple
            and len(candidate) == 6
            and type(candidate[5]) is tuple
        ):
            globals_snapshot = dict(candidate[5])
            inner = globals_snapshot.get("_DERIVE_GOAL_STAKE_SPEC")
            if type(inner) is tuple and len(inner) == 6:
                nested_spec = inner
                break
    assert nested_spec is not None
    reconstructed = _reconstruct_spec(nested_spec)

    writer_published = False

    def interleave_publication() -> None:
        nonlocal writer_published
        writer.open_ticket(
            [_generation_race_leg()],
            "95",
            placed_at=_TS,
        )
        writer.save(path)
        writer_published = True

    object.__setattr__(
        policy,
        "minimum_cash_reserve_fraction",
        _PublicationTriggerDecimal("0", interleave_publication),
    )

    amount = reconstructed(policy, evaluated, Decimal("0.5"))

    assert writer_published is True
    assert amount is None


def test_closure_reachable_stake_vector_spec_rejects_generation_change(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-vector-closure-reconstruct-authority"),
    )
    path = tmp_path / "paper-book.json"

    initial = PaperBook("100")
    initial.save(path)
    evaluated = PaperBook.load(path)
    writer = PaperBook.load(path)

    goal = EconomicGoalContract(
        goal_id="risk-reconstruct-vector-goal",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("1"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("1000"),
        max_risk_of_ruin=Decimal("1"),
        max_concurrent_positions=10,
    )
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )

    quote = MarketEvent(
        event_id="risk-vector-event",
        market_id="risk-vector-market",
        selection_id="risk-vector-selection",
        decimal_odds=Decimal("2"),
        observed_ts="2026-09-27T21:40:00+00:00",
        source_id="risk-vector-provider",
        sequence=1,
        source_ts="2026-09-27T21:39:59+00:00",
        ingest_ts="2026-09-27T21:40:00+00:00",
    )
    context = ProposedTicketRiskContext(
        legs=(
            TicketLeg(
                quote.event_id,
                quote.market_id,
                quote.selection_id,
                quote.decimal_odds,
                sport="soccer",
                exchange_side="back",
            ),
        ),
        quotes=(quote,),
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
        proposal_ts=quote.observed_ts,
    )

    public_root = vars(PaperRiskPolicy)["derive_goal_stake_vector"]
    assert type(public_root) is FunctionType
    nested_spec = None
    for cell in public_root.__closure__ or ():
        try:
            candidate = cell.cell_contents
        except ValueError:
            continue
        if (
            type(candidate) is tuple
            and len(candidate) == 6
            and type(candidate[5]) is tuple
        ):
            globals_snapshot = dict(candidate[5])
            inner = globals_snapshot.get("_DERIVE_GOAL_STAKE_VECTOR_SPEC")
            if type(inner) is tuple and len(inner) == 6:
                nested_spec = inner
                break
    assert nested_spec is not None
    reconstructed = _reconstruct_spec(nested_spec)

    writer_published = False

    def interleave_publication() -> None:
        nonlocal writer_published
        writer.open_ticket(
            [_generation_race_leg()],
            "95",
            placed_at=_TS,
        )
        writer.save(path)
        writer_published = True

    object.__setattr__(
        policy,
        "minimum_cash_reserve_fraction",
        _PublicationTriggerDecimal("0", interleave_publication),
    )

    decision = reconstructed(
        policy,
        evaluated,
        (Decimal("0.5"),),
        contexts=(context,),
        risk_of_ruin_vector_evidence=None,
    )

    assert writer_published is True
    assert decision.action == "WAIT"
    assert decision.stakes == (Decimal("0"),)
    assert "changed during stake-vector allocation" in decision.reason


def test_closure_reachable_evaluate_spec_cannot_bypass_opening_authority() -> None:
    """Reconstructed delegates must still consume product-issued opening economics."""

    book = PaperBook("100")
    ticket = book.open_ticket(
        [
            TicketLeg(
                "risk-private-event",
                "risk-private-market",
                "risk-private-selection",
                Decimal("2"),
                sport="soccer",
                exchange_side="back",
            )
        ],
        "10",
        placed_at=_TS,
    )

    # Keep public structure internally valid while contradicting the private opening
    # commitment created by PaperBook.open_ticket.
    ticket.stake = Decimal("1")
    book.balance = Decimal("99")
    PaperBook._validate_loaded_state(book)

    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    public_root = vars(PaperRiskPolicy)["evaluate"]
    assert type(public_root) is FunctionType
    nested_spec = None
    for cell in public_root.__closure__ or ():
        try:
            candidate = cell.cell_contents
        except ValueError:
            continue
        if (
            type(candidate) is tuple
            and len(candidate) == 6
            and type(candidate[5]) is tuple
        ):
            globals_snapshot = dict(candidate[5])
            inner = globals_snapshot.get("_EVALUATE_SPEC")
            if type(inner) is tuple and len(inner) == 6:
                nested_spec = inner
                break
    assert nested_spec is not None

    reconstructed = _reconstruct_spec(nested_spec)
    decision = reconstructed(policy, book, Decimal("1"))

    assert decision.allowed is False
    assert "virtual bankroll" in decision.reason


def test_closure_reachable_evaluate_spec_cannot_bypass_causal_authority() -> None:
    """Reconstructed delegates must still consume product-issued settlement causality."""

    leg = TicketLeg(
        "risk-causal-event",
        "risk-causal-market",
        "risk-causal-selection",
        Decimal("2"),
        sport="soccer",
        exchange_side="back",
    )
    book = PaperBook("100")
    ticket = book.open_ticket([leg], "10", placed_at=_TS)
    settled_at = "2026-09-27T21:41:00+00:00"
    book.settle(ticket.ticket_id, {leg.quote_key}, settled_at=settled_at)

    # Rewrite all caller-visible settlement facts to a coherent LOSS while the
    # product-issued private causal commitment still records the original WIN.
    ticket.status = TicketStatus.LOST
    ticket.payout = Decimal("0")
    ticket.settled_at = settled_at
    book.balance = Decimal("90")
    book._lifecycle[-1] = ("settle", ticket.ticket_id, (), ())
    PaperBook._validate_loaded_state(book)

    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    public_root = vars(PaperRiskPolicy)["evaluate"]
    assert type(public_root) is FunctionType
    nested_spec = None
    for cell in public_root.__closure__ or ():
        try:
            candidate = cell.cell_contents
        except ValueError:
            continue
        if (
            type(candidate) is tuple
            and len(candidate) == 6
            and type(candidate[5]) is tuple
        ):
            globals_snapshot = dict(candidate[5])
            inner = globals_snapshot.get("_EVALUATE_SPEC")
            if type(inner) is tuple and len(inner) == 6:
                nested_spec = inner
                break
    assert nested_spec is not None

    reconstructed = _reconstruct_spec(nested_spec)
    decision = reconstructed(policy, book, Decimal("1"))

    assert decision.allowed is False
    assert "virtual bankroll" in decision.reason


def test_reconstructed_evaluate_cannot_retarget_book_state_root() -> None:
    """The raw historical evaluator cannot replace its final admission state root."""

    book = PaperBook("100")
    book.balance = Decimal("999")

    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    public_root = vars(PaperRiskPolicy)["evaluate"]
    assert type(public_root) is FunctionType
    nested_spec = None
    for cell in public_root.__closure__ or ():
        try:
            candidate = cell.cell_contents
        except ValueError:
            continue
        if (
            type(candidate) is tuple
            and len(candidate) == 6
            and type(candidate[5]) is tuple
        ):
            globals_snapshot = dict(candidate[5])
            inner = globals_snapshot.get("_EVALUATE_SPEC")
            if type(inner) is tuple and len(inner) == 6:
                nested_spec = inner
                break
    assert nested_spec is not None
    reconstructed = _reconstruct_spec(nested_spec)

    hostile_executed = False

    def hostile_book_state(cls, candidate_book):
        nonlocal hostile_executed
        del cls, candidate_book
        hostile_executed = True
        return (Decimal("100"), Decimal("100"), Decimal("0"), 0)

    hostile_descriptor = classmethod(hostile_book_state)
    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        setattr(PaperRiskPolicy, "_book_state", hostile_descriptor)
    with pytest.raises(TypeError, match="canonical PaperRiskPolicy root is sealed"):
        type.__setattr__(PaperRiskPolicy, "_book_state", hostile_descriptor)

    decision = reconstructed(policy, book, Decimal("1"))

    assert hostile_executed is False
    assert decision.allowed is False
    assert "virtual bankroll" in decision.reason


def test_reconstructed_evaluate_rejects_mutated_policy_helper_graph() -> None:
    """Sealed _book_state must witness helper dispatch before raw delegate authority."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    public_root = vars(PaperRiskPolicy)["evaluate"]
    assert type(public_root) is FunctionType
    nested_spec = None
    for cell in public_root.__closure__ or ():
        try:
            candidate = cell.cell_contents
        except ValueError:
            continue
        if (
            type(candidate) is tuple
            and len(candidate) == 6
            and type(candidate[5]) is tuple
        ):
            globals_snapshot = dict(candidate[5])
            inner = globals_snapshot.get("_EVALUATE_SPEC")
            if type(inner) is tuple and len(inner) == 6:
                nested_spec = inner
                break
    assert nested_spec is not None
    reconstructed = _reconstruct_spec(nested_spec)

    original = vars(PaperRiskPolicy)["_derived_risk_values"]
    hostile_executed = False

    def hostile_derived(self, initial_bankroll, balance, committed_stake, amount):
        nonlocal hostile_executed
        del self, initial_bankroll, balance, committed_stake, amount
        hostile_executed = True
        return (
            Decimal("100"),
            Decimal("0"),
            Decimal("100"),
            Decimal("100"),
            Decimal("0"),
        )

    try:
        setattr(PaperRiskPolicy, "_derived_risk_values", hostile_derived)
        decision = reconstructed(policy, book, Decimal("1"))
    finally:
        if vars(PaperRiskPolicy).get("_derived_risk_values") is not original:
            setattr(PaperRiskPolicy, "_derived_risk_values", original)

    assert hostile_executed is False
    assert decision.allowed is False
    assert "virtual bankroll" in decision.reason

def test_reconstructed_evaluate_rejects_in_place_mutated_policy_helper_code() -> None:
    """Raw reconstructed evaluator must witness helper code, not only descriptor identity."""

    book = PaperBook("100")
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )
    public_root = vars(PaperRiskPolicy)["evaluate"]
    assert type(public_root) is FunctionType
    nested_spec = None
    for cell in public_root.__closure__ or ():
        try:
            candidate = cell.cell_contents
        except ValueError:
            continue
        if (
            type(candidate) is tuple
            and len(candidate) == 6
            and type(candidate[5]) is tuple
        ):
            globals_snapshot = dict(candidate[5])
            inner = globals_snapshot.get("_EVALUATE_SPEC")
            if type(inner) is tuple and len(inner) == 6:
                nested_spec = inner
                break
    assert nested_spec is not None
    reconstructed = _reconstruct_spec(nested_spec)

    helper = vars(PaperRiskPolicy)["_derived_risk_values"]
    original_code = helper.__code__
    hostile_executed = False

    def hostile_derived(self, initial_bankroll, balance, committed_stake, amount):
        nonlocal hostile_executed
        del self, initial_bankroll, balance, committed_stake, amount
        hostile_executed = True
        return (
            Decimal("100"),
            Decimal("0"),
            Decimal("100"),
            Decimal("100"),
            Decimal("0"),
        )

    try:
        helper.__code__ = hostile_derived.__code__
        decision = reconstructed(policy, book, Decimal("1"))
    finally:
        helper.__code__ = original_code

    assert hostile_executed is False
    assert decision.allowed is False
    assert "virtual bankroll" in decision.reason

