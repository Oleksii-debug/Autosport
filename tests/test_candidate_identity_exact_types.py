from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.candidate_search import BeamParlayCandidateSearch, CandidateLeg


class _TrapStr(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("str subclass virtual method must not execute")


class _CandidateLegSubclass(CandidateLeg):
    pass


def _leg() -> CandidateLeg:
    return CandidateLeg(
        quote_key="event-1|market-1|selection-1",
        event_id="event-1",
        decimal_odds=Decimal("2"),
        probability=Decimal("0.5"),
        market_id="market-1",
        selection_id="selection-1",
    )


def test_constructor_rejects_hostile_quote_key_before_hash_or_strip() -> None:
    class _HashTrapStr(str):
        def __hash__(self) -> int:
            raise AssertionError("candidate identity must reject str subclass before hashing")

        def strip(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("candidate identity must reject str subclass before strip")

    with pytest.raises(ValueError, match="quote_key must be a non-empty canonical string"):
        CandidateLeg(
            quote_key=_HashTrapStr("event-1|market-1|selection-1"),  # type: ignore[arg-type]
            event_id="event-1",
            decimal_odds=Decimal("2"),
            probability=Decimal("0.5"),
            market_id="market-1",
            selection_id="selection-1",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("event_id", _TrapStr("event-1")),
        ("market_id", _TrapStr("market-1")),
        ("selection_id", _TrapStr("selection-1")),
        ("event_id", " event-1"),
    ),
)
def test_constructor_rejects_noncanonical_structured_identity(
    field: str,
    value: str,
) -> None:
    kwargs: dict[str, object] = {
        "quote_key": "event-1|market-1|selection-1",
        "event_id": "event-1",
        "decimal_odds": Decimal("2"),
        "probability": Decimal("0.5"),
        "market_id": "market-1",
        "selection_id": "selection-1",
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match="canonical string"):
        CandidateLeg(**kwargs)  # type: ignore[arg-type]


def test_constructor_rejects_half_structured_ticket_identity() -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        CandidateLeg(
            quote_key="event-1|market-1|selection-1",
            event_id="event-1",
            decimal_odds=Decimal("2"),
            probability=Decimal("0.5"),
            market_id="market-1",
            selection_id=None,
        )


@pytest.mark.parametrize("field", ("event_id", "market_id", "selection_id"))
def test_ticket_identity_rejects_str_subclass_before_virtual_method(field: str) -> None:
    leg = _leg()
    object.__setattr__(leg, field, _TrapStr(getattr(leg, field)))

    with pytest.raises(ValueError, match="canonical string"):
        leg.ticket_identity()


def test_ticket_identity_rejects_quote_key_str_subclass_before_virtual_method() -> None:
    leg = _leg()
    object.__setattr__(leg, "quote_key", _TrapStr(leg.quote_key))

    with pytest.raises(ValueError, match="quote_key must be a non-empty canonical string"):
        leg.ticket_identity()




class _TrapList(list):
    def __iter__(self):
        raise AssertionError("list subclass iteration must not execute")


def test_search_rejects_leg_list_subclass_before_iteration() -> None:
    with pytest.raises(ValueError, match="candidate legs must be an exact list"):
        BeamParlayCandidateSearch().search(_TrapList([_leg()]), minimum_legs=1)  # type: ignore[arg-type]


def test_search_rejects_quote_key_str_subclass_before_virtual_method() -> None:
    leg = _leg()
    object.__setattr__(leg, "quote_key", _TrapStr(leg.quote_key))

    with pytest.raises(ValueError, match="quote_key must be a non-empty canonical string"):
        BeamParlayCandidateSearch().search([leg], minimum_legs=1)


def test_search_rejects_event_id_str_subclass_before_virtual_method() -> None:
    leg = _leg()
    object.__setattr__(leg, "event_id", _TrapStr(leg.event_id))

    with pytest.raises(ValueError, match="event_id must be a non-empty canonical string"):
        BeamParlayCandidateSearch().search([leg], minimum_legs=1)


def test_search_rejects_candidate_leg_subclass_before_member_dispatch() -> None:
    hostile = _CandidateLegSubclass(
        quote_key="event-1|market-1|selection-1",
        event_id="event-1",
        decimal_odds=Decimal("2"),
        probability=Decimal("0.5"),
        market_id="market-1",
        selection_id="selection-1",
    )

    with pytest.raises(ValueError, match="must be an exact CandidateLeg"):
        BeamParlayCandidateSearch().search([hostile], minimum_legs=1)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("quote_key", "event-1|market-1|selection-1\x00"),
        ("event_id", "event-1\x00"),
        ("market_id", "market-1\x00"),
        ("selection_id", "selection-1\x00"),
        ("event_id", "event-\ud800"),
    ),
)
def test_ticket_identity_rejects_noncanonical_identity_bytes(
    field: str,
    value: str,
) -> None:
    leg = _leg()
    object.__setattr__(leg, field, value)

    with pytest.raises(ValueError, match="canonical string"):
        leg.ticket_identity()


@pytest.mark.parametrize(
    "quote_key",
    (
        "event-1| market-1|selection-1",
        "event-1|market-1|selection-1 ",
        "event-1|market-1\x00|selection-1",
    ),
)
def test_legacy_ticket_suffix_cannot_mint_noncanonical_structured_identity(
    quote_key: str,
) -> None:
    leg = _leg()
    object.__setattr__(leg, "quote_key", quote_key)

    with pytest.raises(
        ValueError,
        match="structured event/market/selection identity does not match quote_key",
    ):
        leg.ticket_identity()


def test_search_rejects_non_utf8_quote_key_before_identity_use() -> None:
    leg = _leg()
    object.__setattr__(leg, "quote_key", "event-1|market-1|selection-\ud800")

    with pytest.raises(ValueError, match="quote_key must be a non-empty canonical string"):
        BeamParlayCandidateSearch().search([leg], minimum_legs=1)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("event_id", "event-1\nforged"),
        ("market_id", "market-1\tforged"),
        ("selection_id", "selection-1\rforged"),
        ("event_id", "event-1\x7fforged"),
    ),
)
def test_candidate_identity_rejects_non_nul_control_aliases(
    field: str,
    value: str,
) -> None:
    kwargs: dict[str, object] = {
        "quote_key": "event-1|market-1|selection-1",
        "event_id": "event-1",
        "decimal_odds": Decimal("2"),
        "probability": Decimal("0.5"),
        "market_id": "market-1",
        "selection_id": "selection-1",
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match="canonical string"):
        CandidateLeg(**kwargs)  # type: ignore[arg-type]
