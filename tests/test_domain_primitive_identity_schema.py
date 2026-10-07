from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.domain import MarketEvent, MarketType, TicketLeg


_TS = "2026-10-07T00:00:00+00:00"


class _ExplosiveString(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("str subclass strip() must not execute")

    def encode(self, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("str subclass encode() must not execute")

    def __format__(self, spec: str) -> str:
        raise AssertionError("str subclass formatting must not execute")



class _ExplosiveInt(int):
    def __format__(self, spec: str) -> str:
        raise AssertionError("int subclass formatting must not execute")


def _event(**overrides: object) -> MarketEvent:
    payload: dict[str, object] = {
        "event_id": "event-1",
        "market_id": "market-1",
        "selection_id": "selection-1",
        "decimal_odds": Decimal("2.5"),
        "observed_ts": _TS,
        "source_id": "provider-1",
        "sequence": 1,
        "ingest_ts": _TS,
    }
    payload.update(overrides)
    return MarketEvent(**payload)  # type: ignore[arg-type]



def test_market_event_from_dict_rejects_hostile_key_before_hash_or_equality_dispatch() -> None:
    raw = _event().to_dict()

    class HostileKey(str):
        armed = False

        def __hash__(self):
            if self.armed:
                raise AssertionError("hostile market-event key hashed before exact admission")
            return str.__hash__(self)

        def __eq__(self, other):
            if self.armed:
                raise AssertionError("hostile market-event key compared before exact admission")
            return str.__eq__(self, other)

    key = HostileKey("event_id")
    value = raw.pop("event_id")
    raw[key] = value
    key.armed = True

    with pytest.raises(ValueError, match="serialized market event fields mismatch"):
        MarketEvent.from_dict(raw)


def test_market_event_from_dict_rejects_unknown_schema_field() -> None:
    raw = _event().to_dict()
    raw["provider_specific_alias"] = "event-1"

    with pytest.raises(ValueError, match="serialized market event fields mismatch"):
        MarketEvent.from_dict(raw)


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("event_id", "event-1"),
        ("market_id", "market-1"),
        ("selection_id", "selection-1"),
        ("source_id", "provider-1"),
    ),
)
def test_market_event_direct_constructor_rejects_string_subclass_before_dispatch(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        _event(**{field_name: _ExplosiveString(value)})


@pytest.mark.parametrize("sequence", (True, _ExplosiveInt(1)))
def test_market_event_direct_constructor_rejects_noncanonical_sequence(
    sequence: object,
) -> None:
    with pytest.raises(ValueError, match="sequence must be a non-boolean int"):
        _event(sequence=sequence)


@pytest.mark.parametrize("sequence", (-(2**63) - 1, 2**63))
def test_market_event_rejects_sequence_outside_durable_signed_64_range(
    sequence: int,
) -> None:
    with pytest.raises(ValueError, match="sequence must fit signed 64-bit integer"):
        _event(sequence=sequence)


@pytest.mark.parametrize("sequence", (-(2**63), 2**63 - 1))
def test_market_event_accepts_durable_signed_64_sequence_boundaries(
    sequence: int,
) -> None:
    event = _event(sequence=sequence)
    assert event.sequence == sequence
    assert event.to_dict()["sequence"] == sequence


@pytest.mark.parametrize(
    "field_name",
    ("event_id", "market_id", "selection_id", "source_id"),
)
@pytest.mark.parametrize("value", ("", " padded", "padded ", "\x00inside", "line\nbreak", "del\x7finside", "\ud800"))
def test_market_event_direct_constructor_rejects_noncanonical_identity(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        _event(**{field_name: value})


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("event_id", "event-1"),
        ("market_id", "market-1"),
        ("selection_id", "selection-1"),
    ),
)
def test_ticket_leg_direct_constructor_rejects_string_subclass_before_dispatch(
    field_name: str,
    value: str,
) -> None:
    payload = {
        "event_id": "event-1",
        "market_id": "market-1",
        "selection_id": "selection-1",
        "locked_odds": Decimal("2.5"),
    }
    payload[field_name] = _ExplosiveString(value)
    with pytest.raises(ValueError):
        TicketLeg(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize("field_name", ("event_id", "market_id", "selection_id"))
@pytest.mark.parametrize("value", ("", " padded", "padded ", "\x00inside", "line\nbreak", "del\x7finside", "\ud800"))
def test_ticket_leg_direct_constructor_rejects_noncanonical_identity(
    field_name: str,
    value: str,
) -> None:
    payload = {
        "event_id": "event-1",
        "market_id": "market-1",
        "selection_id": "selection-1",
        "locked_odds": Decimal("2.5"),
    }
    payload[field_name] = value
    with pytest.raises(ValueError):
        TicketLeg(**payload)  # type: ignore[arg-type]



@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("source_id", "provider-1"),
        ("event_id", "event-1"),
        ("market_id", "market-1"),
        ("selection_id", "selection-1"),
    ),
)
def test_market_event_dedupe_key_rejects_post_construction_identity_subclass_before_dispatch(
    field_name: str,
    value: str,
) -> None:
    event = _event()
    object.__setattr__(event, field_name, _ExplosiveString(value))

    with pytest.raises(ValueError):
        _ = event.dedupe_key



def test_market_event_dedupe_key_rejects_post_construction_sequence_subclass_before_dispatch() -> None:
    event = _event()
    object.__setattr__(event, "sequence", _ExplosiveInt(1))

    with pytest.raises(ValueError, match="sequence must be a non-boolean int"):
        _ = event.dedupe_key



def test_market_event_dedupe_key_rejects_post_construction_out_of_range_sequence() -> None:
    event = _event()
    object.__setattr__(event, "sequence", 2**63)

    with pytest.raises(ValueError, match="sequence must fit signed 64-bit integer"):
        _ = event.dedupe_key

def test_valid_delimiter_bearing_domain_identity_is_not_normalized_or_rewritten() -> None:
    event = _event(
        event_id="event|2026",
        market_id="market|spread",
        selection_id="player|a",
    )
    leg = TicketLeg(
        event_id="event|2026",
        market_id="market|spread",
        selection_id="player|a",
        locked_odds=Decimal("2.5"),
    )

    assert event.event_id == "event|2026"
    assert event.market_id == "market|spread"
    assert event.selection_id == "player|a"
    assert event.quote_key == leg.quote_key
    assert event.quote_key.startswith("component-boundary-v1-")

@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("source_id", "provider-1"),
        ("event_id", "event-1"),
        ("market_id", "market-1"),
        ("selection_id", "selection-1"),
        ("sport", "football"),
        ("competition_id", "league:one"),
        ("market_semantics_id", "market:semantics"),
        ("provider_source_class", "provider:class"),
        ("exchange_side", "back"),
    ),
)
def test_market_event_to_dict_rejects_post_construction_identity_subclass_before_dispatch(
    field_name: str,
    value: str,
) -> None:
    event = _event(
        sport="football",
        competition_id="league:one",
        market_semantics_id="market:semantics",
        provider_source_class="provider:class",
        exchange_side="back",
    )
    object.__setattr__(event, field_name, _ExplosiveString(value))

    with pytest.raises(ValueError):
        event.to_dict()


@pytest.mark.parametrize("sequence", (True, _ExplosiveInt(1)))
def test_market_event_to_dict_rejects_post_construction_noncanonical_sequence(
    sequence: object,
) -> None:
    event = _event()
    object.__setattr__(event, "sequence", sequence)

    with pytest.raises(ValueError, match="sequence must be a non-boolean int"):
        event.to_dict()

def test_delimiter_bearing_quote_components_cannot_alias_boundaries() -> None:
    left = _event(event_id="a|b", market_id="c", selection_id="d")
    right = _event(event_id="a", market_id="b|c", selection_id="d")

    assert left.quote_key != right.quote_key
    assert left.quote_key.startswith("component-boundary-v1-")
    assert right.quote_key.startswith("component-boundary-v1-")


def test_delimiter_bearing_dedupe_components_cannot_alias_boundaries() -> None:
    left = _event(
        source_id="provider|a",
        event_id="b",
        market_id="c",
        selection_id="d",
        sequence=1,
    )
    right = _event(
        source_id="provider",
        event_id="a|b",
        market_id="c",
        selection_id="d",
        sequence=1,
    )

    assert left.dedupe_key != right.dedupe_key
    assert left.dedupe_key.startswith("component-boundary-v1-")
    assert right.dedupe_key.startswith("component-boundary-v1-")



class _ExplosiveMarketType:
    @property
    def value(self) -> str:
        raise AssertionError("noncanonical market_type member dispatch must not execute")


def test_market_event_direct_constructor_requires_exact_market_type() -> None:
    with pytest.raises(ValueError, match="canonical MarketType"):
        _event(market_type=_ExplosiveMarketType())


def test_market_event_to_dict_revalidates_market_type_before_member_dispatch() -> None:
    event = _event(market_type=MarketType.WINNER)
    object.__setattr__(event, "market_type", _ExplosiveMarketType())

    with pytest.raises(ValueError, match="canonical MarketType"):
        event.to_dict()


def test_market_event_canonical_market_type_round_trips_unchanged() -> None:
    event = _event(market_type=MarketType.TOTAL)

    assert event.to_dict()["market_type"] == MarketType.TOTAL.value
