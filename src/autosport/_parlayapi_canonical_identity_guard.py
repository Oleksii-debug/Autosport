"""Fail closed when Parlay provider labels cannot map to one canonical identity.

The raw Parlay adapter remains the source parser.  This product-load guard validates
provider-owned identity witnesses before any quote from a snapshot can escape:
bookmaker display titles never mint market identity, repeated provider event ids must
retain the same declared event witnesses, and an exact event/market/selection tuple
may occur only once in a snapshot.  Outcome names remain provider-declared selection
tokens because this feed does not expose a stronger selection id; uniqueness is
therefore enforced inside the exact canonical provider market context.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from . import parlayapi_provider as provider
from .providers import ProviderQuote


_ORIGINAL_SNAPSHOT_QUOTES = provider.ParlayApiTableTennisProvider._snapshot_quotes


def _declared_text(event: dict[str, Any], field: str) -> str | None:
    if field not in event or event[field] is None:
        return None
    value = event[field]
    if not isinstance(value, str):
        raise provider.ProviderPayloadError(f"event {field} must be a string")
    if not value or value != value.strip():
        raise provider.ProviderPayloadError(
            f"event {field} must be a non-empty trimmed string"
        )
    return value


def _event_id(event: dict[str, Any]) -> str:
    if "id" in event:
        raw_event_id = event["id"]
    elif "canonical_event_id" in event:
        raw_event_id = event["canonical_event_id"]
    else:
        raise provider.ProviderPayloadError("event is missing id")
    return provider._provider_identity(  # noqa: SLF001 - product guard over owning adapter
        raw_event_id,
        field="event id",
        allow_colon=False,
    )


def _canonical_commence_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise provider.ProviderPayloadError(
            "event commence_time must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise provider.ProviderPayloadError(
            "event commence_time must include a timezone offset"
        )
    try:
        canonical = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise provider.ProviderPayloadError(
            "event commence_time must resolve to a valid UTC instant"
        ) from exc
    return canonical.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _event_binding(event: dict[str, Any], expected_sport: str) -> tuple[str | None, ...]:
    sport_key = _declared_text(event, "sport_key")
    if sport_key is not None and sport_key != expected_sport:
        raise provider.ProviderPayloadError("event sport_key conflicts with provider sport")

    commence_time = _declared_text(event, "commence_time")
    if commence_time is not None:
        commence_time = _canonical_commence_time(commence_time)

    home_team = _declared_text(event, "home_team")
    away_team = _declared_text(event, "away_team")
    if (home_team is None) != (away_team is None):
        raise provider.ProviderPayloadError(
            "event participant identity must declare both home_team and away_team"
        )
    if home_team is not None and home_team == away_team:
        raise provider.ProviderPayloadError("event participants must be distinct")
    return sport_key, commence_time, home_team, away_team


def _validate_stable_bookmaker_keys(events: list[dict[str, Any]]) -> None:
    for event in events:
        bookmakers = event.get("bookmakers", [])
        if not isinstance(bookmakers, list):
            raise provider.ProviderPayloadError("event bookmakers must be a list")
        for bookmaker in bookmakers:
            if not isinstance(bookmaker, dict):
                raise provider.ProviderPayloadError("bookmaker entries must be objects")
            if "key" not in bookmaker:
                raise provider.ProviderPayloadError(
                    "bookmaker is missing stable provider key identity"
                )
            provider._provider_identity(  # noqa: SLF001
                bookmaker["key"],
                field="bookmaker key",
            )


def _strict_snapshot_quotes(
    self: provider.ParlayApiTableTennisProvider,
    events: list[dict[str, Any]],
    observed_ts: str,
    http_status: int,
) -> Iterator[ProviderQuote]:
    _validate_stable_bookmaker_keys(events)

    event_bindings: dict[str, tuple[str | None, ...]] = {}
    for event in events:
        event_id = _event_id(event)
        binding = _event_binding(event, self.sport_key)
        prior = event_bindings.get(event_id)
        if prior is not None and prior != binding:
            raise provider.ProviderPayloadError(
                f"provider event id {event_id!r} has conflicting canonical identity witnesses"
            )
        event_bindings[event_id] = binding

    # Materialize the bounded provider snapshot before yielding any quote.  This is
    # intentional: a duplicate discovered after max_items must not let an earlier
    # partial batch escape as if the snapshot had one unambiguous identity mapping.
    quotes = list(_ORIGINAL_SNAPSHOT_QUOTES(self, events, observed_ts, http_status))
    seen: set[tuple[str, str, str]] = set()
    for quote in quotes:
        identity = (
            quote.provider_event_id,
            quote.provider_market_id,
            quote.provider_selection_id,
        )
        if identity in seen:
            raise provider.ProviderPayloadError(
                "provider snapshot contains ambiguous duplicate event/market/selection identity"
            )
        seen.add(identity)

    yield from quotes


if not getattr(
    provider.ParlayApiTableTennisProvider._snapshot_quotes,
    "_canonical_provider_identity_guard",
    False,
):
    setattr(_strict_snapshot_quotes, "_canonical_provider_identity_guard", True)
    provider.ParlayApiTableTennisProvider._snapshot_quotes = _strict_snapshot_quotes


__all__: list[str] = []
