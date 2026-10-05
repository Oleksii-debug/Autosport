from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport.betfair_authenticated_provider import (
    BetfairAuthenticatedMarketProvider,
    _SCHEMA,
    _event_token,
    _identity_token,
)
from autosport.betfair_authenticated_stream import (
    BetfairAuthenticatedStreamFreshnessRuntime,
)
from autosport.betfair_stream_codec import (
    BETFAIR_STREAM_SOURCE_ID,
    BetfairProviderStreamHealth,
    BetfairQuoteIdentity,
    BetfairQuoteSide,
)
from autosport.betfair_stream_publish_freshness import BetfairStreamFreshnessPolicy
from autosport.live_observation import _validate_observation_ingress
from autosport.providers import CanonicalNormalizer, ProviderBatch, ProviderQuote


def _runtime() -> BetfairAuthenticatedStreamFreshnessRuntime:
    return object.__new__(BetfairAuthenticatedStreamFreshnessRuntime)


def _identity() -> BetfairQuoteIdentity:
    return BetfairQuoteIdentity(
        BETFAIR_STREAM_SOURCE_ID,
        "1.23456789",
        101,
        Decimal("0"),
        BetfairQuoteSide.BACK,
        Decimal("2.5"),
    )


def _metadata(identity: BetfairQuoteIdentity, *, evidence_id: str = "evidence-1") -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "market_id": identity.market_id,
        "selection_id": identity.selection_id,
        "handicap": str(identity.handicap),
        "side": identity.side.value,
        "identity_price": str(identity.price),
        "quote_size": "12.5",
        "evidence_id": evidence_id,
        "subscription_id": "subscription-1",
        "transport_frame_sha256": "a" * 64,
        "provider_health": "up_to_date",
        "provider_publish_time_ms": 1_700_000_000_000,
        "provider_received_time_ms": 1_700_000_000_001,
        "provider_ingested_time_ms": 1_700_000_000_001,
        "durable_disposition": "open",
    }


def _durable_open(sequence: int = 7):
    identity = _identity()
    quote = ProviderQuote(
        provider_event_id=_event_token(identity.market_id),
        provider_market_id=identity.market_id,
        provider_selection_id=_identity_token(identity),
        decimal_odds=Decimal("2.5"),
        observed_ts="2026-10-05T12:00:00+00:00",
        sequence=sequence,
        status="open",
        source_ts="2026-10-05T11:59:59+00:00",
        metadata=_metadata(identity),
        exchange_side="back",
    )
    event = CanonicalNormalizer().normalize(BETFAIR_STREAM_SOURCE_ID, quote)
    return identity, event


def test_reserved_betfair_source_rejects_generic_spoof() -> None:
    class Spoof:
        source_id = BETFAIR_STREAM_SOURCE_ID

        def read_batch(self, max_items: int = 1000) -> ProviderBatch:
            return ProviderBatch(self.source_id, ())

    with pytest.raises(TypeError, match="canonical authenticated"):
        _validate_observation_ingress(
            Spoof(),
            max_items=10,
            policy=None,
            clock=None,
        )


def test_exact_bridge_is_admitted_but_requires_durable_binding() -> None:
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )
    _validate_observation_ingress(
        provider,
        max_items=10,
        policy=None,
        clock=None,
    )
    with pytest.raises(RuntimeError, match="bind durable current"):
        provider.read_batch(10)


def test_restart_binding_restores_open_identity_and_sequence_floor() -> None:
    identity, event = _durable_open(sequence=41)
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )
    provider.bind_durable_current({(event.source_id, event.quote_key): event})

    assert provider.durable_bound
    assert provider._sequence == 41
    assert provider._open_by_identity[identity].sequence == 41


def test_revoked_authenticated_authority_materializes_closed_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, event = _durable_open(sequence=7)
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )
    provider.bind_durable_current({(event.source_id, event.quote_key): event})

    monkeypatch.setattr(
        BetfairAuthenticatedStreamFreshnessRuntime,
        "read_and_ingest",
        lambda self: (),
    )
    monkeypatch.setattr(
        BetfairAuthenticatedStreamFreshnessRuntime,
        "evaluate",
        lambda self, identity, *, policy: SimpleNamespace(decision_eligible=False),
    )

    batch = provider.read_batch(10)

    assert len(batch.quotes) == 1
    tombstone = batch.quotes[0]
    assert tombstone.status == "closed"
    assert tombstone.sequence == 8
    assert tombstone.provider_selection_id == _identity_token(identity)
    assert tombstone.metadata["durable_disposition"] == "closed"
    assert batch.quality_flags == ("BETFAIR_AUTHORITY_REVOKED",)
    assert (
        tombstone.metadata["invalidation_reason"]
        == "authenticated_stream_authority_revoked"
    )
    assert identity not in provider._open_by_identity


def test_eligible_issued_publication_becomes_open_provider_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    evidence = SimpleNamespace(
        quote=SimpleNamespace(
            identity=identity,
            price=Decimal("2.5"),
            size=Decimal("12.5"),
        ),
        evidence_id="evidence-2",
        subscription_id="subscription-1",
        frame_sha256="b" * 64,
        provider_health=BetfairProviderStreamHealth.UP_TO_DATE,
        publish_time_ms=1_700_000_000_000,
        received_time_ms=1_700_000_000_001,
        ingested_time_ms=1_700_000_000_001,
    )
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )
    provider.bind_durable_current({})

    monkeypatch.setattr(
        BetfairAuthenticatedStreamFreshnessRuntime,
        "read_and_ingest",
        lambda self: (evidence,),
    )
    monkeypatch.setattr(
        BetfairAuthenticatedStreamFreshnessRuntime,
        "evaluate",
        lambda self, identity, *, policy: SimpleNamespace(decision_eligible=True),
    )

    batch = provider.read_batch(10)

    assert len(batch.quotes) == 1
    quote = batch.quotes[0]
    assert quote.status == "open"
    assert quote.sequence == 1
    assert quote.decimal_odds == Decimal("2.5")
    assert quote.exchange_side == "back"
    assert quote.metadata["evidence_id"] == "evidence-2"


def test_transition_is_bounded_and_marks_truncated_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )
    provider.bind_durable_current({})

    quotes = tuple(
        ProviderQuote(
            provider_event_id=f"event-{index}",
            provider_market_id="1.23456789",
            provider_selection_id=f"selection-{index}",
            decimal_odds=Decimal("2"),
            observed_ts="2026-10-05T12:00:00+00:00",
            sequence=index + 1,
        )
        for index in range(3)
    )
    monkeypatch.setattr(provider, "_build_transition", lambda: quotes)

    first = provider.read_batch(2)
    second = provider.read_batch(2)

    assert len(first.quotes) == 2
    assert first.quality_flags == ("TRUNCATED_BATCH",)
    assert first.cursor == "2"
    assert len(second.quotes) == 1
    assert second.quality_flags == ()
    assert second.cursor == "3"


def test_durable_drift_after_consumption_fails_closed() -> None:
    identity, event = _durable_open(sequence=7)
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )
    current = {(event.source_id, event.quote_key): event}
    provider.bind_durable_current(current)
    provider.assert_durable_current(current)

    provider._sequence = 8
    with pytest.raises(RuntimeError, match="diverged"):
        provider.assert_durable_current(current)


def test_bridge_refuses_unowned_historical_betfair_state() -> None:
    identity, event = _durable_open(sequence=7)
    event.metadata.clear()
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )
    with pytest.raises(ValueError, match="canonical bridge metadata"):
        provider.bind_durable_current({(event.source_id, event.quote_key): event})


def test_durable_binding_rejects_closed_row_with_open_disposition() -> None:
    _, event = _durable_open(sequence=7)
    forged = replace(event, status="closed")
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )

    with pytest.raises(ValueError, match="closed disposition metadata mismatch"):
        provider.bind_durable_current({(forged.source_id, forged.quote_key): forged})


def test_durable_binding_rejects_selection_identity_drift() -> None:
    _, event = _durable_open(sequence=7)
    forged = replace(event, selection_id="forged-selection")
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )

    with pytest.raises(ValueError, match="selection identity mismatch"):
        provider.bind_durable_current({(forged.source_id, forged.quote_key): forged})


def test_durable_assertion_rejects_same_sequence_open_odds_rewrite() -> None:
    _, event = _durable_open(sequence=7)
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )
    current = {(event.source_id, event.quote_key): event}
    provider.bind_durable_current(current)

    forged = replace(event, decimal_odds=Decimal("3.0"))
    with pytest.raises(ValueError, match="odds do not match quote identity"):
        provider.assert_durable_current(
            {(forged.source_id, forged.quote_key): forged}
        )


def test_durable_assertion_rejects_noncanonical_current_mapping_key() -> None:
    _, event = _durable_open(sequence=7)
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )
    provider.bind_durable_current({(event.source_id, event.quote_key): event})

    with pytest.raises(ValueError, match="key does not match canonical MarketEvent key"):
        provider.assert_durable_current(
            {(event.source_id, "wrong-quote-key"): event}
        )


def test_durable_binding_rejects_noncanonical_current_mapping_key() -> None:
    _, event = _durable_open(sequence=7)
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )

    with pytest.raises(ValueError, match="key does not match canonical MarketEvent key"):
        provider.bind_durable_current({(event.source_id, "wrong-quote-key"): event})


def test_durable_binding_rejects_non_string_identity_price_metadata() -> None:
    _, event = _durable_open(sequence=7)
    metadata = dict(event.metadata)
    metadata["identity_price"] = 2
    forged = replace(event, metadata=metadata)
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )

    with pytest.raises(ValueError, match="identity price is invalid"):
        provider.bind_durable_current({(forged.source_id, forged.quote_key): forged})


def test_durable_binding_rejects_closed_exchange_side_drift() -> None:
    _, event = _durable_open(sequence=7)
    metadata = dict(event.metadata)
    metadata["durable_disposition"] = "closed"
    forged = replace(
        event,
        status="closed",
        source_ts=None,
        metadata=metadata,
        exchange_side="lay",
    )
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )

    with pytest.raises(ValueError, match="closed exchange-side identity mismatch"):
        provider.bind_durable_current({(forged.source_id, forged.quote_key): forged})


def test_durable_assertion_rejects_same_sequence_open_metadata_rewrite() -> None:
    _, event = _durable_open(sequence=7)
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )
    provider.bind_durable_current({(event.source_id, event.quote_key): event})

    metadata = dict(event.metadata)
    metadata["evidence_id"] = "forged-evidence"
    forged = replace(event, metadata=metadata)
    with pytest.raises(RuntimeError, match="diverged"):
        provider.assert_durable_current(
            {(forged.source_id, forged.quote_key): forged}
        )


def test_restart_binding_preserves_last_traded_identity_without_embedded_price() -> None:
    identity = BetfairQuoteIdentity(
        BETFAIR_STREAM_SOURCE_ID,
        "1.23456789",
        101,
        Decimal("0"),
        BetfairQuoteSide.LAST_TRADED,
        None,
    )
    metadata = _metadata(_identity())
    metadata.update(
        {
            "side": identity.side.value,
            "identity_price": None,
            "quote_size": None,
        }
    )
    quote = ProviderQuote(
        provider_event_id=_event_token(identity.market_id),
        provider_market_id=identity.market_id,
        provider_selection_id=_identity_token(identity),
        decimal_odds=Decimal("2.5"),
        observed_ts="2026-10-05T12:00:00+00:00",
        sequence=9,
        status="open",
        source_ts="2026-10-05T11:59:59+00:00",
        metadata=metadata,
        exchange_side=None,
    )
    event = CanonicalNormalizer().normalize(BETFAIR_STREAM_SOURCE_ID, quote)
    provider = BetfairAuthenticatedMarketProvider(
        _runtime(),
        freshness_policy=BetfairStreamFreshnessPolicy(max_age_ms=5_000),
    )

    current = {(event.source_id, event.quote_key): event}
    provider.bind_durable_current(current)
    provider.assert_durable_current(current)

    assert provider._open_by_identity[identity].decimal_odds == Decimal("2.5")
