from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from decimal import Decimal
import hashlib
import json

import pytest

from autosport.betfair_standard_lay_limit_price_bound import (
    BetfairStandardLayLimitPriceBoundError,
    BetfairStandardLayLimitPriceBoundEvidence,
    resolve_betfair_standard_lay_limit_price_bound,
)


def _request() -> dict[str, object]:
    customer_order_ref = "abc123"
    customer_ref = hashlib.sha256(
        f"placeOrders:{customer_order_ref}".encode("utf-8")
    ).hexdigest()[:32]
    return {
        "jsonrpc": "2.0",
        "method": "SportsAPING/v1.0/placeOrders",
        "params": {
            "marketId": "1.234567890",
            "instructions": [
                {
                    "selectionId": 12345,
                    "handicap": 0,
                    "side": "LAY",
                    "orderType": "LIMIT",
                    "limitOrder": {
                        "size": "12.50",
                        "price": "3.20",
                        "persistenceType": "LAPSE",
                    },
                    "customerOrderRef": customer_order_ref,
                }
            ],
            "customerRef": customer_ref,
            "async": False,
        },
        "id": 7,
    }


def _body(request: dict[str, object] | None = None) -> bytes:
    return json.dumps(
        _request() if request is None else request,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")




def _tamper_evidence(
    evidence: BetfairStandardLayLimitPriceBoundEvidence,
    field: str,
    value: object,
) -> BetfairStandardLayLimitPriceBoundEvidence:
    clone = object.__new__(BetfairStandardLayLimitPriceBoundEvidence)
    for item in fields(BetfairStandardLayLimitPriceBoundEvidence):
        object.__setattr__(clone, item.name, getattr(evidence, item.name))
    object.__setattr__(clone, field, value)
    return clone


def test_resolves_exact_standard_lay_limit_request_price_ceiling() -> None:
    body = _body()

    evidence = resolve_betfair_standard_lay_limit_price_bound(body)

    assert evidence.request_sha256 == hashlib.sha256(body).hexdigest()
    assert evidence.market_id == "1.234567890"
    assert evidence.selection_id == 12345
    assert evidence.requested_stake == Decimal("12.50")
    assert evidence.price_ceiling_odds == Decimal("3.20")
    assert evidence.adverse_price_relation == "MATCHED_LAY_ODDS_LE_REQUESTED_ODDS"
    assert evidence.request_shape_proven is True
    assert evidence.matched_fragment_price_ceiling_proven is True
    assert evidence.request_origin_proven is False
    assert evidence.provider_request_validity_proven is False
    assert evidence.acceptance_proven is False
    assert evidence.fill_proven is False
    assert evidence.latency_proven is False
    assert evidence.commission_proven is False
    assert evidence.realized_price_exact is False
    assert evidence.real_money_execution_proven is False


def test_request_identity_binds_exact_serialized_bytes() -> None:
    compact = _body()
    spaced = json.dumps(_request(), sort_keys=True, indent=2).encode("utf-8")

    compact_evidence = resolve_betfair_standard_lay_limit_price_bound(compact)
    spaced_evidence = resolve_betfair_standard_lay_limit_price_bound(spaced)

    assert compact_evidence.price_ceiling_odds == spaced_evidence.price_ceiling_odds
    assert compact_evidence.request_sha256 != spaced_evidence.request_sha256
    assert compact_evidence.evidence_id != spaced_evidence.evidence_id


def test_evidence_serialization_is_deterministic_and_truth_bounded() -> None:
    evidence = resolve_betfair_standard_lay_limit_price_bound(_body())

    first = evidence.to_dict()
    second = evidence.to_dict()

    assert first == second
    assert first["evidence_id"] == evidence.evidence_id
    assert first["matched_fragment_price_ceiling_proven"] is True
    assert first["request_origin_proven"] is False
    assert first["provider_request_validity_proven"] is False
    assert first["acceptance_proven"] is False
    assert first["fill_proven"] is False
    assert first["real_money_execution_proven"] is False


def test_evidence_cannot_be_caller_constructed() -> None:
    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="issued only"):
        BetfairStandardLayLimitPriceBoundEvidence()


@pytest.mark.parametrize("side", ["BACK", "lay", ""])
def test_rejects_non_lay_side(side: str) -> None:
    request = _request()
    request["params"]["instructions"][0]["side"] = side  # type: ignore[index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="LAY LIMIT"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


@pytest.mark.parametrize(
    ("extra_key", "extra_value"),
    [
        ("timeInForce", "FILL_OR_KILL"),
        ("minFillSize", "5.00"),
        ("betTargetType", "BACKERS_PROFIT"),
        ("betTargetSize", "10.00"),
    ],
)
def test_rejects_special_limit_semantics(extra_key: str, extra_value: str) -> None:
    request = _request()
    limit_order = request["params"]["instructions"][0]["limitOrder"]  # type: ignore[index]
    limit_order[extra_key] = extra_value  # type: ignore[index]

    with pytest.raises(
        BetfairStandardLayLimitPriceBoundError,
        match="special or nonstandard",
    ):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_non_lapse_persistence() -> None:
    request = _request()
    limit_order = request["params"]["instructions"][0]["limitOrder"]  # type: ignore[index]
    limit_order["persistenceType"] = "PERSIST"  # type: ignore[index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="LAPSE"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


@pytest.mark.parametrize("price", ["1.00", "1000.01", "NaN", "Infinity", "0"])
def test_rejects_invalid_or_out_of_range_price(price: str) -> None:
    request = _request()
    request["params"]["instructions"][0]["limitOrder"]["price"] = price  # type: ignore[index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


@pytest.mark.parametrize("size", ["0", "-1", "NaN", "Infinity"])
def test_rejects_invalid_size(size: str) -> None:
    request = _request()
    request["params"]["instructions"][0]["limitOrder"]["size"] = size  # type: ignore[index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_multiple_instructions() -> None:
    request = _request()
    instructions = request["params"]["instructions"]  # type: ignore[index]
    instructions.append(deepcopy(instructions[0]))  # type: ignore[union-attr,index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="exactly one"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_async_request() -> None:
    request = _request()
    request["params"]["async"] = True  # type: ignore[index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="synchronous"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_non_positive_selection_id() -> None:
    request = _request()
    request["params"]["instructions"][0]["selectionId"] = 0  # type: ignore[index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="selectionId"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_duplicate_json_keys() -> None:
    body = (
        b'{"id":1,"id":2,"jsonrpc":"2.0","method":"SportsAPING/v1.0/placeOrders",'
        b'"params":{"marketId":"1.2","instructions":[],"customerRef":"x","async":false}}'
    )

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="duplicate JSON"):
        resolve_betfair_standard_lay_limit_price_bound(body)


def test_rejects_unknown_instruction_fields_fail_closed() -> None:
    request = _request()
    request["params"]["instructions"][0]["newProviderOption"] = True  # type: ignore[index]

    with pytest.raises(
        BetfairStandardLayLimitPriceBoundError,
        match="nonstandard Betfair LAY instruction",
    ):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


@pytest.mark.parametrize("price", ["1.01", "1000"])
def test_accepts_exchange_odds_boundaries(price: str) -> None:
    request = _request()
    request["params"]["instructions"][0]["limitOrder"]["price"] = price  # type: ignore[index]

    evidence = resolve_betfair_standard_lay_limit_price_bound(_body(request))

    assert evidence.price_ceiling_odds == Decimal(price)


@pytest.mark.parametrize(("field", "value"), [("id", True), ("id", 0)])
def test_rejects_noncanonical_request_id(field: str, value: object) -> None:
    request = _request()
    request[field] = value

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="request id"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_bool_selection_id() -> None:
    request = _request()
    request["params"]["instructions"][0]["selectionId"] = True  # type: ignore[index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="selectionId"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_nonzero_handicap() -> None:
    request = _request()
    request["params"]["instructions"][0]["handicap"] = 1  # type: ignore[index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="zero-handicap"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_unknown_top_level_parameter() -> None:
    request = _request()
    request["params"]["marketVersion"] = {"version": 1}  # type: ignore[index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="params"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_noncanonical_customer_ref() -> None:
    request = _request()
    request["params"]["customerRef"] = "not-canonical"  # type: ignore[index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="customerRef"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_noncanonical_customer_order_ref() -> None:
    request = _request()
    instruction = request["params"]["instructions"][0]  # type: ignore[index]
    instruction["customerOrderRef"] = "ABC123"  # type: ignore[index]

    with pytest.raises(
        BetfairStandardLayLimitPriceBoundError,
        match="customerOrderRef",
    ):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


@pytest.mark.parametrize("body", [b"", "not-bytes", None])
def test_rejects_missing_or_nonbytes_request_body(body: object) -> None:
    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="exact bytes"):
        resolve_betfair_standard_lay_limit_price_bound(body)  # type: ignore[arg-type]


def test_rejects_non_utf8_request_body() -> None:
    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="UTF-8"):
        resolve_betfair_standard_lay_limit_price_bound(b"\xff")


def test_rejects_malformed_json() -> None:
    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="valid JSON"):
        resolve_betfair_standard_lay_limit_price_bound(b"{")


def test_rejects_non_object_json_envelope() -> None:
    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="JSON object"):
        resolve_betfair_standard_lay_limit_price_bound(b"[]")


def test_rejects_nonfinite_json_number() -> None:
    body = _body().replace(b'"id":7', b'"id":NaN')

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="non-finite JSON"):
        resolve_betfair_standard_lay_limit_price_bound(body)


def test_rejects_wrong_jsonrpc_method() -> None:
    request = _request()
    request["method"] = "SportsAPING/v1.0/cancelOrders"

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="placeOrders JSON-RPC"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_unknown_jsonrpc_envelope_field() -> None:
    request = _request()
    request["trace"] = True

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="JSON-RPC envelope"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_overlong_customer_order_ref() -> None:
    request = _request()
    instruction = request["params"]["instructions"][0]  # type: ignore[index]
    instruction["customerOrderRef"] = "a" * 33  # type: ignore[index]

    with pytest.raises(
        BetfairStandardLayLimitPriceBoundError,
        match="at most 32",
    ):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_non_decimal_price_text() -> None:
    request = _request()
    request["params"]["instructions"][0]["limitOrder"]["price"] = "abc"  # type: ignore[index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="finite decimal"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_sha256", "bad"),
        ("selection_id", True),
        ("handicap", 1),
        ("customer_ref", "not-hex"),
        ("customer_ref", "0" * 32),
        ("customer_order_ref", "ABC123"),
        ("requested_stake", Decimal("0")),
        ("price_ceiling_odds", Decimal("1000.01")),
        ("persistence_type", "PERSIST"),
        ("provider_id", "other"),
        ("request_shape_proven", False),
        ("matched_fragment_price_ceiling_proven", False),
        ("request_origin_proven", True),
        ("provider_request_validity_proven", True),
        ("acceptance_proven", True),
        ("fill_proven", True),
        ("latency_proven", True),
        ("commission_proven", True),
        ("realized_price_exact", True),
        ("real_money_execution_proven", True),
    ],
)
def test_tampered_evidence_fails_closed(field: str, value: object) -> None:
    evidence = resolve_betfair_standard_lay_limit_price_bound(_body())
    tampered = _tamper_evidence(evidence, field, value)

    with pytest.raises(BetfairStandardLayLimitPriceBoundError):
        tampered.to_dict()


@pytest.mark.parametrize(
    ("field", "value"),
    [("price", 3.2), ("size", 12.5)],
)
def test_rejects_non_text_decimal_projection(field: str, value: object) -> None:
    request = _request()
    limit_order = request["params"]["instructions"][0]["limitOrder"]  # type: ignore[index]
    limit_order[field] = value  # type: ignore[index]

    with pytest.raises(BetfairStandardLayLimitPriceBoundError, match="canonical text"):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))


def test_rejects_valid_hex_customer_ref_with_wrong_canonical_projection() -> None:
    request = _request()
    request["params"]["customerRef"] = "0" * 32  # type: ignore[index]

    with pytest.raises(
        BetfairStandardLayLimitPriceBoundError,
        match="canonical customerOrderRef projection",
    ):
        resolve_betfair_standard_lay_limit_price_bound(_body(request))
