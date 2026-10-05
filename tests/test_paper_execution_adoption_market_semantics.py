from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.paper_execution_adoption as adoption_module

from autosport.domain import MarketEvent, TicketLeg
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)


_QUOTE_AT = "2026-10-05T00:00:00+00:00"
_STARTED_AT = "2026-10-05T00:00:00.100000+00:00"
_S1 = "soccer:h2h:v1"
_S2 = "soccer:h2h:v2"


class _DecimalSubclass(Decimal):
    pass


class _MarketEventSubclass(MarketEvent):
    pass


class _HostileSemanticsIdentity:
    comparisons = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return True

    def __str__(self) -> str:
        raise AssertionError("hostile semantics stringification executed")


def _event(semantics: str | None = _S1) -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2.50"),
        observed_ts=_QUOTE_AT,
        source_id="paper-venue",
        sequence=1,
        source_ts=_QUOTE_AT,
        ingest_ts=_QUOTE_AT,
        sport="soccer",
        exchange_side="back",
        market_semantics_id=semantics,
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="semantics-test",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="test-seeded-model",
        seed="fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _runtime(tmp_path: Path) -> PaperExecutionAdoptionRuntime:
    return PaperExecutionAdoptionRuntime(
        book=PaperBook("100.00"),
        ledger=PaperExecutionLedger(tmp_path / "paper-execution.jsonl"),
        config=_config(),
        max_quote_age=timedelta(seconds=5),
        paper_book_path=tmp_path / "paper-book.json",
    )


def _prepared(
    runtime: PaperExecutionAdoptionRuntime,
    semantics: str | None = _S1,
):
    return runtime.prepare_paper_value_action(
        event=_event(semantics),
        stake=Decimal("10.00"),
        decision_id="decision-1",
        account_id="paper-account",
        bankroll_id="paper-bankroll",
        currency="EUR",
    )


@pytest.mark.parametrize(
    "identity",
    ["", " Soccer:h2h:v1", "SOCCER:H2H:V1", "unknown", "mixed", "unspecified"],
)
def test_exposure_binding_rejects_noncanonical_market_semantics(identity: str) -> None:
    with pytest.raises(ValueError, match="market_semantics_id"):
        PaperExposureBinding(
            action_id="action-1",
            sport="soccer",
            bankroll_id="paper-bankroll",
            currency="EUR",
            market_semantics_id=identity,
        )


def test_adoption_semantics_and_type_authority_ignore_public_rebinding(
    monkeypatch,
) -> None:
    event = _event(_S1)
    leg = TicketLeg(
        event.event_id,
        event.market_id,
        event.selection_id,
        event.decimal_odds,
        sport=event.sport,
        exchange_side=event.exchange_side,
        market_semantics_id=event.market_semantics_id,
    )

    monkeypatch.setattr(adoption_module, "TicketLeg", object)
    monkeypatch.setattr(adoption_module, "MarketEvent", object)
    monkeypatch.setattr(
        adoption_module,
        "_canonical_semantic_identity",
        lambda *args, **kwargs: "forged",
    )

    PaperExecutionAdoptionRuntime._require_leg_quote_identity(leg, event)

    with pytest.raises(ValueError, match="market_semantics_id"):
        PaperExposureBinding(
            action_id="action-rebind",
            sport="soccer",
            bankroll_id="paper-bankroll",
            currency="EUR",
            market_semantics_id="SOCCER:H2H:V1",
        )


def test_leg_quote_identity_rejects_hostile_mutation_before_comparison() -> None:
    event = _event(_S1)
    leg = TicketLeg(
        event.event_id,
        event.market_id,
        event.selection_id,
        event.decimal_odds,
        sport=event.sport,
        exchange_side=event.exchange_side,
        market_semantics_id=event.market_semantics_id,
    )
    hostile = _HostileSemanticsIdentity()
    _HostileSemanticsIdentity.comparisons = 0
    object.__setattr__(leg, "market_semantics_id", hostile)

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="market_semantics_id must remain canonical text or None",
    ):
        PaperExecutionAdoptionRuntime._require_leg_quote_identity(leg, event)

    assert _HostileSemanticsIdentity.comparisons == 0


def test_exchange_side_gate_rejects_hostile_value_before_comparison() -> None:
    hostile = _HostileSemanticsIdentity()
    _HostileSemanticsIdentity.comparisons = 0

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="exchange side must remain canonical text or None",
    ):
        PaperExecutionAdoptionRuntime._require_back_compatible_exchange_side(hostile)

    assert _HostileSemanticsIdentity.comparisons == 0


def test_leg_quote_identity_rejects_semantics_substitution() -> None:
    event = _event(_S2)
    leg = TicketLeg(
        event.event_id,
        event.market_id,
        event.selection_id,
        event.decimal_odds,
        sport=event.sport,
        exchange_side=event.exchange_side,
        market_semantics_id=_S1,
    )
    assert leg.quote_key == event.quote_key

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="ticket leg identity does not match canonical execution quote",
    ):
        PaperExecutionAdoptionRuntime._require_leg_quote_identity(leg, event)


@pytest.mark.parametrize(
    ("leg_semantics", "event_semantics"),
    [(_S1, None), (None, _S1)],
)
def test_leg_quote_identity_rejects_none_concrete_semantics_mismatch(
    leg_semantics: str | None,
    event_semantics: str | None,
) -> None:
    event = _event(event_semantics)
    leg = TicketLeg(
        event.event_id,
        event.market_id,
        event.selection_id,
        event.decimal_odds,
        sport=event.sport,
        exchange_side=event.exchange_side,
        market_semantics_id=leg_semantics,
    )
    assert leg.quote_key == event.quote_key

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="ticket leg identity does not match canonical execution quote",
    ):
        PaperExecutionAdoptionRuntime._require_leg_quote_identity(leg, event)


def test_minted_prepared_rejects_post_mint_semantics_mutation(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, _S1)
    binding = prepared.exposure_bindings[0]

    object.__setattr__(binding, "market_semantics_id", _S2)

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="changed after canonical authority minting",
    ):
        runtime.expected_run_id(prepared, "trigger-mutated-semantics")


def test_minted_prepared_rejects_hostile_semantics_before_comparison(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, _S1)
    binding = prepared.exposure_bindings[0]
    hostile = _HostileSemanticsIdentity()
    _HostileSemanticsIdentity.comparisons = 0

    object.__setattr__(binding, "market_semantics_id", hostile)

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="market_semantics_id must remain canonical text",
    ):
        runtime.expected_run_id(prepared, "trigger-hostile-semantics")

    assert _HostileSemanticsIdentity.comparisons == 0


def test_minted_prepared_rejects_post_mint_action_economic_mutation(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, _S1)
    action = prepared.execution_plan.actions[0]

    object.__setattr__(action, "requested_stake", Decimal("99.00"))

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="changed after canonical authority minting",
    ):
        runtime._require_minted(prepared)


def test_minted_prepared_rejects_post_mint_action_identity_mutation(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, _S1)
    action = prepared.execution_plan.actions[0]

    object.__setattr__(action, "selection_id", "selection-forged")

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="changed after canonical authority minting",
    ):
        runtime._require_minted(prepared)


def test_minted_prepared_rejects_post_mint_plan_identity_mutation(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, _S1)

    object.__setattr__(prepared.execution_plan, "decision_id", "decision-forged")

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="changed after canonical authority minting",
    ):
        runtime._require_minted(prepared)


def test_minted_prepared_rejects_post_mint_intent_evidence_mutation(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, _S1)

    object.__setattr__(prepared, "intent_evidence_json", '{"forged":true}')

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="changed after canonical authority minting",
    ):
        runtime._require_minted(prepared)


def test_minted_prepared_rejects_noncanonical_post_mint_action_value_before_hooks(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, _S1)
    action = prepared.execution_plan.actions[0]
    hostile = _HostileSemanticsIdentity()
    _HostileSemanticsIdentity.comparisons = 0

    object.__setattr__(action, "selection_id", hostile)

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="action text must remain canonical",
    ):
        runtime._require_minted(prepared)

    assert _HostileSemanticsIdentity.comparisons == 0


def test_minted_prepared_rejects_post_mint_binding_replacement(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, _S1)
    original = prepared.exposure_bindings[0]
    replacement = PaperExposureBinding(
        action_id=original.action_id,
        sport=original.sport,
        bankroll_id=original.bankroll_id,
        currency=original.currency,
        market_semantics_id=_S2,
    )

    object.__setattr__(prepared, "exposure_bindings", (replacement,))

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="changed after canonical authority minting",
    ):
        runtime.expected_run_id(prepared, "trigger-replaced-binding")


def test_detached_plan_fingerprint_matches_canonical_execution_plan(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, _S1)

    payload = runtime._exposure_scope_payload(prepared)

    assert payload["plan_fingerprint"] == prepared.execution_plan.fingerprint


def test_exposure_scope_digest_ignores_public_hash_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, _S1)
    expected = runtime._exposure_scope_payload(prepared)

    def forbidden(*args, **kwargs):
        raise AssertionError("rebound adoption digest primitive executed")

    monkeypatch.setattr(adoption_module.json, "dumps", forbidden)
    monkeypatch.setattr(adoption_module.hashlib, "sha256", forbidden)
    monkeypatch.setattr(adoption_module, "_digest", forbidden)

    assert runtime._exposure_scope_payload(prepared) == expected


def test_minted_commitment_ignores_execution_plan_fingerprint_rebinding(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, _S1)

    def forbidden(self):
        raise AssertionError("rebound ExecutionPlan.fingerprint executed")

    monkeypatch.setattr(
        type(prepared.execution_plan),
        "fingerprint",
        property(forbidden),
    )

    runtime._require_minted(prepared)


def test_paper_value_bridge_rejects_market_event_subclass(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    canonical = _event(_S1)
    forged = _MarketEventSubclass(
        event_id=canonical.event_id,
        market_id=canonical.market_id,
        selection_id=canonical.selection_id,
        decimal_odds=canonical.decimal_odds,
        observed_ts=canonical.observed_ts,
        source_id=canonical.source_id,
        sequence=canonical.sequence,
        market_type=canonical.market_type,
        status=canonical.status,
        source_ts=canonical.source_ts,
        ingest_ts=canonical.ingest_ts,
        score_state=canonical.score_state,
        metadata=canonical.metadata,
        sport=canonical.sport,
        competition_id=canonical.competition_id,
        market_semantics_id=canonical.market_semantics_id,
        provider_source_class=canonical.provider_source_class,
        exchange_side=canonical.exchange_side,
    )

    with pytest.raises(TypeError, match="canonical MarketEvent"):
        runtime.prepare_paper_value_action(
            event=forged,
            stake=Decimal("10"),
            decision_id="decision-subclass",
            account_id="paper-account",
            bankroll_id="paper-bankroll",
            currency="EUR",
        )


def test_paper_value_bridge_rejects_decimal_subclass_stake(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    with pytest.raises(ValueError, match="exact Decimal"):
        runtime.prepare_paper_value_action(
            event=_event(_S1),
            stake=_DecimalSubclass("10"),
            decision_id="decision-decimal-subclass",
            account_id="paper-account",
            bankroll_id="paper-bankroll",
            currency="EUR",
        )


def test_durable_exposure_scope_binds_market_semantics(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, _S1)

    payload = runtime._exposure_scope_payload(prepared)

    assert payload["bindings"][0]["market_semantics_id"] == _S1
    changed = _prepared(runtime, _S2)
    changed_payload = runtime._exposure_scope_payload(changed)
    assert changed_payload["binding_sha256"] != payload["binding_sha256"]


def test_durable_exposure_scope_preserves_legacy_none_shape(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime, None)

    payload = runtime._exposure_scope_payload(prepared)

    assert "market_semantics_id" not in payload["bindings"][0]


def test_paper_value_prepared_binding_carries_market_semantics(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    prepared = _prepared(runtime)

    assert prepared.exposure_bindings[0].market_semantics_id == _S1


def test_paper_value_action_identity_changes_with_market_semantics(tmp_path) -> None:
    runtime = _runtime(tmp_path)

    first = _prepared(runtime, _S1)
    second = _prepared(runtime, _S2)

    first_action = first.execution_plan.actions[0]
    second_action = second.execution_plan.actions[0]
    assert first_action.quote_id != second_action.quote_id
    assert first_action.action_id != second_action.action_id
    assert first.execution_plan.plan_id != second.execution_plan.plan_id


def test_paper_value_materialization_preserves_market_semantics(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime)

    result = runtime.execute(
        prepared=prepared,
        trigger_id="trigger-1",
        started_at=_STARTED_AT,
        materialize_exposure=True,
    )

    assert len(result.ticket_ids) == 1
    ticket = runtime.book.tickets[result.ticket_ids[0]]
    assert ticket.legs[0].market_semantics_id == _S1

    attempt = result.run.attempts[0]
    action = prepared.execution_plan.actions[0]
    binding = prepared.exposure_bindings[0]
    assert runtime._ticket_matches_attempt(
        ticket=ticket,
        attempt=attempt,
        action=action,
        binding=binding,
    )

    object.__setattr__(ticket.legs[0], "market_semantics_id", _S2)
    assert not runtime._ticket_matches_attempt(
        ticket=ticket,
        attempt=attempt,
        action=action,
        binding=binding,
    )


def test_restart_materialization_preserves_same_semantics_identity(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    prepared = _prepared(runtime)
    first = runtime.execute(
        prepared=prepared,
        trigger_id="trigger-restart",
        started_at=_STARTED_AT,
        materialize_exposure=False,
    )
    assert first.ticket_ids == ()

    book_path = tmp_path / "paper-book.json"
    reloaded = PaperBook.load(book_path)
    restarted = PaperExecutionAdoptionRuntime(
        book=reloaded,
        ledger=runtime.ledger,
        config=runtime.config,
        max_quote_age=runtime.max_quote_age,
        paper_book_path=book_path,
    )
    restarted_prepared = _prepared(restarted)
    resumed = restarted.execute(
        prepared=restarted_prepared,
        trigger_id="trigger-restart",
        started_at=_STARTED_AT,
        materialize_exposure=True,
    )

    assert len(resumed.ticket_ids) == 1
    ticket = reloaded.tickets[resumed.ticket_ids[0]]
    assert ticket.legs[0].market_semantics_id == _S1
