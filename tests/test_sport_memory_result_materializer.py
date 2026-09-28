from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import subprocess
import sys

import pytest

from autosport.continuous_session import SettlementResolution
from autosport.event_lifecycle import (
    CatalogEvent,
    CatalogPage,
    ContinuousEventLifecycle,
    EventLifecycleRecord,
    EventPhase,
)
from autosport.domain import MarketEvent
from autosport.opponent_intelligence import OpponentIntelligenceStore
from autosport.product_runtime import (
    ProductCompositionError,
    build_autonomous_product_runtime,
)
from autosport.participant_identity import (
    AliasRecord,
    EntityIdentity,
    EntityKind,
    ParticipantIdentityRegistry,
    RosterMembership,
)
from autosport.sport_memory_result_materializer import (
    SportMemoryResultBinding,
    SportMemoryResultMaterializationError,
    SportMemoryResultMaterializer,
)
from autosport.storage import SQLiteMarketStore


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
T0 = "2026-09-20T10:00:00Z"
T1 = "2026-09-20T10:01:00Z"
T2 = "2026-09-20T10:02:00Z"
T3 = "2026-09-20T10:03:00Z"
T4 = "2026-09-20T10:04:00Z"
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


def _roster(
    entity_id: str,
    *,
    available_at: str = T0,
    event_id: str = EVENT_ID,
) -> RosterMembership:
    return RosterMembership(
        event_id=event_id,
        source_id="provider-a",
        entity_id=entity_id,
        member_from=T0,
        member_until=None,
        available_at=available_at,
        evidence_sha256=SHA_A,
    )


def _quote(
    selection: str,
    sequence: int,
    *,
    ingest_ts: str = T1,
    competition_id: str = "comp-tour-42",
) -> MarketEvent:
    return MarketEvent(
        event_id=EVENT_ID,
        market_id="match-outcome",
        selection_id=selection,
        decimal_odds=Decimal("2.0"),
        observed_ts=ingest_ts,
        source_id="provider-a",
        sequence=sequence,
        ingest_ts=ingest_ts,
        sport="tennis",
        competition_id=competition_id,
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
        raise AssertionError("result-materializer tests do not consume collector deltas")

    def resolve(
        self,
        record: EventLifecycleRecord,
        *,
        as_of: str,
    ) -> SettlementResolution | None:
        return self.resolution


class _ReplacementProductSource(_ProductSource):
    settlement_authority_id = "provider-a-results-v2"


class _AttackerAuthority:
    def resolve(self, record, *, as_of: str):
        raise AssertionError("attacker resolver must never become product authority")


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


def _binding(
    materializer: SportMemoryResultMaterializer,
    market_store: SQLiteMarketStore,
    *,
    as_of: str = T1,
) -> SportMemoryResultBinding:
    subject = next(
        event
        for event in market_store.events(EVENT_ID)
        if event.selection_id == "sel-alex-17"
    )
    return materializer.freeze_binding(
        event_identity=EVENT_ID,
        source_id="provider-a",
        subject_alias="alex",
        opponent_alias="blair",
        sport_id="tennis",
        league_alias="tour",
        market_context_id="match-outcome",
        subject_quote_key=subject.quote_key,
        as_of=as_of,
        subject_selection_id="sel-alex-17",
        opponent_selection_id="sel-blair-23",
        competition_id="comp-tour-42",
    )


def _settlement(
    materializer: SportMemoryResultMaterializer,
    binding: SportMemoryResultBinding,
    outcome: str = "win",
    *,
    available_at: str = T2,
    evidence_sha256: str = SHA_B,
    event_identity: str = EVENT_ID,
    evidence_id: str = "result-1",
    settlement_ref: str = "settlement-1",
) -> SettlementResolution:
    opponent_outcome = "void" if outcome == "void" else ("loss" if outcome == "win" else "win")
    assert binding.opponent_quote_key is not None
    canonical = SettlementResolution(
        event_identity=event_identity,
        settlement_ref=settlement_ref,
        quote_outcomes={
            binding.subject_quote_key: outcome,
            binding.opponent_quote_key: opponent_outcome,
        },
        evidence_id=evidence_id,
        evidence_sha256=evidence_sha256,
        available_at=available_at,
    )
    return _authoritative_assertion(materializer, canonical)


def _authoritative_assertion(
    materializer: SportMemoryResultMaterializer,
    canonical: SettlementResolution,
) -> SettlementResolution:
    authority = materializer.runtime.coordinator.outcome_authority
    assert isinstance(authority, _ProductSource)
    authority.resolution = canonical
    return replace(canonical)


def test_cold_process_can_import_result_materializer() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import autosport.sport_memory_result_materializer"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_arbitrary_protocol_resolver_is_not_a_materializer_capability(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    _binding(materializer, market_store)
    attacker = _AttackerAuthority()

    with pytest.raises(
        TypeError,
        match="product-owned AutonomousProductRuntime",
    ):
        SportMemoryResultMaterializer(store, attacker)  # type: ignore[arg-type]

    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_public_product_builder_rejects_detached_outcome_resolver(tmp_path):
    source = _ProductSource()
    with pytest.raises(
        ProductCompositionError,
        match="settlement outcome authority must be owned by the configured product source",
    ):
        build_autonomous_product_runtime(
            workspace=tmp_path / "detached-authority",
            source=source,
            outcome_authority=_AttackerAuthority(),
            clock=lambda: T2,
        )


def test_restart_rejects_settlement_authority_identity_substitution(tmp_path):
    first_source = _ProductSource()
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=first_source,
        outcome_authority=first_source,
        clock=lambda: T2,
    )
    runtime.close()

    replacement = _ReplacementProductSource()
    with pytest.raises(
        ProductCompositionError,
        match="settlement authority identity conflicts with durable product composition",
    ):
        build_autonomous_product_runtime(
            workspace=tmp_path,
            source=replacement,
            outcome_authority=replacement,
            clock=lambda: T3,
        )


def test_restart_accepts_same_settlement_resolver_after_install_relocation(tmp_path):
    first_source = _ProductSource()
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=first_source,
        outcome_authority=first_source,
        clock=lambda: T2,
    )
    runtime.close()

    original_code = _ProductSource.resolve.__code__
    try:
        _ProductSource.resolve.__code__ = original_code.replace(
            co_filename=r"C:\\relocated\\Autosport\\provider_source.py",
            co_firstlineno=original_code.co_firstlineno + 100,
        )
        relocated_source = _ProductSource()
        reopened = build_autonomous_product_runtime(
            workspace=tmp_path,
            source=relocated_source,
            outcome_authority=relocated_source,
            clock=lambda: T3,
        )
        reopened.close()
    finally:
        _ProductSource.resolve.__code__ = original_code


def test_restart_rejects_changed_settlement_resolver_semantic_identity(tmp_path):
    first_source = _ProductSource()
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=first_source,
        outcome_authority=first_source,
        clock=lambda: T2,
    )
    runtime.close()

    original_identity = _ProductSource.settlement_resolver_implementation_id
    try:
        _ProductSource.settlement_resolver_implementation_id = (
            "provider-a-results-resolver-v2"
        )
        replacement = _ProductSource()
        with pytest.raises(
            ProductCompositionError,
            match="settlement authority identity conflicts with durable product composition",
        ):
            build_autonomous_product_runtime(
                workspace=tmp_path,
                source=replacement,
                outcome_authority=replacement,
                clock=lambda: T3,
            )
    finally:
        _ProductSource.settlement_resolver_implementation_id = original_identity


def test_per_instance_settlement_resolver_identity_shadow_is_rejected(tmp_path):
    source = _ProductSource()
    source.settlement_resolver_implementation_id = "forged-resolver-v1"
    with pytest.raises(
        ProductCompositionError,
        match="forbids per-instance implementation identity shadowing",
    ):
        build_autonomous_product_runtime(
            workspace=tmp_path,
            source=source,
            outcome_authority=source,
            clock=lambda: T2,
        )


def test_caller_constructed_exact_settlement_cannot_authorize_memory_truth(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)
    assert binding.opponent_quote_key is not None

    for outcome in ("win", "loss", "void"):
        canonical_outcome = "loss" if outcome == "win" else "win"
        _settlement(materializer, binding, canonical_outcome)
        opponent_outcome = (
            "void" if outcome == "void" else ("loss" if outcome == "win" else "win")
        )
        forged = SettlementResolution(
            event_identity=binding.event_identity,
            settlement_ref="settlement-1",
            quote_outcomes={
                binding.subject_quote_key: outcome,
                binding.opponent_quote_key: opponent_outcome,
            },
            evidence_id="result-1",
            evidence_sha256=SHA_B,
            available_at=T2,
        )
        with pytest.raises(
            SportMemoryResultMaterializationError,
            match="settlement assertion differs from product-owned outcome authority",
        ):
            materializer.materialize(binding, forged, as_of=T2)

    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_caller_settlement_payload_must_equal_re_resolved_product_truth(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)
    canonical = _settlement(materializer, binding, "win")
    assert binding.opponent_quote_key is not None

    forged = replace(
        canonical,
        quote_outcomes={
            binding.subject_quote_key: "loss",
            binding.opponent_quote_key: "win",
        },
    )
    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="settlement assertion differs from product-owned outcome authority",
    ):
        materializer.materialize(binding, forged, as_of=T2)

    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_settlement_reference_and_digest_are_assertions_against_product_truth(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)
    canonical = _settlement(materializer, binding, "win")

    for forged in (
        replace(canonical, settlement_ref="caller-settlement"),
        replace(canonical, evidence_sha256=SHA_C),
    ):
        with pytest.raises(
            SportMemoryResultMaterializationError,
            match="settlement assertion differs from product-owned outcome authority",
        ):
            materializer.materialize(binding, forged, as_of=T2)

    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_provider_ids_and_canonical_roots_are_bound_separately_from_display_aliases(tmp_path):
    _, _, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)

    assert binding.subject_selection_id == "sel-alex-17"
    assert binding.subject_selection_id != binding.subject_alias
    assert binding.opponent_selection_id == "sel-blair-23"
    assert binding.opponent_selection_id != binding.opponent_alias
    assert binding.competition_id == "comp-tour-42"
    assert binding.competition_id != binding.league_alias
    assert binding.subject_entity_id == "p-alex"
    assert binding.opponent_entity_id == "p-blair"
    assert binding.league_entity_id == "league-tour"
    assert binding.provider_inference_as_of is None
    assert binding.identity_sha256 is not None
    assert binding.opponent_quote_key is not None
    market_store.close()


def test_canonical_root_identity_tamper_fails_closed(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)
    forged = replace(binding, subject_entity_id="p-forged")

    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="canonical pre-reveal",
    ):
        materializer.materialize(forged, _settlement(materializer, forged), as_of=T2)
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_pre_reveal_provider_inference_persists_causal_cutoff(tmp_path):
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
        competition_id="comp-tour-42",
    )

    assert binding.opponent_selection_id == "sel-blair-23"
    assert binding.provider_inference_as_of == T1
    receipt = materializer.materialize(binding, _settlement(materializer, binding), as_of=T2)
    assert receipt.performance_id is not None
    assert len(store.graph_edges(as_of=T2)) == 1
    market_store.close()


def test_post_reveal_identity_correction_cannot_backdate_provider_inference(tmp_path):
    identities, store, market_store, materializer = _authorities(tmp_path)
    identities.add_alias(_alias("sel-blair-shadow", "p-blair"))
    market_store.append(_quote("sel-blair-shadow", 3))
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
            competition_id="comp-tour-42",
        )

    shadow = identities.resolve_alias_record(
        "provider-a",
        "sel-blair-shadow",
        as_of=T1,
    )
    identities.add_entity(_entity("p-charlie"))
    identities.add_alias(
        _alias(
            "sel-blair-shadow",
            "p-charlie",
            available_at=T3,
            supersedes_record_id=shadow.record_id,
        )
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
        as_of=T3,
        subject_selection_id="sel-alex-17",
        competition_id="comp-tour-42",
    )
    assert binding.opponent_selection_id == "sel-blair-23"
    assert binding.frozen_at == T1
    assert binding.provider_inference_as_of == T3

    settlement = _settlement(materializer, binding, available_at=T2)
    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="provider-selection inference must predate settlement reveal",
    ):
        materializer.materialize(binding, settlement, as_of=T3)

    forged = replace(binding, provider_inference_as_of=None)
    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="canonical pre-reveal",
    ):
        materializer.materialize(forged, _settlement(materializer, forged, available_at=T2), as_of=T3)
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_result_projection_is_exactly_once_across_restart_and_causally_hidden_before_reveal(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)
    settlement = _settlement(materializer, binding)

    first = materializer.materialize(binding, settlement, as_of=T2)
    duplicate = materializer.materialize(binding, settlement, as_of=T3)

    assert first == duplicate
    assert first.performance_id is not None
    assert store.graph_edges(as_of=T1) == ()
    visible = store.graph_edges(as_of=T2)
    assert len(visible) == 1
    assert visible[0].performance_id == first.performance_id
    assert visible[0].score == "1"

    materializer.runtime.close()
    reopened_store = OpponentIntelligenceStore(
        tmp_path / "opponents.json",
        ParticipantIdentityRegistry(tmp_path / "identity.json"),
    )
    reopened_source = _ProductSource()
    reopened_runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=reopened_source,
        clock=lambda: T3,
        outcome_authority=reopened_source,
    )
    reopened_market = reopened_runtime.market_store
    reopened = SportMemoryResultMaterializer(
        reopened_store,
        reopened_runtime,
    )
    replay_binding = _binding(reopened, reopened_market)
    replay = reopened.materialize(replay_binding, _settlement(reopened, replay_binding), as_of=T3)
    assert replay == first
    assert len(reopened_store.graph_edges(as_of=T3)) == 1
    reopened_runtime.close()


def test_result_projection_requires_explicit_store_correction_lineage(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)
    first = materializer.materialize(binding, _settlement(materializer, binding), as_of=T2)
    correction = _settlement(
        materializer,
        binding,
        "loss",
        available_at=T3,
        evidence_sha256=SHA_C,
        evidence_id="result-2",
    )

    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="opponent store rejected",
    ):
        materializer.materialize(binding, correction, as_of=T3)

    second = materializer.materialize(
        binding,
        correction,
        as_of=T3,
        supersedes_performance_id=first.performance_id,
    )
    assert second.performance_id is not None
    assert second.performance_id != first.performance_id
    assert len(store.graph_edges(as_of=T3)) == 1
    assert store.graph_edges(as_of=T3)[0].score == "0"
    market_store.close()


def test_identity_retarget_between_freeze_and_reveal_fails_closed(tmp_path):
    identities, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)
    old = identities.resolve_alias_record("provider-a", "alex", as_of=T1)
    identities.add_entity(_entity("p-alex-corrected"))
    identities.add_alias(
        _alias(
            "alex",
            "p-alex-corrected",
            available_at=T2,
            supersedes_record_id=old.record_id,
        )
    )

    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="identity or event roster changed|canonical participant alias",
    ):
        materializer.materialize(
            binding,
            _settlement(materializer, binding, available_at=T3),
            as_of=T3,
        )
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_late_event_roster_correction_requires_explicit_restatement(tmp_path):
    identities, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)
    identities.add_entity(_entity("p-late"))
    identities.add_roster_membership(_roster("p-late", available_at=T2))

    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="identity or event roster changed",
    ):
        materializer.materialize(
            binding,
            _settlement(materializer, binding, available_at=T3),
            as_of=T3,
        )
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_foreign_event_member_and_selection_alias_collision_fail_closed(tmp_path):
    identities, store, market_store, materializer = _authorities(tmp_path)
    identities.add_entity(_entity("p-charlie"))
    identities.add_alias(_alias("charlie", "p-charlie"))
    identities.add_alias(_alias("sel-charlie-99", "p-charlie"))
    market_store.append(_quote("sel-charlie-99", 3))
    subject = next(
        event
        for event in market_store.events(EVENT_ID)
        if event.selection_id == "sel-alex-17"
    )

    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="canonical members of the bound event roster",
    ):
        materializer.freeze_binding(
            event_identity=EVENT_ID,
            source_id="provider-a",
            subject_alias="alex",
            opponent_alias="charlie",
            sport_id="tennis",
            league_alias="tour",
            market_context_id="match-outcome",
            subject_quote_key=subject.quote_key,
            as_of=T1,
            subject_selection_id="sel-alex-17",
            opponent_selection_id="sel-charlie-99",
            competition_id="comp-tour-42",
        )

    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="subject quote key lacks exact canonical pre-reveal market evidence",
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
            subject_selection_id="sel-blair-23",
            opponent_selection_id="sel-alex-17",
            competition_id="comp-tour-42",
        )
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_same_name_distinct_root_and_foreign_competition_fail_closed(tmp_path):
    identities, store, market_store, materializer = _authorities(tmp_path)
    identities.add_entity(_entity("p-alex-shadow"))
    identities.add_alias(_alias("sel-alex-shadow", "p-alex-shadow"))
    identities.add_roster_membership(_roster("p-alex-shadow"))
    market_store.append(_quote("sel-alex-shadow", 3))
    shadow_quote = next(
        event
        for event in market_store.events(EVENT_ID)
        if event.selection_id == "sel-alex-shadow"
    )

    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="provider subject selection identity does not match canonical participant alias",
    ):
        materializer.freeze_binding(
            event_identity=EVENT_ID,
            source_id="provider-a",
            subject_alias="alex",
            opponent_alias="blair",
            sport_id="tennis",
            league_alias="tour",
            market_context_id="match-outcome",
            subject_quote_key=shadow_quote.quote_key,
            as_of=T1,
            subject_selection_id="sel-alex-shadow",
            opponent_selection_id="sel-blair-23",
            competition_id="comp-tour-42",
        )

    identities.add_entity(_entity("league-foreign", kind=EntityKind.LEAGUE))
    identities.add_alias(_alias("comp-foreign-9", "league-foreign"))
    market_store.append(_quote("sel-alex-17", 4, competition_id="comp-foreign-9"))
    market_store.append(_quote("sel-blair-23", 5, competition_id="comp-foreign-9"))
    foreign_subject = max(
        (
            event
            for event in market_store.events(EVENT_ID)
            if event.selection_id == "sel-alex-17"
            and event.competition_id == "comp-foreign-9"
        ),
        key=lambda event: event.sequence,
    )
    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="provider competition identity does not match canonical league alias",
    ):
        materializer.freeze_binding(
            event_identity=EVENT_ID,
            source_id="provider-a",
            subject_alias="alex",
            opponent_alias="blair",
            sport_id="tennis",
            league_alias="tour",
            market_context_id="match-outcome",
            subject_quote_key=foreign_subject.quote_key,
            as_of=T1,
            subject_selection_id="sel-alex-17",
            opponent_selection_id="sel-blair-23",
            competition_id="comp-foreign-9",
        )
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_future_or_noncanonical_backdated_binding_cannot_enter_memory(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)

    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="not causally available",
    ):
        materializer.materialize(binding, _settlement(materializer, binding), as_of=T1)

    forged = replace(binding, frozen_at=T0)
    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="canonical pre-reveal|lacks canonical pre-reveal",
    ):
        materializer.materialize(forged, _settlement(materializer, forged), as_of=T2)

    post_reveal_backdate = replace(
        binding,
        frozen_at=T1,
        evidence_sha256=SHA_A,
    )
    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="canonical pre-reveal",
    ):
        materializer.materialize(
            post_reveal_backdate,
            _settlement(materializer, post_reveal_backdate),
            as_of=T2,
        )
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_binding_digest_tamper_and_swapped_quote_fail_closed(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)
    opponent = next(
        event
        for event in market_store.events(EVENT_ID)
        if event.selection_id == "sel-blair-23"
    )

    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="canonical pre-reveal",
    ):
        materializer.materialize(
            replace(binding, evidence_sha256=SHA_C),
            _settlement(materializer, replace(binding, evidence_sha256=SHA_C)),
            as_of=T2,
        )

    swapped = replace(binding, subject_quote_key=opponent.quote_key)
    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="subject quote key",
    ):
        materializer.materialize(swapped, _settlement(materializer, swapped), as_of=T2)
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_unrelated_alias_or_market_context_cannot_reuse_valid_source_digest(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)

    for forged in (
        replace(binding, opponent_alias="charlie"),
        replace(binding, market_context_id="unrelated-market"),
        replace(binding, league_alias="other-tour"),
    ):
        with pytest.raises(
            SportMemoryResultMaterializationError,
            match="canonical pre-reveal|canonical identity|canonical participant",
        ):
            materializer.materialize(forged, _settlement(materializer, forged), as_of=T2)
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_event_and_subject_quote_must_match_frozen_binding(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)

    with pytest.raises(SportMemoryResultMaterializationError, match="event does not match"):
        materializer.materialize(
            binding,
            _settlement(materializer, binding, event_identity="provider-a:event-2"),
            as_of=T2,
        )

    settlement = _authoritative_assertion(
        materializer,
        SettlementResolution(
            event_identity=EVENT_ID,
            settlement_ref="settlement-1",
            quote_outcomes={"other-quote": "win"},
            evidence_id="result-1",
            evidence_sha256=SHA_B,
            available_at=T2,
        ),
    )
    with pytest.raises(
        SportMemoryResultMaterializationError,
        match="canonical frozen subject quote",
    ):
        materializer.materialize(binding, settlement, as_of=T2)
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_initial_void_is_consumed_without_fabricating_performance(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)
    settlement = _settlement(
        materializer,
        binding,
        "void",
        available_at=T2,
        evidence_id="result-void",
    )

    receipt = materializer.materialize(binding, settlement, as_of=T2)
    assert receipt.outcome == "void"
    assert receipt.performance_id is None
    assert store.graph_edges(as_of=T3) == ()
    market_store.close()


def test_win_to_void_correction_retires_performance_across_restart_without_fake_score(tmp_path):
    _, store, market_store, materializer = _authorities(tmp_path)
    binding = _binding(materializer, market_store)
    win = materializer.materialize(binding, _settlement(materializer, binding), as_of=T2)
    assert win.performance_id is not None
    assert len(store.graph_edges(as_of=T2)) == 1

    void = _settlement(
        materializer,
        binding,
        "void",
        available_at=T3,
        evidence_sha256=SHA_C,
        evidence_id="result-void-correction",
    )
    receipt = materializer.materialize(
        binding,
        void,
        as_of=T3,
        supersedes_performance_id=win.performance_id,
    )
    assert receipt.outcome == "void"
    assert receipt.performance_id is None
    assert len(store.graph_edges(as_of=T2)) == 1
    assert store.graph_edges(as_of=T3) == ()

    materializer.runtime.close()
    reopened_store = OpponentIntelligenceStore(
        tmp_path / "opponents.json",
        ParticipantIdentityRegistry(tmp_path / "identity.json"),
    )
    reopened_source = _ProductSource()
    reopened_runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=reopened_source,
        clock=lambda: T3,
        outcome_authority=reopened_source,
    )
    reopened_market = reopened_runtime.market_store
    reopened = SportMemoryResultMaterializer(
        reopened_store,
        reopened_runtime,
    )
    reopened_binding = _binding(reopened, reopened_market)
    assert reopened_store.graph_edges(as_of=T4) == ()
    replay = reopened.materialize(
        reopened_binding,
        _settlement(
            reopened,
            reopened_binding,
            "void",
            available_at=T3,
            evidence_sha256=SHA_C,
            evidence_id="result-void-correction",
            ),
        as_of=T4,
        supersedes_performance_id=win.performance_id,
    )
    assert replay == receipt
    assert reopened_store.graph_edges(as_of=T4) == ()
    reopened_runtime.close()

    script = f"""
import sys
from pathlib import Path
from autosport.opponent_intelligence import OpponentIntelligenceStore
from autosport.participant_identity import ParticipantIdentityRegistry

assert 'autosport.sport_memory_result_materializer' not in sys.modules
root = Path({str(tmp_path)!r})
store = OpponentIntelligenceStore(
    root / 'opponents.json',
    ParticipantIdentityRegistry(root / 'identity.json'),
)
assert len(store.graph_edges(as_of={T2!r})) == 1
assert store.graph_edges(as_of={T4!r}) == ()
assert 'autosport.sport_memory_result_materializer' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", script], check=True)
