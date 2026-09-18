from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
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
from .economic_goal_provenance import provenance_for
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


def _canonical_sha256(name: str, value: object) -> str:
    digest = _canonical_context_text(name, value)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256 hex digest")
    return digest


def _sha256_payload(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class RiskOfRuinEvidence:
    """Immutable provenance binding for an externally produced ruin upper bound.

    This object does not estimate risk of ruin.  It only binds a research-produced
    bound to the exact causal evidence, paper portfolio, candidate and stake that
    the bound evaluated.  Producing the bound remains governed by the scientific
    protocol in #367.
    """

    evidence_id: str
    research_protocol_sha256: str
    reproducibility_bundle_sha256: str
    producer_identity: str
    causal_cutoff: str
    evaluated_at: str
    bankroll_id: str
    currency: str
    base_portfolio_sha256: str
    candidate_sha256: str
    evaluated_stake: Decimal
    upper_bound: Decimal

    def __post_init__(self) -> None:
        _canonical_context_text("risk-of-ruin evidence_id", self.evidence_id)
        _canonical_context_text(
            "risk-of-ruin producer_identity", self.producer_identity
        )
        _canonical_sha256(
            "risk-of-ruin research_protocol_sha256",
            self.research_protocol_sha256,
        )
        _canonical_sha256(
            "risk-of-ruin reproducibility_bundle_sha256",
            self.reproducibility_bundle_sha256,
        )
        _canonical_sha256(
            "risk-of-ruin base_portfolio_sha256",
            self.base_portfolio_sha256,
        )
        _canonical_sha256(
            "risk-of-ruin candidate_sha256",
            self.candidate_sha256,
        )
        _canonical_context_text("risk-of-ruin bankroll_id", self.bankroll_id)
        currency = _canonical_context_text("risk-of-ruin currency", self.currency)
        if (
            len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or currency != currency.upper()
        ):
            raise ValueError(
                "risk-of-ruin currency must be a three-letter uppercase ASCII code"
            )

        _, cutoff = _canonical_context_timestamp(
            "risk-of-ruin causal_cutoff", self.causal_cutoff
        )
        _, evaluated = _canonical_context_timestamp(
            "risk-of-ruin evaluated_at", self.evaluated_at
        )
        if cutoff > evaluated:
            raise ValueError(
                "risk-of-ruin causal cutoff must not be after evaluation time"
            )

        if (
            not isinstance(self.evaluated_stake, Decimal)
            or not self.evaluated_stake.is_finite()
            or self.evaluated_stake <= 0
        ):
            raise ValueError(
                "risk-of-ruin evaluated_stake must be a positive finite exact Decimal"
            )
        if (
            not isinstance(self.upper_bound, Decimal)
            or not self.upper_bound.is_finite()
            or self.upper_bound < Decimal("0")
            or self.upper_bound > Decimal("1")
        ):
            raise ValueError(
                "risk-of-ruin upper_bound must be an exact Decimal between 0 and 1"
            )


@dataclass(frozen=True, slots=True)
class RiskOfRuinVectorEvidence:
    """Immutable provenance binding for one complete multi-candidate stake vector.

    The witness carries no permission to estimate risk. It binds an externally
    produced research upper bound to the exact base portfolio, ordered candidate
    vector and complete evaluated stake vector. The producer remains governed by
    the scientific/research protocol; this object only verifies executable use.
    """

    evidence_id: str
    research_protocol_sha256: str
    reproducibility_bundle_sha256: str
    producer_identity: str
    causal_cutoff: str
    evaluated_at: str
    bankroll_id: str
    currency: str
    base_portfolio_sha256: str
    candidate_vector_sha256: str
    evaluated_stakes: tuple[Decimal, ...]
    upper_bound: Decimal

    def __post_init__(self) -> None:
        _canonical_context_text("vector risk-of-ruin evidence_id", self.evidence_id)
        _canonical_context_text(
            "vector risk-of-ruin producer_identity", self.producer_identity
        )
        _canonical_sha256(
            "vector risk-of-ruin research_protocol_sha256",
            self.research_protocol_sha256,
        )
        _canonical_sha256(
            "vector risk-of-ruin reproducibility_bundle_sha256",
            self.reproducibility_bundle_sha256,
        )
        _canonical_sha256(
            "vector risk-of-ruin base_portfolio_sha256",
            self.base_portfolio_sha256,
        )
        _canonical_sha256(
            "vector risk-of-ruin candidate_vector_sha256",
            self.candidate_vector_sha256,
        )
        _canonical_context_text("vector risk-of-ruin bankroll_id", self.bankroll_id)
        currency = _canonical_context_text(
            "vector risk-of-ruin currency", self.currency
        )
        if (
            len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or currency != currency.upper()
        ):
            raise ValueError(
                "vector risk-of-ruin currency must be a three-letter uppercase ASCII code"
            )

        _, cutoff = _canonical_context_timestamp(
            "vector risk-of-ruin causal_cutoff", self.causal_cutoff
        )
        _, evaluated = _canonical_context_timestamp(
            "vector risk-of-ruin evaluated_at", self.evaluated_at
        )
        if cutoff > evaluated:
            raise ValueError(
                "vector risk-of-ruin causal cutoff must not be after evaluation time"
            )

        if type(self.evaluated_stakes) is not tuple or not self.evaluated_stakes:
            raise ValueError(
                "vector risk-of-ruin evaluated_stakes must be a non-empty tuple"
            )
        has_positive = False
        for stake in self.evaluated_stakes:
            if (
                not isinstance(stake, Decimal)
                or not stake.is_finite()
                or stake < Decimal("0")
            ):
                raise ValueError(
                    "vector risk-of-ruin evaluated_stakes must contain non-negative finite exact Decimals"
                )
            has_positive = has_positive or stake > 0
        if not has_positive:
            raise ValueError(
                "vector risk-of-ruin evaluated_stakes must contain a positive stake"
            )
        if (
            not isinstance(self.upper_bound, Decimal)
            or not self.upper_bound.is_finite()
            or self.upper_bound < Decimal("0")
            or self.upper_bound > Decimal("1")
        ):
            raise ValueError(
                "vector risk-of-ruin upper_bound must be an exact Decimal between 0 and 1"
            )


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
    Provider concentration additionally requires source-scoped bookmaker account
    identity for every relevant proposal/open ticket; provider-only history is not
    sufficient account-scoped exposure proof. Sport concentration remains fail-closed
    until canonical sport identity has a durable authority.
    """

    legs: tuple[TicketLeg, ...]
    quotes: tuple[MarketEvent, ...] = ()
    provider_accounts: tuple[tuple[str, str], ...] = ()
    bankroll_id: str | None = None
    currency: str | None = None
    measurement_window_start: str | None = None
    measurement_window_end: str | None = None
    proposal_ts: str | None = None
    # Backward-compatible ingress only.  A bare scalar is never authority for a
    # nontrivial ruin ceiling; use risk_of_ruin_evidence for executable evidence.
    risk_of_ruin_upper_bound: Decimal | None = None
    risk_of_ruin_evidence: RiskOfRuinEvidence | None = None

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

        if type(self.provider_accounts) is not tuple:
            raise ValueError("provider_accounts must be a canonical tuple")
        validated_accounts: list[tuple[str, str]] = []
        for binding in self.provider_accounts:
            if type(binding) is not tuple or len(binding) != 2:
                raise ValueError(
                    "provider_accounts must contain (source_id, account_id) tuples"
                )
            source_id, account_id = binding
            validated_accounts.append(
                (
                    _canonical_context_text("provider account source_id", source_id),
                    _canonical_context_text("provider account_id", account_id),
                )
            )
        canonical_accounts = tuple(validated_accounts)
        if (
            canonical_accounts != tuple(sorted(canonical_accounts))
            or len(canonical_accounts) != len(set(canonical_accounts))
        ):
            raise ValueError("provider_accounts must be sorted and unique")
        account_sources = tuple(source_id for source_id, _ in canonical_accounts)
        if len(account_sources) != len(set(account_sources)):
            raise ValueError(
                "provider_accounts may bind at most one account_id per provider source"
            )
        quote_sources = frozenset(quote.source_id for quote in self.quotes)
        if canonical_accounts and frozenset(account_sources) != quote_sources:
            raise ValueError(
                "provider_accounts must cover quote provider sources exactly"
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
        proposal_time: datetime | None = None
        if self.proposal_ts is not None:
            _, proposal_time = _canonical_context_timestamp(
                "proposal_ts", self.proposal_ts
            )

        if self.measurement_window_start is not None:
            _, start = _canonical_context_timestamp(
                "measurement_window_start", self.measurement_window_start
            )
            _, end = _canonical_context_timestamp(
                "measurement_window_end", self.measurement_window_end
            )
            if start > end:
                raise ValueError("measurement window start must not be after end")
            if proposal_time is not None and end > proposal_time:
                raise ValueError("measurement window end must not be after proposal time")

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
        if self.risk_of_ruin_evidence is not None and not isinstance(
            self.risk_of_ruin_evidence, RiskOfRuinEvidence
        ):
            raise ValueError(
                "risk_of_ruin_evidence must be canonical RiskOfRuinEvidence"
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

    @property
    def provider_account_keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(self.provider_accounts)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class StakeVectorDecision:
    """Pure endogenous multi-candidate paper allocation result."""

    action: str
    stakes: tuple[Decimal, ...]
    reason: str

    def __post_init__(self) -> None:
        if self.action not in {"STAKE_VECTOR", "WAIT", "ZERO"}:
            raise ValueError("stake vector action must be STAKE_VECTOR, WAIT or ZERO")
        if type(self.stakes) is not tuple:
            raise ValueError("stake vector stakes must be a tuple")
        for stake in self.stakes:
            if (
                not isinstance(stake, Decimal)
                or not stake.is_finite()
                or stake < Decimal("0")
            ):
                raise ValueError(
                    "stake vector stakes must contain non-negative finite Decimal values"
                )
        has_positive_stake = any(stake > 0 for stake in self.stakes)
        if self.action == "STAKE_VECTOR" and not has_positive_stake:
            raise ValueError("STAKE_VECTOR requires at least one positive stake")
        if self.action != "STAKE_VECTOR" and has_positive_stake:
            raise ValueError("WAIT/ZERO stake vectors must not contain positive stakes")
        if type(self.reason) is not str or not self.reason:
            raise ValueError("stake vector reason must be a non-empty string")


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
    against the whole open stake set. Provider concentration is executable when
    every relevant open ticket carries canonical durable provider/bankroll
    provenance; missing historical provenance fails closed. Sport concentration
    remains fail-closed until canonical sport identity exists.
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

    def provenance_payload(self) -> dict[str, object]:
        """Canonical identity of the exact executable paper-risk authority."""

        goal = self.economic_goal
        return {
            "schema": "autosport.paper_risk_policy_provenance",
            "schema_version": 1,
            "max_ticket_fraction": str(self.max_ticket_fraction),
            "max_committed_fraction": str(self.max_committed_fraction),
            "minimum_cash_reserve_fraction": str(self.minimum_cash_reserve_fraction),
            "economic_goal_contract_sha256": (
                provenance_for(goal).contract_sha256 if goal is not None else None
            ),
        }

    @property
    def provenance_sha256(self) -> str:
        return _sha256_payload(self.provenance_payload())

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
    def risk_of_ruin_portfolio_sha256(cls, book: PaperBook) -> str | None:
        """Hash the exact validated PaperBook state used by ruin evidence."""

        try:
            PaperBook._validate_loaded_state(book)
            tickets: list[dict[str, object]] = []
            for ticket_id in sorted(book.tickets):
                ticket = book.tickets[ticket_id]
                tickets.append(
                    {
                        "ticket_id": ticket.ticket_id,
                        "stake": str(ticket.stake),
                        "placed_at": ticket.placed_at,
                        "settled_at": ticket.settled_at,
                        "status": ticket.status.value,
                        "payout": str(ticket.payout),
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
                                "locked_odds": str(leg.locked_odds),
                            }
                            for leg in ticket.legs
                        ],
                    }
                )
            lifecycle = []
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
            return _sha256_payload(
                {
                    "schema": "autosport.paper-risk-state.v3",
                    "initial_bankroll": str(book.initial_bankroll),
                    "balance": str(book.balance),
                    "tickets": tickets,
                    "lifecycle": lifecycle,
                }
            )
        except (ArithmeticError, AttributeError, TypeError, ValueError):
            return None

    @staticmethod
    def risk_of_ruin_candidate_sha256(
        context: ProposedTicketRiskContext,
    ) -> str | None:
        """Hash causal candidate evidence while deliberately excluding ruin evidence."""

        if not isinstance(context, ProposedTicketRiskContext):
            return None
        try:
            quotes = [
                quote.to_dict()
                for quote in sorted(context.quotes, key=lambda item: item.quote_key)
            ]
            legs = [
                {
                    "event_id": leg.event_id,
                    "market_id": leg.market_id,
                    "selection_id": leg.selection_id,
                    "locked_odds": str(leg.locked_odds),
                }
                for leg in sorted(context.legs, key=lambda item: item.quote_key)
            ]
            return _sha256_payload(
                {
                    "schema": "autosport.risk-candidate.v2",
                    "legs": legs,
                    "quotes": quotes,
                    "provider_accounts": [
                        {"source_id": source_id, "account_id": account_id}
                        for source_id, account_id in context.provider_accounts
                    ],
                    "bankroll_id": context.bankroll_id,
                    "currency": context.currency,
                    "measurement_window_start": context.measurement_window_start,
                    "measurement_window_end": context.measurement_window_end,
                    "proposal_ts": context.proposal_ts,
                }
            )
        except (ArithmeticError, AttributeError, TypeError, ValueError):
            return None

    @classmethod
    def risk_of_ruin_candidate_vector_sha256(
        cls,
        contexts: tuple[ProposedTicketRiskContext, ...],
    ) -> str | None:
        """Hash the complete ordered candidate vector, excluding ruin evidence."""

        if type(contexts) is not tuple or not contexts:
            return None
        candidate_hashes: list[str] = []
        for context in contexts:
            candidate_sha256 = cls.risk_of_ruin_candidate_sha256(context)
            if candidate_sha256 is None:
                return None
            candidate_hashes.append(candidate_sha256)
        try:
            return _sha256_payload(
                {
                    "schema": "autosport.risk-candidate-vector.v1",
                    "candidate_sha256": candidate_hashes,
                }
            )
        except (ArithmeticError, TypeError, ValueError):
            return None

    @classmethod
    def _risk_of_ruin_evidence_decision(
        cls,
        book: PaperBook,
        amount: Decimal,
        goal: EconomicGoalContract,
        context: ProposedTicketRiskContext,
    ) -> RiskDecision | None:
        if goal.max_risk_of_ruin >= Decimal("1"):
            return None

        evidence = context.risk_of_ruin_evidence
        if evidence is None:
            return RiskDecision(
                False,
                "portfolio risk-of-ruin provenance-bound evidence is required by economic goal",
            )
        if evidence.upper_bound > goal.max_risk_of_ruin:
            return RiskDecision(
                False,
                "portfolio risk-of-ruin upper bound exceeds economic goal limit",
            )

        portfolio_sha256 = cls.risk_of_ruin_portfolio_sha256(book)
        candidate_sha256 = cls.risk_of_ruin_candidate_sha256(context)
        if portfolio_sha256 is None or candidate_sha256 is None:
            return RiskDecision(
                False,
                "portfolio risk-of-ruin evidence cannot be verified against canonical state",
            )
        if (
            evidence.bankroll_id != goal.bankroll_id
            or evidence.currency != goal.currency
            or evidence.bankroll_id != context.bankroll_id
            or evidence.currency != context.currency
            or evidence.base_portfolio_sha256 != portfolio_sha256
            or evidence.candidate_sha256 != candidate_sha256
            or evidence.evaluated_stake != amount
        ):
            return RiskDecision(
                False,
                "portfolio risk-of-ruin evidence does not match exact proposal state",
            )

        if context.proposal_ts is None:
            return RiskDecision(
                False,
                "portfolio risk-of-ruin evidence cannot be verified without proposal time",
            )
        try:
            _, proposal_time = _canonical_context_timestamp(
                "proposal_ts", context.proposal_ts
            )
            _, cutoff = _canonical_context_timestamp(
                "risk-of-ruin causal_cutoff", evidence.causal_cutoff
            )
            _, evaluated = _canonical_context_timestamp(
                "risk-of-ruin evaluated_at", evidence.evaluated_at
            )
        except (TypeError, ValueError):
            return RiskDecision(
                False,
                "portfolio risk-of-ruin evidence time provenance is invalid",
            )
        if cutoff > proposal_time or evaluated > proposal_time:
            return RiskDecision(
                False,
                "portfolio risk-of-ruin evidence uses future information",
            )
        return None

    @classmethod
    def _risk_of_ruin_vector_evidence_decision(
        cls,
        book: PaperBook,
        goal: EconomicGoalContract,
        contexts: tuple[ProposedTicketRiskContext, ...],
        stakes: tuple[Decimal, ...],
        evidence: RiskOfRuinVectorEvidence | None,
    ) -> RiskDecision | None:
        if goal.max_risk_of_ruin >= Decimal("1"):
            return None
        if evidence is None:
            return RiskDecision(
                False,
                "multi-candidate portfolio risk-of-ruin requires vector-bound evidence",
            )
        if not isinstance(evidence, RiskOfRuinVectorEvidence):
            return RiskDecision(
                False,
                "multi-candidate portfolio risk-of-ruin vector evidence is invalid",
            )
        if evidence.upper_bound > goal.max_risk_of_ruin:
            return RiskDecision(
                False,
                "portfolio vector risk-of-ruin upper bound exceeds economic goal limit",
            )
        if type(contexts) is not tuple or type(stakes) is not tuple:
            return RiskDecision(
                False,
                "portfolio vector risk-of-ruin evidence cannot be verified against canonical state",
            )

        portfolio_sha256 = cls.risk_of_ruin_portfolio_sha256(book)
        candidate_vector_sha256 = cls.risk_of_ruin_candidate_vector_sha256(contexts)
        if portfolio_sha256 is None or candidate_vector_sha256 is None:
            return RiskDecision(
                False,
                "portfolio vector risk-of-ruin evidence cannot be verified against canonical state",
            )
        if (
            evidence.bankroll_id != goal.bankroll_id
            or evidence.currency != goal.currency
            or evidence.base_portfolio_sha256 != portfolio_sha256
            or evidence.candidate_vector_sha256 != candidate_vector_sha256
            or evidence.evaluated_stakes != stakes
        ):
            return RiskDecision(
                False,
                "portfolio vector risk-of-ruin evidence does not match exact vector state",
            )

        proposal_times: list[datetime] = []
        for context in contexts:
            if (
                context.bankroll_id != goal.bankroll_id
                or context.currency != goal.currency
                or context.proposal_ts is None
            ):
                return RiskDecision(
                    False,
                    "portfolio vector risk-of-ruin evidence cannot be verified without canonical bankroll/currency/proposal time",
                )
            try:
                _, proposal_time = _canonical_context_timestamp(
                    "proposal_ts", context.proposal_ts
                )
            except (TypeError, ValueError):
                return RiskDecision(
                    False,
                    "portfolio vector risk-of-ruin evidence time provenance is invalid",
                )
            proposal_times.append(proposal_time)

        try:
            _, cutoff = _canonical_context_timestamp(
                "vector risk-of-ruin causal_cutoff", evidence.causal_cutoff
            )
            _, evaluated = _canonical_context_timestamp(
                "vector risk-of-ruin evaluated_at", evidence.evaluated_at
            )
        except (TypeError, ValueError):
            return RiskDecision(
                False,
                "portfolio vector risk-of-ruin evidence time provenance is invalid",
            )
        causal_limit = min(proposal_times)
        if cutoff > causal_limit or evaluated > causal_limit:
            return RiskDecision(
                False,
                "portfolio vector risk-of-ruin evidence uses future information",
            )
        return None

    @classmethod
    def _historical_risk_metrics(
        cls,
        book: PaperBook,
        *,
        realized_loss_window: tuple[datetime, datetime] | None = None,
        causal_cutoff: datetime | None = None,
    ) -> _HistoricalRiskMetrics | None:
        """Derive conservative durable risk facts from canonical PaperBook history.

        When a causal realized-loss window is supplied, losses with proven
        settlement-time provenance are counted only inside that inclusive window.
        Legacy settlements without time provenance are still counted, because
        excluding an unknown-time loss could understate risk. Drawdown, turnover
        and current committed exposure remain whole-history/whole-portfolio facts.
        """

        try:
            PaperBook._validate_loaded_state(book)
            if realized_loss_window is not None:
                window_start, window_end = realized_loss_window
                if (
                    not isinstance(window_start, datetime)
                    or not isinstance(window_end, datetime)
                    or window_start.tzinfo is None
                    or window_start.utcoffset() is None
                    or window_end.tzinfo is None
                    or window_end.utcoffset() is None
                    or window_start > window_end
                ):
                    return None
            if causal_cutoff is not None and (
                not isinstance(causal_cutoff, datetime)
                or causal_cutoff.tzinfo is None
                or causal_cutoff.utcoffset() is None
            ):
                return None
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

                    settlement_time: datetime | None = None
                    settlement_witness = book._settlement_times.get(ticket_id)
                    if settlement_witness is not None:
                        _, settlement_time = _canonical_context_timestamp(
                            "settlement settled_at", settlement_witness
                        )
                        if (
                            causal_cutoff is not None
                            and settlement_time > causal_cutoff
                        ):
                            return None

                    include_loss = True
                    if realized_loss_window is not None and settlement_time is not None:
                        window_start, window_end = realized_loss_window
                        include_loss = window_start <= settlement_time <= window_end
                    if loss > 0 and include_loss:
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
        *,
        context: ProposedTicketRiskContext | None = None,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        """Return maximum additional losing stake allowed by durable history."""

        realized_loss_window: tuple[datetime, datetime] | None = None
        causal_cutoff: datetime | None = None
        if context is not None and context.proposal_ts is not None:
            try:
                _, causal_cutoff = _canonical_context_timestamp(
                    "proposal_ts", context.proposal_ts
                )
            except (TypeError, ValueError):
                return None

        if context is not None and context.measurement_window_start is not None:
            if context.measurement_window_end is None:
                return None
            # A bounded loss window can narrow all-history loss only when it is
            # causally anchored to the proposal instant. Without proposal_ts the
            # safe interpretation is the legacy/all-history upper bound.
            if causal_cutoff is not None:
                try:
                    _, window_start = _canonical_context_timestamp(
                        "measurement_window_start", context.measurement_window_start
                    )
                    _, window_end = _canonical_context_timestamp(
                        "measurement_window_end", context.measurement_window_end
                    )
                    if window_end > causal_cutoff:
                        return None
                except (TypeError, ValueError):
                    return None
                realized_loss_window = (window_start, window_end)

        metrics = cls._historical_risk_metrics(
            book,
            realized_loss_window=realized_loss_window,
            causal_cutoff=causal_cutoff,
        )
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
        elif dimension == "provider":
            proposed_identities = context.source_ids
            identity_attribute = None
            if (
                not proposed_identities
                or not context.provider_accounts
                or context.bankroll_id is None
                or context.currency is None
            ):
                return RiskDecision(
                    False,
                    "owner provider concentration limit cannot be proven without "
                    "canonical whole-portfolio exposure evidence",
                )
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
                if dimension == "provider":
                    if (
                        not ticket.provider_source_ids
                        or not ticket.provider_accounts
                        or frozenset(
                            source_id for source_id, _ in ticket.provider_accounts
                        )
                        != frozenset(ticket.provider_source_ids)
                        or ticket.bankroll_id != context.bankroll_id
                        or ticket.currency != context.currency
                    ):
                        return RiskDecision(
                            False,
                            "owner provider concentration limit cannot be proven without "
                            "canonical whole-portfolio exposure evidence",
                        )
                    identities = frozenset(ticket.provider_source_ids)
                else:
                    assert identity_attribute is not None
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
            ("provider", goal.max_provider_concentration_fraction),
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

        # Sport identity still has no canonical durable authority. Never infer it
        # from event/market/provider strings or free-form metadata.
        if goal.max_sport_concentration_fraction < Decimal("1"):
            return RiskDecision(
                False,
                "owner sport concentration limit cannot be proven without "
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
        *,
        context: ProposedTicketRiskContext | None = None,
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

        if context is not None and not isinstance(
            context, ProposedTicketRiskContext
        ):
            return None

        state = self._book_state(book)
        if state is None:
            return None
        initial_bankroll, balance, committed_stake, open_position_count = state
        if goal.emergency_stop or open_position_count >= goal.max_concurrent_positions:
            return None
        # A nontrivial ruin ceiling is executable only with evidence bound to the
        # exact amount derived below, so a bare caller scalar can never authorize.
        if goal.max_risk_of_ruin < Decimal("1") and context is None:
            return None

        history_rooms = self._goal_history_rooms(book, goal, context=context)
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
        if goal.max_risk_of_ruin < Decimal("1"):
            assert context is not None
            if self._risk_of_ruin_evidence_decision(
                book, amount, goal, context
            ) is not None:
                return None
        return amount

    @staticmethod
    def _shadow_book_for_allocation(book: PaperBook) -> PaperBook | None:
        """Clone canonical paper state for pure sequential allocation checks."""
        try:
            PaperBook._validate_loaded_state(book)
            shadow = PaperBook(book.initial_bankroll)
            shadow.balance = book.balance
            shadow.tickets = dict(book.tickets)
            shadow._lifecycle = list(book._lifecycle)
            shadow._settlement_times = dict(book._settlement_times)
            PaperBook._validate_loaded_state(shadow)
        except (ArithmeticError, AttributeError, TypeError, ValueError):
            return None
        return shadow

    @staticmethod
    def _risk_rejection_requires_wait(reason: str) -> bool:
        return any(
            marker in reason
            for marker in (
                " required",
                "cannot be proven",
                "does not match",
                " is invalid",
                " evidence is invalid",
                "quote exceeds economic goal maximum age",
                "quote timestamp is after proposal timestamp",
            )
        )

    def derive_goal_stake_vector(
        self,
        book: PaperBook,
        signal_strengths: tuple[Decimal | str, ...],
        *,
        contexts: tuple[ProposedTicketRiskContext, ...],
        risk_of_ruin_vector_evidence: RiskOfRuinVectorEvidence | None = None,
    ) -> StakeVectorDecision:
        """Derive a pure, whole-portfolio stake vector under one EconomicGoal.

        Positive candidates are evaluated strongest-signal first with a canonical
        quote-key tie-break. Every accepted candidate is opened only in a shadow
        PaperBook, so later candidates see earlier vector exposure for aggregate
        capital, reserve, history, concurrency and event/market concentration
        limits. The caller's real PaperBook is never mutated.

        Missing or contradictory canonical evidence returns WAIT with an all-zero
        vector. A complete candidate set with no executable positive allocation
        returns ZERO. This is deliberately a risk/allocation primitive only;
        Generic Opportunity/PortfolioPlan orchestration remains outside this API.
        """

        context_count = len(contexts) if type(contexts) is tuple else 0
        zero_vector = tuple(Decimal("0") for _ in range(context_count))
        goal = self.economic_goal
        if goal is None:
            return StakeVectorDecision(
                "WAIT",
                zero_vector,
                "economic goal is required for endogenous stake-vector allocation",
            )
        if type(contexts) is not tuple or type(signal_strengths) is not tuple:
            return StakeVectorDecision(
                "WAIT",
                zero_vector,
                "candidate vector shape is invalid",
            )
        if len(contexts) != len(signal_strengths):
            return StakeVectorDecision(
                "WAIT",
                zero_vector,
                "candidate signal/context cardinality does not match",
            )
        if not contexts:
            return StakeVectorDecision("ZERO", (), "candidate set is empty")
        if any(not isinstance(context, ProposedTicketRiskContext) for context in contexts):
            return StakeVectorDecision(
                "WAIT",
                zero_vector,
                "candidate risk context is invalid",
            )

        candidate_identities = [
            tuple(sorted(leg.quote_key for leg in context.legs))
            for context in contexts
        ]
        if len(candidate_identities) != len(set(candidate_identities)):
            return StakeVectorDecision(
                "WAIT",
                zero_vector,
                "candidate set contains duplicate or ambiguous executable identity",
            )

        parsed_signals: list[Decimal] = []
        for raw_signal in signal_strengths:
            try:
                signal = Decimal(str(raw_signal))
            except (InvalidOperation, TypeError, ValueError):
                return StakeVectorDecision(
                    "WAIT",
                    zero_vector,
                    "candidate signal evidence is invalid",
                )
            if not signal.is_finite():
                return StakeVectorDecision(
                    "WAIT",
                    zero_vector,
                    "candidate signal evidence is invalid",
                )
            parsed_signals.append(signal)

        if goal.emergency_stop:
            return StakeVectorDecision(
                "ZERO",
                zero_vector,
                "economic goal emergency stop is active",
            )
        positive_indices = [
            index for index, signal in enumerate(parsed_signals) if signal > 0
        ]
        if not positive_indices:
            return StakeVectorDecision(
                "ZERO",
                zero_vector,
                "candidate set contains no positive signal",
            )
        vector_ruin_required = (
            goal.max_risk_of_ruin < Decimal("1") and len(positive_indices) > 1
        )
        if vector_ruin_required and risk_of_ruin_vector_evidence is None:
            return StakeVectorDecision(
                "WAIT",
                zero_vector,
                "multi-candidate portfolio risk-of-ruin requires vector-bound evidence",
            )
        if (
            risk_of_ruin_vector_evidence is not None
            and not isinstance(
                risk_of_ruin_vector_evidence, RiskOfRuinVectorEvidence
            )
        ):
            return StakeVectorDecision(
                "WAIT",
                zero_vector,
                "multi-candidate portfolio risk-of-ruin vector evidence is invalid",
            )

        for index in positive_indices:
            context = contexts[index]
            if (
                context.bankroll_id is None
                or context.currency is None
                or context.bankroll_id != goal.bankroll_id
                or context.currency != goal.currency
                or not context.quotes
                or context.proposal_ts is None
            ):
                return StakeVectorDecision(
                    "WAIT",
                    zero_vector,
                    "candidate set lacks canonical bankroll/currency/quote/time evidence",
                )
            if (
                goal.max_risk_of_ruin < Decimal("1")
                and not vector_ruin_required
                and context.risk_of_ruin_evidence is None
            ):
                return StakeVectorDecision(
                    "WAIT",
                    zero_vector,
                    "candidate set lacks provenance-bound portfolio risk-of-ruin evidence",
                )

        allocation_policy = self
        if vector_ruin_required:
            allocation_policy = replace(
                self,
                economic_goal=replace(goal, max_risk_of_ruin=Decimal("1")),
            )

        shadow = allocation_policy._shadow_book_for_allocation(book)
        if shadow is None:
            return StakeVectorDecision(
                "WAIT",
                zero_vector,
                "virtual bankroll allocation state is invalid",
            )

        def candidate_key(index: int) -> tuple[Decimal, tuple[str, ...], int]:
            quote_keys = tuple(leg.quote_key for leg in contexts[index].legs)
            return (-parsed_signals[index], quote_keys, index)

        stakes = [Decimal("0") for _ in contexts]
        for index in sorted(positive_indices, key=candidate_key):
            context = contexts[index]
            amount = allocation_policy.derive_goal_stake(
                shadow,
                parsed_signals[index],
                context=context,
            )
            if amount is None:
                continue
            decision = allocation_policy.evaluate(shadow, amount, context=context)
            if not decision.allowed:
                if self._risk_rejection_requires_wait(decision.reason):
                    return StakeVectorDecision(
                        "WAIT",
                        zero_vector,
                        f"candidate evidence is incomplete: {decision.reason}",
                    )
                continue

            try:
                shadow.open_ticket(
                    context.legs,
                    amount,
                    reason=f"risk-vector-reservation:{index}",
                    placed_at=context.proposal_ts,
                    provider_source_ids=tuple(sorted(context.source_ids)),
                    provider_accounts=context.provider_accounts,
                    bankroll_id=context.bankroll_id,
                    currency=context.currency,
                )
            except (ArithmeticError, AttributeError, TypeError, ValueError):
                return StakeVectorDecision(
                    "WAIT",
                    zero_vector,
                    "virtual bankroll allocation shadow failed closed",
                )
            stakes[index] = amount

        result = tuple(stakes)
        if any(stake > 0 for stake in result):
            if vector_ruin_required:
                vector_ruin_decision = self._risk_of_ruin_vector_evidence_decision(
                    book,
                    goal,
                    contexts,
                    result,
                    risk_of_ruin_vector_evidence,
                )
                if vector_ruin_decision is not None:
                    action = (
                        "ZERO"
                        if vector_ruin_decision.reason
                        == "portfolio vector risk-of-ruin upper bound exceeds economic goal limit"
                        else "WAIT"
                    )
                    return StakeVectorDecision(
                        action,
                        zero_vector,
                        vector_ruin_decision.reason,
                    )
            return StakeVectorDecision(
                "STAKE_VECTOR",
                result,
                "endogenous whole-portfolio stake vector derived",
            )
        return StakeVectorDecision(
            "ZERO",
            zero_vector,
            "no candidate fits the current economic-goal risk envelope",
        )

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
            if goal.max_stake_amount is not None and amount > goal.max_stake_amount:
                return RiskDecision(False, "ticket exceeds economic goal absolute stake limit")
            if open_position_count >= goal.max_concurrent_positions:
                return RiskDecision(False, "economic goal concurrent position limit exceeded")

            history_rooms = self._goal_history_rooms(book, goal, context=context)
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

            quote_decision = self._quote_risk_decision(goal, context)
            if quote_decision is not None:
                return quote_decision

            if goal.max_risk_of_ruin < Decimal("1"):
                ruin_decision = self._risk_of_ruin_evidence_decision(
                    book, amount, goal, context
                )
                if ruin_decision is not None:
                    return ruin_decision

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
