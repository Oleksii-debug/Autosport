from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.matchbook_price_query_contract import (
    MatchbookExchangeType,
    MatchbookOddsType,
    MatchbookPriceMode,
    MatchbookPriceObservationEvidence,
    MatchbookPriceQueryContract,
    MatchbookPriceQueryError,
    MatchbookPriceSide,
)


def _query() -> MatchbookPriceQueryContract:
    return MatchbookPriceQueryContract(
        event_id=101,
        market_id=202,
        runner_id=303,
        exchange_type=MatchbookExchangeType.BACK_LAY,
        odds_type=MatchbookOddsType.DECIMAL,
        currency="EUR",
        side=MatchbookPriceSide.BOTH,
        depth=5,
        price_mode=MatchbookPriceMode.EXPANDED,
        minimum_liquidity=Decimal("2.5"),
        exclude_mirrored_prices=True,
    )


def _observation() -> MatchbookPriceObservationEvidence:
    return MatchbookPriceObservationEvidence(
        query=_query(),
        observed_at=datetime(2026, 9, 22, 7, 15, tzinfo=timezone.utc),
        raw_response_sha256="a" * 64,
    )


@pytest.mark.parametrize("alias", [True, 1.0])
def test_query_schema_version_rejects_python_numeric_aliases(alias: object) -> None:
    raw = _query().to_dict()
    raw["schema_version"] = alias

    with pytest.raises(MatchbookPriceQueryError, match="schema|canonical"):
        MatchbookPriceQueryContract.from_dict(raw)


@pytest.mark.parametrize("alias", [True, 1.0])
def test_observation_schema_version_rejects_python_numeric_aliases(alias: object) -> None:
    raw = _observation().to_dict()
    raw["schema_version"] = alias

    with pytest.raises(MatchbookPriceQueryError, match="schema|canonical"):
        MatchbookPriceObservationEvidence.from_dict(raw)


@pytest.mark.parametrize(
    "field",
    [
        "provider_defaults_used",
        "absence_beyond_depth_proven",
        "omitted_side_absence_proven",
        "execution_liquidity_reserved",
    ],
)
def test_query_fixed_boolean_truth_fields_reject_integer_zero_alias(field: str) -> None:
    raw = _query().to_dict()
    raw[field] = 0

    with pytest.raises(MatchbookPriceQueryError, match="canonical"):
        MatchbookPriceQueryContract.from_dict(raw)


@pytest.mark.parametrize(
    "field",
    [
        "provider_origin_proven",
        "provider_authentication_proven",
        "absence_beyond_depth_proven",
        "omitted_side_absence_proven",
        "execution_liquidity_reserved",
        "grants_execution_authority",
        "grants_real_money_authority",
    ],
)
def test_observation_fixed_boolean_truth_fields_reject_integer_zero_alias(
    field: str,
) -> None:
    raw = _observation().to_dict()
    raw[field] = 0

    with pytest.raises(MatchbookPriceQueryError, match="canonical"):
        MatchbookPriceObservationEvidence.from_dict(raw)
