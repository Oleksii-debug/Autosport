from __future__ import annotations

import pytest

from autosport.participant_identity import IdentityView
from autosport.portfolio_plan import OpportunityEvidence
from autosport.sport_memory_checkpoint import (
    BoundSportMemoryRuntime,
    SportMemoryCheckpointError,
)
from autosport.sport_memory_decision_evidence import (
    bind_sport_memory_to_opportunity_evidence,
)
from autosport.sport_memory_runtime import (
    SportMemoryMatchupEvidence,
    SportMemoryScope,
)

from test_sport_memory_matchup_decision import (
    _bound_runtime_pair,
    _scope as _canonical_scope,
)


T1 = "2026-09-20T10:00:00Z"
T1_PUBLISHED = "2026-09-20T10:01:00Z"
T2 = "2026-09-20T10:05:00Z"
T2_CONSUMED = "2026-09-20T10:05:01Z"
GENERATION = "d" * 64


def _runtime_without_io() -> BoundSportMemoryRuntime:
    """Exact product type with only state needed before verifier dispatch."""

    runtime = object.__new__(BoundSportMemoryRuntime)
    object.__setattr__(runtime, "authority_generation_sha256", GENERATION)
    object.__setattr__(runtime, "_artifacts", {})
    object.__setattr__(runtime, "_consumptions", {})
    object.__setattr__(runtime, "_decision_consumptions", {})
    return runtime


def _matchup() -> SportMemoryMatchupEvidence:
    return SportMemoryMatchupEvidence(
        matchup_id="c" * 64,
        subject_memory_id="a" * 64,
        opponent_memory_id="b" * 64,
        subject_participant_entity_id="participant-a",
        opponent_participant_entity_id="participant-b",
        scope=SportMemoryScope(
            sport_id="tennis",
            league_entity_id="atp",
            market_context_id="match-winner",
        ),
        identity_view=IdentityView.AS_KNOWN_AT_DECISION,
        as_of=T2,
        causal_cutoff=T1,
        published_at=T1_PUBLISHED,
        subject_support=2,
        opponent_support=2,
        subject_uncertainty="0.5",
        opponent_uncertainty="0.5",
        subject_age_seconds=3900,
        opponent_age_seconds=3900,
        authority_generation_sha256=GENERATION,
    )


def _base_evidence() -> OpportunityEvidence:
    return OpportunityEvidence(
        evidence_id="canonical-market-evidence",
        observed_at=T2,
        causal_cutoff=T1,
        reproducibility_sha256="e" * 64,
    )


def test_bound_runtime_forbids_normal_instance_authority_shadow_assignment():
    runtime = _runtime_without_io()

    with pytest.raises(
        SportMemoryCheckpointError,
        match="forbids instance authority shadow: verify_matchup_evidence",
    ):
        runtime.verify_matchup_evidence = lambda matchup: matchup


def test_direct_instance_shadow_cannot_authorize_opportunity_binding():
    runtime = _runtime_without_io()
    matchup = _matchup()
    attacker_called = False

    def attacker_verifier(candidate):
        nonlocal attacker_called
        attacker_called = True
        return candidate

    # Direct dict mutation bypasses __setattr__; product authority must still
    # detect it before dispatching the attacker-selected verifier.
    runtime.__dict__["verify_matchup_evidence"] = attacker_verifier

    with pytest.raises(
        SportMemoryCheckpointError,
        match="detected instance authority shadow: verify_matchup_evidence",
    ):
        bind_sport_memory_to_opportunity_evidence(
            _base_evidence(),
            matchup,
            runtime=runtime,
        )

    assert attacker_called is False


def test_direct_instance_shadow_cannot_mutate_durable_consumption_state(tmp_path):
    # Exercise the mutation guard through a fully composed durable product runtime.
    # The transaction wrapper now requires canonical runtime/upstream paths before
    # verifier dispatch, so the old object.__new__ no-I/O fixture is not a valid
    # mutation-path fixture anymore.
    runtime = _bound_runtime_pair(tmp_path)
    matchup = runtime.matchup_as_of(
        "participant-a",
        "participant-b",
        _canonical_scope(),
        as_of=T2,
    )
    attacker_called = False

    def attacker_verifier(candidate):
        nonlocal attacker_called
        attacker_called = True
        return candidate

    runtime.__dict__["verify_matchup_evidence"] = attacker_verifier

    with pytest.raises(
        SportMemoryCheckpointError,
        match="detected instance authority shadow: verify_matchup_evidence",
    ):
        runtime.record_matchup_consumption(
            decision_id="decision-shadow",
            matchup=matchup,
            decision_cutoff=T2,
            consumed_at=T2_CONSUMED,
        )

    assert attacker_called is False
    assert runtime._consumptions == {}
    assert runtime._decision_consumptions == {}
