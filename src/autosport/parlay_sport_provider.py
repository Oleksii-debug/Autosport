from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from .parlayapi_provider import (
    Clock,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
    ProviderQuote,
    Sleeper,
    Transport,
)


_SPORT_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_RESERVED_DATASET_SCOPE_SPORT_KEYS = frozenset({"unknown", "mixed"})


def _canonical_sport_key(value: object) -> str:
    """Return one URL-safe provider sport identity without alias normalization."""

    if type(value) is not str or not value or value != value.strip():
        raise ValueError("sport_key must be a non-empty canonical string")
    if len(value) > 80 or not value.isascii() or _SPORT_KEY_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "sport_key must use lowercase ASCII letters/digits joined by single underscores"
        )
    if value in _RESERVED_DATASET_SCOPE_SPORT_KEYS:
        raise ValueError("sport_key must not use a reserved dataset scope identity")
    return value


class ParlayApiSportProvider(ParlayApiTableTennisProvider):
    """Configurable read-only ParlayAPI sport adapter.

    This is deliberately a narrow extension of the canonical table-tennis adapter:
    parsing, retry behavior, historical coverage checks, quote semantics and
    transport remain owned by ParlayApiTableTennisProvider.

    sport_key is a provider scope, not proof that the sport is currently active
    or that every market/capability is available. Active-catalog and capability
    evidence remain separate authorities.
    """

    def __init__(
        self,
        sport_key: str,
        api_key: str | None = None,
        *,
        public_preview: bool = False,
        regions: tuple[str, ...] = ("us",),
        markets: tuple[str, ...] = ("h2h", "spreads", "totals"),
        base_url: str = "https://parlay-api.com",
        timeout_seconds: float = 10.0,
        max_attempts: int = 2,
        max_backoff_seconds: float = 2.0,
        transport: Transport | None = None,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        canonical = _canonical_sport_key(sport_key)
        if public_preview and canonical != "table_tennis":
            raise ValueError(
                "public_preview is qualified only for table_tennis; "
                "other sport adapters require authenticated read-only access"
            )

        # These two values form one configured provider identity. Keep them on
        # private backing fields and expose read-only properties so callers cannot
        # retarget later requests or relabel later batches by rebinding public attrs.
        self._sport_key = canonical
        self._source_id = f"parlayapi:{canonical}"

        kwargs: dict[str, Any] = {
            "public_preview": public_preview,
            "regions": regions,
            "markets": markets,
            "base_url": base_url,
            "timeout_seconds": timeout_seconds,
            "max_attempts": max_attempts,
            "max_backoff_seconds": max_backoff_seconds,
        }
        if transport is not None:
            kwargs["transport"] = transport
        if clock is not None:
            kwargs["clock"] = clock
        if sleeper is not None:
            kwargs["sleeper"] = sleeper
        super().__init__(api_key, **kwargs)

    @property
    def sport_key(self) -> str:
        return self._sport_key

    @property
    def source_id(self) -> str:
        return self._source_id

    def _url(self) -> str:
        query = urlencode(
            {
                "regions": ",".join(self.regions),
                "markets": ",".join(self.markets),
                "oddsFormat": "decimal",
                "include": "slim",
            }
        )
        if self.public_preview:
            return f"{self.base_url}/v1/try/table_tennis/odds?{query}"
        return f"{self.base_url}/v1/sports/{self.sport_key}/odds?{query}"

    def _event_quotes(
        self,
        event: dict[str, Any],
        observed_ts: str,
        http_status: int,
    ) -> list[ProviderQuote]:
        """Reject cross-sport substitution before canonical quote materialization."""

        explicit = event.get("sport_key")
        if explicit is not None:
            if type(explicit) is not str or explicit != self.sport_key:
                raise ProviderPayloadError(
                    "provider event sport_key does not match configured sport scope"
                )
            scoped_event = event
        else:
            scoped_event = dict(event)
            scoped_event["sport_key"] = self.sport_key
        return super()._event_quotes(scoped_event, observed_ts, http_status)


__all__ = ["ParlayApiSportProvider"]
