from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Mapping

from .betfair_authenticated_stream import BetfairAuthenticatedStreamFreshnessRuntime
from .betfair_stream_codec import (
    BETFAIR_STREAM_SOURCE_ID,
    BetfairQuoteIdentity,
    BetfairQuoteSide,
)
from .betfair_stream_publish_freshness import (
    BetfairStreamFreshnessPolicy,
    BetfairStreamPublicationEvidence,
)
from .domain import MarketEvent, MarketType
from .providers import ProviderBatch, ProviderQuote


_SCHEMA = "autosport.betfair_authenticated_market_provider.v1"
_MAX_SQLITE_SEQUENCE = (1 << 63) - 1


def _iso_from_epoch_ms(value: int) -> str:
    if type(value) is not int or value < 0:
        raise ValueError("Betfair epoch milliseconds must be a non-negative int")
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Betfair epoch milliseconds are outside supported datetime range") from exc


def _identity_token(identity: BetfairQuoteIdentity) -> str:
    if type(identity) is not BetfairQuoteIdentity:
        raise TypeError("identity must be canonical BetfairQuoteIdentity")
    price = "" if identity.price is None else str(identity.price)
    return (
        f"{identity.selection_id}:{identity.handicap}:"
        f"{identity.side.value}:{price or 'ltp'}"
    )


def _event_token(market_id: str) -> str:
    if type(market_id) is not str or not market_id:
        raise ValueError("Betfair market_id must be a non-empty exact string")
    return "market-" + sha256(market_id.encode("utf-8")).hexdigest()[:32]


def _metadata_from_evidence(
    evidence: BetfairStreamPublicationEvidence,
) -> dict[str, object]:
    identity = evidence.quote.identity
    return {
        "schema": _SCHEMA,
        "market_id": identity.market_id,
        "selection_id": identity.selection_id,
        "handicap": str(identity.handicap),
        "side": identity.side.value,
        "identity_price": None if identity.price is None else str(identity.price),
        "quote_size": None if evidence.quote.size is None else str(evidence.quote.size),
        "evidence_id": evidence.evidence_id,
        "subscription_id": evidence.subscription_id,
        "transport_frame_sha256": evidence.frame_sha256,
        "provider_health": evidence.provider_health.value,
        "provider_publish_time_ms": evidence.publish_time_ms,
        "provider_received_time_ms": evidence.received_time_ms,
        "provider_ingested_time_ms": evidence.ingested_time_ms,
        "durable_disposition": "open",
    }


def _identity_from_metadata(metadata: object) -> BetfairQuoteIdentity:
    if type(metadata) is not dict or metadata.get("schema") != _SCHEMA:
        raise ValueError(
            "durable Betfair stream current state lacks canonical bridge metadata"
        )
    market_id = metadata.get("market_id")
    selection_id = metadata.get("selection_id")
    handicap_raw = metadata.get("handicap")
    side_raw = metadata.get("side")
    price_raw = metadata.get("identity_price")
    if type(market_id) is not str or not market_id:
        raise ValueError("durable Betfair bridge market_id is invalid")
    if type(selection_id) is not int or selection_id <= 0:
        raise ValueError("durable Betfair bridge selection_id is invalid")
    if type(handicap_raw) is not str:
        raise ValueError("durable Betfair bridge handicap is invalid")
    if type(side_raw) is not str:
        raise ValueError("durable Betfair bridge side is invalid")
    try:
        handicap = Decimal(handicap_raw)
        side = BetfairQuoteSide(side_raw)
        price = None if price_raw is None else Decimal(price_raw)
    except (ArithmeticError, ValueError) as exc:
        raise ValueError("durable Betfair bridge identity metadata is invalid") from exc
    return BetfairQuoteIdentity(
        BETFAIR_STREAM_SOURCE_ID,
        market_id,
        selection_id,
        handicap,
        side,
        price,
    )


class BetfairAuthenticatedMarketProvider:
    """Authenticated Betfair Stream -> canonical MarketProvider adapter.

    The adapter is deliberately stateful. Before the first read it must be bound to the
    exact durable current projection from the target SQLiteMarketStore. That binding
    restores the provider-sequence floor and the set of previously durable open quotes,
    so a restart can emit explicit higher-sequence closed tombstones when the fresh
    authenticated image no longer authorizes an old quote.

    It never creates execution authority. It only converts publications that the
    canonical authenticated runtime still proves decision-eligible, and materializes
    loss of that authority as ordinary non-open MarketEvent state through ProviderQuote.
    """

    source_id = BETFAIR_STREAM_SOURCE_ID

    def __init__(
        self,
        runtime: BetfairAuthenticatedStreamFreshnessRuntime,
        *,
        freshness_policy: BetfairStreamFreshnessPolicy,
    ) -> None:
        if type(runtime) is not BetfairAuthenticatedStreamFreshnessRuntime:
            raise TypeError(
                "runtime must be canonical BetfairAuthenticatedStreamFreshnessRuntime"
            )
        if type(freshness_policy) is not BetfairStreamFreshnessPolicy:
            raise TypeError("freshness_policy must be canonical BetfairStreamFreshnessPolicy")
        self._runtime = runtime
        self._policy = BetfairStreamFreshnessPolicy(
            max_age_ms=freshness_policy.max_age_ms,
            max_future_skew_ms=freshness_policy.max_future_skew_ms,
        )
        self._bound = False
        self._sequence = 0
        self._open_by_identity: dict[BetfairQuoteIdentity, ProviderQuote] = {}
        self._pending: tuple[ProviderQuote, ...] = ()
        self._pending_offset = 0
        self._pending_authority_revoked = False

    @property
    def durable_bound(self) -> bool:
        return self._bound

    def bind_durable_current(
        self,
        current: Mapping[tuple[str, str], MarketEvent],
    ) -> None:
        """Bind once to independently proven durable current projection before reads."""

        if self._bound:
            raise RuntimeError("Betfair authenticated provider is already durably bound")
        if type(current) is not dict:
            raise TypeError("current must be an exact dict of canonical market events")

        max_sequence = 0
        restored: dict[BetfairQuoteIdentity, ProviderQuote] = {}
        for key, event in current.items():
            if type(key) is not tuple or len(key) != 2:
                raise ValueError("durable current key must be (source_id, quote_key)")
            if type(event) is not MarketEvent:
                raise TypeError("durable current values must be exact MarketEvent")
            if event.source_id != BETFAIR_STREAM_SOURCE_ID:
                continue
            if type(event.sequence) is not int or event.sequence < 1:
                raise ValueError("durable Betfair bridge sequence must be a positive int")
            if event.sequence > max_sequence:
                max_sequence = event.sequence
            identity = _identity_from_metadata(event.metadata)
            expected_market_id = f"{BETFAIR_STREAM_SOURCE_ID}:{identity.market_id}"
            if event.market_id != expected_market_id:
                raise ValueError("durable Betfair bridge market identity mismatch")
            if event.status != "open":
                continue
            provider_quote = ProviderQuote(
                provider_event_id=_event_token(identity.market_id),
                provider_market_id=identity.market_id,
                provider_selection_id=_identity_token(identity),
                decimal_odds=event.decimal_odds,
                observed_ts=event.observed_ts,
                sequence=event.sequence,
                market_type=event.market_type,
                status="open",
                source_ts=event.source_ts,
                metadata=dict(event.metadata),
                sport=event.sport,
                exchange_side=event.exchange_side,
            )
            if identity in restored:
                raise ValueError("durable Betfair bridge contains duplicate open identity")
            restored[identity] = provider_quote

        if max_sequence < 0 or max_sequence > _MAX_SQLITE_SEQUENCE:
            raise ValueError("durable Betfair bridge sequence floor is invalid")
        self._sequence = max_sequence
        self._open_by_identity = restored
        self._bound = True

    def assert_durable_current(
        self,
        current: Mapping[tuple[str, str], MarketEvent],
    ) -> None:
        """Fail closed if durable state diverged from this consumed stream state."""

        if not self._bound:
            raise RuntimeError("Betfair authenticated provider is not durably bound")
        if type(current) is not dict:
            raise TypeError("current must be an exact dict of canonical market events")

        max_sequence = 0
        durable_open: dict[BetfairQuoteIdentity, int] = {}
        for key, event in current.items():
            if type(key) is not tuple or len(key) != 2:
                raise ValueError("durable current key must be (source_id, quote_key)")
            if type(event) is not MarketEvent:
                raise TypeError("durable current values must be exact MarketEvent")
            if event.source_id != BETFAIR_STREAM_SOURCE_ID:
                continue
            identity = _identity_from_metadata(event.metadata)
            if type(event.sequence) is not int or event.sequence < 1:
                raise ValueError("durable Betfair bridge sequence must be a positive int")
            max_sequence = max(max_sequence, event.sequence)
            if event.status == "open":
                durable_open[identity] = event.sequence

        memory_open = {
            identity: quote.sequence
            for identity, quote in self._open_by_identity.items()
        }
        if max_sequence != self._sequence or durable_open != memory_open:
            raise RuntimeError(
                "durable Betfair current projection diverged from consumed stream state; "
                "reconnect and rebuild the authenticated provider from SQLite"
            )

    def _next_sequence(self) -> int:
        if self._sequence >= _MAX_SQLITE_SEQUENCE:
            raise OverflowError("Betfair bridge exhausted signed 64-bit provider sequence")
        self._sequence += 1
        return self._sequence

    def _open_quote(
        self,
        evidence: BetfairStreamPublicationEvidence,
    ) -> ProviderQuote:
        identity = evidence.quote.identity
        exchange_side = (
            identity.side.value
            if identity.side in {BetfairQuoteSide.BACK, BetfairQuoteSide.LAY}
            else None
        )
        return ProviderQuote(
            provider_event_id=_event_token(identity.market_id),
            provider_market_id=identity.market_id,
            provider_selection_id=_identity_token(identity),
            decimal_odds=evidence.quote.price,
            observed_ts=_iso_from_epoch_ms(evidence.received_time_ms),
            sequence=self._next_sequence(),
            market_type=MarketType.OTHER,
            status="open",
            source_ts=_iso_from_epoch_ms(evidence.publish_time_ms),
            metadata=_metadata_from_evidence(evidence),
            exchange_side=exchange_side,
        )

    def _closed_quote(self, prior: ProviderQuote) -> ProviderQuote:
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        metadata = dict(prior.metadata)
        metadata["durable_disposition"] = "closed"
        metadata["invalidation_reason"] = "authenticated_stream_authority_revoked"
        return ProviderQuote(
            provider_event_id=prior.provider_event_id,
            provider_market_id=prior.provider_market_id,
            provider_selection_id=prior.provider_selection_id,
            decimal_odds=prior.decimal_odds,
            observed_ts=now,
            sequence=self._next_sequence(),
            market_type=prior.market_type,
            status="closed",
            source_ts=None,
            score_state=prior.score_state,
            metadata=metadata,
            sport=prior.sport,
            exchange_side=prior.exchange_side,
        )

    def _build_transition(self) -> tuple[ProviderQuote, ...]:
        issued = self._runtime.read_and_ingest()
        emitted: list[ProviderQuote] = []
        issued_by_identity = {evidence.quote.identity: evidence for evidence in issued}

        # First retire any durable open quote that no longer has exact live authority
        # after the just-consumed authenticated frame.
        for identity, prior in tuple(self._open_by_identity.items()):
            decision = self._runtime.evaluate(identity, policy=self._policy)
            if not decision.decision_eligible:
                emitted.append(self._closed_quote(prior))
                self._open_by_identity.pop(identity, None)

        # Then publish only newly issued evidence that remains decision-eligible under
        # the same authenticated runtime after all frame-level status/epoch updates.
        for identity in sorted(
            issued_by_identity,
            key=lambda item: (
                item.market_id,
                item.selection_id,
                str(item.handicap),
                item.side.value,
                "" if item.price is None else str(item.price),
            ),
        ):
            evidence = issued_by_identity[identity]
            decision = self._runtime.evaluate(identity, policy=self._policy)
            if not decision.decision_eligible:
                continue
            prior = self._open_by_identity.get(identity)
            if prior is not None and prior.metadata.get("evidence_id") == evidence.evidence_id:
                continue
            quote = self._open_quote(evidence)
            self._open_by_identity[identity] = quote
            emitted.append(quote)

        return tuple(emitted)

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if type(max_items) is not int or max_items <= 0:
            raise ValueError("max_items must be a positive non-boolean integer")
        if not self._bound:
            raise RuntimeError(
                "Betfair authenticated provider must bind durable current state before read"
            )

        if self._pending_offset >= len(self._pending):
            self._pending = self._build_transition()
            self._pending_offset = 0
            self._pending_authority_revoked = any(
                quote.status != "open" for quote in self._pending
            )

        start = self._pending_offset
        stop = min(len(self._pending), start + max_items)
        quotes = self._pending[start:stop]
        self._pending_offset = stop
        truncated = stop < len(self._pending)
        authority_revoked = self._pending_authority_revoked
        if not truncated:
            self._pending = ()
            self._pending_offset = 0
            self._pending_authority_revoked = False

        flags: list[str] = []
        if authority_revoked:
            flags.append("BETFAIR_AUTHORITY_REVOKED")
        if truncated:
            flags.append("TRUNCATED_BATCH")
        page_cursor = quotes[-1].sequence if quotes else self._sequence
        return ProviderBatch(
            self.source_id,
            quotes,
            cursor=str(page_cursor),
            quality_flags=tuple(flags),
        )
