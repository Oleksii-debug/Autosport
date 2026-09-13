from __future__ import annotations

from decimal import Decimal

from .domain import MarketEvent


def paper_quote_rejection_reason(event: MarketEvent, stake: Decimal | str) -> str | None:
    """Return a fail-closed reason when a market event cannot support a paper fill.

    Legacy fixtures/providers without explicit execution metadata keep their historical
    behavior. Once a provider makes an explicit execution/capacity assertion, every
    declared negative or malformed assertion is binding.

    Autosport does not currently give ``PaperBook`` stake a canonical currency/unit
    identity. Provider ladder size therefore cannot be dimensionally compared with a
    paper stake, even when both are finite decimals. A future implementation may add a
    typed, provenance-bound unit contract; until then explicit capacity claims fail
    closed rather than silently authorizing paper economics.
    """

    del stake  # No numerical comparison is truthful without a canonical stake unit.
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
    if capacity_flag is True:
        return "paper fill capacity unit is not canonically bound to the paper stake unit"

    # Legacy providers/fixtures with no explicit capacity assertion retain their
    # historical behavior. The fail-closed rule above applies as soon as the provider
    # opts into explicit execution/capacity metadata.
    return None
