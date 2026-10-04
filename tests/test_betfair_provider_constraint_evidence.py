from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, localcontext
import pickle

import pytest

import autosport.betfair_provider_constraint_evidence as constraint_evidence

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
    source_ref: str = SOURCE,
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
        source_ref=source_ref,
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
    cutoff = T0 + timedelta(days=1)

    future_only = resolve(future, as_of=cutoff)
    empty = resolve(as_of=cutoff)

    assert future_only.state is BetfairConstraintResolutionState.NO_EVIDENCE
    assert future_only.candidate_generation_sha256s == ()
    assert future_only.min_standard_size is None
    assert future_only.resolution_sha256 == empty.resolution_sha256


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
    assert item.real_money_execution is False
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



def test_semantic_decimal_identity_ignores_ambient_context_rounding() -> None:
    first = observation(
        min_size=Decimal("1.2345671"),
        min_payout=Decimal("10"),
        source_revision="first-high-precision",
    )
    second = observation(
        min_size=Decimal("1.2345672"),
        min_payout=Decimal("10"),
        source_revision="second-high-precision",
        source_sha256=HASH_B,
    )

    with localcontext() as context:
        context.prec = 6
        assert first.semantic_sha256 != second.semantic_sha256

    result = resolve(first, second)
    assert result.state is BetfairConstraintResolutionState.CONFLICTING_UNVERIFIED



def test_source_reference_text_is_bounded() -> None:
    with pytest.raises(BetfairProviderConstraintError, match="bounded canonical"):
        observation(source_ref="x" * 4097)


def test_source_revision_text_is_bounded() -> None:
    with pytest.raises(BetfairProviderConstraintError, match="bounded canonical"):
        observation(source_revision="r" * 4097)



def test_hard_false_authority_surfaces_are_non_python_and_sealed() -> None:
    item = observation()
    result = resolve(item)

    for cls, instance, names in (
        (
            BetfairProviderConstraintObservation,
            item,
            (
                "provider_origin_proven",
                "current_constraint_authority",
                "execution_authorized",
                "real_money_execution",
            ),
        ),
        (
            type(result),
            result,
            (
                "provider_origin_proven",
                "current_constraint_authority",
                "execution_authorized",
                "real_money_execution",
            ),
        ),
    ):
        for name in names:
            descriptor = cls.__dict__[name]
            assert isinstance(descriptor, property)
            assert descriptor.fget is not None
            assert not hasattr(descriptor.fget, "__code__")
            assert getattr(instance, name) is False

        with pytest.raises(TypeError, match="authority surface is sealed"):
            cls.current_constraint_authority = property(lambda _self: True)

        with pytest.raises(TypeError, match="authority surface is sealed"):
            cls._current_constraint_authority_constant = True


def test_structural_evidence_and_resolution_are_instance_immutable() -> None:
    item = observation()
    original_generation = item.generation_sha256
    with pytest.raises(AttributeError):
        object.__setattr__(item, "min_standard_size", Decimal("2"))
    with pytest.raises(AttributeError):
        object.__setattr__(item, "available_at", T0 + timedelta(days=3))
    assert item.min_standard_size == Decimal("1")
    assert item.generation_sha256 == original_generation

    result = resolve(item)
    original_resolution = result.resolution_sha256
    with pytest.raises(AttributeError):
        object.__setattr__(result, "min_standard_size", Decimal("999"))
    with pytest.raises(AttributeError):
        object.__setattr__(
            result,
            "state",
            BetfairConstraintResolutionState.CONFLICTING_UNVERIFIED,
        )
    assert result.min_standard_size == Decimal("1")
    assert result.state is BetfairConstraintResolutionState.CONSISTENT_UNVERIFIED
    assert result.resolution_sha256 == original_resolution
    assert result.current_constraint_authority is False
    assert result.execution_authorized is False


def test_structural_evidence_class_identity_surface_is_sealed() -> None:
    for name, value in (
        ("min_standard_size", property(lambda _self: Decimal("999"))),
        ("available_at", property(lambda _self: T0)),
        ("semantic_sha256", property(lambda _self: "0" * 64)),
        ("generation_sha256", property(lambda _self: "0" * 64)),
    ):
        with pytest.raises(TypeError, match="authority surface is sealed"):
            setattr(BetfairProviderConstraintObservation, name, value)


def test_constraint_evidence_tuple_reconstruction_round_trip() -> None:
    item = observation()
    restored_item = pickle.loads(pickle.dumps(item))
    assert type(restored_item) is BetfairProviderConstraintObservation
    assert restored_item == item
    assert restored_item.semantic_sha256 == item.semantic_sha256
    assert restored_item.generation_sha256 == item.generation_sha256
    assert restored_item.provider_origin_proven is False

    result = resolve(item)
    restored_result = pickle.loads(pickle.dumps(result))
    assert type(restored_result) is type(result)
    assert restored_result == result
    assert restored_result.resolution_sha256 == result.resolution_sha256
    assert restored_result.current_constraint_authority is False
    assert restored_result.execution_authorized is False


def test_resolver_captures_structural_dependencies_against_module_rebinding(
    monkeypatch,
) -> None:
    item = observation()

    monkeypatch.setattr(constraint_evidence, "PROVIDER_ID", "attacker")
    monkeypatch.setattr(constraint_evidence, "ORDER_FAMILY", "OTHER")
    monkeypatch.setattr(constraint_evidence, "SCHEMA_VERSION", 999)
    monkeypatch.setattr(constraint_evidence, "_MAX_OBSERVATIONS", 0)
    monkeypatch.setattr(constraint_evidence, "_MAX_DECIMAL_DIGITS", 1)
    monkeypatch.setattr(constraint_evidence, "_MAX_ABS_EXPONENT", 0)
    monkeypatch.setattr(
        constraint_evidence,
        "BetfairProviderConstraintObservation",
        object,
    )
    monkeypatch.setattr(
        constraint_evidence,
        "BetfairProviderConstraintResolution",
        object,
    )
    monkeypatch.setattr(
        constraint_evidence,
        "BetfairConstraintResolutionState",
        object,
    )
    monkeypatch.setattr(
        constraint_evidence,
        "_utc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("live _utc dispatch")
        ),
    )
    monkeypatch.setattr(
        constraint_evidence,
        "_scope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("live _scope dispatch")
        ),
    )
    monkeypatch.setattr(
        constraint_evidence,
        "_currency",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("live _currency dispatch")
        ),
    )
    monkeypatch.setattr(
        constraint_evidence,
        "_canonical_sha256",
        lambda *_args, **_kwargs: "0" * 64,
    )

    result = resolve_betfair_standard_limit_constraint_evidence(
        observations=(item,),
        as_of=T0 + timedelta(days=1),
        jurisdiction_scope="UK_INTERNATIONAL",
        currency_code="GBP",
    )
    assert result.state is BetfairConstraintResolutionState.CONSISTENT_UNVERIFIED
    assert result.min_standard_size == Decimal("1")
    assert result.min_payout == Decimal("10")
    assert result.provider_origin_proven is False
    assert result.current_constraint_authority is False
    assert result.execution_authorized is False


def test_observation_digests_use_non_python_immutable_getters() -> None:
    item = observation()
    for name in ("semantic_sha256", "generation_sha256"):
        descriptor = BetfairProviderConstraintObservation.__dict__[name]
        assert isinstance(descriptor, property)
        assert descriptor.fget is not None
        assert not hasattr(descriptor.fget, "__code__")
        assert len(getattr(item, name)) == 64

    semantic_before = item.semantic_sha256
    generation_before = item.generation_sha256
    with pytest.raises(AttributeError):
        object.__setattr__(item, "semantic_sha256", "0" * 64)
    with pytest.raises(AttributeError):
        object.__setattr__(item, "generation_sha256", "0" * 64)
    assert item.semantic_sha256 == semantic_before
    assert item.generation_sha256 == generation_before

def test_observation_type_rejects_subclass_authority_laundering() -> None:
    with pytest.raises(TypeError, match="authority surface classes are final"):
        class ForgedObservation(BetfairProviderConstraintObservation):
            _current_constraint_authority_constant = True


def test_resolution_type_rejects_subclass_authority_laundering() -> None:
    resolution_type = type(resolve(observation()))

    with pytest.raises(TypeError, match="authority surface classes are final"):
        class ForgedResolution(resolution_type):
            execution_authorized = property(lambda _self: True)


class _MutableOffsetTz(tzinfo):
    def __init__(self, offset: timedelta) -> None:
        self.offset = offset

    def utcoffset(self, _dt: datetime | None) -> timedelta:
        return self.offset

    def dst(self, _dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, _dt: datetime | None) -> str:
        return "MUTABLE"


def test_observation_freezes_normalized_time_against_mutable_tzinfo() -> None:
    mutable_zone = _MutableOffsetTz(timedelta(0))
    item = observation(
        retrieved_at=datetime(2026, 9, 1, 0, 0, tzinfo=mutable_zone),
        reviewed_at=datetime(2026, 9, 1, 0, 1, tzinfo=mutable_zone),
        available_at=datetime(2026, 9, 1, 0, 2, tzinfo=mutable_zone),
        effective_from=datetime(2026, 9, 1, 0, 0, tzinfo=mutable_zone),
        review_expires_at=datetime(2026, 9, 8, 0, 0, tzinfo=mutable_zone),
    )
    cutoff = T0 + timedelta(minutes=3)
    generation_before = item.generation_sha256
    available_before = item.available_at.astimezone(UTC)

    assert item.available_at.tzinfo is UTC
    assert resolve(item, as_of=cutoff).state is (
        BetfairConstraintResolutionState.CONSISTENT_UNVERIFIED
    )

    mutable_zone.offset = timedelta(hours=-12)

    assert item.generation_sha256 == generation_before
    assert item.available_at.astimezone(UTC) == available_before
    assert resolve(item, as_of=cutoff).state is (
        BetfairConstraintResolutionState.CONSISTENT_UNVERIFIED
    )

