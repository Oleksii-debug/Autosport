from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.paper_live_sandbox_campaign import (
    SIMULATION_MODEL_ID,
    BlindPaperCampaignPlan,
    BlindPaperSandboxCampaign,
    SandboxCampaignError,
    parse_sandbox_live_payload,
    run_network_endurance,
)


T0 = datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _payload(index: int, *, positive_move: bool = True) -> dict[str, object]:
    timestamp_ms = int((T0 + timedelta(seconds=index)).timestamp() * 1000)
    if index % 2 == 0:
        first_price = 120
        second_price = 110
    elif positive_move:
        first_price = 100
        second_price = 100
    else:
        first_price = 140
        second_price = 130
    return {
        "sport_key": "table_tennis",
        "period": "FT",
        "count": 2,
        "results": [
            {
                "match_id": "sandbox-table-1",
                "sport_key": "table_tennis",
                "source": "pinnacle",
                "period_key": "FT",
                "market": "h2h",
                "line": 0,
                "side": "home",
                "price": first_price,
                "timestamp_ms": timestamp_ms,
                "age_seconds": 1,
            },
            {
                "match_id": "sandbox-table-2",
                "sport_key": "table_tennis",
                "source": "pinnacle",
                "period_key": "FT",
                "market": "h2h",
                "line": 0,
                "side": "away",
                "price": second_price,
                "timestamp_ms": timestamp_ms,
                "age_seconds": 2,
            },
        ],
    }


def _snapshot(index: int, *, positive_move: bool = True):
    return parse_sandbox_live_payload(
        _payload(index, positive_move=positive_move),
        available_at=_stamp(T0 + timedelta(seconds=index, microseconds=500_000)),
    )


def _plan() -> BlindPaperCampaignPlan:
    return BlindPaperCampaignPlan(
        campaign_id="account-c-sandbox-endurance-20261026",
        started_at=_stamp(T0),
        cutoff_at=_stamp(T0 + timedelta(hours=3)),
    )


def test_parser_preserves_source_time_and_marks_no_observed_truth() -> None:
    snapshot = _snapshot(1)

    assert len(snapshot.quotes) == 2
    assert snapshot.observed_at == _stamp(T0 + timedelta(seconds=1))
    assert snapshot.available_at > snapshot.observed_at
    assert all(quote.decimal_odds > Decimal("1") for quote in snapshot.quotes)
    assert len(snapshot.snapshot_sha256) == 64


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda raw: raw.update({"sport_key": "tennis"}), "scope mismatch"),
        (lambda raw: raw.update({"count": 3}), "count must match"),
        (
            lambda raw: raw["results"][0].update({"timestamp_ms": True}),
            "time fields are invalid",
        ),
        (
            lambda raw: raw["results"][0].update({"price": 0}),
            "non-zero integer",
        ),
    ],
)
def test_parser_fails_closed_on_scope_count_time_and_price(mutation, message) -> None:
    raw = _payload(1)
    mutation(raw)

    with pytest.raises(SandboxCampaignError, match=message):
        parse_sandbox_live_payload(
            raw,
            available_at=_stamp(T0 + timedelta(seconds=2)),
        )


def test_blind_policy_campaign_learns_with_parlay_majority_but_stays_simulated() -> None:
    campaign = BlindPaperSandboxCampaign(_plan())

    for index in range(1, 12):
        assert campaign.step(_snapshot(index), _snapshot(index + 1))

    summary = campaign.summary()
    assert summary["truth"] == "simulated"
    assert summary["simulation_model_id"] == SIMULATION_MODEL_ID
    assert summary["paper_only"] is True
    assert summary["real_money_execution"] is False
    assert summary["forward_evidence_eligible"] is False
    assert summary["promotion_authorized"] is False
    assert summary["profitability_claim"] is False
    assert summary["policy_generation"] == 11
    assert summary["counters"]["PARLAY_2"] >= 7
    assert summary["counters"]["SINGLE"] >= 2
    assert campaign.book.committed_stake == Decimal("0")
    assert campaign.policy.estimates[0].action_type == "PARLAY_2"


def test_negative_american_odds_do_not_create_inexact_followup_stake() -> None:
    campaign = BlindPaperSandboxCampaign(_plan())
    snapshots = []
    for index in range(1, 7):
        raw = _payload(index)
        raw["results"][0]["price"] = -110 if index % 2 else -120
        raw["results"][1]["price"] = -125 if index % 2 else -135
        snapshots.append(
            parse_sandbox_live_payload(
                raw,
                available_at=_stamp(
                    T0 + timedelta(seconds=index, microseconds=500_000)
                ),
            )
        )

    for decision, outcome in zip(snapshots, snapshots[1:], strict=False):
        assert campaign.step(decision, outcome)

    assert campaign.policy.generation == 5
    assert campaign.book.committed_stake == Decimal("0")
    assert campaign.book.balance.is_finite()


def test_unchanged_snapshot_is_deduplicated_before_second_action() -> None:
    campaign = BlindPaperSandboxCampaign(_plan())
    decision = _snapshot(1)
    outcome = _snapshot(2)

    assert campaign.step(decision, outcome)
    before = campaign.summary()
    assert campaign.step(decision, _snapshot(3)) is False
    assert campaign.summary() == before


def test_restart_restores_exact_policy_environment_and_paperbook(tmp_path) -> None:
    campaign = BlindPaperSandboxCampaign(_plan())
    for index in range(1, 6):
        campaign.step(_snapshot(index), _snapshot(index + 1))
    campaign.save(tmp_path)

    restored = BlindPaperSandboxCampaign.load(tmp_path)
    assert restored.summary() == campaign.summary()
    assert restored.step(_snapshot(6), _snapshot(7))
    restored.save(tmp_path)

    reopened = BlindPaperSandboxCampaign.load(tmp_path)
    assert reopened.policy.generation == 6
    assert reopened.environment.checkpoint().step_index == 6
    assert reopened.book.committed_stake == Decimal("0")


def test_restart_rejects_attempt_to_relabel_simulation_as_observed(tmp_path) -> None:
    campaign = BlindPaperSandboxCampaign(_plan())
    campaign.step(_snapshot(1), _snapshot(2))
    campaign.save(tmp_path)
    state_path = tmp_path / "campaign_state.json"
    raw = json.loads(state_path.read_text("utf-8"))
    raw["plan"]["truth"] = "observed"
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SandboxCampaignError, match="plan payload is not canonical"):
        BlindPaperSandboxCampaign.load(tmp_path)


def test_outcome_must_be_strictly_after_decision_snapshot() -> None:
    campaign = BlindPaperSandboxCampaign(_plan())
    same_time = _snapshot(1)

    with pytest.raises(SandboxCampaignError, match="after decision snapshot"):
        campaign.step(same_time, same_time)


def test_three_hour_endurance_is_bounded_by_frozen_cutoff() -> None:
    plan = BlindPaperCampaignPlan(
        campaign_id="bounded-cutoff",
        started_at=_stamp(T0),
        cutoff_at=_stamp(T0 + timedelta(seconds=3)),
    )
    campaign = BlindPaperSandboxCampaign(plan)

    with pytest.raises(SandboxCampaignError, match="exceeds frozen campaign cutoff"):
        campaign.step(_snapshot(2), _snapshot(4))


def test_network_runner_checkpoints_and_emits_sanitized_progress(
    tmp_path,
    monkeypatch,
) -> None:
    campaign = BlindPaperSandboxCampaign(_plan())
    snapshots = iter((_snapshot(1), _snapshot(2)))
    monotonic = iter((0.0, 0.0, 0.1, 0.2, 2.0))
    events = []
    monkeypatch.setattr(
        "autosport.paper_live_sandbox_campaign.fetch_sandbox_live_snapshot",
        lambda: next(snapshots),
    )
    monkeypatch.setattr(
        "autosport.paper_live_sandbox_campaign.time.monotonic",
        lambda: next(monotonic),
    )
    monkeypatch.setattr(
        "autosport.paper_live_sandbox_campaign.time.sleep",
        lambda _seconds: None,
    )

    summary = run_network_endurance(
        campaign,
        workspace=tmp_path,
        duration_seconds=1,
        poll_interval_seconds=5,
        progress=events.append,
    )

    assert summary["network_snapshots_segment"] == 2
    assert summary["policy_generation"] == 1
    assert len(events) == 2
    assert events[-1]["truth"] == "simulated"
    assert events[-1]["profitability_claim"] is False
    assert "snapshot_sha256" not in events[-1]
    assert (tmp_path / "campaign_state.json").is_file()
