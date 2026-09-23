from __future__ import annotations

from dataclasses import replace
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
    MatchbookPriceRepresentation,
    MatchbookPriceSide,
)


def _query(**changes: object) -> MatchbookPriceQueryContract:
    values: dict[str, object] = {
        "event_id": 101,
        "market_id": 202,
        "runner_id": 303,
        "exchange_type": MatchbookExchangeType.BACK_LAY,
        "odds_type": MatchbookOddsType.DECIMAL,
        "currency": "EUR",
        "side": MatchbookPriceSide.BOTH,
        "depth": 5,
        "price_mode": MatchbookPriceMode.EXPANDED,
        "minimum_liquidity": Decimal("2.50"),
        "exclude_mirrored_prices": True,
    }
    values.update(changes)
    return MatchbookPriceQueryContract(**values)  # type: ignore[arg-type]


def test_exact_get_prices_contract_projects_every_material_query_semantic() -> None:
    query = _query()
    assert (
        query.request_path
        == "/edge/rest/events/101/markets/202/runners/303/prices"
    )
    assert query.query_params() == (
        ("exchange-type", "back-lay"),
        ("odds-type", "DECIMAL"),
        ("depth", "5"),
        ("currency", "EUR"),
        ("minimum-liquidity", "2.5"),
        ("price-mode", "expanded"),
        ("exclude-mirrored-prices", "true"),
    )
    assert query.to_dict()["side_scope"] == "both"
    assert query.provider_defaults_used is False


def test_every_material_semantic_changes_request_identity() -> None:
    base = _query()
    variants = (
        replace(base, event_id=102),
        replace(base, market_id=203),
        replace(base, runner_id=304),
        replace(base, odds_type=MatchbookOddsType.US),
        replace(base, currency="GBP"),
        replace(base, side=MatchbookPriceSide.BACK),
        replace(base, depth=6),
        replace(
            base,
            price_mode=MatchbookPriceMode.AGGREGATED,
        ),
        replace(
            base,
            minimum_liquidity=Decimal("3"),
        ),
        replace(base, exclude_mirrored_prices=False),
        replace(
            base,
            exchange_type=MatchbookExchangeType.BINARY,
            side=MatchbookPriceSide.WIN,
        ),
    )
    assert (
        len(
            {
                base.contract_sha256,
                *(item.contract_sha256 for item in variants),
            }
        )
        == 12
    )


def test_both_sides_are_explicit_even_when_provider_omits_side_param() -> None:
    both = _query(side=MatchbookPriceSide.BOTH)
    back = _query(side=MatchbookPriceSide.BACK)
    assert all(
        name != "side" for name, _ in both.query_params()
    )
    assert ("side", "back") in back.query_params()
    assert both.to_dict()["side_scope"] == "both"
    assert both.omitted_side_absence_proven is False
    assert both.contract_sha256 != back.contract_sha256


def test_side_must_match_exchange_type() -> None:
    with pytest.raises(
        MatchbookPriceQueryError, match="incompatible"
    ):
        _query(side=MatchbookPriceSide.WIN)
    with pytest.raises(
        MatchbookPriceQueryError, match="incompatible"
    ):
        _query(
            exchange_type=MatchbookExchangeType.BINARY,
            side=MatchbookPriceSide.BACK,
        )


def test_aggregated_display_cannot_alias_expanded_price_levels() -> None:
    expanded = _query(
        price_mode=MatchbookPriceMode.EXPANDED
    )
    aggregated = _query(
        price_mode=MatchbookPriceMode.AGGREGATED
    )
    assert (
        expanded.provider_price_representation
        is MatchbookPriceRepresentation.EXPANDED_LEVELS
    )
    assert (
        aggregated.provider_price_representation
        is MatchbookPriceRepresentation.AGGREGATED_DISPLAY
    )
    assert (
        expanded.contract_sha256
        != aggregated.contract_sha256
    )


def test_depth_and_minimum_liquidity_never_prove_absent_market_liquidity() -> None:
    shallow = _query(
        depth=1,
        minimum_liquidity=Decimal("100"),
    )
    deep = _query(
        depth=20,
        minimum_liquidity=Decimal("0"),
    )
    assert shallow.absence_beyond_depth_proven is False
    assert deep.absence_beyond_depth_proven is False
    assert shallow.execution_liquidity_reserved is False
    assert deep.execution_liquidity_reserved is False
    assert (
        shallow.contract_sha256
        != deep.contract_sha256
    )


def test_currency_is_identity_bearing_for_available_amount() -> None:
    eur = _query(currency="EUR")
    usd = _query(currency="USD")
    assert eur.contract_sha256 != usd.contract_sha256
    assert ("currency", "EUR") in eur.query_params()
    assert ("currency", "USD") in usd.query_params()


def test_contract_round_trip_is_canonical_and_tamper_evident() -> None:
    query = _query()
    raw = query.to_dict()
    assert (
        MatchbookPriceQueryContract.from_dict(raw)
        == query
    )
    tampered = dict(raw)
    tampered["depth"] = 4
    with pytest.raises(
        MatchbookPriceQueryError, match="canonical"
    ):
        MatchbookPriceQueryContract.from_dict(tampered)


def test_observation_binds_query_response_and_time_without_minting_authority() -> None:
    query = _query()
    evidence = MatchbookPriceObservationEvidence(
        query=query,
        observed_at=datetime(
            2026, 9, 22, 7, 15, tzinfo=timezone.utc
        ),
        raw_response_sha256="a" * 64,
    )
    raw = evidence.to_dict()
    assert (
        raw["query_contract_sha256"]
        == query.contract_sha256
    )
    assert raw["raw_response_sha256"] == "a" * 64
    assert raw["provider_origin_proven"] is False
    assert (
        raw["provider_authentication_proven"] is False
    )
    assert raw["execution_liquidity_reserved"] is False
    assert raw["grants_execution_authority"] is False
    assert raw["grants_real_money_authority"] is False
    assert (
        MatchbookPriceObservationEvidence.from_dict(raw)
        == evidence
    )


def test_observation_identity_changes_with_response_time_or_query() -> None:
    evidence = MatchbookPriceObservationEvidence(
        query=_query(),
        observed_at=datetime(
            2026, 9, 22, 7, 15, tzinfo=timezone.utc
        ),
        raw_response_sha256="a" * 64,
    )
    changed_response = replace(
        evidence, raw_response_sha256="b" * 64
    )
    changed_time = replace(
        evidence,
        observed_at=datetime(
            2026, 9, 22, 7, 16, tzinfo=timezone.utc
        ),
    )
    changed_query = replace(
        evidence, query=_query(depth=6)
    )
    assert (
        len(
            {
                evidence.evidence_id,
                changed_response.evidence_id,
                changed_time.evidence_id,
                changed_query.evidence_id,
            }
        )
        == 4
    )


def test_invalid_or_implicit_semantics_fail_closed() -> None:
    with pytest.raises(MatchbookPriceQueryError):
        _query(event_id=True)
    with pytest.raises(MatchbookPriceQueryError):
        _query(depth=0)
    with pytest.raises(MatchbookPriceQueryError):
        _query(currency="JPY")
    with pytest.raises(MatchbookPriceQueryError):
        _query(minimum_liquidity=2.0)
    with pytest.raises(MatchbookPriceQueryError):
        _query(
            minimum_liquidity=Decimal("NaN")
        )
    with pytest.raises(MatchbookPriceQueryError):
        _query(exclude_mirrored_prices=1)


def test_observation_requires_utc_and_lowercase_sha256() -> None:
    query = _query()
    with pytest.raises(
        MatchbookPriceQueryError,
        match="timezone-aware",
    ):
        MatchbookPriceObservationEvidence(
            query=query,
            observed_at=datetime(
                2026, 9, 22, 7, 15
            ),
            raw_response_sha256="a" * 64,
        )
    with pytest.raises(
        MatchbookPriceQueryError,
        match="lowercase SHA-256",
    ):
        MatchbookPriceObservationEvidence(
            query=query,
            observed_at=datetime(
                2026,
                9,
                22,
                7,
                15,
                tzinfo=timezone.utc,
            ),
            raw_response_sha256="A" * 64,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("event_id", 1 << 63),
        ("market_id", 1 << 63),
        ("runner_id", 1 << 63),
    ),
)
def test_provider_wire_integer_domains_fail_closed(
    field: str,
    value: int,
) -> None:
    with pytest.raises(
        MatchbookPriceQueryError,
        match="documented Matchbook integer domain",
    ):
        _query(**{field: value})

    assert getattr(_query(**{field: (1 << 63) - 1}), field) == (1 << 63) - 1


def test_provider_wire_depth_domain_fails_closed() -> None:
    with pytest.raises(
        MatchbookPriceQueryError,
        match="documented Matchbook integer domain",
    ):
        _query(depth=1 << 31)

    assert _query(depth=(1 << 31) - 1).depth == (1 << 31) - 1


def test_zero_with_extreme_negative_exponent_serializes_without_expansion() -> None:
    query = _query(
        minimum_liquidity=Decimal("0E-1000000000")
    )
    assert ("minimum-liquidity", "0") in query.query_params()
    assert query.to_dict()["minimum_liquidity"] == "0"


def test_minimum_liquidity_rejects_oversized_fixed_point_materialization() -> None:
    oversized = Decimal("1." + ("1" * 600))
    with pytest.raises(
        MatchbookPriceQueryError,
        match="serialized Decimal exceeds safety bound",
    ):
        _query(minimum_liquidity=oversized)


@pytest.mark.parametrize(
    "minimum_liquidity",
    (
        Decimal("1e309"),
        Decimal("1e-10000"),
    ),
)
def test_minimum_liquidity_stays_inside_provider_double_domain(
    minimum_liquidity: Decimal,
) -> None:
    with pytest.raises(
        MatchbookPriceQueryError,
        match="documented Matchbook double domain",
    ):
        _query(minimum_liquidity=minimum_liquidity)

    assert _query(
        minimum_liquidity=Decimal("1.5e-323")
    ).minimum_liquidity == Decimal("1.5e-323")


def _observation() -> MatchbookPriceObservationEvidence:
    return MatchbookPriceObservationEvidence(
        query=_query(),
        observed_at=datetime(
            2026, 9, 22, 7, 15, tzinfo=timezone.utc
        ),
        raw_response_sha256="a" * 64,
    )


@pytest.mark.parametrize("alias", [True, 1.0])
def test_query_schema_version_rejects_python_numeric_aliases(
    alias: object,
) -> None:
    raw = _query().to_dict()
    raw["schema_version"] = alias

    with pytest.raises(
        MatchbookPriceQueryError, match="schema|canonical"
    ):
        MatchbookPriceQueryContract.from_dict(raw)


@pytest.mark.parametrize("alias", [True, 1.0])
def test_observation_schema_version_rejects_python_numeric_aliases(
    alias: object,
) -> None:
    raw = _observation().to_dict()
    raw["schema_version"] = alias

    with pytest.raises(
        MatchbookPriceQueryError, match="schema|canonical"
    ):
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
def test_query_fixed_boolean_truth_fields_reject_integer_zero_alias(
    field: str,
) -> None:
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
