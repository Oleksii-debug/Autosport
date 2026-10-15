from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.portfolio_live_scenario import (
    FrozenLiveScenarioSpec,
    LivePortfolioScenarioError,
    LivePortfolioScenarioEvidence,
    LivePositionScenarioComponent,
    evaluate_live_portfolio_scenario,
)


_SHA = "a" * 64
_POSITION_SHA = "b" * 64


def _issued_evidence() -> LivePortfolioScenarioEvidence:
    spec = FrozenLiveScenarioSpec(
        scenario_id="scenario-1",
        batch_id="batch-1",
        portfolio_state_sha256=_SHA,
        generation=1,
        expected_position_ids=("position-1",),
        batch_opened_at="2026-09-21T18:00:00+00:00",
        batch_closed_at="2026-09-21T18:00:02+00:00",
        max_observation_skew_microseconds=0,
    )
    component = LivePositionScenarioComponent(
        position_id="position-1",
        event_id="event-1",
        market_id="market-1",
        provider_id="provider-1",
        account_id="account-1",
        batch_id="batch-1",
        portfolio_state_sha256=_SHA,
        generation=1,
        position_state_sha256=_POSITION_SHA,
        observed_at="2026-09-21T18:00:01+00:00",
        committed_at="2026-09-21T18:00:01+00:00",
        capital_at_risk=Decimal("10"),
        conservative_loss_upper_bound=Decimal("7"),
    )
    return evaluate_live_portfolio_scenario(spec, (component,))


def test_caller_cannot_directly_mint_authority_bearing_scenario_evidence() -> None:
    issued = _issued_evidence()

    with pytest.raises(LivePortfolioScenarioError):
        LivePortfolioScenarioEvidence(
            scenario_id=issued.scenario_id,
            batch_id=issued.batch_id,
            portfolio_state_sha256=issued.portfolio_state_sha256,
            generation=issued.generation,
            position_count=issued.position_count,
            total_capital_at_risk=issued.total_capital_at_risk,
            conservative_componentwise_loss_upper_bound=(
                issued.conservative_componentwise_loss_upper_bound
            ),
            observation_skew_microseconds=issued.observation_skew_microseconds,
            structural_vector_complete=True,
            source_authority_proven=True,
            portfolio_universe_authoritative=True,
            cross_provider_atomicity_proven=True,
            terminal_outcome_exactness_proven=True,
            grants_sizing_authority=True,
            grants_execution_authority=True,
            grants_settlement_authority=True,
            evidence_sha256=issued.evidence_sha256,
        )


def test_caller_cannot_upgrade_evaluator_result_with_dataclass_replace() -> None:
    issued = _issued_evidence()

    with pytest.raises(LivePortfolioScenarioError):
        replace(
            issued,
            source_authority_proven=True,
            portfolio_universe_authoritative=True,
            grants_execution_authority=True,
        )
