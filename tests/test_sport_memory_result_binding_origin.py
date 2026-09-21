from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.continuous_session import SettlementResolution
from autosport.domain import MarketEvent
from autosport.event_lifecycle import (
    CatalogEvent,
    CatalogPage,
    ContinuousEventLifecycle,
    EventLifecycleRecord,
    EventPhase,
)
from autosport.opponent_intelligence import OpponentIntelligenceStore
from autosport.product_runtime import build_autonomous_product_runtime
from autosport.participant_identity import (
    AliasRecord,
    EntityIdentity,
    EntityKind,
    ParticipantIdentityRegistry,
    RosterMembership,
)
from autosport.sport_memory_result_materializer import (
    SportMemoryResultMaterializationError,
    SportMemoryResultMaterializer,
    _source_pair_digest,
)
from autosport.storage import SQLiteMarketStore


SHA_A = "a" * 64
SHA_B = "b" * 64
T0 = "2026-09-20T10:00:00Z"
T1 = "2026-09-20T10:01:00Z"
T2 = "2026-09-20T10:02:00Z"
T3 = "2026-09-20T10:03:00Z"
EVENT_ID = "provider-a:event-1"


def _entity(entity_id: str, *, kind: EntityKind = EntityKind.PARTICIPANT) -> EntityIdentity:
    return EntityIdentity(
        entity_id=entity_id,
        kind=kind,
        source_reference=f"provider:{entity_id}",
        evidence_sha256=SHA_A,
        first_known_at=T0,
        available_at=T0,
    )


def _alias(
    text: str,
    entity_id: str,
    *,
    available_at: str = T0,
    supersedes_record_id: str | None = None,
) -> AliasRecord:
    return AliasRecord(
        source_id="provider-a",
        alias=text,
        entity_id=entity_id,
        valid_from=T0,
        valid_until=None,
        available_at=available_at,
        evidence_sha256=SHA_A,
        recorded_at=available_at,
        supersedes_record_id=supersedes_record_id,
    )


def _roster(entity_id: str) -> RosterMembership:
    return RosterMembership(
        event_id=EVENT_ID,
        source_id="provider-a",
        entity_id=entity_id,
        member_from=T0,
        member_until=None,
        available_at=T0,
        evidence_sha256=SHA_A,
    )


def _quote(selection: str, sequence: int) -> MarketEvent:
    return MarketEvent(
        event_id=EVENT_ID,
        market_id="match-outcome",
        selection_id=selection,
        decimal_odds=Decimal("2.0"),
        observed_ts=T1,
        source_id="provider-a",
        sequence=sequence,
        ingest_ts=T1,
        sport="tennis",
        competition_id="comp-tour-42",
        market_semantics_id="match-outcome",
        provider_source_class="sportsbook",
    )


class _ProductSource:
    source_id = "provider-a"
    stream_epoch = "epoch-1"
    settlement_authority_id = "provider-a-results-v1"
    settlement_configuration_sha256 = SHA_A
    settlement_resolver_implementation_id = "provider-a-results-resolver-v1"

    def __init__(self) -> None:
        self.resolution: SettlementResolution | None = None

    def fetch_catalog_page(self, checkpoint):
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor="unused",
            position=0,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()

    def resolve_event(self, delta):
        raise AssertionError("binding-origin tests do not consume collector deltas")

    def resolve(
        self,
        record: EventLifecycleRecord,
        *,
        as_of: str,
    ) -> SettlementResolution | None:
        return self.resolution


def _authorities(
    root: Path,
) -> tuple[
    ParticipantIdentityRegistry,
    OpponentIntelligenceStore,
    SQLiteMarketStore,
    SportMemoryResultMaterializer,
]:
    identities = ParticipantIdentityRegistry.initialize_pristine(root / "identity.json")
    for entity in (
        _entity("p-alex"),
        _entity("p-blair"),
        _entity("league-tour", kind=EntityKind.LEAGUE),
    ):
        identities.add_entity(entity)
    for alias in (
        _alias("alex", "p-alex"),
        _alias("sel-alex-17", "p-alex"),
        _alias("blair", "p-blair"),
        _alias("sel-blair-23", "p-blair"),
        _alias("tour", "league-tour"),
        _alias("comp-tour-42", "league-tour"),
    ):
        identities.add_alias(alias)
    identities.add_roster_membership(_roster("p-alex"))
    identities.add_roster_membership(_roster("p-blair"))
    opponent_store = OpponentIntelligenceStore.initialize_pristine(
        root / "opponents.json",
        identities,
    )
    source = _ProductSource()
    runtime = build_autonomous_product_runtime(
        workspace=root,
        source=source,
        clock=lambda: T2,
        outcome_authority=source,
    )
    market_store = runtime.market_store
    market_store.append(_quote("sel-alex-17", 1))
    market_store.append(_quote("sel-blair-23", 2))
    lifecycle = runtime.lifecycle
    lifecycle.apply_page(
        CatalogPage(
            source_id="provider-a",
            stream_epoch="epoch-1",
            cursor="cursor-1",
            position=0,
            events=(
                CatalogEvent(
                    source_id="provider-a",
                    sport="tennis",
                    event_id="event-1",
                    phase=EventPhase.COMPLETED,
                    available_at=T2,
                    completion_ref="completion-1",
                    settlement_ref="settlement-1",
                ),
            ),
        ),
        discovered_at=T2,
    )
    materializer = SportMemoryResultMaterializer(
        opponent_store,
        runtime,
    )
    return identities, opponent_store, market_store, materializer


def test_recomputed_digest_cannot_backdate_post_reveal_provider_choice(tmp_path: Path) -> None:
    identities, store, market_store, materializer = _authorities(tmp_path)

    # At T1 two provider selections are indistinguishable as the opponent from the
    # canonical causal identity view, so pre-reveal product evidence cannot choose one.
    identities.add_alias(_alias("sel-blair-shadow", "p-blair"))
    market_store.append(_quote("sel-blair-shadow", 3))
    shadow = identities.resolve_alias_record(
        "provider-a",
        "sel-blair-shadow",
        as_of=T1,
    )

    subject = next(
        event
        for event in market_store.events(EVENT_ID)
        if event.selection_id == "sel-alex-17"
    )
    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="cannot be resolved uniquely",
    ):
        materializer.freeze_binding(
            event_identity=EVENT_ID,
            source_id="provider-a",
            subject_alias="alex",
            opponent_alias="blair",
            sport_id="tennis",
            league_alias="tour",
            market_context_id="match-outcome",
            subject_quote_key=subject.quote_key,
            as_of=T1,
            subject_selection_id="sel-alex-17",
            opponent_selection_id="sel-blair-23",
            competition_id="comp-tour-42",
        )

    # Settlement is revealed at T2. Only afterwards, at T3, an identity correction
    # makes one provider selection look uniquely correct.
    identities.add_entity(_entity("p-charlie"))
    identities.add_alias(
        _alias(
            "sel-blair-shadow",
            "p-charlie",
            available_at=T3,
            supersedes_record_id=shadow.record_id,
        )
    )
    post_reveal = materializer.freeze_binding(
        event_identity=EVENT_ID,
        source_id="provider-a",
        subject_alias="alex",
        opponent_alias="blair",
        sport_id="tennis",
        league_alias="tour",
        market_context_id="match-outcome",
        subject_quote_key=subject.quote_key,
        as_of=T3,
        subject_selection_id="sel-alex-17",
        opponent_selection_id="sel-blair-23",
        competition_id="comp-tour-42",
    )
    assert post_reveal.provider_binding_as_of == T3
    assert post_reveal.provider_inference_as_of is None

    # Recreate the historical payload and recompute its deterministic digest while
    # falsely declaring that the now-chosen explicit provider IDs were bound at T1.
    # Content authentication alone would accept this; canonical T1 re-derivation must
    # still reject because provider choice was ambiguous before settlement reveal.
    historical_subject, historical_opponent, historical_frozen = (
        materializer._resolve_source_pair(
            event_identity=EVENT_ID,
            source_id="provider-a",
            subject_selection_id="sel-alex-17",
            opponent_selection_id="sel-blair-23",
            sport_id="tennis",
            competition_id="comp-tour-42",
            market_context_id="match-outcome",
            subject_quote_key=subject.quote_key,
            opponent_quote_key=post_reveal.opponent_quote_key,
            as_of=T1,
        )
    )
    historical_identity = materializer._resolve_identity_snapshot(
        event_identity=EVENT_ID,
        source_id="provider-a",
        subject_alias="alex",
        opponent_alias="blair",
        league_alias="tour",
        subject_selection_id="sel-alex-17",
        opponent_selection_id="sel-blair-23",
        competition_id="comp-tour-42",
        as_of=historical_frozen,
    )
    forged = replace(
        post_reveal,
        provider_binding_as_of=T1,
        provider_inference_as_of=None,
        evidence_sha256=_source_pair_digest(
            historical_subject,
            historical_opponent,
            historical_frozen,
            historical_identity.sha256,
            T1,
            None,
        ),
    )
    settlement = SettlementResolution(
        event_identity=EVENT_ID,
        settlement_ref="settlement-1",
        quote_outcomes={
            forged.subject_quote_key: "win",
            forged.opponent_quote_key: "loss",
        },
        evidence_id="result-1",
        evidence_sha256=SHA_B,
        available_at=T2,
    )

    authority = materializer.runtime.coordinator.outcome_authority
    assert isinstance(authority, _ProductSource)
    authority.resolution = settlement

    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="cannot be resolved uniquely",
    ):
        materializer.materialize(forged, settlement, as_of=T3)
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_provider_binding_cutoff_at_or_after_settlement_reveal_fails_closed(tmp_path: Path) -> None:
    _, store, market_store, materializer = _authorities(tmp_path)
    subject = next(
        event
        for event in market_store.events(EVENT_ID)
        if event.selection_id == "sel-alex-17"
    )
    binding = materializer.freeze_binding(
        event_identity=EVENT_ID,
        source_id="provider-a",
        subject_alias="alex",
        opponent_alias="blair",
        sport_id="tennis",
        league_alias="tour",
        market_context_id="match-outcome",
        subject_quote_key=subject.quote_key,
        as_of=T1,
        subject_selection_id="sel-alex-17",
        opponent_selection_id="sel-blair-23",
        competition_id="comp-tour-42",
    )
    forged = replace(binding, provider_binding_as_of=T2)
    settlement = SettlementResolution(
        event_identity=EVENT_ID,
        settlement_ref="settlement-1",
        quote_outcomes={
            forged.subject_quote_key: "win",
            forged.opponent_quote_key: "loss",
        },
        evidence_id="result-1",
        evidence_sha256=SHA_B,
        available_at=T2,
    )

    authority = materializer.runtime.coordinator.outcome_authority
    assert isinstance(authority, _ProductSource)
    authority.resolution = settlement

    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="provider-selection binding must predate settlement reveal",
    ):
        materializer.materialize(forged, settlement, as_of=T3)
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()
