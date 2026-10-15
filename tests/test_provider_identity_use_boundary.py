from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.providers import CanonicalNormalizer, ProviderBatch, ProviderQuote


class _TrapStr(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("provider identity str subclass virtual method must not execute")


class _HostileQuoteSubclass(ProviderQuote):
    def __post_init__(self) -> None:
        object.__setattr__(self, "member_accesses", 0)
        object.__setattr__(self, "_armed", False)

    def __getattribute__(self, name: str):
        if name in {"member_accesses", "_armed", "__class__", "__dict__", "__post_init__"}:
            return object.__getattribute__(self, name)
        if not object.__getattribute__(self, "_armed"):
            return object.__getattribute__(self, name)
        object.__setattr__(
            self,
            "member_accesses",
            object.__getattribute__(self, "member_accesses") + 1,
        )
        raise AssertionError(f"hostile ProviderQuote member accessed: {name}")


def _quote() -> ProviderQuote:
    return ProviderQuote(
        provider_event_id="event-1",
        provider_market_id="winner",
        provider_selection_id="home",
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-10-07T00:00:00+00:00",
        sequence=1,
        sport="football",
    )


@pytest.mark.parametrize(
    "field",
    ("provider_event_id", "provider_market_id", "provider_selection_id", "sport"),
)
def test_provider_quote_rejects_identity_str_subclass_before_virtual_method(field: str) -> None:
    values: dict[str, object] = {
        "provider_event_id": "event-1",
        "provider_market_id": "winner",
        "provider_selection_id": "home",
        "decimal_odds": Decimal("2.00"),
        "observed_ts": "2026-10-07T00:00:00+00:00",
        "sequence": 1,
        "sport": "football",
    }
    values[field] = _TrapStr(str(values[field]))

    with pytest.raises((TypeError, ValueError)):
        ProviderQuote(**values)  # type: ignore[arg-type]


def test_source_identity_rejects_str_subclass_before_virtual_method() -> None:
    source_id = _TrapStr("provider-a")

    with pytest.raises(TypeError, match="source_id must be str"):
        ProviderBatch(source_id, ())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="source_id must be str"):
        CanonicalNormalizer().normalize(source_id, _quote())  # type: ignore[arg-type]


def test_source_identity_rejects_lone_surrogate() -> None:
    with pytest.raises(ValueError, match="valid UTF-8"):
        ProviderBatch("provider-\ud800", ())
    with pytest.raises(ValueError, match="valid UTF-8"):
        CanonicalNormalizer().normalize("provider-\ud800", _quote())


@pytest.mark.parametrize(
    "field",
    ("provider_event_id", "provider_market_id", "provider_selection_id", "sport"),
)
def test_provider_quote_rejects_lone_surrogate_identity(field: str) -> None:
    values: dict[str, object] = {
        "provider_event_id": "event-1",
        "provider_market_id": "winner",
        "provider_selection_id": "home",
        "decimal_odds": Decimal("2.00"),
        "observed_ts": "2026-10-07T00:00:00+00:00",
        "sequence": 1,
        "sport": "football",
    }
    values[field] = "\ud800"

    with pytest.raises(ValueError):
        ProviderQuote(**values)  # type: ignore[arg-type]


def test_use_boundaries_reject_post_construction_lone_surrogate_identity() -> None:
    quote = _quote()
    object.__setattr__(quote, "provider_selection_id", "\ud800")

    with pytest.raises(ValueError, match="valid UTF-8"):
        ProviderBatch("provider-a", (quote,))
    with pytest.raises(ValueError, match="valid UTF-8"):
        CanonicalNormalizer().normalize("provider-a", quote)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("provider_event_id", "event|forged", "reserved identity delimiter"),
        ("provider_market_id", "winner|forged", "reserved identity delimiter"),
        ("provider_selection_id", "home\nforged", "control characters"),
        ("sequence", True, "sequence"),
        ("sport", "foot\x00ball", "control characters"),
    ),
)
def test_batch_revalidates_exact_quote_identity_after_object_tamper(
    field: str,
    value: object,
    message: str,
) -> None:
    quote = _quote()
    object.__setattr__(quote, field, value)

    with pytest.raises((TypeError, ValueError), match=message):
        ProviderBatch("provider-a", (quote,))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("provider_event_id", "event|forged", "reserved identity delimiter"),
        ("provider_market_id", "winner|forged", "reserved identity delimiter"),
        ("provider_selection_id", "home\x7fforged", "control characters"),
        ("sequence", False, "sequence"),
        ("sport", _TrapStr("football"), "sport must be str"),
    ),
)
def test_normalizer_revalidates_exact_quote_identity_after_object_tamper(
    field: str,
    value: object,
    message: str,
) -> None:
    quote = _quote()
    object.__setattr__(quote, field, value)

    with pytest.raises((TypeError, ValueError), match=message):
        CanonicalNormalizer().normalize("provider-a", quote)


def test_normalizer_rejects_quote_subclass_before_member_access() -> None:
    quote = _HostileQuoteSubclass(
        provider_event_id="event-1",
        provider_market_id="winner",
        provider_selection_id="home",
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-10-07T00:00:00+00:00",
        sequence=1,
        sport="football",
    )

    object.__setattr__(quote, "_armed", True)

    with pytest.raises(TypeError, match="quote must be ProviderQuote"):
        CanonicalNormalizer().normalize("provider-a", quote)

    assert quote.member_accesses == 0


def test_batch_cursor_rejects_subclass_control_and_non_utf8_text() -> None:
    with pytest.raises(TypeError, match="cursor must be str"):
        ProviderBatch("provider-a", (_quote(),), cursor=_TrapStr("1"))
    with pytest.raises(ValueError, match="cursor must not contain control"):
        ProviderBatch("provider-a", (_quote(),), cursor="1\n2")
    with pytest.raises(ValueError, match="valid UTF-8"):
        ProviderBatch("provider-a", (_quote(),), cursor="\ud800")



def test_normalizer_rejects_metadata_dict_subclass_before_items_dispatch() -> None:
    dispatch_calls: list[str] = []

    class HostileMetadata(dict):
        def items(self):
            dispatch_calls.append("items")
            raise AssertionError("metadata mapping dispatch must not execute")

    quote = _quote()
    object.__setattr__(quote, "metadata", HostileMetadata({"origin": "fixture"}))

    with pytest.raises(TypeError, match="metadata must be an exact dict"):
        CanonicalNormalizer().normalize("provider-a", quote)

    assert dispatch_calls == []


def test_normalizer_rejects_nested_json_container_subclass_before_iteration() -> None:
    dispatch_calls: list[str] = []

    class HostileList(list):
        def __iter__(self):
            dispatch_calls.append("iter")
            raise AssertionError("nested JSON list dispatch must not execute")

    quote = _quote()
    object.__setattr__(quote, "metadata", {"nested": HostileList(["value"])})

    with pytest.raises(TypeError, match="non-canonical JSON value type HostileList"):
        CanonicalNormalizer().normalize("provider-a", quote)

    assert dispatch_calls == []


def test_normalizer_rejects_json_key_subclass_before_hash_dispatch() -> None:
    dispatch_calls: list[str] = []

    class HostileKey(str):
        armed = False

        def __hash__(self) -> int:
            if self.armed:
                dispatch_calls.append("hash")
                raise AssertionError("JSON key hash dispatch must not execute")
            return str.__hash__(self)

    key = HostileKey("origin")
    metadata = {key: "fixture"}
    key.armed = True

    quote = _quote()
    object.__setattr__(quote, "metadata", metadata)

    with pytest.raises(TypeError, match="non-string JSON object key"):
        CanonicalNormalizer().normalize("provider-a", quote)

    assert dispatch_calls == []


def test_valid_provider_identity_remains_byte_stable() -> None:
    quote = _quote()
    batch = ProviderBatch("provider-a", (quote,), cursor="1")
    event = CanonicalNormalizer().normalize(batch.source_id, batch.quotes[0])

    assert event.source_id == "provider-a"
    assert event.event_id == "provider-a:event-1"
    assert event.market_id == "provider-a:winner"
    assert event.selection_id == "provider-a:home"
