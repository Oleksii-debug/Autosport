from __future__ import annotations

"""Fresh re-resolution verifier for Betfair standard-LIMIT price-bound records.

``BetfairStandardLimitPriceBoundEvidence`` is a Python value record, not an
unforgeable capability: Python callers can bypass a raising ``__init__`` with
``object.__new__``/``object.__setattr__``.  Positive product authority therefore
must not depend on object origin.  This module makes acceptance derive again from
the canonical bound plan, action identity, and production ``place_action`` request
shape, then exact-compares the supplied record and returns the freshly resolved
canonical record for downstream use.
"""

from decimal import Decimal

from .betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
    resolve_betfair_standard_limit_price_bound,
)
from .supervised_execution import BoundSupervisedExecutionPlan


_EVIDENCE_FIELDS = (
    "execution_plan_id",
    "execution_plan_sha256",
    "portfolio_plan_sha256",
    "intent_id",
    "intent_sha256",
    "action_id",
    "bookmaker_id",
    "account_id",
    "event_id",
    "market_id",
    "selection_id",
    "side",
    "requested_stake",
    "price_floor_odds",
    "quote_id",
    "quote_observed_at",
    "quote_expires_at",
    "decision_at",
    "instruction_sha256",
    "provider_contract_id",
    "provider_contract_ref",
    "write_adapter_id",
    "write_adapter_version",
    "status",
    "zero_adverse_price_deterioration",
    "execution_feasibility_proven",
    "realized_price_exact",
)


def _exact_snapshot(
    evidence: BetfairStandardLimitPriceBoundEvidence,
) -> tuple[tuple[str, type[object], object], ...]:
    if type(evidence) is not BetfairStandardLimitPriceBoundEvidence:
        raise BetfairStandardLimitPriceBoundError(
            "price-bound evidence must be the exact canonical evidence type"
        )

    snapshot: list[tuple[str, type[object], object]] = []
    for field in _EVIDENCE_FIELDS:
        try:
            value = object.__getattribute__(evidence, field)
        except (AttributeError, TypeError) as exc:
            raise BetfairStandardLimitPriceBoundError(
                "price-bound evidence is incomplete"
            ) from exc
        comparable: object = str(value) if type(value) is Decimal else value
        snapshot.append((field, type(value), comparable))
    return tuple(snapshot)


def verify_betfair_standard_limit_price_bound(
    *,
    evidence: BetfairStandardLimitPriceBoundEvidence,
    bound: BoundSupervisedExecutionPlan,
    action_id: str,
) -> BetfairStandardLimitPriceBoundEvidence:
    """Accept a candidate record only by fresh canonical re-resolution.

    The supplied record is never the trust root.  A fresh canonical resolver run
    re-validates the exact bound/action and captures the current production
    ``place_action`` request using the existing fail-before-I/O transport.  Every
    authority-bearing field is then exact-compared (including Decimal textual
    representation), and the fresh canonical record is returned.  Downstream
    code must use that returned record rather than retaining the caller object.
    """

    expected = resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action_id,
    )
    if _exact_snapshot(evidence) != _exact_snapshot(expected):
        raise BetfairStandardLimitPriceBoundError(
            "price-bound evidence does not match fresh canonical re-resolution"
        )
    return expected
