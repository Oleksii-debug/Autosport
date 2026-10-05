"""Bind canonical market semantics into the existing PaperRiskPolicy identity roots.

This is a pre-seal composition repair, not a second risk engine or authority.  The
package imports it after the raw :mod:`autosport.risk` definitions exist but before
generation/private wrappers and the final PaperRiskPolicy root seal snapshot those
functions.  It therefore extends the one canonical risk graph with the durable
``market_semantics_id`` introduced by the PAPER schema-8 lineage.

The repair is deliberately negative-authority only: it changes identity/evidence
binding and rejects mismatched market semantics.  It does not estimate ruin risk,
issue risk evidence, widen an EconomicGoalContract, or create execution permission.
"""

from __future__ import annotations

from types import FunctionType

from . import domain as _domain
from . import paper as _paper
from . import risk as _risk


_POLICY_TYPE = _risk.PaperRiskPolicy
_CONTEXT_TYPE = _risk.ProposedTicketRiskContext
_LEG_TYPE = _domain.TicketLeg
_QUOTE_TYPE = _domain.MarketEvent
_BOOK_TYPE = _paper.PaperBook
_TICKET_STATUS_TYPE = _domain.TicketStatus
_CANONICAL_SHA256_PAYLOAD = _risk._sha256_payload
_CANONICAL_CONTEXT_VALIDATOR = _CONTEXT_TYPE.__post_init__
_CANONICAL_PROPOSED_LEG_VALIDATOR = _risk._validate_proposed_ticket_leg
_CANONICAL_MARKET_SETTLEMENT_KEY = _domain._CANONICAL_MARKET_SETTLEMENT_KEY
_CANONICAL_SEMANTIC_IDENTITY = _domain._CANONICAL_SEMANTIC_IDENTITY

_policy_namespace = vars(_POLICY_TYPE)
_original_quote_descriptor = _policy_namespace.get("_quote_risk_decision")
if (
    type(_original_quote_descriptor) is not staticmethod
    or type(_original_quote_descriptor.__func__) is not FunctionType
):
    raise RuntimeError("canonical PaperRiskPolicy quote-risk root is unavailable")
_CANONICAL_QUOTE_RISK_DECISION = _original_quote_descriptor.__func__


def _leg_settlement_key(
    leg: object,
    _leg_type=_LEG_TYPE,
    _leg_validator=_CANONICAL_PROPOSED_LEG_VALIDATOR,
    _semantic_identity=_CANONICAL_SEMANTIC_IDENTITY,
    _settlement_key=_CANONICAL_MARKET_SETTLEMENT_KEY,
) -> str:
    if type(leg) is not _leg_type:
        raise ValueError("proposal leg must be an exact TicketLeg")
    _leg_validator(leg)
    semantics = leg.market_semantics_id
    if semantics is not None:
        _semantic_identity(semantics, "leg market_semantics_id")
    return _settlement_key(leg.quote_key, semantics)


def _quote_settlement_key(
    quote: object,
    _quote_type=_QUOTE_TYPE,
    _semantic_identity=_CANONICAL_SEMANTIC_IDENTITY,
    _settlement_key=_CANONICAL_MARKET_SETTLEMENT_KEY,
) -> str:
    if type(quote) is not _quote_type:
        raise ValueError("proposal quote must be an exact MarketEvent")
    # Re-run the canonical serializer/parser contract so object.__setattr__ changes
    # after context construction cannot smuggle noncanonical quote fields into a
    # money-moving decision.
    rebuilt = _quote_type.from_dict(quote.to_dict())
    if rebuilt != quote:
        raise ValueError("proposal quote is non-canonical")
    semantics = quote.market_semantics_id
    if semantics is not None:
        _semantic_identity(semantics, "quote market_semantics_id")
    return _settlement_key(quote.quote_key, semantics)


def _validate_context_market_semantics(
    context: object,
    _context_type=_CONTEXT_TYPE,
    _base_validator=_CANONICAL_CONTEXT_VALIDATOR,
    _leg_key=_leg_settlement_key,
    _quote_key=_quote_settlement_key,
) -> None:
    if type(context) is not _context_type:
        raise ValueError("risk context must be canonical ProposedTicketRiskContext")

    # The source validator owns every pre-existing context invariant.  This layer
    # adds only the new settlement-semantic relation and intentionally delegates
    # all other schema/economic checks back to that canonical implementation.
    _base_validator(context)

    leg_keys = tuple(_leg_key(leg) for leg in context.legs)
    if len(leg_keys) != len(set(leg_keys)):
        raise ValueError("proposed ticket contains duplicate settlement identity")

    quote_keys = tuple(_quote_key(quote) for quote in context.quotes)
    if len(quote_keys) != len(set(quote_keys)):
        raise ValueError("proposed ticket contains duplicate quote settlement identity")
    if quote_keys and set(quote_keys) != set(leg_keys):
        raise ValueError(
            "proposal quote market semantics must match proposed legs exactly"
        )


def _context_post_init(
    self: object,
    _validator=_validate_context_market_semantics,
) -> None:
    _validator(self)


def _portfolio_payload(
    book: object,
    _book_type=_BOOK_TYPE,
    _ticket_status_type=_TICKET_STATUS_TYPE,
) -> dict[str, object]:
    if type(book) is not _book_type:
        raise ValueError("risk portfolio requires exact PaperBook")
    _book_type._validate_loaded_state(book)

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
                        "sport": leg.sport,
                        "exchange_side": leg.exchange_side,
                        "market_semantics_id": leg.market_semantics_id,
                    }
                    for leg in ticket.legs
                ],
            }
        )

    lifecycle: list[dict[str, object]] = []
    for raw_entry in book._lifecycle:
        action, ticket_id, winners, voids = _book_type._validate_lifecycle_entry(
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

    return {
        "schema": "autosport.paper-risk-state.v5",
        "initial_bankroll": str(book.initial_bankroll),
        "balance": str(book.balance),
        "tickets": tickets,
        "lifecycle": lifecycle,
    }


def _risk_of_ruin_portfolio_sha256(
    cls: type,
    book: object,
    _policy_type=_POLICY_TYPE,
    _payload_builder=_portfolio_payload,
    _digest=_CANONICAL_SHA256_PAYLOAD,
) -> str | None:
    if cls is not _policy_type or type(book) is not _BOOK_TYPE:
        return None
    try:
        return _digest(_payload_builder(book))
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return None


def _candidate_payload(
    context: object,
    _context_type=_CONTEXT_TYPE,
    _validator=_validate_context_market_semantics,
    _leg_key=_leg_settlement_key,
    _quote_key=_quote_settlement_key,
) -> dict[str, object]:
    if type(context) is not _context_type:
        raise ValueError("risk candidate requires canonical ProposedTicketRiskContext")
    _validator(context)

    quote_pairs = sorted(
        ((_quote_key(quote), quote) for quote in context.quotes),
        key=lambda item: item[0],
    )
    leg_pairs = sorted(
        ((_leg_key(leg), leg) for leg in context.legs),
        key=lambda item: item[0],
    )

    return {
        "schema": "autosport.risk-candidate.v4",
        "legs": [
            {
                "event_id": leg.event_id,
                "market_id": leg.market_id,
                "selection_id": leg.selection_id,
                "locked_odds": str(leg.locked_odds),
                "sport": leg.sport,
                "exchange_side": leg.exchange_side,
                "market_semantics_id": leg.market_semantics_id,
            }
            for _, leg in leg_pairs
        ],
        "quotes": [quote.to_dict() for _, quote in quote_pairs],
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


def _risk_of_ruin_candidate_sha256(
    context: object,
    _context_type=_CONTEXT_TYPE,
    _payload_builder=_candidate_payload,
    _digest=_CANONICAL_SHA256_PAYLOAD,
) -> str | None:
    if type(context) is not _context_type:
        return None
    try:
        return _digest(_payload_builder(context))
    except (ArithmeticError, AttributeError, TypeError, ValueError):
        return None


def _quote_risk_decision(
    goal: object,
    context: object,
    _validator=_validate_context_market_semantics,
    _delegate=_CANONICAL_QUOTE_RISK_DECISION,
    _decision_type=_risk.RiskDecision,
):
    try:
        _validator(context)
    except (AttributeError, TypeError, ValueError):
        return _decision_type(False, "proposed ticket quote risk evidence is invalid")
    return _delegate(goal, context)


def _install() -> None:
    # ProposedTicketRiskContext remains the one source-defined type.  Extend its
    # post-init validation rather than introducing a parallel context schema.
    _CONTEXT_TYPE.__post_init__ = _context_post_init

    setattr(
        _POLICY_TYPE,
        "risk_of_ruin_portfolio_sha256",
        classmethod(_risk_of_ruin_portfolio_sha256),
    )
    setattr(
        _POLICY_TYPE,
        "risk_of_ruin_candidate_sha256",
        staticmethod(_risk_of_ruin_candidate_sha256),
    )
    setattr(
        _POLICY_TYPE,
        "_quote_risk_decision",
        staticmethod(_quote_risk_decision),
    )

    quote_descriptor = vars(_POLICY_TYPE).get("_quote_risk_decision")
    if (
        type(quote_descriptor) is not staticmethod
        or quote_descriptor.__func__ is not _quote_risk_decision
    ):
        raise RuntimeError("market-semantics quote-risk composition failed")

    witnesses = _risk._PAPER_RISK_EVALUATE_HELPER_WITNESSES
    if type(witnesses) is not tuple:
        raise RuntimeError("canonical PaperRiskPolicy evaluate witnesses are unavailable")
    replaced = False
    rebuilt: list[tuple[object, ...]] = []
    for witness in witnesses:
        if witness[0] != "_quote_risk_decision":
            rebuilt.append(witness)
            continue
        rebuilt.append(
            (
                "_quote_risk_decision",
                quote_descriptor,
                _quote_risk_decision,
                _quote_risk_decision.__code__,
                True,
            )
        )
        replaced = True
    if not replaced:
        raise RuntimeError("canonical PaperRiskPolicy quote-risk witness is unavailable")
    _risk._PAPER_RISK_EVALUATE_HELPER_WITNESSES = tuple(rebuilt)


_install()
del _install
del _policy_namespace
del _original_quote_descriptor
