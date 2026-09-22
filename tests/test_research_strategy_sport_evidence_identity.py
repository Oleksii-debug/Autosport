from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal

from autosport.domain import MarketEvent, MarketType
from autosport.research_strategy import (
    market_event_evidence_hash,
    research_market_snapshot_hash,
)


def _event(*, sport: str) -> MarketEvent:
    timestamp = "2026-09-22T10:00:00+00:00"
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2.00"),
        observed_ts=timestamp,
        source_id="provider",
        sequence=1,
        market_type=next(iter(MarketType)),
        status="open",
        source_ts=timestamp,
        ingest_ts=timestamp,
        score_state=None,
        metadata={},
        sport=sport,
    )


def _legacy_event_projection(event: MarketEvent) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "market_id": event.market_id,
        "selection_id": event.selection_id,
        "decimal_odds": str(event.decimal_odds),
        "observed_ts": event.observed_ts,
        "source_id": event.source_id,
        "sequence": event.sequence,
        "market_type": event.market_type.value,
        "status": event.status,
        "source_ts": event.source_ts,
        "score_state": event.score_state,
        "metadata": event.metadata,
    }


def _sha256_json(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_research_event_evidence_hash_binds_sport_identity() -> None:
    football = _event(sport="football")
    tennis = replace(football, sport="tennis")

    assert football.quote_key == tennis.quote_key
    assert football.sport != tennis.sport
    assert market_event_evidence_hash(football) != market_event_evidence_hash(tennis)


def test_research_market_snapshot_hash_binds_sport_identity() -> None:
    football = _event(sport="football")
    tennis = replace(football, sport="tennis")
    quote_key = football.quote_key

    assert research_market_snapshot_hash(
        {quote_key: football},
        (quote_key,),
    ) != research_market_snapshot_hash(
        {quote_key: tennis},
        (quote_key,),
    )


def test_v2_research_hashes_do_not_alias_legacy_unversioned_domain() -> None:
    football = _event(sport="football")
    quote_key = football.quote_key
    legacy_projection = _legacy_event_projection(football)

    legacy_event_hash = _sha256_json(legacy_projection)
    legacy_snapshot_hash = _sha256_json({quote_key: legacy_projection})

    assert market_event_evidence_hash(football) != legacy_event_hash
    assert research_market_snapshot_hash(
        {quote_key: football},
        (quote_key,),
    ) != legacy_snapshot_hash
