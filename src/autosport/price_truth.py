from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .domain import MarketEvent


def paper_quote_rejection_reason(event: MarketEvent, stake: Decimal | str) -> str | None:
    """Return a fail-closed reason when a market event cannot support a paper fill.

    Legacy fixtures/providers without explicit execution metadata keep their historical
    behavior.  Once a provider makes an explicit execution/capacity assertion, every
    declared negative or malformed assertion is binding.
    """

    metadata = event.metadata
    if metadata.get("execution_quote_verified") is False:
        return "price evidence is not a verified executable quote"
    if metadata.get("paper_fill_eligible") is False:
        reason = metadata.get("paper_fill_eligibility_reason")
        if isinstance(reason, str) and reason.strip():
            return f"paper fill is not eligible: {reason.strip()}"
        return "paper fill is not eligible for this quote"

    capacity_flag = metadata.get("paper_fill_capacity_verified")
    if capacity_flag is False:
        return "paper fill capacity is not verified"
    if capacity_flag is not True:
        return None

    raw_capacity = metadata.get("paper_fill_available_size")
    if raw_capacity is None:
        return "verified paper fill capacity is missing available size"
    try:
        capacity = Decimal(str(raw_capacity))
        amount = Decimal(str(stake))
    except (InvalidOperation, ValueError):
        return "paper fill capacity or stake is not a valid decimal"
    if not capacity.is_finite() or capacity < 0:
        return "paper fill capacity is not a finite non-negative value"
    if not amount.is_finite() or amount <= 0:
        return "paper stake is not a finite positive value"
    if amount > capacity:
        return "paper stake exceeds observed available-to-back size"
    return None
