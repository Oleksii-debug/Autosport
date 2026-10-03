from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import urllib.request as _urllib_request

import pytest

from autosport.betfair_account_identity import (
    build_betfair_authenticated_client,
    resolve_betfair_authenticated_account_identity,
)
from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BETTING_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_price_ladder_admission import (
    BetfairPriceLadderAdmission,
    BetfairPriceLadderAuthority,
    BetfairPriceLadderError,
    PriceLadderAdmissionState,
)


FIXED_NOW = datetime(2026, 9, 22, 8, 46, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    class Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, traceback):
            return False
        def read(self, limit: int) -> bytes:
            assert limit >= len(self._payload)
            return self._payload

    def open(self, request, data=None, timeout: float = 0):
        assert data is None
        assert request.data is not None
        decoded = json.loads(request.data.decode("utf-8"))
        if request.full_url == ACCOUNT_JSON_RPC_ENDPOINT:
            assert decoded["method"] == "AccountAPING/v1.0/getAccountDetails"
            raw = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": decoded["id"],
                    "result": {
                        "currencyCode": "EUR",
                        "localeCode": "en",
                        "region": "GBR",
                        "timezone": "Europe/London",
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8")
            return self.Response(raw)
        assert request.full_url == BETTING_JSON_RPC_ENDPOINT
        if not self.responses:
            raise AssertionError("unexpected Betfair market transport call")
        payload = json.loads(self.responses.pop(0).decode("utf-8"))
        payload["id"] = decoded["id"]
        self.calls.append(
            {
                "url": request.full_url,
                "headers": {key.lower(): value for key, value in request.header_items()},
                "body": request.data,
                "timeout_seconds": timeout,
            }
        )
        return self.Response(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
def response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def market_row(
    ladder_type: str | None,
    *,
    market_id: str = "1.234",
    line_range: dict[str, object] | None = None,
) -> dict[str, object]:
    description: dict[str, object] = {}
    if ladder_type is not None:
        description["priceLadderDescription"] = {"type": ladder_type}
    if line_range is not None:
        description["lineRangeInfo"] = line_range
    return {"marketId": market_id, "description": description}


def authority_for(monkeypatch, *responses: bytes):
    transport = FakeTransport(list(responses))
    monkeypatch.setattr(_urllib_request, "_opener", transport)
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-secret", "session-secret")
    )
    identity = resolve_betfair_authenticated_account_identity(client)
    return BetfairPriceLadderAuthority(client, identity), client, transport


def acquire_for(monkeypatch, ladder_type: str | None):
    authority, client, transport = authority_for(monkeypatch, 
        monkeypatch,
        response([market_row(ladder_type)], 1),
    )
    observation = authority.acquire("1.234")
    return authority, observation, client, transport

def test_acquisition_reuses_canonical_readonly_client_and_binds_exact_request(monkeypatch):
    authority, _, transport = authority_for(monkeypatch, 
        response([market_row("CLASSIC")], 1)
    )

    observation = authority.acquire("1.234")

    assert observation.market_id == "1.234"
    assert observation.ladder_type == "CLASSIC"
    observation.assert_authoritative()
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == BETTING_JSON_RPC_ENDPOINT
    request = json.loads(call["body"])
    assert request["method"] == "SportsAPING/v1.0/listMarketCatalogue"
    assert request["params"] == {
        "filter": {"marketIds": ["1.234"]},
        "marketProjection": ["MARKET_DESCRIPTION"],
        "maxResults": 1,
    }
    assert "app-secret" not in repr(observation)
    assert "session-secret" not in repr(observation)


@pytest.mark.parametrize(
    "price",
    [
        "1.01",
        "2.00",
        "3.00",
        "4.00",
        "6.00",
        "10.00",
        "20.00",
        "30.00",
        "50.00",
        "100.00",
        "1000.00",
        "2.98",
        "3.95",
        "9.80",
        "19.50",
        "48.00",
        "95.00",
        "990.00",
    ],
)
def test_classic_exact_valid_ticks_cover_all_band_boundaries(
    price: str,
    monkeypatch,
):
    authority, observation, _, _ = acquire_for(monkeypatch, "CLASSIC")

    result = authority.resolve(
        observation=observation,
        market_id="1.234",
        price=price,
        selection_id=17,
    )

    assert (
        result.state
        is PriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    )
    assert result.execution_authorized is False
    result.assert_authoritative()


@pytest.mark.parametrize(
    "price",
    [
        "1.001",
        "2.01",
        "3.01",
        "4.05",
        "6.10",
        "10.10",
        "20.50",
        "31.00",
        "52.00",
        "101.00",
        "1000.01",
    ],
)
def test_classic_off_grid_prices_fail_without_float_tolerance(
    price: str,
    monkeypatch,
):
    authority, observation, _, _ = acquire_for(monkeypatch, "CLASSIC")

    result = authority.resolve(
        observation=observation,
        market_id="1.234",
        price=Decimal(price),
    )

    assert result.state is PriceLadderAdmissionState.PRICE_LADDER_INVALID
    result.assert_authoritative()


@pytest.mark.parametrize(
    "price",
    ["1.01", "2.01", "6.10", "999.99", "1000.00"],
)
def test_finest_uses_one_cent_grid_instead_of_classic(
    price: str,
    monkeypatch,
):
    authority, observation, _, _ = acquire_for(monkeypatch, "FINEST")

    result = authority.resolve(
        observation=observation,
        market_id="1.234",
        price=price,
    )

    assert (
        result.state
        is PriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    )
    result.assert_authoritative()


@pytest.mark.parametrize(
    "price",
    ["1.001", "1.00", "1000.001", "1000.01"],
)
def test_finest_rejects_out_of_grid_or_bounds(price: str, monkeypatch):
    authority, observation, _, _ = acquire_for(monkeypatch, "FINEST")

    result = authority.resolve(
        observation=observation,
        market_id="1.234",
        price=price,
    )

    assert result.state is PriceLadderAdmissionState.PRICE_LADDER_INVALID


def test_missing_price_ladder_description_remains_unknown_not_classic(monkeypatch):
    authority, observation, _, _ = acquire_for(monkeypatch, None)

    result = authority.resolve(
        observation=observation,
        market_id="1.234",
        price="2.00",
    )

    assert observation.ladder_type is None
    assert result.state is PriceLadderAdmissionState.UNKNOWN_UNPROVEN
    result.assert_authoritative()


def test_unknown_future_ladder_is_preserved_but_not_guessed(monkeypatch):
    authority, observation, _, _ = acquire_for(monkeypatch, 
        "FUTURE_PROVIDER_LADDER"
    )

    result = authority.resolve(
        observation=observation,
        market_id="1.234",
        price="2.00",
    )

    assert observation.ladder_type == "FUTURE_PROVIDER_LADDER"
    assert (
        result.state
        is PriceLadderAdmissionState.UNSUPPORTED_LADDER_SEMANTICS
    )
    result.assert_authoritative()


def test_line_range_metadata_is_preserved_but_current_action_semantics_fail_closed(monkeypatch):
    authority, _, _ = authority_for(monkeypatch, 
        response(
            [
                market_row(
                    "LINE_RANGE",
                    line_range={
                        "minUnitValue": -5.0,
                        "maxUnitValue": 5.0,
                        "interval": 0.5,
                        "marketUnit": "Goals",
                    },
                )
            ],
            1,
        )
    )

    observation = authority.acquire("1.234")
    assert observation.line_range is not None
    assert observation.line_range.min_unit_value == Decimal("-5.0")
    assert observation.line_range.max_unit_value == Decimal("5.0")
    assert observation.line_range.interval == Decimal("0.5")
    assert observation.line_range.market_unit == "Goals"

    result = authority.resolve(
        observation=observation,
        market_id="1.234",
        price="0.5",
        selection_id=17,
        handicap="0.5",
    )

    assert (
        result.state
        is PriceLadderAdmissionState.UNSUPPORTED_LADDER_SEMANTICS
    )
    assert result.execution_authorized is False
    result.assert_authoritative()


@pytest.mark.parametrize(
    "price",
    [2.0, 2, True, float("nan"), float("inf")],
)
def test_binary_float_bool_and_non_decimal_numeric_price_ingress_fails(
    price,
    monkeypatch,
):
    authority, observation, _, _ = acquire_for(monkeypatch, "CLASSIC")

    with pytest.raises(
        BetfairPriceLadderError,
        match="Decimal or exact decimal text",
    ):
        authority.resolve(
            observation=observation,
            market_id="1.234",
            price=price,
        )


@pytest.mark.parametrize(
    "price",
    ["NaN", "Infinity", "-Infinity"],
)
def test_nonfinite_decimal_text_fails_before_admission(price: str, monkeypatch):
    authority, observation, _, _ = acquire_for(monkeypatch, "CLASSIC")

    with pytest.raises(
        BetfairPriceLadderError,
        match="finite Decimal",
    ):
        authority.resolve(
            observation=observation,
            market_id="1.234",
            price=price,
        )


def test_market_a_evidence_cannot_be_reused_for_market_b(monkeypatch):
    authority, observation, _, _ = acquire_for(monkeypatch, "CLASSIC")

    with pytest.raises(
        BetfairPriceLadderError,
        match="different market",
    ):
        authority.resolve(
            observation=observation,
            market_id="1.999",
            price="2.00",
        )


def test_caller_copy_cannot_mint_market_definition_or_admission_authority(monkeypatch):
    authority, observation, _, _ = acquire_for(monkeypatch, "CLASSIC")
    copied_observation = replace(observation)

    with pytest.raises(BetfairPriceLadderError, match="not issued"):
        copied_observation.assert_authoritative()
    with pytest.raises(BetfairPriceLadderError, match="not issued"):
        authority.resolve(
            observation=copied_observation,
            market_id="1.234",
            price="2.00",
        )

    result = authority.resolve(
        observation=observation,
        market_id="1.234",
        price="2.00",
    )
    copied_result = replace(result)
    with pytest.raises(BetfairPriceLadderError, match="not issued"):
        copied_result.assert_authoritative()


def test_later_market_definition_supersedes_old_positive_admission(monkeypatch):
    authority, _, _ = authority_for(monkeypatch, 
        response([market_row("CLASSIC")], 1),
        response([market_row("FINEST")], 2),
    )
    first = authority.acquire("1.234")
    old_result = authority.resolve(
        observation=first,
        market_id="1.234",
        price="2.00",
    )
    old_result.assert_authoritative()

    second = authority.acquire("1.234")

    with pytest.raises(
        BetfairPriceLadderError,
        match="current acquisition",
    ):
        authority.resolve(
            observation=first,
            market_id="1.234",
            price="2.00",
        )
    with pytest.raises(BetfairPriceLadderError, match="superseded"):
        old_result.assert_authoritative()

    new_result = authority.resolve(
        observation=second,
        market_id="1.234",
        price="2.01",
    )
    assert (
        new_result.state
        is PriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    )
    new_result.assert_authoritative()


def test_same_price_classic_vs_finest_is_bound_to_provider_acquired_ladder(monkeypatch):
    classic_authority, classic, _, _ = acquire_for(monkeypatch, "CLASSIC")
    finest_authority, finest, _, _ = acquire_for(monkeypatch, "FINEST")

    classic_result = classic_authority.resolve(
        observation=classic,
        market_id="1.234",
        price="2.01",
    )
    finest_result = finest_authority.resolve(
        observation=finest,
        market_id="1.234",
        price="2.01",
    )

    assert (
        classic_result.state
        is PriceLadderAdmissionState.PRICE_LADDER_INVALID
    )
    assert (
        finest_result.state
        is PriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    )
    assert (
        classic_result.admission_sha256
        != finest_result.admission_sha256
    )


def test_exact_selection_and_handicap_are_bound_into_result_identity(monkeypatch):
    authority, observation, _, _ = acquire_for(monkeypatch, "CLASSIC")

    first = authority.resolve(
        observation=observation,
        market_id="1.234",
        price="2.00",
        selection_id=17,
        handicap="0",
    )
    second = authority.resolve(
        observation=observation,
        market_id="1.234",
        price="2.00",
        selection_id=18,
        handicap="0",
    )

    assert first.admission_sha256 != second.admission_sha256
    first.assert_authoritative()
    second.assert_authoritative()


def test_instance_rpc_shadow_cannot_replace_canonical_readonly_acquisition(monkeypatch):
    authority, client, transport = authority_for(monkeypatch, 
        response([market_row("CLASSIC")], 1)
    )

    client._rpc = lambda *_args, **_kwargs: (
        _ for _ in ()
    ).throw(
        AssertionError("instance shadow must not execute")
    )
    observation = authority.acquire("1.234")

    assert observation.ladder_type == "CLASSIC"
    assert len(transport.calls) == 1


def test_client_subclass_is_not_accepted_as_provider_authority(monkeypatch):
    class FakeClient(BetfairReadOnlyClient):
        pass

    client = FakeClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=FakeTransport([]),
        clock=lambda: FIXED_NOW,
    )

    with pytest.raises(
        BetfairPriceLadderError,
        match="exact canonical",
    ):
        BetfairPriceLadderAuthority(client, object())


@pytest.mark.parametrize(
    "result, message",
    [
        ([], "exact market definition"),
        (
            [
                market_row("CLASSIC"),
                market_row("CLASSIC"),
            ],
            "exact market definition",
        ),
        (
            [market_row("CLASSIC", market_id="1.999")],
            "different market",
        ),
        (
            [market_row("classic")],
            "uppercase ASCII",
        ),
    ],
)
def test_malformed_or_ambiguous_market_definition_fails_closed(
    result: object,
    message: str,
    monkeypatch,
):
    authority, _, _ = authority_for(monkeypatch, response(result, 1))

    with pytest.raises(BetfairPriceLadderError, match=message):
        authority.acquire("1.234")


def test_malformed_line_range_metadata_fails_closed_instead_of_partial_use(monkeypatch):
    authority, _, _ = authority_for(monkeypatch, 
        response(
            [
                market_row(
                    "LINE_RANGE",
                    line_range={
                        "minUnitValue": -5.0,
                        "maxUnitValue": 5.0,
                        "marketUnit": "Goals",
                    },
                )
            ],
            1,
        )
    )

    with pytest.raises(
        BetfairPriceLadderError,
        match="interval is missing",
    ):
        authority.acquire("1.234")


def test_post_issue_mutation_is_detected_even_with_frozen_dataclass_bypass(monkeypatch):
    authority, observation, _, _ = acquire_for(monkeypatch, "CLASSIC")
    result = authority.resolve(
        observation=observation,
        market_id="1.234",
        price="2.00",
    )

    object.__setattr__(
        result,
        "price",
        Decimal("3.00"),
    )

    with pytest.raises(
        BetfairPriceLadderError,
        match="digest mismatch",
    ):
        result.assert_authoritative()


def test_admission_direct_construction_never_grants_execution_authority(monkeypatch):
    authority, observation, _, _ = acquire_for(monkeypatch, "CLASSIC")
    issued = authority.resolve(
        observation=observation,
        market_id="1.234",
        price="2.00",
    )

    forged = BetfairPriceLadderAdmission(
        state=issued.state,
        market_id=issued.market_id,
        ladder_type=issued.ladder_type,
        price=issued.price,
        observation_sha256=issued.observation_sha256,
        admission_sha256=issued.admission_sha256,
    )
    assert forged.execution_authorized is False
    with pytest.raises(BetfairPriceLadderError, match="not issued"):
        forged.assert_authoritative()


def test_caller_injected_transport_cannot_borrow_product_origin(monkeypatch):
    authority, observation, _, _ = acquire_for(monkeypatch, "CLASSIC")
    identity = authority._account_identity

    class InjectedTransport:
        def post(self, url, *, headers, body, timeout_seconds):
            return response([market_row("CLASSIC")], 1)

    direct_client = BetfairReadOnlyClient(
        BetfairSessionCredentials("forged-app", "forged-session"),
        transport=InjectedTransport(),
        clock=lambda: FIXED_NOW,
    )
    with pytest.raises(
        BetfairPriceLadderError,
        match="authenticated origin",
    ):
        BetfairPriceLadderAuthority(direct_client, identity)

    observation.assert_authoritative()


def test_post_issue_k07_context_mutation_revokes_observation_and_admission(monkeypatch):
    authority, observation, client, _ = acquire_for(monkeypatch, "CLASSIC")
    admission = authority.resolve(
        observation=observation,
        market_id="1.234",
        price="2.00",
    )
    observation.assert_authoritative()
    admission.assert_authoritative()

    client._credentials = BetfairSessionCredentials("rotated-app", "rotated-session")

    with pytest.raises(
        BetfairPriceLadderError,
        match="authenticated origin is no longer authoritative",
    ):
        observation.assert_authoritative()
    with pytest.raises(
        BetfairPriceLadderError,
        match="authenticated origin is no longer authoritative",
    ):
        admission.assert_authoritative()
