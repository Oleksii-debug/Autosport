import pytest

from autosport.betfair_marketbook_request_budget import (
    MarketBookBudgetError,
    MarketBookRequestBudget,
)


def _tamper(request: MarketBookRequestBudget, field: str, value: object) -> MarketBookRequestBudget:
    """Model constructor-bypass/deserialized state crossing a later trust boundary."""

    object.__setattr__(request, field, value)
    return request


def test_unknown_price_data_cannot_become_allowed_after_post_init_tamper():
    request = MarketBookRequestBudget(("1.1",), ("EX_BEST_OFFERS",))
    _tamper(request, "price_data", ("EX_UNKNOWN",))

    with pytest.raises(MarketBookBudgetError):
        _ = request.allowed


def test_empty_market_scope_cannot_become_allowed_after_post_init_tamper():
    request = MarketBookRequestBudget(("1.1",), ("EX_BEST_OFFERS",))
    _tamper(request, "market_ids", ())

    with pytest.raises(MarketBookBudgetError):
        _ = request.allowed
