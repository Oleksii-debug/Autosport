"""Read-only operator reporting for canonical PAPER portfolio risk state.

This module deliberately does not estimate risk of ruin and does not create a
second risk authority. It projects the same durable :class:`PaperBook` replay
facts used by :class:`PaperRiskPolicy` enforcement into a presentation/evidence
shape. A historical PAPER lifecycle is not a probability model, so
``risk_of_ruin_upper_bound`` remains ``None`` here by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext

from .domain import TicketStatus
from .economic_goal import EconomicGoalContract
from .economic_goal_provenance import provenance_for
from .economic_goal_store import economic_goal_from_payload, economic_goal_to_payload
from .paper import PaperBook
from .risk import PaperRiskPolicy


RISK_REPORT_SCHEMA = "autosport.paper-risk-report.v4"
RISK_REPORT_SCOPE_PAPER_ONLY = "PAPER_ONLY"
DRAWDOWN_METRIC_REALIZED_SETTLED_EQUITY = "REALIZED_SETTLED_EQUITY_DRAWDOWN"
RISK_OF_RUIN_STATUS_UNKNOWN = "UNKNOWN_REQUIRES_PROVENANCE_BOUND_EVIDENCE"
_INITIAL_EQUITY_POINT_ID = "paper-initial-bankroll"


@dataclass(frozen=True, slots=True)
class _HistoricalMaxDrawdown:
    amount: Decimal
    fraction: Decimal | None
    peak_id: str | None
    trough_id: str | None
    current_equity: Decimal
    peak_equity: Decimal


@dataclass(frozen=True, slots=True)
class PaperRiskReport:
    """Non-authoritative read projection of canonical PAPER risk facts.

    The portfolio-risk state commitment and all historical values are derived from the exact
    ``PaperBook`` lifecycle consumed by risk enforcement. ``drawdown_loss_room``
    is the same conservative additional-loss room used by the active economic
    goal check; it includes currently committed stake as potential loss.

    ``risk_of_ruin_upper_bound`` is intentionally absent from historical replay.
    A probability bound may only come from the separately provenance-bound
    ``RiskOfRuinEvidence`` contract in :mod:`autosport.risk`.
    """

    schema: str
    scope: str
    drawdown_metric_class: str
    includes_live_execution_exposure: bool
    live_execution_headroom_authoritative: bool
    portfolio_risk_state_sha256: str
    goal_id: str
    goal_revision: int
    bankroll_id: str
    currency: str
    goal_contract_sha256: str
    initial_bankroll: Decimal
    current_equity: Decimal
    peak_equity: Decimal
    committed_stake: Decimal
    realized_gross_loss: Decimal
    turnover: Decimal
    current_drawdown_amount: Decimal
    historical_max_drawdown_amount: Decimal
    historical_max_drawdown_fraction: Decimal | None
    historical_max_drawdown_peak_id: str | None
    historical_max_drawdown_trough_id: str | None
    drawdown_loss_room: Decimal
    max_drawdown_fraction: Decimal
    risk_of_ruin_limit: Decimal
    risk_of_ruin_upper_bound: None
    risk_of_ruin_status: str


def _lifecycle_point_id(index: int, action: str, ticket_id: str) -> str:
    """Return a re-resolvable identity for one durable PaperBook lifecycle point."""
    return f"paper-lifecycle:{index}:{action}:{ticket_id}"


def _historical_max_drawdown(book: PaperBook) -> _HistoricalMaxDrawdown | None:
    """Replay the durable PAPER equity path and preserve its worst drawdown episode.

    Equal maximum episodes keep the earliest causal peak/trough pair. A zero-drawdown
    history has no loss episode, so both identities remain None rather than
    fabricating a trough. Peak/trough IDs are derived only from the durable lifecycle
    order + ticket identity (or the durable initial-bankroll origin).
    """

    try:
        PaperBook._validate_loaded_state(book)
        replay_balance = book.initial_bankroll
        replay_committed = Decimal("0")
        running_peak = book.initial_bankroll
        running_peak_id = _INITIAL_EQUITY_POINT_ID
        maximum = Decimal("0")
        maximum_fraction: Decimal | None = (
            Decimal("0") if running_peak > 0 else None
        )
        maximum_peak_id: str | None = None
        maximum_trough_id: str | None = None

        for index, raw_entry in enumerate(book._lifecycle):
            action, ticket_id, winners_raw, voids_raw = (
                PaperBook._validate_lifecycle_entry(raw_entry)
            )
            ticket = book.tickets.get(ticket_id)
            if ticket is None:
                return None

            if action == "open":
                replay_balance = PaperBook._debit_balance(
                    replay_balance,
                    ticket.stake,
                )
                replay_committed = PaperRiskPolicy._exact_positive_sum(
                    (replay_committed, ticket.stake)
                )
            else:
                _, _, replay_balance = PaperBook._settlement_result(
                    ticket,
                    replay_balance,
                    set(winners_raw),
                    set(voids_raw),
                )
                with localcontext(PaperRiskPolicy._decimal_context()):
                    replay_committed = replay_committed - ticket.stake
                if replay_committed < 0:
                    return None

            equity = PaperRiskPolicy._exact_positive_sum(
                (replay_balance, replay_committed)
            )
            point_id = _lifecycle_point_id(index, action, ticket_id)
            if equity > running_peak:
                running_peak = equity
                running_peak_id = point_id
                continue

            with localcontext(PaperRiskPolicy._decimal_context()):
                drawdown = running_peak - equity
            if drawdown < 0:
                return None
            if drawdown > maximum:
                maximum = drawdown
                if running_peak > 0:
                    with localcontext(PaperRiskPolicy._decimal_context()):
                        maximum_fraction = drawdown / running_peak
                else:
                    maximum_fraction = None
                maximum_peak_id = running_peak_id
                maximum_trough_id = point_id

        current_committed = PaperRiskPolicy._exact_positive_sum(
            tuple(
                ticket.stake
                for ticket in book.tickets.values()
                if ticket.status is TicketStatus.OPEN
            )
        )
        current_equity = PaperRiskPolicy._exact_positive_sum(
            (book.balance, current_committed)
        )
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return None

    if replay_balance != book.balance or replay_committed != current_committed:
        return None
    values = (
        maximum,
        current_equity,
        running_peak,
    )
    if any(
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < Decimal("0")
        for value in values
    ):
        return None
    if running_peak <= 0 or current_equity > running_peak:
        return None
    if maximum_fraction is not None and (
        not isinstance(maximum_fraction, Decimal)
        or not maximum_fraction.is_finite()
        or maximum_fraction < Decimal("0")
    ):
        return None
    if maximum == 0 and (
        maximum_peak_id is not None or maximum_trough_id is not None
    ):
        return None
    if maximum > 0 and (
        maximum_peak_id is None or maximum_trough_id is None
    ):
        return None

    return _HistoricalMaxDrawdown(
        amount=maximum,
        fraction=maximum_fraction,
        peak_id=maximum_peak_id,
        trough_id=maximum_trough_id,
        current_equity=current_equity,
        peak_equity=running_peak,
    )


def build_paper_risk_report(
    book: PaperBook,
    goal: EconomicGoalContract,
) -> PaperRiskReport:
    """Build a fail-closed report from the same replay authority as risk policy.

    This function is read-only. It neither mutates the book nor authorizes a
    stake/action. Invalid or internally inconsistent PAPER state raises
    ``ValueError`` rather than publishing partial or caller-shaped risk metrics.
    """

    if type(book) is not PaperBook:
        raise TypeError("book must be canonical PaperBook")
    if type(goal) is not EconomicGoalContract:
        raise TypeError("goal must be canonical EconomicGoalContract")

    # Frozen dataclasses remain technically mutable through low-level same-process
    # operations such as object.__setattr__. Capture one canonical persisted-value
    # snapshot and fence the source contract before, immediately after capture, and
    # again before return so report fields/headroom cannot mix two goal revisions.
    goal_provenance_before = provenance_for(goal)
    goal_snapshot = economic_goal_from_payload(economic_goal_to_payload(goal))
    goal_snapshot_provenance = provenance_for(goal_snapshot)
    goal_provenance_after_capture = provenance_for(goal)
    if (
        goal_provenance_before != goal_snapshot_provenance
        or goal_provenance_after_capture != goal_snapshot_provenance
    ):
        raise ValueError("canonical economic goal changed during reporting")

    before_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    if before_sha256 is None:
        raise ValueError("canonical PAPER risk state cannot be reported")

    metrics = PaperRiskPolicy._historical_risk_metrics(book)
    rooms = PaperRiskPolicy._goal_history_rooms(book, goal_snapshot)
    maximum_drawdown = _historical_max_drawdown(book)
    after_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    if (
        metrics is None
        or rooms is None
        or maximum_drawdown is None
        or after_sha256 is None
    ):
        raise ValueError("canonical PAPER risk state cannot be reported")
    if after_sha256 != before_sha256:
        raise ValueError("canonical PAPER risk state changed during reporting")
    if (
        maximum_drawdown.current_equity != metrics.current_equity
        or maximum_drawdown.peak_equity != metrics.peak_equity
    ):
        raise ValueError("canonical PAPER drawdown replay is inconsistent")

    _, _, drawdown_loss_room, _ = rooms
    try:
        with localcontext(PaperRiskPolicy._decimal_context()):
            current_drawdown_amount = metrics.peak_equity - metrics.current_equity
    except DecimalException as exc:
        raise ValueError("canonical PAPER drawdown is not exactly representable") from exc
    if current_drawdown_amount < 0:
        raise ValueError("canonical PAPER drawdown state is inconsistent")

    report = PaperRiskReport(
        schema=RISK_REPORT_SCHEMA,
        scope=RISK_REPORT_SCOPE_PAPER_ONLY,
        drawdown_metric_class=DRAWDOWN_METRIC_REALIZED_SETTLED_EQUITY,
        includes_live_execution_exposure=False,
        live_execution_headroom_authoritative=False,
        portfolio_risk_state_sha256=after_sha256,
        goal_id=goal_snapshot.goal_id,
        goal_revision=goal_snapshot.revision,
        bankroll_id=goal_snapshot.bankroll_id,
        currency=goal_snapshot.currency,
        goal_contract_sha256=goal_snapshot_provenance.contract_sha256,
        initial_bankroll=metrics.initial_bankroll,
        current_equity=metrics.current_equity,
        peak_equity=metrics.peak_equity,
        committed_stake=metrics.committed_stake,
        realized_gross_loss=metrics.realized_gross_loss,
        turnover=metrics.turnover,
        current_drawdown_amount=current_drawdown_amount,
        historical_max_drawdown_amount=maximum_drawdown.amount,
        historical_max_drawdown_fraction=maximum_drawdown.fraction,
        historical_max_drawdown_peak_id=maximum_drawdown.peak_id,
        historical_max_drawdown_trough_id=maximum_drawdown.trough_id,
        drawdown_loss_room=drawdown_loss_room,
        max_drawdown_fraction=goal_snapshot.max_drawdown_fraction,
        risk_of_ruin_limit=goal_snapshot.max_risk_of_ruin,
        risk_of_ruin_upper_bound=None,
        risk_of_ruin_status=RISK_OF_RUIN_STATUS_UNKNOWN,
    )

    if provenance_for(goal) != goal_snapshot_provenance:
        raise ValueError("canonical economic goal changed during reporting")
    return report
