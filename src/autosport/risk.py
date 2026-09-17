from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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

from .domain import MarketEvent, PaperTicket, TicketLeg, TicketStatus
from .economic_goal import EconomicGoalContract
from .paper import PaperBook


def _canonical_context_text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    return value


def _canonical_context_timestamp(name: str, value: object) -> tuple[str, datetime]:
    timestamp = _canonical_context_text(name, value)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware ISO-8601")
    return timestamp, parsed


@dataclass(frozen=True, slots=True)
class ProposedTicketRiskContext:
    """Typed, non-persistent facts about one proposed paper ticket.

    ``legs`` are the canonical proposed event/market/selection identity and locked
    odds. ``quotes`` reuse canonical MarketEvent source/quote identity; quote
    evidence must cover every proposed leg exactly once whenever an economic-goal
    quote check is evaluated. ``proposal_ts`` binds freshness to the proposal
    instant. Optional bankroll/currency and measurement-window identities are
    carried unchanged and never inferred.

    This seam carries the canonical proposal-local identities needed for parlay,
    market deny-list, and provider deny-list enforcement. Canonical sport identity
    is deliberately absent until the upstream #339 identity authority exists, so
    any non-empty owner sport deny-list must fail closed instead of being guessed.
    Session/day loss, drawdown and turnover are conservatively bounded from the
    validated PaperBook lifecycle: all-history gross realized loss upper-bounds
    any bounded loss window, stake-basis equity preserves open stake at cost until
    settlement, and turnover counts every durable ticket stake. A probabilistic
    risk-of-ruin ceiling requires an explicit canonical upper-bound witness in this
    context; it is never inferred from PaperBook balances. Event/market concentration
    is derived exactly from canonical open PaperBook stake plus the proposed stake.
    Provider/sport concentration remains fail-closed until those historical identities
    have canonical durable authorities.
    """

    legs: tuple[TicketLeg, ...]
    quotes: tuple[MarketEvent, ...] = ()
    bankroll_id: str | None = None
    currency: str | None = None
    measurement_window_start: str | None = None
    measurement_window_end: str | None = None
    proposal_ts: str | None = None
    risk_of_ruin_upper_bound: Decimal | None = None

    def __post_init__(self) -> None:
        if type(self.legs) is not tuple or not self.legs:
            raise ValueError("proposed ticket context requires a non-empty tuple of legs")

        leg_keys: set[str] = set()
        for leg in self.legs:
            try:
                PaperBook._validate_ticket_leg(leg)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("proposed ticket context contains an invalid leg") from exc
            if leg.quote_key in leg_keys:
                raise ValueError("proposed ticket context contains duplicate leg identity")
            leg_keys.add(leg.quote_key)

        if type(self.quotes) is not tuple:
            raise ValueError("proposed ticket quotes must be a tuple")

        quote_keys: set[str] = set()
        for quote in self.quotes:
            if type(quote) is not MarketEvent:
                raise ValueError("proposed ticket context contains an invalid quote")
            try:
                validated_quote = MarketEvent.from_dict(quote.to_dict())
                _canonical_context_timestamp("quote observed_ts", quote.observed_ts)
                if quote.source_ts is not None:
                    _canonical_context_timestamp("quote source_ts", quote.source_ts)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("proposed ticket context contains an invalid quote") from exc
            if validated_quote != quote:
                raise ValueError("proposed ticket context contains a non-canonical quote")
            if quote.quote_key in quote_keys:
                raise ValueError("proposed ticket context contains duplicate quote identity")
            quote_keys.add(quote.quote_key)

        if quote_keys and quote_keys != leg_keys:
            raise ValueError(
                "proposed ticket quote evidence must cover every proposed leg exactly once"
            )

        if (self.bankroll_id is None) != (self.currency is None):
            raise ValueError("bankroll_id and currency must be supplied together")
        if self.bankroll_id is not None:
            _canonical_context_text("bankroll_id", self.bankroll_id)
            currency = _canonical_context_text("currency", self.currency)
            if (
                len(currency) != 3
                or not currency.isascii()
                or not currency.isalpha()
                or currency != currency.upper()
            ):
                raise ValueError("currency must be a three-letter uppercase ASCII code")

        if (self.measurement_window_start is None) != (
            self.measurement_window_end is None
        ):
            raise ValueError(
                "measurement window start and end must be supplied together"
            )
        if self.proposal_ts is not None:
            _canonical_context_timestamp("proposal_ts", self.proposal_ts)

        if self.measurement_window_start is not None:
            _, start = _canonical_context_timestamp(
                "measurement_window_start", self.measurement_window_start
            )
            _, end = _canonical_context_timestamp(
                "measurement_window_end", self.measurement_window_end
            )
            if start > end:
                raise ValueError("measurement window start must not be after end")

        if self.risk_of_ruin_upper_bound is not None:
            bound = self.risk_of_ruin_upper_bound
            if (
                not isinstance(bound, Decimal)
                or not bound.is_finite()
                or bound < Decimal("0")
                or bound > Decimal("1")
            ):
                raise ValueError(
                    "risk_of_ruin_upper_bound must be an exact Decimal between 0 and 1"
                )

    @property
    def parlay_leg_count(self) -> int:
        return len(self.legs)

    @property
    def event_ids(self) -> frozenset[str]:
        return frozenset(leg.event_id for leg in self.legs)

    @property
    def market_ids(self) -> frozenset[str]:
        return frozenset(leg.market_id for leg in self.legs)

    @property
    def source_ids(self) -> frozenset[str]:
        return frozenset(quote.source_id for quote in self.quotes)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class _HistoricalRiskMetrics:
    """Derived, non-persistent risk facts from one validated PaperBook lifecycle."""

    initial_bankroll: Decimal
    current_equity: Decimal
    peak_equity: Decimal
    committed_stake: Decimal
    realized_gross_loss: Decimal
    turnover: Decimal


@dataclass(frozen=True, slots=True)
class PaperRiskPolicy:
    """Paper-lab guardrails. Limits are explicit and deterministic, never inferred by an LLM.

    ``economic_goal`` can only tighten the locally proven executable limits in
    this policy. It binds per-ticket stake, aggregate committed capital,
    concurrent open paper positions, owner emergency stop, proposal-local parlay
    and deny-list restrictions, quote freshness, execution slippage, and the
    available data-quality proof boundary. ``ProposedTicketRiskContext`` is the
    typed, non-persistent evidence seam for these proposal-local checks; an active
    economic goal therefore fails closed when that evidence seam is absent.
    Session/day loss, drawdown and turnover are enforced conservatively from the
    canonical PaperBook lifecycle and therefore survive snapshot restart without a
    second state authority. Risk-of-ruin remains evidence-gated because a balance
    history is not a probability model. Event/market concentration is enforced
    against the whole open stake set; provider/sport concentration remains
    fail-closed where canonical historical identity is unavailable.
    """

    max_ticket_fraction: Decimal = Decimal("0.02")
    max_committed_fraction: Decimal = Decimal("0.20")
    minimum_cash_reserve_fraction: Decimal = Decimal("0.20")
    economic_goal: EconomicGoalContract | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "max_ticket_fraction",
            "max_committed_fraction",
            "minimum_cash_reserve_fraction",
        ):
            raw_value = getattr(self, field_name)
            try:
                value = Decimal(str(raw_value))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"{field_name} must be a finite decimal") from exc
            if not value.is_finite():
                raise ValueError(f"{field_name} must be a finite decimal")
            if value < 0 or value > 1:
                raise ValueError(f"{field_name} must be between 0 and 1 inclusive")
            object.__setattr__(self, field_name, value)
        if self.economic_goal is not None and not isinstance(
            self.economic_goal, EconomicGoalContract
        ):
            raise TypeError("economic_goal must be an EconomicGoalContract or None")

    @staticmethod
    def _decimal_context() -> Context:
        context = Context(prec=28, Emin=-999999, Emax=999999)
        context.traps[Inexact] = True
        context.traps[InvalidOperation] = True
        context.traps[Overflow] = True
        context.traps[Underflow] = True
        context.clear_flags()
        return context

    @staticmethod
    def _exact_positive_sum(values: tuple[Decimal, ...]) -> Decimal:
        """Sum canonical non-negative exposure exactly, independent of caller context."""
        if not values:
            return Decimal("0")
        min_exponent: int | None = None
        max_adjusted: int | None = None
        nonzero_count = 0
        for value in values:
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("committed exposure must contain non-negative finite Decimal values")
            if value.is_zero():
                continue
            decimal_tuple = value.as_tuple()
            exponent = int(decimal_tuple.exponent)
            adjusted = exponent + len(decimal_tuple.digits) - 1
            min_exponent = exponent if min_exponent is None else min(min_exponent, exponent)
            max_adjusted = adjusted if max_adjusted is None else max(max_adjusted, adjusted)
            nonzero_count += 1
        if nonzero_count == 0:
            return Decimal("0")
        assert min_exponent is not None and max_adjusted is not None
        required_precision = max_adjusted - min_exponent + 1 + len(str(nonzero_count))
        context = Context(
            prec=max(1, required_precision),
            rounding=ROUND_HALF_EVEN,
            Emin=-999999,
            Emax=999999,
        )
        context.traps[Inexact] = True
        context.traps[InvalidOperation] = True
        context.traps[Overflow] = True
        context.traps[Underflow] = True
        context.clear_flags()
        with localcontext(context):
            return sum(values, Decimal("0"))

    @classmethod
    def _book_state(
        cls, book: PaperBook
    ) -> tuple[Decimal, Decimal, Decimal, int] | None:
        try:
            tickets = book.tickets
            if not isinstance(tickets, dict):
                return None
            for ticket_key, ticket in tickets.items():
                if not isinstance(ticket, PaperTicket) or not isinstance(ticket.status, TicketStatus):
                    return None
                if (
                    not isinstance(ticket.ticket_id, str)
                    or not ticket.ticket_id
                    or ticket_key != ticket.ticket_id
                ):
                    return None

            # PaperBook owns canonical lifecycle/settlement semantics. Do not impose the
            # risk context's Inexact trap on that validator.
            PaperBook._validate_loaded_state(book)
            initial_bankroll = book.initial_bankroll
            balance = book.balance
            open_tickets = tuple(
                ticket for ticket in tickets.values() if ticket.status is TicketStatus.OPEN
            )
            committed_stake = cls._exact_positive_sum(
                tuple(ticket.stake for ticket in open_tickets)
            )
            open_position_count = len(open_tickets)
        except (ArithmeticError, AttributeError, TypeError, ValueError):
            return None

        values = (initial_bankroll, balance, committed_stake)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
            return None
        if initial_bankroll <= 0 or balance < 0 or committed_stake < 0:
            return None
        return initial_bankroll, balance, committed_stake, open_position_count

    @classmethod
    def _historical_risk_metrics(
        cls, book: PaperBook
    ) -> _HistoricalRiskMetrics | None:
        """Derive conservative durable risk facts from canonical PaperBook history.

        Gross realized loss deliberately ignores wins, so it upper-bounds the loss
        accumulated in any unknown session/day sub-window. Stake-basis equity is
        cash plus open stake at cost: opening a ticket cannot manufacture drawdown,
        while settlement changes equity by exactly payout minus stake. This is a
        derived view only; PaperBook remains the sole bankroll/lifecycle authority.
        """

        try:
            PaperBook._validate_loaded_state(book)
            replay_balance = book.initial_bankroll
            replay_committed = Decimal("0")
            peak_equity = book.initial_bankroll
            realized_gross_loss = Decimal("0")
            turnover = Decimal("0")

            for raw_entry in book._lifecycle:
                action, ticket_id, winners_raw, voids_raw = (
                    PaperBook._validate_lifecycle_entry(raw_entry)
                )
                ticket = book.tickets.get(ticket_id)
                if ticket is None:
                    return None

                if action == "open":
                    replay_balance = PaperBook._debit_balance(
                        replay_balance, ticket.stake
                    )
                    replay_committed = cls._exact_positive_sum(
                        (replay_committed, ticket.stake)
                    )
                    turnover = cls._exact_positive_sum((turnover, ticket.stake))
                else:
                    _, payout, replay_balance = PaperBook._settlement_result(
                        ticket,
                        replay_balance,
                        set(winners_raw),
                        set(voids_raw),
                    )
                    with localcontext(cls._decimal_context()):
                        replay_committed = replay_committed - ticket.stake
                        loss = (
                            ticket.stake - payout
                            if payout < ticket.stake
                            else Decimal("0")
                        )
                    if replay_committed < 0:
                        return None
                    if loss > 0:
                        realized_gross_loss = cls._exact_positive_sum(
                            (realized_gross_loss, loss)
                        )

                equity = cls._exact_positive_sum(
                    (replay_balance, replay_committed)
                )
                if equity > peak_equity:
                    peak_equity = equity

            current_committed = cls._exact_positive_sum(
                tuple(
                    ticket.stake
                    for ticket in book.tickets.values()
                    if ticket.status is TicketStatus.OPEN
                )
            )
            current_equity = cls._exact_positive_sum(
                (book.balance, current_committed)
            )
        except (ArithmeticError, AttributeError, TypeError, ValueError):
            return None

        if replay_balance != book.balance or replay_committed != current_committed:
            return None
        values = (
            book.initial_bankroll,
            current_equity,
            peak_equity,
            current_committed,
            realized_gross_loss,
            turnover,
        )
        if any(
            not isinstance(value, Decimal)
            or not value.is_finite()
            or value < Decimal("0")
            for value in values
        ):
            return None
        if peak_equity <= 0 or current_equity > peak_equity:
            return None
        return _HistoricalRiskMetrics(
            initial_bankroll=book.initial_bankroll,
            current_equity=current_equity,
            peak_equity=peak_equity,
            committed_stake=current_committed,
            realized_gross_loss=realized_gross_loss,
            turnover=turnover,
        )

    @classmethod
    def _goal_history_rooms(
        cls,
        book: PaperBook,
        goal: EconomicGoalContract,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        """Return maximum additional losing stake allowed by durable history."""

        metrics = cls._historical_risk_metrics(book)
        if metrics is None:
            return None
        try:
            with localcontext(cls._decimal_context()):
                session_limit = (
                    metrics.initial_bankroll * goal.max_session_loss_fraction
                )
                day_limit = metrics.initial_bankroll * goal.max_day_loss_fraction
                drawdown_limit = (
                    metrics.peak_equity * goal.max_drawdown_fraction
                )
                turnover_limit = (
                    metrics.initial_bankroll * goal.max_turnover_fraction
                )
                # Existing open stake is part of the whole-portfolio worst-case
                # loss envelope. A new proposal may consume only the room left
                # after every current open ticket is treated as a full loss.
                session_room = (
                    session_limit
                    - metrics.realized_gross_loss
                    - metrics.committed_stake
                )
                day_room = (
                    day_limit
                    - metrics.realized_gross_loss
                    - metrics.committed_stake
                )
                drawdown_floor = metrics.peak_equity - drawdown_limit
                drawdown_room = (
                    metrics.current_equity
                    - drawdown_floor
                    - metrics.committed_stake
                )
                turnover_room = turnover_limit - metrics.turnover
        except (ArithmeticError, TypeError, ValueError):
            return None

        rooms = (session_room, day_room, drawdown_room, turnover_room)
        if any(not value.is_finite() for value in rooms):
            return None
        return rooms

    @staticmethod
    def _fraction_exceeds(
        numerator: Decimal,
        denominator: Decimal,
        limit: Decimal,
    ) -> bool:
        """Compare one exact Decimal ratio to a limit without rounding."""
        values = (numerator, denominator, limit)
        if any(
            not isinstance(value, Decimal) or not value.is_finite()
            for value in values
        ):
            raise ValueError("concentration ratio requires finite Decimal values")
        if numerator < 0 or denominator <= 0 or limit < 0 or limit > 1:
            raise ValueError("concentration ratio values are out of range")

        numerator_num, numerator_den = numerator.as_integer_ratio()
        denominator_num, denominator_den = denominator.as_integer_ratio()
        limit_num, limit_den = limit.as_integer_ratio()
        return (
            numerator_num * denominator_den * limit_den
            > limit_num * numerator_den * denominator_num
        )

    @classmethod
    def _identity_concentration_decision(
        cls,
        book: PaperBook,
        amount: Decimal,
        context: ProposedTicketRiskContext,
        *,
        dimension: str,
        limit: Decimal,
    ) -> RiskDecision | None:
        """Enforce exact whole-open-portfolio event/market stake concentration."""
        if limit >= Decimal("1"):
            return None
        if dimension == "event":
            proposed_identities = context.event_ids
            identity_attribute = "event_id"
        elif dimension == "market":
            proposed_identities = context.market_ids
            identity_attribute = "market_id"
        else:
            raise ValueError("unsupported concentration dimension")

        state = cls._book_state(book)
        if state is None:
            return RiskDecision(False, "virtual bankroll state is invalid")
        _, _, committed_stake, _ = state

        try:
            total_exposure = cls._exact_positive_sum((committed_stake, amount))
            exposure_by_identity: dict[str, Decimal] = {}
            for ticket in book.tickets.values():
                if ticket.status is not TicketStatus.OPEN:
                    continue
                identities = frozenset(
                    getattr(leg, identity_attribute) for leg in ticket.legs
                )
                for identity in identities:
                    exposure_by_identity[identity] = cls._exact_positive_sum(
                        (
                            exposure_by_identity.get(identity, Decimal("0")),
                            ticket.stake,
                        )
                    )

            for identity in proposed_identities:
                proposed_exposure = cls._exact_positive_sum(
                    (
                        exposure_by_identity.get(identity, Decimal("0")),
                        amount,
                    )
                )
                if cls._fraction_exceeds(
                    proposed_exposure,
                    total_exposure,
                    limit,
                ):
                    return RiskDecision(
                        False,
                        f"owner {dimension} concentration limit exceeded",
                    )
        except (ArithmeticError, AttributeError, TypeError, ValueError):
            return RiskDecision(
                False,
                f"owner {dimension} concentration evidence is invalid",
            )
        return None

    @classmethod
    def _proposal_restriction_decision(
        cls,
        book: PaperBook,
        amount: Decimal,
        goal: EconomicGoalContract,
        context: ProposedTicketRiskContext,
    ) -> RiskDecision | None:
        """Enforce owner restrictions supported by canonical proposal/open-book identity."""
        if context.parlay_leg_count > goal.max_parlay_legs:
            return RiskDecision(False, "economic goal parlay leg limit exceeded")
        if context.market_ids & goal.blocked_markets:
            return RiskDecision(False, "proposed ticket contains an owner-blocked market")
        if context.source_ids & goal.blocked_providers:
            return RiskDecision(False, "proposed ticket uses an owner-blocked provider")
        if goal.blocked_sports:
            return RiskDecision(
                False,
                "owner sport deny-list cannot be proven without canonical sport identity",
            )

        for dimension, limit in (
            ("event", goal.max_event_concentration_fraction),
            ("market", goal.max_market_concentration_fraction),
        ):
            decision = cls._identity_concentration_decision(
                book,
                amount,
                context,
                dimension=dimension,
                limit=limit,
            )
            if decision is not None:
                return decision

        # PaperTicket deliberately persists event/market/selection identity but not
        # provider or sport identity. Never invent those historical dimensions from
        # current quote evidence. Restrictive provider/sport concentration therefore
        # remains fail-closed until their canonical durable identity authorities exist.
        for dimension, limit in (
            ("provider", goal.max_provider_concentration_fraction),
            ("sport", goal.max_sport_concentration_fraction),
        ):
            if limit < Decimal("1"):
                return RiskDecision(
                    False,
                    f"owner {dimension} concentration limit cannot be proven without "
                    "canonical whole-portfolio exposure evidence",
                )
        return None

    @staticmethod
    def _quote_risk_decision(
        goal: EconomicGoalContract,
        context: ProposedTicketRiskContext,
    ) -> RiskDecision | None:
        """Enforce proposal-local quote controls from canonical supplied evidence."""
        if not context.quotes:
            return RiskDecision(False, "proposed ticket quote evidence is required")
        if context.proposal_ts is None:
            return RiskDecision(False, "proposal timestamp is required for quote risk checks")

        try:
            _, proposal_time = _canonical_context_timestamp("proposal_ts", context.proposal_ts)
            quotes_by_key = {quote.quote_key: quote for quote in context.quotes}
            with localcontext(PaperRiskPolicy._decimal_context()):
                for leg in context.legs:
                    quote = quotes_by_key[leg.quote_key]
                    quote_ts = quote.source_ts if quote.source_ts is not None else quote.observed_ts
                    _, quote_time = _canonical_context_timestamp("quote timestamp", quote_ts)
                    age_delta = proposal_time - quote_time
                    age_seconds = (
                        Decimal(age_delta.days * 86400 + age_delta.seconds)
                        + (Decimal(age_delta.microseconds) / Decimal("1000000"))
                    )
                    if age_seconds < 0:
                        return RiskDecision(False, "quote timestamp is after proposal timestamp")
                    if age_seconds > goal.max_quote_age_seconds:
                        return RiskDecision(False, "quote exceeds economic goal maximum age")

                    if quote.decimal_odds <= 0 or leg.locked_odds <= 0:
                        return RiskDecision(False, "quote or locked odds are invalid")
                    # Compare (quote - locked) / quote to the configured ceiling
                    # as an exact rational inequality. Decimal division can be
                    # repeating (for example 0.10 / 2.10 == 1/21), and this
                    # policy's protective Decimal context deliberately traps
                    # Inexact rather than silently rounding economic evidence.
                    if quote.decimal_odds > leg.locked_odds:
                        quote_num, quote_den = quote.decimal_odds.as_integer_ratio()
                        locked_num, locked_den = leg.locked_odds.as_integer_ratio()
                        limit_num, limit_den = (
                            goal.max_execution_slippage_fraction.as_integer_ratio()
                        )
                        adverse_num = quote_num * locked_den - locked_num * quote_den
                        adverse_den = locked_den * quote_num
                        if adverse_num * limit_den > limit_num * adverse_den:
                            return RiskDecision(
                                False,
                                "quote-to-proposal slippage exceeds economic goal limit",
                            )
        except (ArithmeticError, KeyError, TypeError, ValueError):
            return RiskDecision(False, "proposed ticket quote risk evidence is invalid")

        # MarketEvent has canonical generic metadata, but no canonical data-quality
        # score contract. Do not reinterpret provider metadata as owner-grade truth.
        if goal.minimum_data_quality > 0:
            return RiskDecision(
                False,
                "minimum data quality cannot be proven from canonical quote evidence",
            )
        return None

    def _effective_fraction_limits(self) -> tuple[Decimal, Decimal]:
        goal = self.economic_goal
        if goal is None:
            return self.max_ticket_fraction, self.max_committed_fraction
        return (
            min(self.max_ticket_fraction, goal.max_stake_fraction),
            min(self.max_committed_fraction, goal.max_capital_at_risk_fraction),
        )

    def derive_goal_stake(
        self,
        book: PaperBook,
        signal_strength: Decimal | str,
    ) -> Decimal | None:
        """Derive one bounded paper stake when an EconomicGoalContract is active.

        The signal may tighten the executable proposal, but it can never enlarge
        the owner goal or local PaperRiskPolicy envelope. Invalid state, exhausted
        capacity, emergency STOP, non-positive signal, or Decimal uncertainty
        returns ZERO semantics as None rather than inventing a stake.
        """

        goal = self.economic_goal
        if goal is None:
            return None
        try:
            signal = Decimal(str(signal_strength))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not signal.is_finite() or signal <= 0:
            return None

        state = self._book_state(book)
        if state is None:
            return None
        initial_bankroll, balance, committed_stake, open_position_count = state
        if goal.emergency_stop or open_position_count >= goal.max_concurrent_positions:
            return None
        # This proposal-only sizing API has no canonical probabilistic ruin
        # witness input. A nontrivial owner ruin ceiling therefore means the
        # method must return ZERO rather than emit a stake that has not passed
        # every required economic-risk evidence boundary.
        if goal.max_risk_of_ruin < Decimal("1"):
            return None

        history_rooms = self._goal_history_rooms(book, goal)
        if history_rooms is None:
            return None

        try:
            ticket_fraction, committed_fraction = self._effective_fraction_limits()
            signal_fraction = min(signal, Decimal("1"))
            with localcontext(self._decimal_context()):
                signal_limit = initial_bankroll * signal_fraction
                ticket_limit = initial_bankroll * ticket_fraction
                committed_limit = initial_bankroll * committed_fraction
                reserve_limit = initial_bankroll * self.minimum_cash_reserve_fraction
                committed_room = committed_limit - committed_stake
                reserve_room = balance - reserve_limit
            caps = [
                signal_limit,
                ticket_limit,
                committed_room,
                reserve_room,
                balance,
                *history_rooms,
            ]
            if goal.max_stake_amount is not None:
                caps.append(goal.max_stake_amount)
            amount = min(caps)
        except (ArithmeticError, TypeError, ValueError):
            return None

        if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
            return None
        return amount

    def _derived_risk_values(
        self,
        initial_bankroll: Decimal,
        balance: Decimal,
        committed_stake: Decimal,
        amount: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal] | None:
        try:
            ticket_fraction, committed_fraction = self._effective_fraction_limits()
            # Protective caps/reserve remain fail-closed on any limit-relaxing rounding.
            with localcontext(self._decimal_context()):
                ticket_limit = initial_bankroll * ticket_fraction
                committed_limit = initial_bankroll * committed_fraction
                remaining_balance = balance - amount
                reserve_limit = initial_bankroll * self.minimum_cash_reserve_fraction
            # Exposure itself can legitimately require more than 28 significant digits even
            # when every PaperBook debit was canonical, so aggregate it exactly.
            aggregate_committed = self._exact_positive_sum((committed_stake, amount))
        except (ArithmeticError, TypeError, ValueError):
            return None

        values = (
            ticket_limit,
            aggregate_committed,
            committed_limit,
            remaining_balance,
            reserve_limit,
        )
        if any(not value.is_finite() for value in values):
            return None
        return values

    def evaluate(
        self,
        book: PaperBook,
        stake: Decimal | str,
        *,
        context: ProposedTicketRiskContext | None = None,
    ) -> RiskDecision:
        if context is not None and not isinstance(context, ProposedTicketRiskContext):
            return RiskDecision(False, "proposed ticket risk context is invalid")

        try:
            amount = Decimal(str(stake))
        except (InvalidOperation, ValueError):
            return RiskDecision(False, "stake must be a finite decimal")
        if not amount.is_finite():
            return RiskDecision(False, "stake must be a finite decimal")
        if amount <= 0:
            return RiskDecision(False, "stake must be positive")

        state = self._book_state(book)
        if state is None:
            return RiskDecision(False, "virtual bankroll state is invalid")
        initial_bankroll, balance, committed_stake, open_position_count = state

        goal = self.economic_goal
        if goal is not None:
            if goal.emergency_stop:
                return RiskDecision(False, "economic goal emergency stop is active")
            if context is None:
                return RiskDecision(
                    False,
                    "proposed ticket risk context is required for economic goal quote checks",
                )
            if context.bankroll_id is None or context.currency is None:
                return RiskDecision(
                    False,
                    "proposed ticket bankroll and currency identity are required for economic goal",
                )
            if context.bankroll_id != goal.bankroll_id:
                return RiskDecision(
                    False,
                    "proposed ticket bankroll identity does not match economic goal",
                )
            if context.currency != goal.currency:
                return RiskDecision(
                    False,
                    "proposed ticket currency does not match economic goal",
                )
            restriction_decision = self._proposal_restriction_decision(
                book,
                amount,
                goal,
                context,
            )
            if restriction_decision is not None:
                return restriction_decision
            quote_decision = self._quote_risk_decision(goal, context)
            if quote_decision is not None:
                return quote_decision
            if goal.max_stake_amount is not None and amount > goal.max_stake_amount:
                return RiskDecision(False, "ticket exceeds economic goal absolute stake limit")
            if open_position_count >= goal.max_concurrent_positions:
                return RiskDecision(False, "economic goal concurrent position limit exceeded")

            history_rooms = self._goal_history_rooms(book, goal)
            if history_rooms is None:
                return RiskDecision(False, "virtual bankroll risk history is invalid")
            session_room, day_room, drawdown_room, turnover_room = history_rooms
            history_limits = (
                (
                    session_room,
                    "economic goal conservative session loss limit exceeded",
                ),
                (
                    day_room,
                    "economic goal conservative day loss limit exceeded",
                ),
                (drawdown_room, "economic goal drawdown limit exceeded"),
                (turnover_room, "economic goal turnover limit exceeded"),
            )
            for room, reason in history_limits:
                if amount > room:
                    return RiskDecision(False, reason)

            if goal.max_risk_of_ruin < Decimal("1"):
                if context.risk_of_ruin_upper_bound is None:
                    return RiskDecision(
                        False,
                        "portfolio risk-of-ruin evidence is required by economic goal",
                    )
                if context.risk_of_ruin_upper_bound > goal.max_risk_of_ruin:
                    return RiskDecision(
                        False,
                        "portfolio risk-of-ruin upper bound exceeds economic goal limit",
                    )

        derived = self._derived_risk_values(initial_bankroll, balance, committed_stake, amount)
        if derived is None:
            return RiskDecision(False, "virtual bankroll state is invalid")
        ticket_limit, aggregate_committed, committed_limit, remaining_balance, reserve_limit = derived

        if amount > ticket_limit:
            return RiskDecision(False, "ticket exceeds configured bankroll fraction")
        if aggregate_committed > committed_limit:
            return RiskDecision(False, "aggregate committed stake limit exceeded")
        if remaining_balance < reserve_limit:
            return RiskDecision(False, "minimum virtual cash reserve would be violated")
        return RiskDecision(True, "allowed")
