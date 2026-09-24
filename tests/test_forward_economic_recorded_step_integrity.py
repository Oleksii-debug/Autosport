from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import autosport.forward_economic_evidence as forward_evidence
from autosport.forward_economic_evidence import (
    AlphaAllocation,
    BetSide,
    FamilywiseAlphaRegistry,
    ForwardDecisionObservation,
    ForwardEconomicEvidenceAccumulator,
    ForwardEconomicEvidenceError,
    ForwardEconomicProtocol,
    ResolvedPolicyOutcome,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
UNIVERSE_SHA = "e" * 64
AUTHORITY_SHA = "f" * 64
EXECUTION_SHA = "c" * 64
SETTLEMENT_SHA = "d" * 64
SUBSTITUTED_SETTLEMENT_SHA = "9" * 64


class Resolver:
    def __init__(self, mapping: dict[tuple[int, str], ResolvedPolicyOutcome]) -> None:
        self.mapping = mapping
        self.authority_sha256 = AUTHORITY_SHA

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        return self.mapping[(sequence, policy_id)]


def _protocol() -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="forward-family-step-integrity",
        total_alpha=Decimal("0.2"),
        allocations=(
            AlphaAllocation(
                challenger_id="challenger",
                alpha=Decimal("0.2"),
            ),
        ),
        sealed_at=T0,
    )
    return ForwardEconomicProtocol(
        protocol_id="forward-protocol-step-integrity",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="universe-step-integrity",
        universe_sha256=UNIVERSE_SHA,
        authority_binding_sha256=AUTHORITY_SHA,
        alpha_registry=registry,
        minimum_events=5,
        risk_unit_currency=Decimal("10"),
        maximum_accepted_odds=Decimal("10"),
        maximum_drawdown_currency=Decimal("100"),
        absolute_lambda=Decimal("0.5"),
        paired_lambda=Decimal("0.5"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
    )


def _observation(sequence: int) -> ForwardDecisionObservation:
    suffix = f"{sequence + 1:x}"
    return ForwardDecisionObservation(
        sequence=sequence,
        universe_sha256=UNIVERSE_SHA,
        universe_event_sha256=suffix * 64,
        challenger_decision_sha256=("a" * 63) + suffix,
        champion_decision_sha256=("b" * 63) + suffix,
    )


def _challenger(
    observation: ForwardDecisionObservation,
) -> ResolvedPolicyOutcome:
    return ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=observation.sequence,
        universe_event_sha256=observation.universe_event_sha256,
        decision_sha256=observation.challenger_decision_sha256,
        decision_committed_at=T0
        + timedelta(minutes=2, seconds=observation.sequence),
        side=BetSide.BACK,
        accepted_odds=Decimal("2"),
        accepted_stake=Decimal("10"),
        net_pnl_currency=Decimal("10"),
        execution_evidence_sha256=EXECUTION_SHA,
        execution_accepted_at=T0
        + timedelta(minutes=3, seconds=observation.sequence),
        settlement_evidence_sha256=SETTLEMENT_SHA,
        settlement_available_at=T0
        + timedelta(hours=1, seconds=observation.sequence),
    )


def _champion(
    observation: ForwardDecisionObservation,
) -> ResolvedPolicyOutcome:
    return ResolvedPolicyOutcome(
        policy_id="champion",
        sequence=observation.sequence,
        universe_event_sha256=observation.universe_event_sha256,
        decision_sha256=observation.champion_decision_sha256,
        decision_committed_at=T0
        + timedelta(minutes=2, seconds=observation.sequence),
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal("0"),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
    )


def test_recorded_step_cannot_rebind_positive_evidence_after_publication() -> None:
    observations = [_observation(sequence) for sequence in range(5)]
    mapping: dict[tuple[int, str], ResolvedPolicyOutcome] = {}
    for observation in observations:
        mapping[(observation.sequence, "challenger")] = _challenger(observation)
        mapping[(observation.sequence, "champion")] = _champion(observation)

    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    resolver = Resolver(mapping)
    record_return = None
    for observation in observations:
        record_return = accumulator.record(observation, resolver)
    assert record_return is not None

    before = accumulator.summary()
    assert before.positive_authority_verified is False
    assert before.scientific_promotion_gate_passed is False
    assert (
        accumulator.steps[-1].challenger_settlement_evidence_sha256
        == SETTLEMENT_SHA
    )

    object.__setattr__(
        record_return,
        "challenger_settlement_evidence_sha256",
        SUBSTITUTED_SETTLEMENT_SHA,
    )
    assert (
        accumulator.steps[-1].challenger_settlement_evidence_sha256
        == SETTLEMENT_SHA
    )
    assert accumulator.summary().evidence_sha256 == before.evidence_sha256

    # Dataclass frozen=True is not an authority boundary.  If a caller-visible
    # step is the accumulator's stored object, object.__setattr__ can substitute
    # a different settlement lineage after the statistics were accumulated.
    exposed_step = accumulator.steps[-1]
    try:
        object.__setattr__(
            exposed_step,
            "challenger_settlement_evidence_sha256",
            SUBSTITUTED_SETTLEMENT_SHA,
        )
    except (AttributeError, TypeError):
        # A future implementation may expose a non-mutable value/copy.
        return

    try:
        after = accumulator.summary()
    except ForwardEconomicEvidenceError:
        # Detecting post-publication integrity drift and failing closed is safe.
        return

    # A safe copy-by-value/snapshot implementation keeps canonical published
    # evidence unchanged even if an exposed object can itself be mutated.
    assert (
        accumulator.steps[-1].challenger_settlement_evidence_sha256
        == SETTLEMENT_SHA
    )
    assert after.evidence_sha256 == before.evidence_sha256
    assert after.positive_authority_verified is False
    assert after.scientific_promotion_gate_passed is False


def _filled_accumulator(event_count: int = 5) -> tuple[ForwardEconomicEvidenceAccumulator, Resolver]:
    observations = [_observation(sequence) for sequence in range(event_count + 1)]
    mapping: dict[tuple[int, str], ResolvedPolicyOutcome] = {}
    for observation in observations:
        mapping[(observation.sequence, "challenger")] = _challenger(observation)
        mapping[(observation.sequence, "champion")] = _champion(observation)

    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    resolver = Resolver(mapping)
    for observation in observations[:event_count]:
        accumulator.record(observation, resolver)
    return accumulator, resolver


@pytest.mark.parametrize(
    ("cache_name", "replacement"),
    (
        ("_absolute_log_e", Decimal("999")),
        ("_paired_log_e", Decimal("-999")),
        ("_challenger_total", Decimal("123456")),
        ("_champion_total", Decimal("654321")),
        ("_challenger_peak", Decimal("777")),
        ("_challenger_max_drawdown", Decimal("888")),
    ),
)
def test_summary_fails_closed_on_mutable_aggregate_cache_drift(
    cache_name: str,
    replacement: Decimal,
) -> None:
    accumulator, _resolver = _filled_accumulator()
    committed_steps = accumulator.steps
    object.__setattr__(accumulator, cache_name, replacement)

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="internal aggregate state integrity drift",
    ):
        accumulator.summary()

    assert accumulator.steps == committed_steps


def test_record_revalidates_aggregate_cache_before_appending_next_step() -> None:
    accumulator, resolver = _filled_accumulator(event_count=1)
    committed_steps = accumulator.steps
    object.__setattr__(accumulator, "_paired_log_e", Decimal("123"))

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="internal aggregate state integrity drift",
    ):
        accumulator.record(_observation(1), resolver)

    assert accumulator.steps == committed_steps
    assert accumulator.next_sequence == 1


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    (
        ("challenger_settlement_evidence_sha256", SUBSTITUTED_SETTLEMENT_SHA),
        ("challenger_decision_sha256", "8" * 64),
    ),
)
def test_private_recorded_step_identity_mutation_fails_closed_on_read_paths(
    field_name: str,
    replacement: object,
) -> None:
    accumulator, _resolver = _filled_accumulator()
    before = accumulator.summary()
    assert before.evidence_sha256

    object.__setattr__(accumulator._steps[-1], field_name, replacement)

    for access in (
        lambda: accumulator.steps,
        lambda: accumulator.next_sequence,
        lambda: accumulator.summary(),
    ):
        with pytest.raises(
            ForwardEconomicEvidenceError,
            match="internal recorded step identity integrity drift",
        ):
            access()


def test_record_fails_closed_before_append_on_private_step_identity_drift() -> None:
    accumulator, resolver = _filled_accumulator(event_count=1)
    assert len(accumulator._steps) == 1
    object.__setattr__(
        accumulator._steps[-1],
        "challenger_settlement_evidence_sha256",
        SUBSTITUTED_SETTLEMENT_SHA,
    )

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="internal recorded step identity integrity drift",
    ):
        accumulator.record(_observation(1), resolver)

    assert len(accumulator._steps) == 1


def test_recorded_step_guard_rejects_to_payload_dispatch_rebind(monkeypatch) -> None:
    accumulator, _resolver = _filled_accumulator(event_count=1)

    def forged_to_payload(_self: object) -> dict[str, object]:
        return {"schema_version": "forged"}

    monkeypatch.setattr(
        forward_evidence.ForwardEconomicStep,
        "to_payload",
        forged_to_payload,
    )

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="internal recorded step identity executable integrity drift",
    ):
        accumulator.summary()


def test_recorded_step_guard_rejects_to_payload_code_mutation() -> None:
    accumulator, _resolver = _filled_accumulator(event_count=1)
    method = forward_evidence.ForwardEconomicStep.to_payload
    original_code = method.__code__

    def forged_to_payload(_self: object) -> dict[str, object]:
        raise AssertionError("tampered to_payload body must not execute")

    try:
        method.__code__ = forged_to_payload.__code__
        with pytest.raises(
            ForwardEconomicEvidenceError,
            match="internal recorded step identity executable integrity drift",
        ):
            accumulator.summary()
    finally:
        method.__code__ = original_code


def test_recorded_step_guard_rejects_canonical_digest_rebind(monkeypatch) -> None:
    accumulator, _resolver = _filled_accumulator(event_count=1)

    monkeypatch.setattr(
        forward_evidence,
        "_canonical_digest",
        lambda _value: "0" * 64,
    )

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="internal recorded step identity executable integrity drift",
    ):
        accumulator.summary()

def test_recorded_step_guard_rejects_stateful_instant_text_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accumulator, _resolver = _filled_accumulator(event_count=1)
    baseline = accumulator.summary()
    original_instant_text = forward_evidence._instant_text
    calls = 0

    def stateful_instant_text(value: datetime) -> str:
        nonlocal calls
        calls += 1
        text = original_instant_text(value)
        if calls % 3 == 2:
            return text.replace("2026-", "2099-", 1)
        return text

    monkeypatch.setattr(
        forward_evidence,
        "_instant_text",
        stateful_instant_text,
    )

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="internal recorded step identity executable integrity drift",
    ):
        accumulator.summary()

    # Reject transitive substitution before it can participate in either guard
    # validation or raw summary publication.
    assert calls == 0

    monkeypatch.setattr(
        forward_evidence,
        "_instant_text",
        original_instant_text,
    )
    assert accumulator.summary().evidence_sha256 == baseline.evidence_sha256

def test_summary_rejects_transitive_json_encoder_publication_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accumulator, _resolver = _filled_accumulator(event_count=1)
    baseline = accumulator.summary()
    encoder_type = forward_evidence.json.JSONEncoder
    original_encode = encoder_type.encode
    forged_publications = 0

    def stateful_encode(self: object, value: object) -> str:
        nonlocal forged_publications
        text = original_encode(self, value)
        if (
            type(value) is dict
            and value.get("schema_version") == 1
            and "steps" in value
        ):
            forged_publications += 1
            return text.replace("2026-", "2099-", 1)
        return text

    monkeypatch.setattr(encoder_type, "encode", stateful_encode)

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="recorded step evidence publication identity drift",
    ):
        accumulator.summary()

    assert forged_publications == 1

    monkeypatch.setattr(encoder_type, "encode", original_encode)
    assert accumulator.summary().evidence_sha256 == baseline.evidence_sha256



def test_recorded_step_guard_rejects_json_encoder_bootstrap_rebind_before_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoder_type = forward_evidence.json.JSONEncoder
    forged_calls = 0

    def forged_encode(self: object, value: object) -> str:
        nonlocal forged_calls
        forged_calls += 1
        raise AssertionError("forged JSONEncoder.encode must not execute")

    monkeypatch.setattr(encoder_type, "encode", forged_encode)

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="internal recorded step identity executable integrity drift",
    ):
        ForwardEconomicEvidenceAccumulator(_protocol())

    # The executable fence runs before raw_init, so a pre-construction encoder
    # replacement cannot define protocol identity or the private empty-prefix
    # evidence baseline.
    assert forged_calls == 0


def test_recorded_step_guard_rejects_json_encoder_factory_rebind_before_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoder_module = forward_evidence.json.encoder
    forged_calls = 0

    def forged_factory(*args: object, **kwargs: object) -> object:
        nonlocal forged_calls
        forged_calls += 1
        raise AssertionError("forged JSON encoder factory must not execute")

    monkeypatch.setattr(encoder_module, "c_make_encoder", forged_factory)

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="internal recorded step identity executable integrity drift",
    ):
        ForwardEconomicEvidenceAccumulator(_protocol())

    # The lower encoder factory is part of the canonical json.dumps execution
    # graph on CPython and must be sealed before any accumulator authority can
    # bootstrap from it.
    assert forged_calls == 0
