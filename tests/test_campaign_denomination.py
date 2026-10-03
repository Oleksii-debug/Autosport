from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from autosport.campaign_denomination import (
    CampaignDenominationBinding,
    CampaignDenominationError,
    rehydrate_campaign_denomination_binding,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 21, 2, 20, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def binding(**overrides: object) -> CampaignDenominationBinding:
    values: dict[str, object] = {
        "campaign_id": "campaign-7",
        "campaign_version": "12",
        "campaign_sha256": SHA_A,
        "session_id": "session-3",
        "run_id": "run-9",
        "evaluation_id": "evaluation-4",
        "projection_sha256": SHA_B,
        "economic_goal_id": "goal-2",
        "economic_goal_revision": 5,
        "economic_goal_sha256": SHA_C,
        "bankroll_id": "bankroll-main",
        "portfolio_identity": "portfolio-main",
        "currency": "EUR",
        "effective_at": NOW,
        "observed_at": NOW + timedelta(seconds=1),
        "available_at": NOW + timedelta(seconds=2),
        "source_evidence_sha256": SHA_D,
    }
    values.update(overrides)
    return CampaignDenominationBinding(**values)


def exact_values(value: CampaignDenominationBinding) -> dict[str, object]:
    return {
        "campaign_id": value.campaign_id,
        "campaign_version": value.campaign_version,
        "campaign_sha256": value.campaign_sha256,
        "session_id": value.session_id,
        "run_id": value.run_id,
        "evaluation_id": value.evaluation_id,
        "projection_sha256": value.projection_sha256,
        "economic_goal_id": value.economic_goal_id,
        "economic_goal_revision": value.economic_goal_revision,
        "economic_goal_sha256": value.economic_goal_sha256,
        "bankroll_id": value.bankroll_id,
        "portfolio_identity": value.portfolio_identity,
        "currency": value.currency,
    }


def test_exact_unchanged_retry_has_same_binding_identity() -> None:
    first = binding()
    second = binding()

    assert first == second
    assert first.binding_id == second.binding_id
    first.verify_exact(**exact_values(second))


def test_later_goal_or_currency_cannot_relabel_finalized_binding() -> None:
    value = binding()

    with pytest.raises(CampaignDenominationError, match="economic_goal_revision"):
        value.verify_exact(**{**exact_values(value), "economic_goal_revision": 6})

    with pytest.raises(CampaignDenominationError, match="currency"):
        value.verify_exact(**{**exact_values(value), "currency": "USD"})


def test_wrong_bankroll_portfolio_and_campaign_membership_reject() -> None:
    value = binding()
    mutations = {
        "bankroll_id": "bankroll-shadow",
        "portfolio_identity": "portfolio-shadow",
        "campaign_sha256": "e" * 64,
        "session_id": "session-shadow",
        "run_id": "run-shadow",
        "evaluation_id": "evaluation-shadow",
        "projection_sha256": "f" * 64,
    }

    for name, changed in mutations.items():
        with pytest.raises(CampaignDenominationError, match=name):
            value.verify_exact(**{**exact_values(value), name: changed})


def test_cross_currency_cost_requires_explicit_fx_authority() -> None:
    value = binding(currency="EUR")
    value.verify_cost_currency("EUR")

    with pytest.raises(CampaignDenominationError, match="explicit FX authority"):
        value.verify_cost_currency("GBP")


def test_binding_is_immutable_and_digest_detects_semantic_drift() -> None:
    value = binding()

    with pytest.raises(FrozenInstanceError):
        value.currency = "USD"  # type: ignore[misc]

    assert replace(value, bankroll_id="bankroll-other", binding_id="").binding_id != value.binding_id
    assert replace(value, economic_goal_revision=6, binding_id="").binding_id != value.binding_id


def test_rehydrate_rejects_binding_digest_tamper() -> None:
    value = binding()
    payload = dict(value.canonical_payload)
    payload["currency"] = "USD"

    with pytest.raises(CampaignDenominationError, match="binding_id"):
        rehydrate_campaign_denomination_binding(payload)


def test_rehydrate_rejects_missing_or_extra_schema_keys() -> None:
    payload = dict(binding().canonical_payload)
    payload.pop("bankroll_id")
    with pytest.raises(CampaignDenominationError, match="keys are not exact"):
        rehydrate_campaign_denomination_binding(payload)

    payload = dict(binding().canonical_payload)
    payload["owner_label"] = "caller"
    with pytest.raises(CampaignDenominationError, match="keys are not exact"):
        rehydrate_campaign_denomination_binding(payload)


def test_currency_is_canonical_and_cannot_be_caller_normalized() -> None:
    for bad in ("eur", "EURO", " EU", "12A", "", "SLL"):
        with pytest.raises(CampaignDenominationError, match="currency"):
            binding(currency=bad)

    assert binding(currency="SLE").currency == "SLE"


def test_timestamps_must_be_causal_timezone_aware_and_non_future_relative_order() -> None:
    with pytest.raises(CampaignDenominationError, match="timezone-aware"):
        binding(effective_at=NOW.replace(tzinfo=None))

    with pytest.raises(CampaignDenominationError, match="not causal"):
        binding(
            effective_at=NOW + timedelta(seconds=3),
            observed_at=NOW + timedelta(seconds=1),
            available_at=NOW + timedelta(seconds=2),
        )

    with pytest.raises(CampaignDenominationError, match="not causal"):
        binding(
            effective_at=NOW,
            observed_at=NOW + timedelta(seconds=3),
            available_at=NOW + timedelta(seconds=2),
        )


def test_digests_and_revision_are_strictly_typed() -> None:
    with pytest.raises(CampaignDenominationError, match="campaign_sha256"):
        binding(campaign_sha256="A" * 64)
    with pytest.raises(CampaignDenominationError, match="projection_sha256"):
        binding(projection_sha256="x")
    with pytest.raises(CampaignDenominationError, match="non-negative integer"):
        binding(economic_goal_revision=True)
    with pytest.raises(CampaignDenominationError, match="non-negative integer"):
        binding(economic_goal_revision=-1)
