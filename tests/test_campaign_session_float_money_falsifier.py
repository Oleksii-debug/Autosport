from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.campaign_evidence import CampaignError, CampaignOutcome, SessionEvidence


def _session_values() -> dict[str, object]:
    return {
        "session_id": "session-float-money-1",
        "run_id": "run-float-money-1",
        "evidence_id": "evidence-float-money-1",
        "source_sha256": "11" * 32,
        "research_protocol_id": "protocol-1",
        "protocol_sha256": "22" * 32,
        "dataset_snapshot_id": "dataset-1",
        "dataset_manifest_sha256": "33" * 32,
        "strategy_version_id": "strategy-1",
        "model_version_id": "model-1",
        "config_sha256": "44" * 32,
        "evaluation_window_start": "2026-09-01T00:00:00Z",
        "evaluation_window_end": "2026-09-01T01:00:00Z",
        "as_of": "2026-09-01T02:00:00Z",
        "available_at": "2026-09-01T02:00:00Z",
        "outcome_reveal_after": "2026-09-01T01:30:00Z",
        "observation_timestamps": (
            "2026-09-01T00:10:00Z",
            "2026-09-01T00:50:00Z",
        ),
        "observation_membership_sha256": "55" * 32,
        "starting_bankroll": Decimal("1000"),
        "ending_bankroll": Decimal("1060"),
        "net_profit": Decimal("60"),
        "turnover": Decimal("600"),
        "bets": 10,
        "wins": 6,
        "losses": 4,
        "voids": 0,
        "brier_sum": None,
        "log_loss_sum": None,
        "prediction_count": 0,
        "max_drawdown": None,
        "peak_exposure": None,
        "risk_of_ruin": None,
        "volatility": None,
        "outcome": CampaignOutcome.POSITIVE,
    }


def test_exact_decimal_money_inputs_remain_admissible() -> None:
    session = SessionEvidence.build(**_session_values())

    assert type(session.starting_bankroll) is Decimal
    assert type(session.ending_bankroll) is Decimal
    assert type(session.net_profit) is Decimal
    assert type(session.turnover) is Decimal


@pytest.mark.parametrize(
    ("field", "float_value"),
    [
        ("starting_bankroll", 1000.0),
        ("ending_bankroll", 1060.0),
        ("net_profit", 60.0),
        ("turnover", 600.0),
    ],
)
def test_binary_float_cannot_enter_campaign_money_evidence(
    field: str,
    float_value: float,
) -> None:
    values = _session_values()
    values[field] = float_value

    with pytest.raises(CampaignError):
        SessionEvidence.build(**values)
