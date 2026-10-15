from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

import autosport.risk_turnover_evidence as turnover_evidence

from autosport.domain import TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.paper import PaperBook
from autosport.risk_day_window import ProductDayRiskWindowStore
from autosport.risk_turnover_evidence import (
    PaperDayTurnoverEvidenceError,
    PaperDayTurnoverEvidenceIncompleteError,
    PaperDayTurnoverEvidenceMismatchError,
    PaperDayTurnoverResolver,
)


class _HostileText(str):
    hook_calls = 0

    def __ne__(self, other: object) -> bool:
        type(self).hook_calls += 1
        raise AssertionError("hostile text inequality executed")

    def strip(self, *args: object, **kwargs: object) -> str:
        type(self).hook_calls += 1
        raise AssertionError("hostile text strip executed")


class _HostileInt(int):
    hook_calls = 0

    def __ne__(self, other: object) -> bool:
        type(self).hook_calls += 1
        raise AssertionError("hostile integer inequality executed")

    def __le__(self, other: object) -> bool:
        type(self).hook_calls += 1
        raise AssertionError("hostile integer comparison executed")


class _HostileDecimal(Decimal):
    hook_calls = 0

    def is_finite(self) -> bool:
        type(self).hook_calls += 1
        raise AssertionError("hostile Decimal predicate executed")

    def __lt__(self, other: object) -> bool:
        type(self).hook_calls += 1
        raise AssertionError("hostile Decimal comparison executed")


def _store(tmp_path):
    return ProductDayRiskWindowStore(
        tmp_path / "workspace",
        authority_root=tmp_path / "authority",
    )


def _goal(
    *,
    currency: str = "EUR",
    max_turnover: str = "1",
    revision: int = 1,
) -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="owner-goal",
        revision=revision,
        bankroll_id="paper-bankroll",
        currency=currency,
        max_turnover_fraction=Decimal(max_turnover),
    )


def _goal_store(
    day_store: ProductDayRiskWindowStore,
    *,
    currency: str = "EUR",
    max_turnover: str = "1",
) -> EconomicGoalStore:
    expected = _goal(currency=currency, max_turnover=max_turnover)
    store = EconomicGoalStore(day_store.workspace)
    if store.path.exists():
        assert store.load() == expected
    else:
        store.initialize_owner(expected)
    return store


def _leg(suffix: str, odds: str = "2") -> TicketLeg:
    return TicketLeg(
        event_id=f"event-{suffix}",
        market_id=f"market-{suffix}",
        selection_id=f"selection-{suffix}",
        locked_odds=Decimal(odds),
        sport="tennis",
    )


def _inside(window, *, hours: int = 1) -> str:
    start = datetime.fromisoformat(window.window_start.replace("Z", "+00:00"))
    return (start + timedelta(hours=hours)).isoformat()


def _before(window) -> str:
    start = datetime.fromisoformat(window.window_start.replace("Z", "+00:00"))
    return (start - timedelta(seconds=1)).isoformat()


def _book(initial: str = "100") -> PaperBook:
    return PaperBook(Decimal(initial))


def _open(
    book: PaperBook,
    window,
    *,
    stake: str,
    suffix: str,
    placed_at: str | None = None,
    currency: str | None = "EUR",
    bankroll_id: str | None = "paper-bankroll",
    legs: tuple[TicketLeg, ...] | None = None,
):
    return book.open_ticket(
        legs or (_leg(suffix),),
        Decimal(stake),
        placed_at=placed_at or _inside(window),
        bankroll_id=bankroll_id,
        currency=currency,
        reason=f"test-{suffix}",
    )


def _resolve_current(
    *,
    book: PaperBook,
    goal_store: EconomicGoalStore,
    window_store: ProductDayRiskWindowStore,
    window_evidence,
):
    """Persist the test book before exercising product-issued turnover evidence."""

    book.save(window_store.workspace / "paper_book.json")
    return PaperDayTurnoverResolver.resolve(
        book=book,
        goal_store=goal_store,
        window_store=window_store,
        window_evidence=window_evidence,
    )


@pytest.mark.parametrize(
    "method_name",
    ["expanduser", "resolve", "__truediv__"],
)
def test_turnover_resolver_rejects_workspace_path_dispatch_rebinding(
    tmp_path,
    monkeypatch,
    method_name,
):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store)
    book = _book()
    _open(book, window, stake="10", suffix="path-dispatch")
    book_path = store.workspace / "paper_book.json"
    book.save(book_path)
    canonical_book = PaperBook.load(book_path)
    hostile_called = False

    def hostile_path_method(*args, **kwargs):
        nonlocal hostile_called
        del args, kwargs
        hostile_called = True
        raise AssertionError("mutated Path method executed")

    monkeypatch.setattr(turnover_evidence.Path, method_name, hostile_path_method)

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="turnover workspace path authority changed",
    ):
        PaperDayTurnoverResolver.resolve(
            book=canonical_book,
            goal_store=goal_store,
            window_store=store,
            window_evidence=window,
        )

    assert hostile_called is False


def test_current_day_turnover_counts_ticket_stake_once_for_parlay(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(
        book,
        window,
        stake="25",
        suffix="parlay",
        legs=(_leg("a", "2"), _leg("b", "1.5")),
    )

    evidence = _resolve_current(
        book=book,
        goal_store=_goal_store(store, max_turnover="1"),
        window_store=store,
        window_evidence=window,
    )

    assert evidence.confirmed_turnover == Decimal("25")
    assert evidence.constituent_count == 1
    assert evidence.turnover_cap == Decimal("100")
    assert evidence.residual_headroom == Decimal("75")
    assert not evidence.breached
    assert evidence.metric_class == "PAPER_ACCEPTED_TURNOVER"
    assert evidence.scope_class == "UTC_DAY"
    assert not evidence.atomic_admission_authority
    assert not evidence.account_wide_provider_turnover_complete
    assert not evidence.real_money_execution_authorized


def test_evidence_value_contract_rejects_scalar_subclasses_before_hooks(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(book, window, stake="25", suffix="scalar-fence")
    evidence = _resolve_current(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )

    _HostileText.hook_calls = 0
    _HostileInt.hook_calls = 0
    _HostileDecimal.hook_calls = 0

    with pytest.raises(PaperDayTurnoverEvidenceError):
        replace(evidence, schema=_HostileText(evidence.schema))
    with pytest.raises(PaperDayTurnoverEvidenceError):
        replace(
            evidence,
            schema_version=_HostileInt(evidence.schema_version),
        )
    with pytest.raises(PaperDayTurnoverEvidenceError):
        replace(
            evidence,
            goal_revision=_HostileInt(evidence.goal_revision),
        )
    with pytest.raises(PaperDayTurnoverEvidenceError):
        replace(
            evidence,
            confirmed_turnover=_HostileDecimal(
                str(evidence.confirmed_turnover)
            ),
        )

    assert _HostileText.hook_calls == 0
    assert _HostileInt.hook_calls == 0
    assert _HostileDecimal.hook_calls == 0


def test_previous_day_ticket_does_not_enter_current_utc_day(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(
        book,
        window,
        stake="30",
        suffix="old",
        placed_at=_before(window),
    )
    _open(book, window, stake="20", suffix="current")

    evidence = _resolve_current(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )

    assert evidence.confirmed_turnover == Decimal("20")
    assert evidence.constituent_count == 1


def test_settlement_and_payout_do_not_erase_or_inflate_accepted_turnover(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    ticket = _open(
        book,
        window,
        stake="10",
        suffix="settle",
        legs=(_leg("settle-a", "2"), _leg("settle-b", "1.5")),
    )

    before = _resolve_current(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )
    book.settle(
        ticket.ticket_id,
        {leg.quote_key for leg in ticket.legs},
        settled_at=_inside(window, hours=2),
    )
    after = _resolve_current(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )

    assert after.confirmed_turnover == Decimal("10")
    assert after.constituent_sha256 == before.constituent_sha256
    assert after.evidence_sha256 == before.evidence_sha256


def test_breach_truth_is_reported_not_discarded(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(book, window, stake="60", suffix="breach")

    evidence = _resolve_current(
        book=book,
        goal_store=_goal_store(store, max_turnover="0.5"),
        window_store=store,
        window_evidence=window,
    )

    assert evidence.confirmed_turnover == Decimal("60")
    assert evidence.turnover_cap == Decimal("50")
    assert evidence.residual_headroom == Decimal("0")
    assert evidence.breached


def test_current_day_ticket_requires_exact_goal_bankroll_and_currency(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(
        book,
        window,
        stake="10",
        suffix="legacy",
        bankroll_id=None,
        currency=None,
    )

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="bankroll identity",
    ):
        _resolve_current(
            book=book,
            goal_store=_goal_store(store),
            window_store=store,
            window_evidence=window,
        )


def test_cross_currency_ticket_cannot_be_silently_aggregated(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(book, window, stake="10", suffix="eur", currency="EUR")

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="currency identity",
    ):
        _resolve_current(
            book=book,
            goal_store=_goal_store(store, currency="USD"),
            window_store=store,
            window_evidence=window,
        )


def test_require_current_rejects_caller_forged_turnover_scalar(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(book, window, stake="10", suffix="forgery")

    canonical = _resolve_current(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )
    forged = replace(canonical, evidence_sha256="0" * 64)

    with pytest.raises(PaperDayTurnoverEvidenceMismatchError):
        PaperDayTurnoverResolver.require_current(
            forged,
            book=book,
            goal_store=_goal_store(store),
            window_store=store,
            window_evidence=window,
        )


def test_new_ticket_invalidates_old_evidence_on_reresolution(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    book = _book()
    _open(book, window, stake="10", suffix="one")

    first = _resolve_current(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )
    _open(book, window, stake="5", suffix="two", placed_at=_inside(window, hours=2))
    book.save(store.workspace / "paper_book.json")

    with pytest.raises(PaperDayTurnoverEvidenceMismatchError):
        PaperDayTurnoverResolver.require_current(
            first,
            book=book,
            goal_store=_goal_store(store),
            window_store=store,
            window_evidence=window,
        )

    second = _resolve_current(
        book=book,
        goal_store=_goal_store(store),
        window_store=store,
        window_evidence=window,
    )
    assert second.confirmed_turnover == Decimal("15")
    assert second.constituent_count == 2
    assert second.evidence_sha256 != first.evidence_sha256


def test_caller_selected_empty_book_cannot_erase_durable_day_turnover(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store)
    durable_book = _book()
    _open(durable_book, window, stake="40", suffix="durable-turnover")
    durable_book.save(store.workspace / "paper_book.json")

    caller_selected_empty = _book()

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="current durable workspace authority",
    ):
        PaperDayTurnoverResolver.resolve(
            book=caller_selected_empty,
            goal_store=goal_store,
            window_store=store,
            window_evidence=window,
        )

    canonical = PaperBook.load(store.workspace / "paper_book.json")
    evidence = PaperDayTurnoverResolver.resolve(
        book=canonical,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )
    assert evidence.confirmed_turnover == Decimal("40")
    assert evidence.residual_headroom == Decimal("60")


def test_stale_bound_book_cannot_revalidate_after_durable_generation_advances(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store)
    writer = _book()
    _open(writer, window, stake="10", suffix="first-generation")
    writer.save(store.workspace / "paper_book.json")
    stale = PaperBook.load(store.workspace / "paper_book.json")

    _open(writer, window, stake="15", suffix="second-generation")
    writer.save(store.workspace / "paper_book.json")

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="current durable workspace authority",
    ):
        PaperDayTurnoverResolver.resolve(
            book=stale,
            goal_store=goal_store,
            window_store=store,
            window_evidence=window,
        )

    evidence = PaperDayTurnoverResolver.resolve(
        book=writer,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )
    assert evidence.confirmed_turnover == Decimal("25")


def test_synthetic_day_clock_cannot_mint_positive_turnover_evidence(tmp_path):
    epoch_ns = 1_800_000_000 * 1_000_000_000
    store = ProductDayRiskWindowStore(
        tmp_path / "workspace",
        authority_root=tmp_path / "authority",
        _test_clock=lambda: epoch_ns,
    )
    window = store.current()
    book = _book()
    _open(book, window, stake="10", suffix="synthetic")

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="not current product authority",
    ):
        _resolve_current(
            book=book,
            goal_store=_goal_store(store),
            window_store=store,
            window_evidence=window,
        )


def test_subclassed_book_cannot_override_authority_boundary(tmp_path):
    class DerivedPaperBook(PaperBook):
        pass

    store = _store(tmp_path)
    window = store.current()
    book = DerivedPaperBook(Decimal("100"))

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="canonical PaperBook",
    ):
        PaperDayTurnoverResolver.resolve(
            book=book,
            goal_store=_goal_store(store),
            window_store=store,
            window_evidence=window,
        )


def test_durable_goal_tightening_invalidates_old_turnover_evidence(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store, max_turnover="1")
    book = _book()
    _open(book, window, stake="60", suffix="goal-tighten")

    first = _resolve_current(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )
    assert first.turnover_cap == Decimal("100")
    assert not first.breached

    tightened = replace(
        goal_store.load(),
        revision=2,
        max_turnover_fraction=Decimal("0.5"),
    )
    goal_store.persist_automatic_successor(tightened)

    with pytest.raises(PaperDayTurnoverEvidenceMismatchError):
        PaperDayTurnoverResolver.require_current(
            first,
            book=book,
            goal_store=goal_store,
            window_store=store,
            window_evidence=window,
        )

    second = _resolve_current(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )
    assert second.goal_revision == 2
    assert second.goal_contract_sha256 != first.goal_contract_sha256
    assert second.turnover_cap == Decimal("50")
    assert second.confirmed_turnover == Decimal("60")
    assert second.breached


def test_goal_and_day_authority_must_share_workspace(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    foreign_goal_store = EconomicGoalStore(tmp_path / "foreign-workspace")
    foreign_goal_store.initialize_owner(_goal())
    book = _book()

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="share one canonical workspace",
    ):
        _resolve_current(
            book=book,
            goal_store=foreign_goal_store,
            window_store=store,
            window_evidence=window,
        )


def test_ticket_mapping_permutation_preserves_turnover_identity(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store)
    book = _book()
    _open(book, window, stake="10", suffix="perm-a")
    _open(book, window, stake="20", suffix="perm-b", placed_at=_inside(window, hours=2))

    first = _resolve_current(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )
    # Permute the complete canonical OPEN history, not only its materialized
    # mapping. PaperBook requires lifecycle open order and ticket insertion order
    # to agree; reversing only one side correctly represents corruption.
    book.tickets = dict(reversed(tuple(book.tickets.items())))
    book._lifecycle = list(reversed(book._lifecycle))
    second = _resolve_current(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )

    assert second.confirmed_turnover == Decimal("30")
    assert second.constituent_sha256 == first.constituent_sha256
    assert second.evidence_sha256 == first.evidence_sha256


def test_decimal_scale_alias_preserves_turnover_identity(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store)
    book = _book()
    ticket = _open(book, window, stake="10", suffix="scale")

    first = _resolve_current(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )
    ticket.stake = Decimal("10.00")
    second = _resolve_current(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )

    assert second.confirmed_turnover == Decimal("10")
    assert second.constituent_sha256 == first.constituent_sha256
    assert second.evidence_sha256 == first.evidence_sha256


def test_goal_store_instance_load_shadow_cannot_mint_turnover_headroom(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store, max_turnover="0.5")
    book = _book()
    _open(book, window, stake="20", suffix="goal-shadow")

    canonical = _resolve_current(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )
    goal_store.load = lambda: _goal(max_turnover="999")

    observed = _resolve_current(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )

    assert observed == canonical
    assert observed.turnover_cap == Decimal("50")
    assert observed.residual_headroom == Decimal("30")


def test_day_store_instance_require_current_shadow_cannot_accept_forged_window(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store)
    book = _book()
    _open(book, window, stake="10", suffix="window-shadow")
    forged = replace(window, state_sha256="0" * 64)
    store.require_current = lambda candidate: candidate

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="not current product authority",
    ):
        _resolve_current(
            book=book,
            goal_store=goal_store,
            window_store=store,
            window_evidence=forged,
        )


def test_noncanonical_goal_store_path_fails_closed_before_goal_read(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store)
    book = _book()
    goal_store.path = goal_store.path.with_name("alternate_goal.json")

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="economic goal store path is not canonical",
    ):
        _resolve_current(
            book=book,
            goal_store=goal_store,
            window_store=store,
            window_evidence=window,
        )


def test_noncanonical_day_store_path_fails_closed_before_window_read(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store)
    book = _book()
    store.state_path = store.state_path.with_name("alternate_day.json")

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="risk day store path is not canonical",
    ):
        _resolve_current(
            book=book,
            goal_store=goal_store,
            window_store=store,
            window_evidence=window,
        )


def test_turnover_resolver_subclass_cannot_resolve_product_evidence(tmp_path):
    class DerivedResolver(PaperDayTurnoverResolver):
        pass

    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store)
    book = _book()

    with pytest.raises(
        PaperDayTurnoverEvidenceIncompleteError,
        match="canonical exact resolver class",
    ):
        DerivedResolver.resolve(
            book=book,
            goal_store=goal_store,
            window_store=store,
            window_evidence=window,
        )


def test_turnover_require_current_rejects_subclass_before_override_dispatch(tmp_path):
    class DerivedResolver(PaperDayTurnoverResolver):
        @classmethod
        def resolve(cls, **kwargs):
            del cls, kwargs
            raise AssertionError("resolver subclass override executed")

    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store)
    book = _book()
    candidate = _resolve_current(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )

    with pytest.raises(
        PaperDayTurnoverEvidenceMismatchError,
        match="canonical exact resolver class",
    ):
        DerivedResolver.require_current(
            candidate,
            book=book,
            goal_store=goal_store,
            window_store=store,
            window_evidence=window,
        )



def test_resolver_ignores_runtime_economic_goal_store_module_rebind(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store, max_turnover="0.5")
    book = _book()
    _open(book, window, stake="10", suffix="goal-module-rebind")
    expected = _resolve_current(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )

    original = turnover_evidence.EconomicGoalStore
    try:
        turnover_evidence.EconomicGoalStore = object()
        observed = _resolve_current(
            book=book,
            goal_store=goal_store,
            window_store=store,
            window_evidence=window,
        )
    finally:
        turnover_evidence.EconomicGoalStore = original

    assert observed == expected


def test_resolver_ignores_runtime_day_store_module_rebind(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store, max_turnover="0.5")
    book = _book()
    _open(book, window, stake="10", suffix="day-store-module-rebind")
    expected = _resolve_current(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )

    original = turnover_evidence.ProductDayRiskWindowStore
    try:
        turnover_evidence.ProductDayRiskWindowStore = object()
        observed = _resolve_current(
            book=book,
            goal_store=goal_store,
            window_store=store,
            window_evidence=window,
        )
    finally:
        turnover_evidence.ProductDayRiskWindowStore = original

    assert observed == expected


def test_resolver_ignores_runtime_captured_dependency_alias_rebind(tmp_path):
    store = _store(tmp_path)
    window = store.current()
    goal_store = _goal_store(store, max_turnover="0.5")
    book = _book()
    _open(book, window, stake="10", suffix="dependency-alias-rebind")
    expected = _resolve_current(
        book=book,
        goal_store=goal_store,
        window_store=store,
        window_evidence=window,
    )

    original = turnover_evidence._RISK_DAY_REQUIRE_CURRENT
    try:
        turnover_evidence._RISK_DAY_REQUIRE_CURRENT = (
            lambda candidate_store, candidate: replace(
                candidate,
                state_sha256="0" * 64,
                product_clock_authoritative=True,
            )
        )
        observed = _resolve_current(
            book=book,
            goal_store=goal_store,
            window_store=store,
            window_evidence=window,
        )
    finally:
        turnover_evidence._RISK_DAY_REQUIRE_CURRENT = original

    assert observed == expected


def test_resolver_class_authority_entrypoints_reject_runtime_replacement():
    for name in ("resolve", "require_current"):
        original = PaperDayTurnoverResolver.__dict__[name]
        with pytest.raises(TypeError, match="authority method is sealed"):
            setattr(PaperDayTurnoverResolver, name, classmethod(lambda cls, **kwargs: None))
        assert PaperDayTurnoverResolver.__dict__[name] is original


def test_resolver_class_authority_entrypoints_reject_runtime_deletion():
    for name in ("resolve", "require_current"):
        original = PaperDayTurnoverResolver.__dict__[name]
        with pytest.raises(TypeError, match="authority method is sealed"):
            delattr(PaperDayTurnoverResolver, name)
        assert PaperDayTurnoverResolver.__dict__[name] is original
