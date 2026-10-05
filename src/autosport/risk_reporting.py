"""Read-only operator reporting for canonical PAPER portfolio risk state.

This module deliberately does not estimate risk of ruin and does not create a
second risk authority. It projects the same durable :class:`PaperBook` replay
facts used by :class:`PaperRiskPolicy` enforcement into a presentation/evidence
shape. A historical PAPER lifecycle is not a probability model, so
``risk_of_ruin_upper_bound`` remains ``None`` here by construction.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal, DecimalException, Inexact, localcontext
from pathlib import Path

from .domain import TicketStatus
from .economic_goal import EconomicGoalContract
from .economic_goal_provenance import provenance_for
from .economic_goal_store import (
    EconomicGoalStore,
    economic_goal_from_payload,
    economic_goal_to_payload,
)
from .paper import (
    PaperBook,
    _require_paperbook_causal_history_authority,
    _require_ticket_opening_authority,
)
from .risk import PaperRiskPolicy


RISK_REPORT_SCHEMA = "autosport.paper-risk-report.v5"
EQUITY_PATH_SCHEMA = "autosport.paper-equity-path.v1"
PAPER_EQUITY_SOURCE_STATE_SCHEMA = "autosport.paper-equity-source-state.v1"
DRAWDOWN_EVIDENCE_SCHEMA = "autosport.paper-realized-settled-drawdown.v1"
HISTORY_VIEW_RESTATED_CURRENT = "RESTATED_CURRENT_HISTORY"
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
class PaperEquityPathPoint:
    sequence: int
    point_id: str
    action: str
    ticket_id: str | None
    available_at: str | None
    winning_quote_keys: tuple[str, ...]
    void_quote_keys: tuple[str, ...]
    equity: Decimal


@dataclass(frozen=True, slots=True)
class ProductIssuedPaperEquityPath:
    schema: str
    scope: str
    goal_id: str
    goal_revision: int
    bankroll_id: str
    currency: str
    goal_contract_sha256: str
    portfolio_risk_state_sha256: str
    paperbook_source_state_sha256: str
    history_view: str
    historical_as_known_supported: bool
    initial_equity: Decimal
    points: tuple[PaperEquityPathPoint, ...]
    point_count: int
    path_sha256: str
    current_equity: Decimal
    minimum_equity: Decimal
    minimum_equity_point_id: str
    availability_complete: bool
    settled_history_complete: bool
    money_scope_complete: bool
    opening_capital_authority_complete: bool
    applicable_costs_complete: bool
    net_equity_authoritative: bool
    correction_lineage_complete: bool
    restated_history_authoritative: bool
    frozen_scope_complete: bool
    historical_reresolution_complete: bool


@dataclass(frozen=True, slots=True)
class ProductIssuedPaperDrawdownEvidence:
    schema: str
    metric_class: str
    equity_path_sha256: str
    paperbook_source_state_sha256: str
    equity_path_point_count: int
    goal_id: str
    goal_revision: int
    bankroll_id: str
    currency: str
    history_view: str
    historical_as_known_supported: bool
    initial_equity: Decimal
    current_equity: Decimal
    peak_equity: Decimal
    minimum_equity: Decimal
    minimum_equity_point_id: str
    max_drawdown_amount: Decimal
    max_drawdown_fraction: Decimal | None
    max_drawdown_peak_id: str | None
    max_drawdown_trough_id: str | None
    availability_complete: bool
    settled_history_complete: bool
    money_scope_complete: bool
    opening_capital_authority_complete: bool
    applicable_costs_complete: bool
    net_equity_authoritative: bool
    correction_lineage_complete: bool
    restated_history_authoritative: bool
    frozen_scope_complete: bool
    historical_reresolution_complete: bool
    evidence_sha256: str


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
    paperbook_source_state_sha256: str
    equity_path_sha256: str
    drawdown_evidence_sha256: str
    equity_path_point_count: int
    equity_path_availability_complete: bool
    settled_history_complete: bool
    money_scope_complete: bool
    opening_capital_authority_complete: bool
    applicable_costs_complete: bool
    net_equity_authoritative: bool
    correction_lineage_complete: bool
    restated_history_authoritative: bool
    frozen_scope_complete: bool
    historical_reresolution_complete: bool
    history_view: str
    historical_as_known_supported: bool
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


def _decimal_text(value: Decimal, label: str) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{label} must be an exact finite Decimal")
    return str(value)


def _paper_equity_source_state_sha256(book: PaperBook) -> str:
    """Hash complete canonical PAPER source state used by equity evidence."""

    if type(book) is not PaperBook:
        raise TypeError("book must be canonical PaperBook")
    _require_product_issued_paper_state(book)
    PaperBook._validate_loaded_state(book)

    tickets: list[dict[str, object]] = []
    for ticket_id in sorted(book.tickets):
        ticket = book.tickets[ticket_id]
        tickets.append(
            {
                "ticket_id": ticket.ticket_id,
                "stake": _decimal_text(ticket.stake, "ticket stake"),
                "placed_at": ticket.placed_at,
                "settled_at": ticket.settled_at,
                "status": ticket.status.value,
                "payout": _decimal_text(ticket.payout, "ticket payout"),
                "strategy_reason": ticket.strategy_reason,
                "provider_source_ids": list(ticket.provider_source_ids),
                "provider_accounts": [
                    {"source_id": source_id, "account_id": account_id}
                    for source_id, account_id in ticket.provider_accounts
                ],
                "bankroll_id": ticket.bankroll_id,
                "currency": ticket.currency,
                "legs": [
                    {
                        "event_id": leg.event_id,
                        "market_id": leg.market_id,
                        "selection_id": leg.selection_id,
                        "locked_odds": _decimal_text(
                            leg.locked_odds,
                            "ticket leg locked_odds",
                        ),
                        "sport": leg.sport,
                        "exchange_side": leg.exchange_side,
                    }
                    for leg in ticket.legs
                ],
            }
        )

    lifecycle: list[dict[str, object]] = []
    for raw_entry in book._lifecycle:
        action, ticket_id, winners, voids = PaperBook._validate_lifecycle_entry(
            raw_entry
        )
        lifecycle.append(
            {
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

    payload = {
        "schema": PAPER_EQUITY_SOURCE_STATE_SCHEMA,
        "initial_bankroll": _decimal_text(book.initial_bankroll, "initial_bankroll"),
        "balance": _decimal_text(book.balance, "balance"),
        "tickets": tickets,
        "lifecycle": lifecycle,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _equity_path_payload(
    *,
    goal_snapshot: EconomicGoalContract,
    goal_contract_sha256: str,
    portfolio_risk_state_sha256: str,
    paperbook_source_state_sha256: str,
    initial_equity: Decimal,
    points: tuple[PaperEquityPathPoint, ...],
    availability_complete: bool,
    settled_history_complete: bool,
    money_scope_complete: bool,
    opening_capital_authority_complete: bool,
    applicable_costs_complete: bool,
    net_equity_authoritative: bool,
    correction_lineage_complete: bool,
    restated_history_authoritative: bool,
    frozen_scope_complete: bool,
    historical_reresolution_complete: bool,
) -> dict[str, object]:
    return {
        "schema": EQUITY_PATH_SCHEMA,
        "scope": RISK_REPORT_SCOPE_PAPER_ONLY,
        "goal_id": goal_snapshot.goal_id,
        "goal_revision": goal_snapshot.revision,
        "bankroll_id": goal_snapshot.bankroll_id,
        "currency": goal_snapshot.currency,
        "goal_contract_sha256": goal_contract_sha256,
        "portfolio_risk_state_sha256": portfolio_risk_state_sha256,
        "paperbook_source_state_sha256": paperbook_source_state_sha256,
        "history_view": HISTORY_VIEW_RESTATED_CURRENT,
        "historical_as_known_supported": False,
        "initial_equity": _decimal_text(initial_equity, "initial_equity"),
        "availability_complete": availability_complete,
        "settled_history_complete": settled_history_complete,
        "money_scope_complete": money_scope_complete,
        "opening_capital_authority_complete": opening_capital_authority_complete,
        "applicable_costs_complete": applicable_costs_complete,
        "net_equity_authoritative": net_equity_authoritative,
        "correction_lineage_complete": correction_lineage_complete,
        "restated_history_authoritative": restated_history_authoritative,
        "frozen_scope_complete": frozen_scope_complete,
        "historical_reresolution_complete": historical_reresolution_complete,
        "points": [
            {
                "sequence": point.sequence,
                "point_id": point.point_id,
                "action": point.action,
                "ticket_id": point.ticket_id,
                "available_at": point.available_at,
                "winning_quote_keys": list(point.winning_quote_keys),
                "void_quote_keys": list(point.void_quote_keys),
                "equity": _decimal_text(point.equity, "point equity"),
            }
            for point in points
        ],
    }


def _require_product_issued_paper_state(book: PaperBook) -> None:
    """Require the detached authorities that make mutable PAPER state product-issued."""

    try:
        _require_ticket_opening_authority(book)
        _require_paperbook_causal_history_authority(book)
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("canonical PAPER product-issued authority is unavailable") from exc


def build_product_issued_paper_equity_path(
    book: PaperBook,
    goal: EconomicGoalContract,
) -> ProductIssuedPaperEquityPath:
    """Derive one immutable, re-resolvable current PAPER equity path.

    The returned object is evidence, not staking authority. Its identity is
    derived only from canonical PaperBook lifecycle state and the canonical
    owner EconomicGoal snapshot; no caller-provided drawdown, minimum-equity or
    risk-of-ruin scalar participates in issuance.
    """

    if type(book) is not PaperBook:
        raise TypeError("book must be canonical PaperBook")
    if type(goal) is not EconomicGoalContract:
        raise TypeError("goal must be canonical EconomicGoalContract")

    _require_product_issued_paper_state(book)

    goal_provenance_before = provenance_for(goal)
    goal_snapshot = economic_goal_from_payload(economic_goal_to_payload(goal))
    goal_snapshot_provenance = provenance_for(goal_snapshot)
    if (
        goal_provenance_before != goal_snapshot_provenance
        or provenance_for(goal) != goal_snapshot_provenance
    ):
        raise ValueError("canonical economic goal changed during equity-path issuance")

    before_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    before_source_sha256 = _paper_equity_source_state_sha256(book)
    if before_sha256 is None:
        raise ValueError("canonical PAPER risk state cannot issue an equity path")

    try:
        PaperBook._validate_loaded_state(book)
        replay_balance = book.initial_bankroll
        replay_committed = Decimal("0")
        points: list[PaperEquityPathPoint] = [
            PaperEquityPathPoint(
                sequence=0,
                point_id=_INITIAL_EQUITY_POINT_ID,
                action="initial",
                ticket_id=None,
                available_at=None,
                winning_quote_keys=(),
                void_quote_keys=(),
                equity=book.initial_bankroll,
            )
        ]
        # PaperBook does not currently persist a product-issued availability
        # timestamp for the opening capital point. Therefore the full path cannot
        # honestly claim complete causal availability even when every settlement
        # carries settled_at.
        availability_complete = False
        for index, raw_entry in enumerate(book._lifecycle):
            action, ticket_id, winners_raw, voids_raw = (
                PaperBook._validate_lifecycle_entry(raw_entry)
            )
            ticket = book.tickets.get(ticket_id)
            if ticket is None:
                raise ValueError("PAPER lifecycle references missing ticket")
            if action == "open":
                replay_balance = PaperBook._debit_balance(
                    replay_balance,
                    ticket.stake,
                )
                replay_committed = PaperRiskPolicy._exact_positive_sum(
                    (replay_committed, ticket.stake)
                )
                available_at = ticket.placed_at
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
                    raise ValueError("PAPER lifecycle committed stake became negative")
                available_at = ticket.settled_at
                if available_at is None:
                    availability_complete = False
            equity = PaperRiskPolicy._exact_positive_sum(
                (replay_balance, replay_committed)
            )
            points.append(
                PaperEquityPathPoint(
                    sequence=index + 1,
                    point_id=_lifecycle_point_id(index, action, ticket_id),
                    action=action,
                    ticket_id=ticket_id,
                    available_at=available_at,
                    winning_quote_keys=tuple(winners_raw),
                    void_quote_keys=tuple(voids_raw),
                    equity=equity,
                )
            )
    except (ArithmeticError, AttributeError, TypeError, ValueError) as exc:
        raise ValueError("canonical PAPER equity path cannot be resolved") from exc

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
    if replay_balance != book.balance or replay_committed != current_committed:
        raise ValueError("canonical PAPER equity path does not replay exact current state")
    if points[-1].equity != current_equity:
        raise ValueError("canonical PAPER equity path current equity is inconsistent")

    after_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    after_source_sha256 = _paper_equity_source_state_sha256(book)
    if after_sha256 is None or after_sha256 != before_sha256:
        raise ValueError("canonical PAPER risk state changed during equity-path issuance")
    if after_source_sha256 != before_source_sha256:
        raise ValueError("canonical PAPER source state changed during equity-path issuance")
    if provenance_for(goal) != goal_snapshot_provenance:
        raise ValueError("canonical economic goal changed during equity-path issuance")

    point_tuple = tuple(points)
    settled_history_complete = all(
        ticket.status is not TicketStatus.OPEN for ticket in book.tickets.values()
    )
    money_scope_complete = bool(book.tickets) and all(
        ticket.bankroll_id == goal_snapshot.bankroll_id
        and ticket.currency == goal_snapshot.currency
        for ticket in book.tickets.values()
    )
    # Current PaperBook persistence owns the opening numeric balance but does not
    # durably bind that opening capital to EconomicGoal.bankroll_id/currency.
    # Ticket-level provenance cannot retroactively mint that owner-capital
    # authority. Keep the fact explicit and fail closed for downstream financial
    # consumers until the canonical PaperBook/opening-capital authority carries it.
    opening_capital_authority_complete = False
    # PaperBook settlement arithmetic is gross of campaign/provider/execution
    # monetary costs. Cost authorities exist elsewhere in the product, but this
    # path does not yet compose and re-resolve them for the exact capital scope.
    applicable_costs_complete = False
    net_equity_authoritative = False
    # Current PaperBook settlements are one-shot and expose no append-only
    # correction/resettlement lineage. A current snapshot can be displayed, but
    # cannot claim an authoritative corrected/restated historical view.
    correction_lineage_complete = False
    restated_history_authoritative = False
    # Current resolver has no durable frozen-cutoff record that can be
    # re-resolved after later legitimate PaperBook history is appended.
    frozen_scope_complete = False
    historical_reresolution_complete = False
    payload = _equity_path_payload(
        goal_snapshot=goal_snapshot,
        goal_contract_sha256=goal_snapshot_provenance.contract_sha256,
        portfolio_risk_state_sha256=after_sha256,
        paperbook_source_state_sha256=after_source_sha256,
        initial_equity=book.initial_bankroll,
        points=point_tuple,
        availability_complete=availability_complete,
        settled_history_complete=settled_history_complete,
        money_scope_complete=money_scope_complete,
        opening_capital_authority_complete=opening_capital_authority_complete,
        applicable_costs_complete=applicable_costs_complete,
        net_equity_authoritative=net_equity_authoritative,
        correction_lineage_complete=correction_lineage_complete,
        restated_history_authoritative=restated_history_authoritative,
        frozen_scope_complete=frozen_scope_complete,
        historical_reresolution_complete=historical_reresolution_complete,
    )
    path_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    minimum_point = min(point_tuple, key=lambda point: (point.equity, point.sequence))
    return ProductIssuedPaperEquityPath(
        schema=EQUITY_PATH_SCHEMA,
        scope=RISK_REPORT_SCOPE_PAPER_ONLY,
        goal_id=goal_snapshot.goal_id,
        goal_revision=goal_snapshot.revision,
        bankroll_id=goal_snapshot.bankroll_id,
        currency=goal_snapshot.currency,
        goal_contract_sha256=goal_snapshot_provenance.contract_sha256,
        portfolio_risk_state_sha256=after_sha256,
        paperbook_source_state_sha256=after_source_sha256,
        history_view=HISTORY_VIEW_RESTATED_CURRENT,
        historical_as_known_supported=False,
        initial_equity=book.initial_bankroll,
        points=point_tuple,
        point_count=len(point_tuple),
        path_sha256=path_sha256,
        current_equity=current_equity,
        minimum_equity=minimum_point.equity,
        minimum_equity_point_id=minimum_point.point_id,
        availability_complete=availability_complete,
        settled_history_complete=settled_history_complete,
        money_scope_complete=money_scope_complete,
        opening_capital_authority_complete=opening_capital_authority_complete,
        applicable_costs_complete=applicable_costs_complete,
        net_equity_authoritative=net_equity_authoritative,
        correction_lineage_complete=correction_lineage_complete,
        restated_history_authoritative=restated_history_authoritative,
        frozen_scope_complete=frozen_scope_complete,
        historical_reresolution_complete=historical_reresolution_complete,
    )


def _historical_max_drawdown_from_path(
    path: ProductIssuedPaperEquityPath,
) -> _HistoricalMaxDrawdown:
    running_peak = path.initial_equity
    running_peak_id = _INITIAL_EQUITY_POINT_ID
    maximum = Decimal("0")
    maximum_fraction: Decimal | None = Decimal("0") if running_peak > 0 else None
    maximum_peak_id: str | None = None
    maximum_trough_id: str | None = None
    for point in path.points[1:]:
        equity = point.equity
        if equity > running_peak:
            running_peak = equity
            running_peak_id = point.point_id
            continue
        with localcontext(PaperRiskPolicy._decimal_context()):
            drawdown = running_peak - equity
        if drawdown < 0:
            raise ValueError("canonical PAPER equity path has negative drawdown")
        if drawdown > maximum:
            maximum = drawdown
            if running_peak > 0:
                ratio_context = PaperRiskPolicy._decimal_context()
                ratio_context.traps[Inexact] = False
                with localcontext(ratio_context):
                    maximum_fraction = drawdown / running_peak
            else:
                maximum_fraction = None
            maximum_peak_id = running_peak_id
            maximum_trough_id = point.point_id
    return _HistoricalMaxDrawdown(
        amount=maximum,
        fraction=maximum_fraction,
        peak_id=maximum_peak_id,
        trough_id=maximum_trough_id,
        current_equity=path.current_equity,
        peak_equity=running_peak,
    )


def verify_product_issued_paper_equity_path(
    book: PaperBook,
    goal: EconomicGoalContract,
    evidence: ProductIssuedPaperEquityPath,
) -> ProductIssuedPaperEquityPath:
    """Re-resolve product state and require byte-semantic equity-path equality."""

    if type(evidence) is not ProductIssuedPaperEquityPath:
        raise TypeError("equity-path evidence must be exact ProductIssuedPaperEquityPath")
    resolved = build_product_issued_paper_equity_path(book, goal)
    if resolved != evidence:
        raise ValueError("equity-path evidence does not match canonical product state")
    return resolved


def _require_minimum_equity_prerequisites(
    path: ProductIssuedPaperEquityPath,
) -> None:
    """Fail on the most specific unavailable authority needed by minimum equity."""

    if not path.settled_history_complete:
        raise ValueError(
            "minimum equity requires all economically material PAPER tickets settled"
        )
    if any(
        point.action == "settle" and point.available_at is None
        for point in path.points
    ):
        raise ValueError(
            "minimum equity requires complete durable availability chronology"
        )
    if not path.money_scope_complete:
        raise ValueError(
            "minimum equity requires exact bankroll and currency provenance"
        )
    if not path.opening_capital_authority_complete:
        raise ValueError(
            "minimum equity requires product-issued opening-capital authority"
        )
    if not path.availability_complete:
        raise ValueError(
            "minimum equity requires complete durable availability chronology"
        )
    if not path.applicable_costs_complete or not path.net_equity_authoritative:
        raise ValueError(
            "minimum equity requires complete authoritative net monetary costs"
        )
    if (
        not path.correction_lineage_complete
        or not path.restated_history_authoritative
    ):
        raise ValueError(
            "minimum equity requires authoritative correction/restatement lineage"
        )
    if not path.frozen_scope_complete or not path.historical_reresolution_complete:
        raise ValueError(
            "minimum equity requires durable frozen-scope historical re-resolution"
        )


def verified_settled_minimum_equity(
    book: PaperBook,
    goal: EconomicGoalContract,
    evidence: ProductIssuedPaperEquityPath,
) -> tuple[Decimal, str]:
    """Return the exact minimum only from a complete re-verified settled path.

    This is a causal capital-history fact, not a probability estimate and not
    risk approval. Positive risk-of-ruin issuance remains owned by #967.
    """

    resolved = verify_product_issued_paper_equity_path(book, goal, evidence)
    _require_minimum_equity_prerequisites(resolved)
    return resolved.minimum_equity, resolved.minimum_equity_point_id


def _drawdown_evidence_payload(
    *,
    path: ProductIssuedPaperEquityPath,
    maximum: _HistoricalMaxDrawdown,
) -> dict[str, object]:
    return {
        "schema": DRAWDOWN_EVIDENCE_SCHEMA,
        "metric_class": DRAWDOWN_METRIC_REALIZED_SETTLED_EQUITY,
        "equity_path_sha256": path.path_sha256,
        "paperbook_source_state_sha256": path.paperbook_source_state_sha256,
        "equity_path_point_count": path.point_count,
        "goal_id": path.goal_id,
        "goal_revision": path.goal_revision,
        "bankroll_id": path.bankroll_id,
        "currency": path.currency,
        "history_view": path.history_view,
        "historical_as_known_supported": path.historical_as_known_supported,
        "initial_equity": _decimal_text(path.initial_equity, "initial_equity"),
        "current_equity": _decimal_text(path.current_equity, "current_equity"),
        "peak_equity": _decimal_text(maximum.peak_equity, "peak_equity"),
        "minimum_equity": _decimal_text(path.minimum_equity, "minimum_equity"),
        "minimum_equity_point_id": path.minimum_equity_point_id,
        "max_drawdown_amount": _decimal_text(maximum.amount, "max_drawdown_amount"),
        "max_drawdown_fraction": (
            None
            if maximum.fraction is None
            else _decimal_text(maximum.fraction, "max_drawdown_fraction")
        ),
        "max_drawdown_peak_id": maximum.peak_id,
        "max_drawdown_trough_id": maximum.trough_id,
        "availability_complete": path.availability_complete,
        "settled_history_complete": path.settled_history_complete,
        "money_scope_complete": path.money_scope_complete,
        "opening_capital_authority_complete": path.opening_capital_authority_complete,
        "applicable_costs_complete": path.applicable_costs_complete,
        "net_equity_authoritative": path.net_equity_authoritative,
        "correction_lineage_complete": path.correction_lineage_complete,
        "restated_history_authoritative": path.restated_history_authoritative,
        "frozen_scope_complete": path.frozen_scope_complete,
        "historical_reresolution_complete": path.historical_reresolution_complete,
    }


def build_product_issued_paper_drawdown_evidence(
    book: PaperBook,
    goal: EconomicGoalContract,
) -> ProductIssuedPaperDrawdownEvidence:
    path = build_product_issued_paper_equity_path(book, goal)
    maximum = _historical_max_drawdown_from_path(path)
    payload = _drawdown_evidence_payload(path=path, maximum=maximum)
    evidence_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return ProductIssuedPaperDrawdownEvidence(
        schema=DRAWDOWN_EVIDENCE_SCHEMA,
        metric_class=DRAWDOWN_METRIC_REALIZED_SETTLED_EQUITY,
        equity_path_sha256=path.path_sha256,
        paperbook_source_state_sha256=path.paperbook_source_state_sha256,
        equity_path_point_count=path.point_count,
        goal_id=path.goal_id,
        goal_revision=path.goal_revision,
        bankroll_id=path.bankroll_id,
        currency=path.currency,
        history_view=path.history_view,
        historical_as_known_supported=path.historical_as_known_supported,
        initial_equity=path.initial_equity,
        current_equity=path.current_equity,
        peak_equity=maximum.peak_equity,
        minimum_equity=path.minimum_equity,
        minimum_equity_point_id=path.minimum_equity_point_id,
        max_drawdown_amount=maximum.amount,
        max_drawdown_fraction=maximum.fraction,
        max_drawdown_peak_id=maximum.peak_id,
        max_drawdown_trough_id=maximum.trough_id,
        availability_complete=path.availability_complete,
        settled_history_complete=path.settled_history_complete,
        money_scope_complete=path.money_scope_complete,
        opening_capital_authority_complete=path.opening_capital_authority_complete,
        applicable_costs_complete=path.applicable_costs_complete,
        net_equity_authoritative=path.net_equity_authoritative,
        correction_lineage_complete=path.correction_lineage_complete,
        restated_history_authoritative=path.restated_history_authoritative,
        frozen_scope_complete=path.frozen_scope_complete,
        historical_reresolution_complete=path.historical_reresolution_complete,
        evidence_sha256=evidence_sha256,
    )


def verify_product_issued_paper_drawdown_evidence(
    book: PaperBook,
    goal: EconomicGoalContract,
    evidence: ProductIssuedPaperDrawdownEvidence,
) -> ProductIssuedPaperDrawdownEvidence:
    """Reject copied/replaced caller evidence unless canonical state reissues it."""

    if type(evidence) is not ProductIssuedPaperDrawdownEvidence:
        raise TypeError(
            "drawdown evidence must be exact ProductIssuedPaperDrawdownEvidence"
        )
    resolved = build_product_issued_paper_drawdown_evidence(book, goal)
    if resolved != evidence:
        raise ValueError("drawdown evidence does not match canonical product state")
    return resolved


def _same_canonical_paperbook_state(left: PaperBook, right: PaperBook) -> bool:
    """Compare complete validated PAPER state for durable read-race detection.

    The canonical risk digest remains the published risk identity. This exact
    value comparison is intentionally only a resolver race detector because the
    current risk digest can omit non-risk leg dimensions that still matter to
    product evidence identity.
    """

    if type(left) is not PaperBook or type(right) is not PaperBook:
        return False
    try:
        _require_product_issued_paper_state(left)
        _require_product_issued_paper_state(right)
        PaperBook._validate_loaded_state(left)
        PaperBook._validate_loaded_state(right)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False
    return (
        left.initial_bankroll == right.initial_bankroll
        and left.balance == right.balance
        and left.tickets == right.tickets
        and left._lifecycle == right._lifecycle
        and left._settlement_times == right._settlement_times
    )


def _durable_source_pair(
    *,
    paper_book_path: str,
    workspace: str,
) -> tuple[PaperBook, EconomicGoalContract]:
    if type(paper_book_path) is not str or not paper_book_path:
        raise TypeError("paper_book_path must be an exact non-empty str")
    if type(workspace) is not str or not workspace:
        raise TypeError("workspace must be an exact non-empty str")

    workspace_path = Path(workspace)
    paper_path = Path(paper_book_path)
    if not workspace_path.is_absolute():
        raise ValueError("workspace must be an absolute canonical product workspace path")
    if not paper_path.is_absolute():
        raise ValueError("paper_book_path must be an absolute canonical product path")
    expected_paper_path = workspace_path / "paper_book.json"
    actual_locator = os.path.normcase(os.path.normpath(str(paper_path)))
    expected_locator = os.path.normcase(os.path.normpath(str(expected_paper_path)))
    if actual_locator != expected_locator:
        raise ValueError(
            "paper_book_path must be the canonical workspace/paper_book.json"
        )

    store = EconomicGoalStore(workspace_path)
    goal_before = store.load()
    goal_before_provenance = provenance_for(goal_before)
    book = PaperBook.load(paper_path)
    goal_after = store.load()
    if provenance_for(goal_after) != goal_before_provenance:
        raise ValueError("durable economic goal changed during equity-path resolution")

    before_state = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    if before_state is None:
        raise ValueError("durable PaperBook cannot issue canonical equity-path evidence")
    book_after = PaperBook.load(paper_path)
    after_state = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book_after)
    if (
        after_state is None
        or after_state != before_state
        or not _same_canonical_paperbook_state(book, book_after)
    ):
        raise ValueError("durable PaperBook changed during equity-path resolution")

    # Close the cross-file read interval after the final PaperBook read. Without
    # this last owner-goal read, a durable goal replacement occurring after
    # goal_after but before book_after could publish a mixed source pair.
    goal_final = store.load()
    if provenance_for(goal_final) != goal_before_provenance:
        raise ValueError("durable economic goal changed during equity-path resolution")
    return book_after, goal_final


def resolve_durable_product_issued_paper_equity_path(
    *,
    paper_book_path: str,
    workspace: str,
) -> ProductIssuedPaperEquityPath:
    """Re-resolve equity-path evidence from durable product-owned state only."""

    book, goal = _durable_source_pair(
        paper_book_path=paper_book_path,
        workspace=workspace,
    )
    return build_product_issued_paper_equity_path(book, goal)


def resolve_durable_product_issued_paper_drawdown_evidence(
    *,
    paper_book_path: str,
    workspace: str,
) -> ProductIssuedPaperDrawdownEvidence:
    """Re-resolve drawdown evidence from the same durable equity-path source."""

    book, goal = _durable_source_pair(
        paper_book_path=paper_book_path,
        workspace=workspace,
    )
    return build_product_issued_paper_drawdown_evidence(book, goal)


def verify_durable_product_issued_paper_equity_path(
    *,
    paper_book_path: str,
    workspace: str,
    evidence: ProductIssuedPaperEquityPath,
) -> ProductIssuedPaperEquityPath:
    """Require caller evidence to equal a fresh durable product re-resolution."""

    if type(evidence) is not ProductIssuedPaperEquityPath:
        raise TypeError("equity-path evidence must be exact ProductIssuedPaperEquityPath")
    resolved = resolve_durable_product_issued_paper_equity_path(
        paper_book_path=paper_book_path,
        workspace=workspace,
    )
    if resolved != evidence:
        raise ValueError("equity-path evidence does not match durable product state")
    return resolved


def verify_durable_product_issued_paper_drawdown_evidence(
    *,
    paper_book_path: str,
    workspace: str,
    evidence: ProductIssuedPaperDrawdownEvidence,
) -> ProductIssuedPaperDrawdownEvidence:
    """Require drawdown evidence to equal a fresh durable product re-resolution."""

    if type(evidence) is not ProductIssuedPaperDrawdownEvidence:
        raise TypeError(
            "drawdown evidence must be exact ProductIssuedPaperDrawdownEvidence"
        )
    resolved = resolve_durable_product_issued_paper_drawdown_evidence(
        paper_book_path=paper_book_path,
        workspace=workspace,
    )
    if resolved != evidence:
        raise ValueError("drawdown evidence does not match durable product state")
    return resolved


def resolve_durable_verified_settled_minimum_equity(
    *,
    paper_book_path: str,
    workspace: str,
) -> tuple[Decimal, str, str]:
    """Return minimum equity only from complete durable product-owned history.

    The third value is the equity-path SHA-256. This function supplies a
    re-resolvable causal capital-history fact only; it does not estimate or
    authorize risk of ruin.
    """

    resolved = resolve_durable_product_issued_paper_equity_path(
        paper_book_path=paper_book_path,
        workspace=workspace,
    )
    _require_minimum_equity_prerequisites(resolved)
    return (
        resolved.minimum_equity,
        resolved.minimum_equity_point_id,
        resolved.path_sha256,
    )


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
                    # This fraction is descriptive evidence, not money/risk
                    # enforcement arithmetic. Preserve the canonical risk precision,
                    # exponent bounds and rounding while allowing the deterministic
                    # rounded representation required for recurring ratios.
                    ratio_context = PaperRiskPolicy._decimal_context()
                    ratio_context.traps[Inexact] = False
                    with localcontext(ratio_context):
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

    before_source_sha256 = _paper_equity_source_state_sha256(book)
    before_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    if before_sha256 is None:
        raise ValueError("canonical PAPER risk state cannot be reported")

    metrics = PaperRiskPolicy._historical_risk_metrics(book)
    rooms = PaperRiskPolicy._goal_history_rooms(book, goal_snapshot)
    equity_path = build_product_issued_paper_equity_path(book, goal_snapshot)
    drawdown_evidence = build_product_issued_paper_drawdown_evidence(
        book,
        goal_snapshot,
    )
    if drawdown_evidence.equity_path_sha256 != equity_path.path_sha256:
        raise ValueError("canonical PAPER drawdown evidence path identity is inconsistent")
    # Preserve the established independent replay callback boundary as a
    # consistency/race falsifier. It is not the published evidence authority:
    # the product-issued path/drawdown digests above are. This cross-check also
    # catches state mutation between the two independent canonical replays.
    replay_crosscheck = _historical_max_drawdown(book)
    if replay_crosscheck is None:
        raise ValueError("canonical PAPER drawdown replay is unavailable")
    maximum_drawdown = _HistoricalMaxDrawdown(
        amount=drawdown_evidence.max_drawdown_amount,
        fraction=drawdown_evidence.max_drawdown_fraction,
        peak_id=drawdown_evidence.max_drawdown_peak_id,
        trough_id=drawdown_evidence.max_drawdown_trough_id,
        current_equity=drawdown_evidence.current_equity,
        peak_equity=drawdown_evidence.peak_equity,
    )
    after_source_sha256 = _paper_equity_source_state_sha256(book)
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
    if after_source_sha256 != before_source_sha256:
        raise ValueError("canonical PAPER source state changed during reporting")
    if (
        maximum_drawdown.current_equity != metrics.current_equity
        or maximum_drawdown.peak_equity != metrics.peak_equity
        or replay_crosscheck != maximum_drawdown
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
        paperbook_source_state_sha256=equity_path.paperbook_source_state_sha256,
        equity_path_sha256=equity_path.path_sha256,
        drawdown_evidence_sha256=drawdown_evidence.evidence_sha256,
        equity_path_point_count=equity_path.point_count,
        equity_path_availability_complete=equity_path.availability_complete,
        settled_history_complete=equity_path.settled_history_complete,
        money_scope_complete=equity_path.money_scope_complete,
        opening_capital_authority_complete=equity_path.opening_capital_authority_complete,
        applicable_costs_complete=equity_path.applicable_costs_complete,
        net_equity_authoritative=equity_path.net_equity_authoritative,
        correction_lineage_complete=equity_path.correction_lineage_complete,
        restated_history_authoritative=equity_path.restated_history_authoritative,
        frozen_scope_complete=equity_path.frozen_scope_complete,
        historical_reresolution_complete=equity_path.historical_reresolution_complete,
        history_view=equity_path.history_view,
        historical_as_known_supported=equity_path.historical_as_known_supported,
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
