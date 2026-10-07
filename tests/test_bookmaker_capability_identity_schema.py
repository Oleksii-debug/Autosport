from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityError,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    BookmakerPositionObservation,
    BookmakerPositionState,
)


TS = "2026-10-07T00:00:00+00:00"
SHA = "a" * 64


class _TrapStr(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("str subclass strip must not execute")

    def encode(self, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("str subclass encode must not execute")


class _TrapFrozenSet(frozenset):
    def __iter__(self):
        raise AssertionError("frozenset subclass iteration must not execute")

    def __contains__(self, item: object) -> bool:
        raise AssertionError("frozenset subclass membership must not execute")


class _TrapTuple(tuple):
    def __iter__(self):
        raise AssertionError("tuple subclass iteration must not execute")


def _facts(*caps: BookmakerCapability) -> tuple[BookmakerCapabilityFact, ...]:
    return tuple(
        BookmakerCapabilityFact(cap, BookmakerCapabilityState.SUPPORTED)
        for cap in caps
    )


def _profile(**overrides: object) -> BookmakerCapabilityProfile:
    values: dict[str, object] = {
        "venue_id": "book-a",
        "account_id": "acct-a",
        "adapter_id": "adapter-a",
        "adapter_version": "1.0",
        "profile_version": 1,
        "facts": _facts(
            BookmakerCapability.BALANCE_READ,
            BookmakerCapability.OPEN_POSITIONS_READ,
        ),
        "observed_at": TS,
        "source_ref": "provider-evidence",
        "source_payload_sha256": SHA,
    }
    values.update(overrides)
    return BookmakerCapabilityProfile(**values)  # type: ignore[arg-type]


def _balance(**overrides: object) -> BookmakerBalanceObservation:
    values: dict[str, object] = {
        "venue_id": "book-a",
        "account_id": "acct-a",
        "adapter_id": "adapter-a",
        "observation_id": "balance-1",
        "currency": "EUR",
        "available_balance": Decimal("90"),
        "observed_at": TS,
        "source_payload_sha256": SHA,
        "total_balance": Decimal("100"),
        "total_balance_source_ref": "provider-total",
    }
    values.update(overrides)
    return BookmakerBalanceObservation(**values)  # type: ignore[arg-type]


def _position(**overrides: object) -> BookmakerPositionObservation:
    values: dict[str, object] = {
        "venue_id": "book-a",
        "account_id": "acct-a",
        "adapter_id": "adapter-a",
        "observation_id": "position-1",
        "external_position_id": "provider-position-1",
        "state": BookmakerPositionState.OPEN,
        "currency": "EUR",
        "observed_at": TS,
        "source_payload_sha256": SHA,
        "provider_amount": Decimal("10"),
        "provider_amount_semantics": "backer_stake",
        "external_receipt_id": "receipt-1",
    }
    values.update(overrides)
    return BookmakerPositionObservation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("factory", "field_name"),
    (
        (_profile, "venue_id"),
        (_profile, "account_id"),
        (_profile, "adapter_id"),
        (_balance, "venue_id"),
        (_balance, "account_id"),
        (_balance, "adapter_id"),
        (_balance, "observation_id"),
        (_position, "venue_id"),
        (_position, "account_id"),
        (_position, "adapter_id"),
        (_position, "observation_id"),
        (_position, "external_position_id"),
        (_position, "external_receipt_id"),
    ),
)
def test_account_position_identifiers_reject_str_subclasses_before_dispatch(
    factory,
    field_name: str,
) -> None:
    with pytest.raises(BookmakerCapabilityError, match="exact canonical identity text"):
        factory(**{field_name: _TrapStr("identity")})


@pytest.mark.parametrize(
    ("factory", "field_name"),
    (
        (_profile, "account_id"),
        (_balance, "observation_id"),
        (_position, "external_position_id"),
        (_position, "external_receipt_id"),
    ),
)
def test_account_position_identifiers_reject_non_utf8_aliases(
    factory,
    field_name: str,
) -> None:
    with pytest.raises(BookmakerCapabilityError, match="UTF-8 identity text"):
        factory(**{field_name: "identity\ud800"})


def test_profile_rejects_facts_tuple_subclass_before_dispatch() -> None:
    facts = _TrapTuple(
        (
            BookmakerCapabilityFact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        )
    )
    with pytest.raises(BookmakerCapabilityError, match="exact tuple"):
        _profile(facts=facts)


def test_profile_rejects_capability_fact_subclass_before_dispatch() -> None:
    class HostileFact(BookmakerCapabilityFact):
        armed = False

        def __getattribute__(self, name: str):
            if type(self).armed and name in {"capability", "state"}:
                raise AssertionError("capability fact subclass dispatch must not execute")
            return super().__getattribute__(name)

    fact = HostileFact(
        BookmakerCapability.BALANCE_READ,
        BookmakerCapabilityState.SUPPORTED,
    )
    HostileFact.armed = True
    with pytest.raises(BookmakerCapabilityError, match="exact BookmakerCapabilityFact"):
        _profile(facts=(fact,))


def test_profile_revalidates_exact_fact_after_post_init_mutation() -> None:
    fact = BookmakerCapabilityFact(
        BookmakerCapability.BALANCE_READ,
        BookmakerCapabilityState.SUPPORTED,
    )
    profile = _profile(facts=(fact,))
    object.__setattr__(fact, "state", "supported")

    with pytest.raises(BookmakerCapabilityError, match="BookmakerCapabilityState"):
        profile.to_canonical_dict()
    with pytest.raises(BookmakerCapabilityError, match="BookmakerCapabilityState"):
        profile.state_of(BookmakerCapability.BALANCE_READ)


def test_snapshot_rejects_observed_capabilities_subclass_before_dispatch() -> None:
    observed = _TrapFrozenSet(
        {
            BookmakerCapability.BALANCE_READ,
            BookmakerCapability.OPEN_POSITIONS_READ,
        }
    )

    with pytest.raises(BookmakerCapabilityError, match="exact frozenset"):
        BookmakerAccountSnapshot(
            profile=_profile(),
            observed_capabilities=observed,
            observed_at=TS,
            balance=_balance(),
            open_positions=(_position(),),
        )


@pytest.mark.parametrize("field_name", ("open_positions", "settled_positions"))
def test_snapshot_rejects_position_tuple_subclass_before_dispatch(
    field_name: str,
) -> None:
    values: dict[str, object] = {
        "profile": _profile(),
        "observed_capabilities": frozenset(
            {
                BookmakerCapability.BALANCE_READ,
                BookmakerCapability.OPEN_POSITIONS_READ,
            }
        ),
        "observed_at": TS,
        "balance": _balance(),
    }
    values[field_name] = _TrapTuple((_position(),))

    with pytest.raises(BookmakerCapabilityError, match="exact tuple"):
        BookmakerAccountSnapshot(**values)  # type: ignore[arg-type]


def test_snapshot_revalidates_profile_identity_after_post_init_tamper() -> None:
    profile = _profile()
    object.__setattr__(profile, "account_id", " acct-a")

    with pytest.raises(BookmakerCapabilityError, match="profile.account_id"):
        BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=frozenset(
                {
                    BookmakerCapability.BALANCE_READ,
                    BookmakerCapability.OPEN_POSITIONS_READ,
                }
            ),
            observed_at=TS,
            balance=_balance(),
            open_positions=(_position(),),
        )


def test_snapshot_revalidates_nested_observation_identities_after_tamper() -> None:
    balance = _balance()
    position = _position()
    object.__setattr__(balance, "observation_id", " balance-1")
    object.__setattr__(position, "external_position_id", "provider-position-1 ")

    with pytest.raises(BookmakerCapabilityError, match="balance.observation_id"):
        BookmakerAccountSnapshot(
            profile=_profile(),
            observed_capabilities=frozenset(
                {
                    BookmakerCapability.BALANCE_READ,
                    BookmakerCapability.OPEN_POSITIONS_READ,
                }
            ),
            observed_at=TS,
            balance=balance,
            open_positions=(position,),
        )

    object.__setattr__(balance, "observation_id", "balance-1")
    with pytest.raises(BookmakerCapabilityError, match="external_position_id"):
        BookmakerAccountSnapshot(
            profile=_profile(),
            observed_capabilities=frozenset(
                {
                    BookmakerCapability.BALANCE_READ,
                    BookmakerCapability.OPEN_POSITIONS_READ,
                }
            ),
            observed_at=TS,
            balance=balance,
            open_positions=(position,),
        )


def test_valid_account_position_identity_spelling_remains_stable() -> None:
    profile = _profile()
    balance = _balance()
    position = _position()
    snapshot = BookmakerAccountSnapshot(
        profile=profile,
        observed_capabilities=frozenset(
            {
                BookmakerCapability.BALANCE_READ,
                BookmakerCapability.OPEN_POSITIONS_READ,
            }
        ),
        observed_at=TS,
        balance=balance,
        open_positions=(position,),
    )

    assert snapshot.profile.account_id == "acct-a"
    assert snapshot.balance is not None
    assert snapshot.balance.observation_id == "balance-1"
    assert snapshot.open_positions[0].external_position_id == "provider-position-1"
