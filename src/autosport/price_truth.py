from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Iterable

from .domain import MarketEvent


_BETFAIR_HISTORICAL_SOURCE_ID = "betfair_exchange_historical"
_NON_EXECUTABLE_PRICE_SEMANTICS = frozenset(
    {
        "betfair_last_traded_price",
        "legacy_betfair_last_traded_price_unverified",
        "betfair_available_to_back_unavailable",
        "betfair_market_definition_state_transition",
        "betfair_runner_roster_removed",
    }
)


@dataclass(frozen=True, slots=True)
class MarketPriceTruth:
    """Machine-readable boundary between research observations and executable prices."""

    price_semantics: str
    executable_quote_verified: bool
    paper_fill_fidelity_verified: bool
    source_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_ids"] = list(self.source_ids)
        return payload


def _normalise_source_ids(
    source_ids: Iterable[object],
    *,
    field: str = "source_ids",
) -> tuple[str, ...]:
    """Canonicalize source identity ordering without coercing or trimming identity bytes."""

    if isinstance(source_ids, (str, bytes)):
        raise ValueError(f"{field} must be an iterable of canonical source-id strings")
    normalized: set[str] = set()
    for value in source_ids:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{field} must contain non-empty trimmed strings")
        normalized.add(value)
    return tuple(sorted(normalized))


def _validate_semantic_execution_pair(price_semantics: str, executable_quote_verified: bool) -> None:
    if price_semantics in _NON_EXECUTABLE_PRICE_SEMANTICS and executable_quote_verified:
        raise ValueError(f"{price_semantics} is observational and cannot be executable-quote verified")


def _governed_source_ids(payload: dict[str, Any]) -> tuple[str, ...] | None:
    governance = payload.get("dataset_governance")
    if not isinstance(governance, dict):
        return None
    raw = governance.get("source_ids")
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise ValueError("dataset_governance.source_ids is malformed")
    try:
        return _normalise_source_ids(raw, field="dataset_governance.source_ids")
    except ValueError as exc:
        raise ValueError("dataset_governance.source_ids is malformed") from exc


def classify_market_price_truth(
    source_ids: Iterable[str],
    *,
    price_semantics: str | None = None,
    execution_quote_verified: bool | None = None,
) -> MarketPriceTruth:
    """Classify price truth, preferring explicit canonical semantics over provider identity.

    Provider identity is only a conservative legacy fallback. New governed datasets must
    carry strategy-visible price semantics on the canonical market events themselves so
    two data products from the same provider can truthfully expose different price types.
    An offered quote still does not prove a paper fill at that quote, so fill fidelity
    remains false unless a separate execution/fill model is ever verified.
    """

    normalized = _normalise_source_ids(source_ids)
    if price_semantics is not None:
        if not isinstance(price_semantics, str) or not price_semantics.strip():
            raise ValueError("price_semantics must be a non-empty string when explicit")
        if not isinstance(execution_quote_verified, bool):
            raise ValueError("explicit price semantics require boolean execution_quote_verified")
        semantics = price_semantics.strip()
        _validate_semantic_execution_pair(semantics, execution_quote_verified)
        return MarketPriceTruth(
            price_semantics=semantics,
            executable_quote_verified=execution_quote_verified,
            paper_fill_fidelity_verified=False,
            source_ids=normalized,
        )

    if normalized == (_BETFAIR_HISTORICAL_SOURCE_ID,):
        return MarketPriceTruth(
            price_semantics="legacy_betfair_last_traded_price_unverified",
            executable_quote_verified=False,
            paper_fill_fidelity_verified=False,
            source_ids=normalized,
        )
    return MarketPriceTruth(
        price_semantics="unspecified_or_mixed_observation",
        executable_quote_verified=False,
        paper_fill_fidelity_verified=False,
        source_ids=normalized,
    )


def market_price_truth_from_events(events: Iterable[Any]) -> MarketPriceTruth:
    """Derive truth from canonical event metadata without assuming provider semantics."""

    materialized = tuple(events)
    source_ids = _normalise_source_ids(
        (getattr(event, "source_id", "") for event in materialized),
        field="event source_ids",
    )
    if not materialized:
        return classify_market_price_truth(source_ids)

    explicit_truth: set[tuple[str, bool]] = set()
    for event in materialized:
        metadata = getattr(event, "metadata", None)
        if not isinstance(metadata, dict):
            return MarketPriceTruth(
                price_semantics="unspecified_or_mixed_observation",
                executable_quote_verified=False,
                paper_fill_fidelity_verified=False,
                source_ids=source_ids,
            )
        semantics = metadata.get("price_semantics")
        executable = metadata.get("execution_quote_verified")
        if not isinstance(semantics, str) or not semantics.strip() or not isinstance(executable, bool):
            return MarketPriceTruth(
                price_semantics="unspecified_or_mixed_observation",
                executable_quote_verified=False,
                paper_fill_fidelity_verified=False,
                source_ids=source_ids,
            )
        explicit_truth.add((semantics.strip(), executable))

    if len(explicit_truth) != 1:
        return MarketPriceTruth(
            price_semantics="unspecified_or_mixed_observation",
            executable_quote_verified=False,
            paper_fill_fidelity_verified=False,
            source_ids=source_ids,
        )

    semantics, executable = next(iter(explicit_truth))
    return classify_market_price_truth(
        source_ids,
        price_semantics=semantics,
        execution_quote_verified=executable,
    )


def market_price_truth_from_run_summary(payload: dict[str, Any]) -> MarketPriceTruth:
    """Load explicit run truth, falling back conservatively for older summaries."""

    governed_source_ids = _governed_source_ids(payload)
    explicit = payload.get("market_price_truth")
    dataset_schema_version = payload.get("dataset_schema_version")
    if (
        explicit is not None
        and isinstance(dataset_schema_version, int)
        and not isinstance(dataset_schema_version, bool)
        and dataset_schema_version >= 2
        and governed_source_ids is None
    ):
        raise ValueError(
            "explicit market_price_truth for schema-v2 data requires dataset_governance.source_ids"
        )
    if explicit is not None:
        if not isinstance(explicit, dict):
            raise ValueError("market_price_truth must be an object")
        semantics = explicit.get("price_semantics")
        executable = explicit.get("executable_quote_verified")
        fill_fidelity = explicit.get("paper_fill_fidelity_verified")
        source_ids = explicit.get("source_ids")
        if not (
            isinstance(semantics, str)
            and semantics.strip()
            and isinstance(executable, bool)
            and isinstance(fill_fidelity, bool)
            and isinstance(source_ids, list)
        ):
            raise ValueError("market_price_truth is malformed")
        if fill_fidelity:
            raise ValueError(
                "paper fill fidelity cannot be verified by market price truth without independent fill evidence"
            )
        try:
            normalized_source_ids = _normalise_source_ids(
                source_ids,
                field="market_price_truth.source_ids",
            )
        except ValueError as exc:
            raise ValueError("market_price_truth is malformed") from exc
        if governed_source_ids is not None and normalized_source_ids != governed_source_ids:
            raise ValueError("market_price_truth.source_ids do not match dataset_governance.source_ids")
        return classify_market_price_truth(
            normalized_source_ids,
            price_semantics=semantics.strip(),
            execution_quote_verified=executable,
        )

    if governed_source_ids is not None:
        return classify_market_price_truth(governed_source_ids)
    return classify_market_price_truth(())


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
