from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.betfair_provider_constraint_evidence import (
    BetfairConstraintResolutionState,
    BetfairProviderConstraintError,
    BetfairProviderConstraintObservation,
    resolve_betfair_standard_limit_constraint_evidence,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 1, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
SOURCE = "https://betfair-developer-docs.atlassian.net/wiki/example"


def observation(
    *,
    min_size: Decimal = Decimal("1"),
    min_payout: Decimal | None = Decimal("10"),
    lower_enabled: bool = True,
    scope: str = "UK_INTERNATIONAL",
    currency: str = "GBP",
    source_revision: str = "current",
    source_sha256: str = HASH_A,
    retrieved_at: datetime = T0,
    reviewed_at: datetime = T0 + timedelta(minutes=1),
    available_at: datetime = T0 + timedelta(minutes=2),
    effective_from: datetime = T0,
    effective_until: datetime | None = None,
    review_expires_at: datetime = T0 + timedelta(days=7),
) -> BetfairProviderConstraintObservation:
    return BetfairProviderConstraintObservation(
        provider_id="betfair",
        jurisdiction_scope=scope,
        currency_code=currency,
        min_standard_size=min_size,
        min_payout=min_payout,
        lower_minimum_payout_enabled=lower_enabled,
        source_ref=SOURCE,
        source_revision=source_revision,
        source_sha256=source_sha256,
        retrieved_at=retrieved_at,
        reviewed_at=reviewed_at,
        available_at=available_at,
        effective_from=effective_from,
        effective_until=effective_until,
        review_expires_at=review_expires_at,
    )


def resolve(
    *items: BetfairProviderConstraintObservation,
    as_of: datetime = T0 + timedelta(days=1),
    scope: str = "UK_INTERNATIONAL",
    currency: str = "GBP",
):
    return resolve_betfair_standard_limit_constraint_evidence(
        observations=tuple(items),
        as_of=as_of,
        jurisdiction_scope=scope,
        currency_code=currency,
    )


def test_consistent_records_are_structural_only() -> None:
    first = observation()
    second = observation(
        source_revision="same-semantics-second-source",
        source_sha256=HASH_B,
        retrieved_at=T0 + timedelta(minutes=3),
        reviewed_at=T0 + timedelta(minutes=4),
        available_at=T0 + timedelta(minutes=5),
    )

    result = resolve(first, second)

    assert result.state is BetfairConstraintResolutionState.CONSISTENT_UNVERIFIED
    assert result.min_standard_size == Decimal("1")
    assert result.min_payout == Decimal("10")
    assert result.lower_minimum_payout_enabled is True
    assert result.provider_origin_proven is False
    assert result.current_constraint_authority is False
    assert result.execution_authorized is False
    assert result.real_money_execution is False


def test_overlapping_conflicting_current_records_fail_closed() -> None:
    current = observation(min_size=Decimal("1"), source_revision="placeOrders-current")
    historical = observation(
        min_size=Decimal("2"),
        source_revision="currency-parameters-v3",
        source_sha256=HASH_B,
    )

    result = resolve(current, historical)

    assert result.state is BetfairConstraintResolutionState.CONFLICTING_UNVERIFIED
    assert result.semantic_sha256 is None
    assert result.min_standard_size is None
    assert result.min_payout is None
    assert result.lower_minimum_payout_enabled is None
    assert result.execution_authorized is False


def test_replay_cannot_see_evidence_before_product_availability() -> None:
    future = observation(available_at=T0 + timedelta(days=2))

    result = resolve(future, as_of=T0 + timedelta(days=1))

    assert result.state is BetfairConstraintResolutionState.CAUSALLY_UNAVAILABLE
    assert result.min_standard_size is None


def test_expired_review_does_not_remain_current() -> None:
    stale = observation(review_expires_at=T0 + timedelta(hours=1))

    result = resolve(stale, as_of=T0 + timedelta(days=1))

    assert result.state is BetfairConstraintResolutionState.REVIEW_EXPIRED
    assert result.current_constraint_authority is False


def test_non_overlapping_rule_generations_resolve_by_effective_interval() -> None:
    boundary = T0 + timedelta(days=2)
    old = observation(
        min_size=Decimal("2"),
        source_revision="old",
        effective_until=boundary,
        review_expires_at=T0 + timedelta(days=10),
    )
    new = observation(
        min_size=Decimal("1"),
        source_revision="new",
        source_sha256=HASH_B,
        retrieved_at=T0 + timedelta(days=2),
        reviewed_at=T0 + timedelta(days=2, minutes=1),
        available_at=T0 + timedelta(days=2, minutes=2),
        effective_from=boundary,
        review_expires_at=T0 + timedelta(days=10),
    )

    result = resolve(old, new, as_of=T0 + timedelta(days=3))

    assert result.state is BetfairConstraintResolutionState.CONSISTENT_UNVERIFIED
    assert result.min_standard_size == Decimal("1")
    assert result.execution_authorized is False


def test_future_effective_record_is_not_current_rule() -> None:
    future_rule = observation(effective_from=T0 + timedelta(days=2))

    result = resolve(future_rule, as_of=T0 + timedelta(days=1))

    assert result.state is BetfairConstraintResolutionState.NO_APPLICABLE_RULE
    assert result.min_standard_size is None


@pytest.mark.parametrize(
    "bad",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("0"),
        Decimal("-1"),
        1,
        1.0,
        True,
    ],
)
def test_invalid_or_non_decimal_minimum_is_rejected(bad: object) -> None:
    with pytest.raises(BetfairProviderConstraintError):
        observation(min_size=bad)  # type: ignore[arg-type]


def test_lower_minimum_enabled_requires_payout() -> None:
    with pytest.raises(BetfairProviderConstraintError):
        observation(min_payout=None, lower_enabled=True)


def test_lower_minimum_disabled_cannot_carry_payout() -> None:
    with pytest.raises(BetfairProviderConstraintError):
        observation(min_payout=Decimal("10"), lower_enabled=False)


def test_disabled_exception_can_be_represented_without_payout() -> None:
    es = observation(
        scope="ES",
        currency="EUR",
        min_size=Decimal("2"),
        min_payout=None,
        lower_enabled=False,
    )

    result = resolve(
        es,
        scope="ES",
        currency="EUR",
    )

    assert result.state is BetfairConstraintResolutionState.CONSISTENT_UNVERIFIED
    assert result.lower_minimum_payout_enabled is False
    assert result.min_payout is None
    assert result.execution_authorized is False


def test_same_source_identity_with_changed_semantics_is_new_conflicting_generation() -> None:
    first = observation(min_size=Decimal("2"), source_revision="same-url-revision")
    changed = observation(
        min_size=Decimal("1"),
        source_revision="same-url-revision",
        source_sha256=HASH_B,
    )

    assert first.semantic_sha256 != changed.semantic_sha256
    assert first.generation_sha256 != changed.generation_sha256
    result = resolve(first, changed)
    assert result.state is BetfairConstraintResolutionState.CONFLICTING_UNVERIFIED


def test_availability_change_preserves_semantics_but_changes_generation() -> None:
    first = observation()
    later = observation(
        available_at=T0 + timedelta(minutes=6),
        source_revision="same-rule-later-availability",
    )

    assert first.semantic_sha256 == later.semantic_sha256
    assert first.generation_sha256 != later.generation_sha256


def test_resolution_digest_is_candidate_order_independent() -> None:
    first = observation()
    second = observation(
        source_revision="second",
        source_sha256=HASH_B,
        retrieved_at=T0 + timedelta(minutes=3),
        reviewed_at=T0 + timedelta(minutes=4),
        available_at=T0 + timedelta(minutes=5),
    )

    left = resolve(first, second)
    right = resolve(second, first)

    assert left.resolution_sha256 == right.resolution_sha256
    assert left.candidate_generation_sha256s == right.candidate_generation_sha256s


def test_public_serialized_shape_never_mints_provider_authority() -> None:
    item = observation()
    result = resolve(item)

    assert item.provider_origin_proven is False
    assert item.current_constraint_authority is False
    assert item.execution_authorized is False
    assert result.provider_origin_proven is False
    assert result.current_constraint_authority is False
    assert result.execution_authorized is False
    assert result.real_money_execution is False



def test_equivalent_decimal_encodings_share_semantics() -> None:
    integer = observation(min_size=Decimal("1"), min_payout=Decimal("10"))
    scaled = observation(
        min_size=Decimal("1.0"),
        min_payout=Decimal("10.00"),
        source_revision="scaled-decimals",
        source_sha256=HASH_B,
    )

    assert integer.semantic_sha256 == scaled.semantic_sha256
    result = resolve(integer, scaled)
    assert result.state is BetfairConstraintResolutionState.CONSISTENT_UNVERIFIED
    assert result.min_standard_size == Decimal("1")


def test_duplicate_generation_does_not_change_resolution_identity() -> None:
    item = observation()

    single = resolve(item)
    duplicate = resolve(item, item)

    assert duplicate.resolution_sha256 == single.resolution_sha256
    assert duplicate.candidate_generation_sha256s == single.candidate_generation_sha256s
    assert len(duplicate.candidate_generation_sha256s) == 1


def test_observation_batch_is_bounded() -> None:
    item = observation()

    with pytest.raises(BetfairProviderConstraintError, match="bounded limit"):
        resolve_betfair_standard_limit_constraint_evidence(
            observations=tuple(item for _ in range(257)),
            as_of=T0 + timedelta(days=1),
            jurisdiction_scope="UK_INTERNATIONAL",
            currency_code="GBP",
        )
