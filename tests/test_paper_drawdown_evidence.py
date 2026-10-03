from dataclasses import replace
from decimal import Context, Decimal, localcontext
from pathlib import Path
from types import FunctionType

import pytest

import autosport.economic_goal_store as economic_goal_store_module
import autosport.paper as paper_module
from autosport.domain import TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.paper import PaperBook
from autosport.paper_drawdown_evidence import (
    DRAW_DOWN_METRIC_CLASS,
    DRAW_DOWN_SCOPE,
    PaperDrawdownEvidenceError,
    PaperDrawdownEvidenceMismatchError,
    require_current_paper_drawdown_evidence,
    resolve_paper_drawdown_evidence,
)
from autosport.risk_reporting import build_paper_risk_report


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-product-drawdown-evidence",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("1"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("1"),
        max_risk_of_ruin=Decimal("1"),
        max_concurrent_positions=10,
    )


def _leg(suffix: str, odds: str = "2") -> TicketLeg:
    return TicketLeg(
        event_id=f"event-{suffix}",
        market_id=f"market-{suffix}",
        selection_id=f"selection-{suffix}",
        locked_odds=Decimal(odds),
    )


def _initialize(tmp_path) -> PaperBook:
    EconomicGoalStore(tmp_path).initialize_owner(_goal())
    book = PaperBook("100")
    book.save(tmp_path / "paper_book.json")
    return book


def _open(
    book: PaperBook,
    suffix: str,
    stake: str,
    placed_at: str,
    *,
    bankroll_id: str = "paper-bankroll",
    currency: str = "USD",
    odds: str = "2",
):
    return book.open_ticket(
        [_leg(suffix, odds)],
        Decimal(stake),
        placed_at=placed_at,
        bankroll_id=bankroll_id,
        currency=currency,
    )


def test_open_stake_is_not_realized_settled_drawdown(tmp_path):
    book = _initialize(tmp_path)
    _open(book, "open-only", "80", "2026-09-20T10:00:00+00:00")
    book.save(tmp_path / "paper_book.json")

    evidence = resolve_paper_drawdown_evidence(tmp_path)

    assert book.balance == Decimal("20")
    assert evidence.scope == DRAW_DOWN_SCOPE
    assert evidence.metric_class == DRAW_DOWN_METRIC_CLASS
    assert evidence.initial_equity == Decimal("100")
    assert evidence.current_equity == Decimal("100")
    assert evidence.minimum_equity == Decimal("100")
    assert evidence.historical_max_drawdown_amount == Decimal("0")
    assert evidence.historical_max_drawdown_fraction == Decimal("0")
    assert evidence.current_drawdown_amount == Decimal("0")
    assert evidence.open_position_count == 1
    assert len(evidence.points) == 2
    assert evidence.points[-1].action == "open"
    assert evidence.points[-1].realized_delta == Decimal("0")
    assert evidence.points[-1].equity == Decimal("100")


def test_loss_creates_realized_drawdown_only_at_settlement(tmp_path):
    book = _initialize(tmp_path)
    ticket = _open(book, "loss", "80", "2026-09-20T10:00:00+00:00")
    book.settle(
        ticket.ticket_id,
        set(),
        settled_at="2026-09-20T11:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")

    evidence = resolve_paper_drawdown_evidence(tmp_path)

    assert evidence.current_equity == Decimal("20")
    assert evidence.minimum_equity == Decimal("20")
    assert evidence.peak_equity == Decimal("100")
    assert evidence.historical_max_drawdown_amount == Decimal("80")
    assert evidence.historical_max_drawdown_fraction == Decimal("0.8")
    assert evidence.current_drawdown_amount == Decimal("80")
    assert evidence.historical_max_drawdown_peak_id == "paper-initial-equity"
    assert evidence.historical_max_drawdown_trough_id == (
        f"paper-lifecycle:1:settle:{ticket.ticket_id}"
    )
    assert evidence.points[1].action == "open"
    assert evidence.points[1].equity == Decimal("100")
    assert evidence.points[2].action == "settle"
    assert evidence.points[2].realized_delta == Decimal("-80")
    assert evidence.points[2].equity == Decimal("20")
    assert evidence.settlement_availability_complete is True


def test_void_does_not_create_realized_drawdown(tmp_path):
    book = _initialize(tmp_path)
    ticket = _open(book, "void", "80", "2026-09-20T10:00:00+00:00")
    book.settle(
        ticket.ticket_id,
        set(),
        {ticket.legs[0].quote_key},
        settled_at="2026-09-20T11:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")

    evidence = resolve_paper_drawdown_evidence(tmp_path)

    assert evidence.current_equity == Decimal("100")
    assert evidence.historical_max_drawdown_amount == Decimal("0")
    assert evidence.points[-1].realized_delta == Decimal("0")
    assert evidence.open_position_count == 0


def test_win_raises_realized_peak_without_transient_stake_drawdown(tmp_path):
    book = _initialize(tmp_path)
    ticket = _open(book, "win", "80", "2026-09-20T10:00:00+00:00")
    book.settle(
        ticket.ticket_id,
        {ticket.legs[0].quote_key},
        settled_at="2026-09-20T11:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")

    evidence = resolve_paper_drawdown_evidence(tmp_path)

    assert evidence.current_equity == Decimal("180")
    assert evidence.peak_equity == Decimal("180")
    assert evidence.minimum_equity == Decimal("100")
    assert evidence.historical_max_drawdown_amount == Decimal("0")
    assert evidence.current_drawdown_amount == Decimal("0")
    assert evidence.points[-1].realized_delta == Decimal("80")
    assert evidence.recovered_to_peak is True


def test_recovery_does_not_erase_historical_max_drawdown(tmp_path):
    book = _initialize(tmp_path)
    losing = _open(book, "first-loss", "50", "2026-09-20T10:00:00+00:00")
    book.settle(
        losing.ticket_id,
        set(),
        settled_at="2026-09-20T11:00:00+00:00",
    )
    winning = _open(book, "recovery", "50", "2026-09-20T12:00:00+00:00")
    book.settle(
        winning.ticket_id,
        {winning.legs[0].quote_key},
        settled_at="2026-09-20T13:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")

    evidence = resolve_paper_drawdown_evidence(tmp_path)

    assert evidence.current_equity == Decimal("100")
    assert evidence.current_drawdown_amount == Decimal("0")
    assert evidence.historical_max_drawdown_amount == Decimal("50")
    assert evidence.historical_max_drawdown_fraction == Decimal("0.5")
    assert evidence.recovered_to_peak is True


def test_fractional_max_is_independent_of_largest_absolute_drawdown(tmp_path):
    book = _initialize(tmp_path)
    first_loss = _open(book, "fraction-loss", "50", "2026-09-20T10:00:00+00:00")
    book.settle(
        first_loss.ticket_id,
        set(),
        settled_at="2026-09-20T11:00:00+00:00",
    )
    new_peak = _open(
        book,
        "high-peak",
        "50",
        "2026-09-20T12:00:00+00:00",
        odds="24",
    )
    book.settle(
        new_peak.ticket_id,
        {new_peak.legs[0].quote_key},
        settled_at="2026-09-20T13:00:00+00:00",
    )
    second_loss = _open(book, "amount-loss", "60", "2026-09-20T14:00:00+00:00")
    book.settle(
        second_loss.ticket_id,
        set(),
        settled_at="2026-09-20T15:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")

    evidence = resolve_paper_drawdown_evidence(tmp_path)
    report = build_paper_risk_report(book, _goal())

    assert evidence.peak_equity == Decimal("1200")
    assert evidence.current_equity == Decimal("1140")
    assert evidence.historical_max_drawdown_amount == Decimal("60")
    assert evidence.historical_max_drawdown_fraction == Decimal("0.5")
    assert evidence.historical_max_drawdown_peak_id == (
        f"paper-lifecycle:3:settle:{new_peak.ticket_id}"
    )
    assert evidence.historical_max_drawdown_trough_id == (
        f"paper-lifecycle:5:settle:{second_loss.ticket_id}"
    )
    assert report.historical_max_drawdown_amount == Decimal("60")
    assert report.historical_max_drawdown_fraction == Decimal("0.5")


def test_product_evidence_matches_merged_risk_report_drawdown_projection(tmp_path):
    book = _initialize(tmp_path)
    winning = _open(book, "new-peak", "50", "2026-09-20T10:00:00+00:00")
    book.settle(
        winning.ticket_id,
        {winning.legs[0].quote_key},
        settled_at="2026-09-20T11:00:00+00:00",
    )
    losing = _open(book, "post-peak-loss", "50", "2026-09-20T12:00:00+00:00")
    book.settle(
        losing.ticket_id,
        set(),
        settled_at="2026-09-20T13:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")

    evidence = resolve_paper_drawdown_evidence(tmp_path)
    report = build_paper_risk_report(book, _goal())

    assert evidence.current_equity == report.current_equity
    assert evidence.peak_equity == report.peak_equity
    assert (
        evidence.historical_max_drawdown_amount
        == report.historical_max_drawdown_amount
    )
    assert (
        evidence.historical_max_drawdown_fraction
        == report.historical_max_drawdown_fraction
    )
    assert (
        evidence.historical_max_drawdown_peak_id
        == report.historical_max_drawdown_peak_id
    )
    assert (
        evidence.historical_max_drawdown_trough_id
        == report.historical_max_drawdown_trough_id
    )
    assert evidence.current_drawdown_amount == report.current_drawdown_amount


def test_restart_reresolves_identical_path_and_evidence_identity(tmp_path):
    book = _initialize(tmp_path)
    ticket = _open(book, "restart", "25", "2026-09-20T10:00:00+00:00")
    book.settle(
        ticket.ticket_id,
        set(),
        settled_at="2026-09-20T11:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")

    before = resolve_paper_drawdown_evidence(tmp_path)
    after = resolve_paper_drawdown_evidence(tmp_path)

    assert after == before
    assert after.path_sha256 == before.path_sha256
    assert after.source_state_sha256 == before.source_state_sha256
    assert after.evidence_sha256 == before.evidence_sha256
    assert require_current_paper_drawdown_evidence(tmp_path, before) == before


def test_old_evidence_fails_current_reresolution_after_new_settlement(tmp_path):
    book = _initialize(tmp_path)
    before = resolve_paper_drawdown_evidence(tmp_path)

    ticket = _open(book, "late-loss", "10", "2026-09-20T10:00:00+00:00")
    book.settle(
        ticket.ticket_id,
        set(),
        settled_at="2026-09-20T11:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")

    with pytest.raises(
        PaperDrawdownEvidenceMismatchError,
        match="not current canonical product authority",
    ):
        require_current_paper_drawdown_evidence(tmp_path, before)


def test_caller_modified_scalar_cannot_pass_current_reresolution(tmp_path):
    _initialize(tmp_path)
    evidence = resolve_paper_drawdown_evidence(tmp_path)
    forged = replace(
        evidence,
        historical_max_drawdown_amount=Decimal("1"),
    )

    with pytest.raises(PaperDrawdownEvidenceMismatchError):
        require_current_paper_drawdown_evidence(tmp_path, forged)


def test_missing_settlement_time_is_explicitly_not_as_known_authority(tmp_path):
    book = _initialize(tmp_path)
    ticket = _open(book, "untimed-loss", "10", "2026-09-20T10:00:00+00:00")
    book.settle(ticket.ticket_id, set(), settled_at=None)
    book.save(tmp_path / "paper_book.json")

    evidence = resolve_paper_drawdown_evidence(tmp_path)

    assert evidence.current_equity == Decimal("90")
    assert evidence.settlement_availability_complete is False
    assert evidence.as_known_at_supported is False
    assert evidence.points[-1].event_time is None


def test_ticket_denomination_must_match_durable_owner_goal(tmp_path):
    book = _initialize(tmp_path)
    _open(
        book,
        "wrong-denomination",
        "10",
        "2026-09-20T10:00:00+00:00",
        bankroll_id="other-bankroll",
        currency="EUR",
    )
    book.save(tmp_path / "paper_book.json")

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="denomination is not bound to the durable goal",
    ):
        resolve_paper_drawdown_evidence(tmp_path)


def test_open_event_changes_path_identity_without_minting_drawdown(tmp_path):
    book = _initialize(tmp_path)
    before = resolve_paper_drawdown_evidence(tmp_path)

    _open(book, "identity-only", "10", "2026-09-20T10:00:00+00:00")
    book.save(tmp_path / "paper_book.json")
    after = resolve_paper_drawdown_evidence(tmp_path)

    assert after.path_sha256 != before.path_sha256
    assert after.source_state_sha256 != before.source_state_sha256
    assert after.historical_max_drawdown_amount == before.historical_max_drawdown_amount
    assert after.current_equity == before.current_equity


def test_evidence_shape_rejects_impossible_internal_metrics(tmp_path):
    _initialize(tmp_path)
    evidence = resolve_paper_drawdown_evidence(tmp_path)

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="historical fraction cannot exceed one",
    ):
        replace(evidence, historical_max_drawdown_fraction=Decimal("1.01"))

    with pytest.raises(PaperDrawdownEvidenceError, match="minimum equity is invalid"):
        replace(evidence, minimum_equity=Decimal("101"))

    with pytest.raises(PaperDrawdownEvidenceError, match="recovery flag is inconsistent"):
        replace(evidence, recovered_to_peak=False)

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="settlement availability flag is invalid",
    ):
        replace(evidence, settlement_availability_complete=1)

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="initial/open equity point cannot carry realized P&L",
    ):
        replace(evidence.points[0], realized_delta=Decimal("1"))


def test_evidence_shape_rejects_lifecycle_and_path_claim_drift(tmp_path):
    book = _initialize(tmp_path)
    opened = _open(book, "shape-open", "10", "2026-09-20T10:00:00+00:00")
    book.save(tmp_path / "paper_book.json")
    open_evidence = resolve_paper_drawdown_evidence(tmp_path)

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="open count does not match the path",
    ):
        replace(open_evidence, open_position_count=0)

    duplicate_open = replace(
        open_evidence.points[-1],
        sequence=len(open_evidence.points),
        point_id="paper-forged-duplicate-open",
    )
    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="ticket may open only once",
    ):
        replace(
            open_evidence,
            points=open_evidence.points + (duplicate_open,),
        )

    book.settle(
        opened.ticket_id,
        set(),
        settled_at="2026-09-20T11:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")
    loss_evidence = resolve_paper_drawdown_evidence(tmp_path)

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="settlement availability does not match the path",
    ):
        replace(loss_evidence, settlement_availability_complete=False)

    forged_settle = replace(loss_evidence.points[-1], sequence=1)
    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="settlement must follow one canonical open",
    ):
        replace(
            loss_evidence,
            points=(loss_evidence.points[0], forged_settle),
            open_position_count=0,
        )

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="loss episode identity is not in the path",
    ):
        replace(
            loss_evidence,
            historical_max_drawdown_peak_id="paper-forged-peak",
        )

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="current drawdown exceeds historical maximum",
    ):
        replace(
            loss_evidence,
            current_drawdown_amount=loss_evidence.historical_max_drawdown_amount
            + Decimal("1"),
        )

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="current drawdown does not match the path",
    ):
        replace(
            loss_evidence,
            current_drawdown_amount=loss_evidence.current_drawdown_amount
            - Decimal("1"),
        )

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="historical maximum drawdown does not match the path",
    ):
        replace(
            loss_evidence,
            historical_max_drawdown_amount=loss_evidence.historical_max_drawdown_amount
            + Decimal("1"),
        )

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="historical fraction does not match the path",
    ):
        replace(
            loss_evidence,
            historical_max_drawdown_fraction=(
                loss_evidence.historical_max_drawdown_fraction - Decimal("0.01")
            ),
        )


def test_evidence_shape_rejects_equity_transition_drift(tmp_path):
    book = _initialize(tmp_path)
    opened = _open(book, "transition-open", "10", "2026-09-20T10:00:00+00:00")
    book.save(tmp_path / "paper_book.json")
    open_evidence = resolve_paper_drawdown_evidence(tmp_path)

    forged_open = replace(
        open_evidence.points[-1],
        equity=open_evidence.points[-1].equity - Decimal("1"),
    )
    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="equity transition does not match realized delta",
    ):
        replace(
            open_evidence,
            points=open_evidence.points[:-1] + (forged_open,),
        )

    book.settle(
        opened.ticket_id,
        set(),
        settled_at="2026-09-20T11:00:00+00:00",
    )
    book.save(tmp_path / "paper_book.json")
    loss_evidence = resolve_paper_drawdown_evidence(tmp_path)
    forged_settle = replace(
        loss_evidence.points[-1],
        realized_delta=loss_evidence.points[-1].realized_delta + Decimal("1"),
    )
    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="equity transition does not match realized delta",
    ):
        replace(
            loss_evidence,
            points=loss_evidence.points[:-1] + (forged_settle,),
        )

    # Ambient precision must not turn 100 + (-9) into a rounded 90 and let a
    # forged settlement delta explain the canonical 90 equity point.
    rounded_forgery = replace(
        loss_evidence.points[-1],
        realized_delta=Decimal("-9"),
    )
    with localcontext(Context(prec=1)):
        with pytest.raises(
            PaperDrawdownEvidenceError,
            match="equity transition does not match realized delta",
        ):
            replace(
                loss_evidence,
                points=loss_evidence.points[:-1] + (rounded_forgery,),
            )


def test_evidence_shape_rejects_noncanonical_equity_point_objects(tmp_path):
    _initialize(tmp_path)
    evidence = resolve_paper_drawdown_evidence(tmp_path)
    canonical = evidence.points[0]

    class _PointProxy:
        sequence = canonical.sequence
        point_id = canonical.point_id
        action = canonical.action
        ticket_id = canonical.ticket_id
        equity = canonical.equity
        realized_delta = canonical.realized_delta
        event_time = canonical.event_time

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="path requires canonical equity points",
    ):
        replace(evidence, points=(_PointProxy(),))


def test_resolver_rejects_in_place_captured_helper_code_rebinding(tmp_path):
    _initialize(tmp_path)

    frozen = resolve_paper_drawdown_evidence
    resolver = next(
        cell.cell_contents
        for cell in frozen.__closure__ or ()
        if type(cell.cell_contents) is FunctionType
        and cell.cell_contents.__name__ == "resolver"
    )
    canonical_sha256 = next(
        cell.cell_contents
        for cell in resolver.__closure__ or ()
        if type(cell.cell_contents) is FunctionType
        and cell.cell_contents.__name__ == "canonical_sha256"
    )
    original_code = canonical_sha256.__code__

    sha256 = object()
    json_dumps = object()

    def forged_sha256(_payload):
        # Match the captured helper's two-cell closure so Python permits an
        # in-place __code__ rebind while the function object identity stays fixed.
        _ = (sha256, json_dumps)
        return "0" * 64

    assert len(forged_sha256.__code__.co_freevars) == len(
        canonical_sha256.__code__.co_freevars
    )

    try:
        canonical_sha256.__code__ = forged_sha256.__code__
        with pytest.raises(
            PaperDrawdownEvidenceError,
            match="dependency executable authority changed",
        ):
            resolve_paper_drawdown_evidence(tmp_path)
    finally:
        canonical_sha256.__code__ = original_code

    # Restoration must recover the exact canonical product resolver.
    evidence = resolve_paper_drawdown_evidence(tmp_path)
    assert evidence.source_state_sha256 != "0" * 64
    assert evidence.path_sha256 != "0" * 64
    assert evidence.evidence_sha256 != "0" * 64


def test_resolver_rejects_paperbook_load_bytes_rebinding_before_execution(
    tmp_path,
    monkeypatch,
) -> None:
    _initialize(tmp_path)
    hostile_called = False

    def hostile_load_bytes(cls, payload):
        nonlocal hostile_called
        del cls, payload
        hostile_called = True
        return PaperBook("1000000")

    monkeypatch.setattr(
        PaperBook,
        "load_bytes",
        classmethod(hostile_load_bytes),
    )

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="durable source resolver authority changed",
    ):
        resolve_paper_drawdown_evidence(tmp_path)

    assert hostile_called is False


def test_resolver_rejects_economic_goal_parser_rebinding_before_execution(
    tmp_path,
    monkeypatch,
) -> None:
    _initialize(tmp_path)
    hostile_called = False

    def hostile_goal_parser(_text):
        nonlocal hostile_called
        hostile_called = True
        return _goal()

    monkeypatch.setattr(
        economic_goal_store_module,
        "economic_goal_from_json",
        hostile_goal_parser,
    )

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="durable source resolver authority changed",
    ):
        resolve_paper_drawdown_evidence(tmp_path)

    assert hostile_called is False


def test_resolver_rejects_paperbook_from_raw_rebinding_before_execution(
    tmp_path,
    monkeypatch,
) -> None:
    _initialize(tmp_path)
    hostile_called = False

    def hostile_from_raw(cls, raw):
        nonlocal hostile_called
        del cls, raw
        hostile_called = True
        return PaperBook("1000000")

    monkeypatch.setattr(
        PaperBook,
        "_from_raw_snapshot",
        classmethod(hostile_from_raw),
    )

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="durable source resolver authority changed",
    ):
        resolve_paper_drawdown_evidence(tmp_path)

    assert hostile_called is False


@pytest.mark.parametrize(
    "dependency_name",
    ("strict_json_loads", "economic_goal_from_payload"),
)
def test_resolver_rejects_goal_parser_dependency_rebinding_before_execution(
    tmp_path,
    monkeypatch,
    dependency_name,
) -> None:
    _initialize(tmp_path)
    hostile_called = False

    def hostile(*_args, **_kwargs):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile goal parser dependency executed")

    monkeypatch.setattr(
        economic_goal_store_module,
        dependency_name,
        hostile,
    )

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="durable source resolver authority changed",
    ):
        resolve_paper_drawdown_evidence(tmp_path)

    assert hostile_called is False


def test_resolver_rejects_paperbook_lifecycle_validator_rebinding_before_execution(
    tmp_path,
    monkeypatch,
) -> None:
    _initialize(tmp_path)
    hostile_called = False

    def hostile_validate(cls, book):
        nonlocal hostile_called
        del cls, book
        hostile_called = True

    monkeypatch.setattr(
        PaperBook,
        "_validate_loaded_state",
        classmethod(hostile_validate),
    )

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="PaperBook parse/validation authority changed: _validate_loaded_state",
    ):
        resolve_paper_drawdown_evidence(tmp_path)

    assert hostile_called is False


def test_resolver_rejects_paper_module_path_rebinding_before_execution(
    tmp_path,
    monkeypatch,
) -> None:
    _initialize(tmp_path)
    monkeypatch.setattr(paper_module, "Path", object())

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="PaperBook durable load global changed: Path",
    ):
        resolve_paper_drawdown_evidence(tmp_path)


def test_resolver_rejects_economic_goal_store_path_rebinding_before_execution(
    tmp_path,
    monkeypatch,
) -> None:
    _initialize(tmp_path)
    monkeypatch.setattr(economic_goal_store_module, "Path", object())

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="economic-goal store authority changed",
    ):
        resolve_paper_drawdown_evidence(tmp_path)


def test_resolver_rejects_workspace_path_dispatch_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    _initialize(tmp_path)
    hostile_called = False

    def hostile_resolve(self, *args, **kwargs):
        nonlocal hostile_called
        del self, args, kwargs
        hostile_called = True
        return tmp_path

    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", hostile_resolve)

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="workspace path authority changed",
    ):
        resolve_paper_drawdown_evidence(tmp_path)

    assert hostile_called is False

    monkeypatch.setattr(Path, "resolve", original_resolve)
    evidence = resolve_paper_drawdown_evidence(tmp_path)
    assert evidence.scope == DRAW_DOWN_SCOPE


def test_resolver_rejects_economic_goal_store_constructor_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    _initialize(tmp_path)
    hostile_called = False
    original_init = EconomicGoalStore.__init__

    def hostile_init(self, workspace):
        nonlocal hostile_called
        hostile_called = True
        original_init(self, workspace)

    monkeypatch.setattr(EconomicGoalStore, "__init__", hostile_init)

    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="economic-goal store authority changed",
    ):
        resolve_paper_drawdown_evidence(tmp_path)

    assert hostile_called is False

    monkeypatch.setattr(EconomicGoalStore, "__init__", original_init)
    evidence = resolve_paper_drawdown_evidence(tmp_path)
    assert evidence.metric_class == DRAW_DOWN_METRIC_CLASS


def test_missing_durable_sources_fail_closed(tmp_path):
    with pytest.raises(
        PaperDrawdownEvidenceError,
        match="canonical PAPER drawdown source cannot be resolved",
    ):
        resolve_paper_drawdown_evidence(tmp_path)
