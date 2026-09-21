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

from .economic_goal import EconomicGoalContract
from .paper import PaperBook
from .risk import PaperRiskPolicy


RISK_REPORT_SCHEMA = "autosport.paper-risk-report.v1"
RISK_OF_RUIN_STATUS_UNKNOWN = "UNKNOWN_REQUIRES_PROVENANCE_BOUND_EVIDENCE"


@dataclass(frozen=True, slots=True)
class PaperRiskReport:
    """Non-authoritative read projection of canonical PAPER risk facts.

    The state commitment and all historical values are derived from the exact
    ``PaperBook`` lifecycle consumed by risk enforcement. ``drawdown_loss_room``
    is the same conservative additional-loss room used by the active economic
    goal check; it includes currently committed stake as potential loss.

    ``risk_of_ruin_upper_bound`` is intentionally absent from historical replay.
    A probability bound may only come from the separately provenance-bound
    ``RiskOfRuinEvidence`` contract in :mod:`autosport.risk`.
    """

    schema: str
    paper_state_sha256: str
    goal_id: str
    goal_revision: int
    initial_bankroll: Decimal
    current_equity: Decimal
    peak_equity: Decimal
    committed_stake: Decimal
    realized_gross_loss: Decimal
    turnover: Decimal
    current_drawdown_amount: Decimal
    drawdown_loss_room: Decimal
    max_drawdown_fraction: Decimal
    risk_of_ruin_limit: Decimal
    risk_of_ruin_upper_bound: None
    risk_of_ruin_status: str


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

    metrics = PaperRiskPolicy._historical_risk_metrics(book)
    rooms = PaperRiskPolicy._goal_history_rooms(book, goal)
    paper_state_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    if metrics is None or rooms is None or paper_state_sha256 is None:
        raise ValueError("canonical PAPER risk state cannot be reported")

    _, _, drawdown_loss_room, _ = rooms
    try:
        with localcontext(PaperRiskPolicy._decimal_context()):
            current_drawdown_amount = metrics.peak_equity - metrics.current_equity
    except DecimalException as exc:
        raise ValueError("canonical PAPER drawdown is not exactly representable") from exc
    if current_drawdown_amount < 0:
        raise ValueError("canonical PAPER drawdown state is inconsistent")

    return PaperRiskReport(
        schema=RISK_REPORT_SCHEMA,
        paper_state_sha256=paper_state_sha256,
        goal_id=goal.goal_id,
        goal_revision=goal.revision,
        initial_bankroll=metrics.initial_bankroll,
        current_equity=metrics.current_equity,
        peak_equity=metrics.peak_equity,
        committed_stake=metrics.committed_stake,
        realized_gross_loss=metrics.realized_gross_loss,
        turnover=metrics.turnover,
        current_drawdown_amount=current_drawdown_amount,
        drawdown_loss_room=drawdown_loss_room,
        max_drawdown_fraction=goal.max_drawdown_fraction,
        risk_of_ruin_limit=goal.max_risk_of_ruin,
        risk_of_ruin_upper_bound=None,
        risk_of_ruin_status=RISK_OF_RUIN_STATUS_UNKNOWN,
    )
