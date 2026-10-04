from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
import json
from threading import Event, Thread
from time import monotonic, sleep
import urllib.request as urllib_request

import pytest

import autosport.betfair_account_readonly as readonly
import autosport.betfair_price_ladder_admission as subject
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_price_ladder_admission import (
    BetfairPriceLadderAdmissionState,
    assess_betfair_price_ladder_admission,
)


class PriceLadderTransport:
    def __init__(
        self,
        ladder_type: str = "CLASSIC",
        *,
        returned_market: str = "1.234",
        line_range: tuple[str, str, str, str] | None = None,
    ) -> None:
        self.ladder_type = ladder_type
        self.returned_market = returned_market
        self.line_range = line_range
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body.decode("utf-8"))
        self.calls.append(request)
        assert request["method"] == "SportsAPING/v1.0/listMarketCatalogue"
        assert request["params"] == {
            "filter": {"marketIds": ["1.234"]},
            "marketProjection": ["MARKET_DESCRIPTION"],
            "maxResults": 1,
        }
        description = (
            '{"priceLadderDescription":{"type":"'
            + self.ladder_type
            + '"}'
        )
        if self.line_range is not None:
            minimum, maximum, interval, unit = self.line_range
            description += (
                ',"lineRangeInfo":{"minUnitValue":'
                + minimum
                + ',"maxUnitValue":'
                + maximum
                + ',"interval":'
                + interval
                + ',"marketUnit":"'
                + unit
                + '"}'
            )
        description += "}"
        return (
            '{"jsonrpc":"2.0","id":'
            + str(request["id"])
            + ',"result":[{"marketId":"'
            + self.returned_market
            + '","description":'
            + description
            + "}]}"
        ).encode("utf-8")


class _BytesResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self, amount: int = -1) -> bytes:
        return self._payload if amount < 0 else self._payload[:amount]


class _CanonicalUrlOpenerHarness:
    def __init__(self, transport: PriceLadderTransport) -> None:
        self._transport = transport

    def open(self, request, data=None, timeout=None):
        header_items = {
            key.lower(): value for key, value in request.header_items()
        }
        payload = self._transport.post(
            request.full_url,
            headers={
                "X-Application": header_items["x-application"],
                "X-Authentication": header_items["x-authentication"],
                "Content-Type": header_items["content-type"],
            },
            body=request.data or b"",
            timeout_seconds=float(timeout),
        )
        return _BytesResponse(payload)


def _canonical_receipt(
    transport: PriceLadderTransport,
    *,
    venue_id: str = "betfair",
):
    original_opener = urllib_request._opener
    try:
        urllib_request._opener = _CanonicalUrlOpenerHarness(transport)
        client = BetfairReadOnlyClient(
            BetfairSessionCredentials("app-key", "session-token"),
            venue_id=venue_id,
            account_id="acct-1",
        )
        receipt = client.read_market_price_ladder("1.234")
    finally:
        urllib_request._opener = original_opener
    return receipt, client


def _canonical_read(
    client: BetfairReadOnlyClient,
    transport: PriceLadderTransport,
    *,
    market_id: str = "1.234",
):
    original_opener = urllib_request._opener
    try:
        urllib_request._opener = _CanonicalUrlOpenerHarness(transport)
        return client.read_market_price_ladder(market_id)
    finally:
        urllib_request._opener = original_opener


def _injected_receipt(transport: PriceLadderTransport):
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        venue_id="betfair",
        account_id="acct-1",
    )
    return client.read_market_price_ladder("1.234")


def _assess(receipt, price: Decimal):
    return assess_betfair_price_ladder_admission(
        receipt,
        price,
        max_evidence_age=timedelta(seconds=5),
    )


def test_admissible_authority_property_rebinding_is_rejected():
    with pytest.raises(
        TypeError,
        match="Betfair price-ladder authority surface is sealed: admissible",
    ):
        subject.BetfairPriceLadderAdmission.admissible = property(
            lambda _result: True
        )

    with pytest.raises(
        TypeError,
        match="Betfair price-ladder authority surface is sealed: admissible",
    ):
        del subject.BetfairPriceLadderAdmission.admissible


def test_market_description_request_is_exact_and_decimal_metadata_is_preserved():
    transport = PriceLadderTransport(
        "LINE_RANGE",
        line_range=("1.5", "10.5", "0.5", "points"),
    )
    receipt = _injected_receipt(transport)

    assert receipt.market_id == "1.234"
    assert receipt.ladder_type == "LINE_RANGE"
    assert receipt.line_range_min == Decimal("1.5")
    assert receipt.line_range_max == Decimal("10.5")
    assert receipt.line_range_interval == Decimal("0.5")
    assert receipt.line_range_unit == "points"
    assert len(receipt.request_scope_sha256) == 64
    assert len(receipt.evidence.source_payload_sha256) == 64
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "price",
    [
        "1.01",
        "2.00",
        "2.02",
        "3.00",
        "3.05",
        "4.00",
        "4.10",
        "6.00",
        "6.20",
        "10.00",
        "10.50",
        "20.00",
        "21.00",
        "30.00",
        "32.00",
        "50.00",
        "55.00",
        "100.00",
        "110.00",
        "1000.00",
    ],
)
def test_classic_exact_grid_and_band_boundaries_are_admissible(price: str):
    receipt, client = _canonical_receipt(PriceLadderTransport("CLASSIC"))
    result = _assess(receipt, Decimal(price))

    assert result.state is BetfairPriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    assert result.admissible is True
    assert result.reasons == ()
    assert result.provider_id == "betfair"
    assert result.market_id == "1.234"
    assert len(result.evidence_digest) == 64


@pytest.mark.parametrize(
    "price",
    ["1.00", "2.01", "3.01", "6.10", "20.50", "31.00", "101.00", "1001.00"],
)
def test_classic_off_grid_or_out_of_bounds_price_is_invalid(price: str):
    receipt, client = _canonical_receipt(PriceLadderTransport("CLASSIC"))
    result = _assess(receipt, Decimal(price))

    assert result.state is BetfairPriceLadderAdmissionState.PRICE_LADDER_INVALID
    assert result.admissible is False
    assert result.reasons == ()


def test_finest_uses_one_cent_grid_not_classic_bands():
    receipt, client = _canonical_receipt(PriceLadderTransport("FINEST"))

    result = _assess(receipt, Decimal("2.01"))
    assert result.state is BetfairPriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    assert result.admissible is True

    off_grid = _assess(receipt, Decimal("2.001"))
    assert off_grid.state is BetfairPriceLadderAdmissionState.PRICE_LADDER_INVALID
    assert off_grid.admissible is False


@pytest.mark.parametrize(
    ("ladder_type", "price", "expected_state"),
    [
        (
            "CLASSIC",
            "1000.00",
            BetfairPriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE,
        ),
        (
            "CLASSIC",
            "999.99",
            BetfairPriceLadderAdmissionState.PRICE_LADDER_INVALID,
        ),
        (
            "FINEST",
            "999.99",
            BetfairPriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE,
        ),
        (
            "FINEST",
            "999.991",
            BetfairPriceLadderAdmissionState.PRICE_LADDER_INVALID,
        ),
    ],
)
def test_tick_admission_is_independent_of_mutable_decimal_context(
    ladder_type,
    price,
    expected_state,
):
    receipt, client = _canonical_receipt(PriceLadderTransport(ladder_type))

    with localcontext() as context:
        context.prec = 1
        result = _assess(receipt, Decimal(price))

    assert result.state is expected_state
    assert result.admissible is (
        expected_state
        is BetfairPriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    )
    assert client is not None


def test_out_of_order_incompatible_definition_reads_fail_closed_until_fresh_read():
    class OutOfOrderPriceLadderTransport(PriceLadderTransport):
        def __init__(self) -> None:
            super().__init__("CLASSIC")
            self.first_started = Event()
            self.release_first = Event()
            self.second_returned = Event()

        def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            body: bytes,
            timeout_seconds: float,
        ) -> bytes:
            request = json.loads(body.decode("utf-8"))
            if request["id"] == 1:
                self.first_started.set()
                if not self.release_first.wait(timeout=5):
                    raise AssertionError("timed out waiting to release first read")
                self.ladder_type = "CLASSIC"
                return super().post(
                    url,
                    headers=headers,
                    body=body,
                    timeout_seconds=timeout_seconds,
                )

            self.ladder_type = "FINEST"
            payload = super().post(
                url,
                headers=headers,
                body=body,
                timeout_seconds=timeout_seconds,
            )
            if request["id"] == 2:
                self.second_returned.set()
            return payload

    transport = OutOfOrderPriceLadderTransport()
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        venue_id="betfair",
        account_id="acct-1",
    )
    receipts: dict[str, object] = {}
    errors: list[BaseException] = []

    def read(name: str) -> None:
        try:
            receipts[name] = client.read_market_price_ladder("1.234")
        except BaseException as exc:
            errors.append(exc)

    original_opener = urllib_request._opener
    first = Thread(target=read, args=("older",))
    second = Thread(target=read, args=("newer",))
    try:
        urllib_request._opener = _CanonicalUrlOpenerHarness(transport)
        first.start()
        assert transport.first_started.wait(timeout=5)
        second.start()
        assert transport.second_returned.wait(timeout=5)
        second.join(timeout=5)
        assert not second.is_alive()
        transport.release_first.set()
        first.join(timeout=5)
        assert not first.is_alive()
    finally:
        transport.release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)
        urllib_request._opener = original_opener

    assert errors == []
    older = receipts["older"]
    newer = receipts["newer"]

    # Incompatible overlapping reads have no provider revision token that can
    # prove which response is current. Network completion order must not choose.
    with pytest.raises(
        BetfairReadOnlyError,
        match="lacks canonical direct Betfair provider IO origin",
    ):
        _assess(older, Decimal("2.00"))
    with pytest.raises(
        BetfairReadOnlyError,
        match="lacks canonical direct Betfair provider IO origin",
    ):
        _assess(newer, Decimal("2.01"))

    fresh = _canonical_read(client, transport)
    fresh_result = _assess(fresh, Decimal("2.01"))
    assert fresh.ladder_type == "FINEST"
    assert fresh_result.admissible is True

    with pytest.raises(
        BetfairReadOnlyError,
        match="lacks canonical direct Betfair provider IO origin",
    ):
        _assess(older, Decimal("2.00"))
    with pytest.raises(
        BetfairReadOnlyError,
        match="lacks canonical direct Betfair provider IO origin",
    ):
        _assess(newer, Decimal("2.01"))


def test_cached_positive_admission_expires_and_reread_cannot_rejuvenate_it():
    transport = PriceLadderTransport("CLASSIC")
    receipt, client = _canonical_receipt(transport)
    result = assess_betfair_price_ladder_admission(
        receipt,
        Decimal("2.00"),
        max_evidence_age=timedelta(milliseconds=500),
    )
    assert result.admissible is True

    deadline = monotonic() + 3.0
    while result.admissible and monotonic() < deadline:
        sleep(0.02)
    assert result.admissible is False

    # A later same-definition receipt may establish fresh authority for a new
    # decision, but it must not rejuvenate the expired cached result.
    fresh_receipt = _canonical_read(client, transport)
    fresh_result = _assess(fresh_receipt, Decimal("2.00"))
    assert fresh_result.admissible is True
    assert result.admissible is False


def test_same_market_definition_reread_preserves_current_generation():
    transport = PriceLadderTransport("CLASSIC")
    first_receipt, client = _canonical_receipt(transport)
    first_result = _assess(first_receipt, Decimal("2.00"))
    assert first_result.admissible is True

    second_receipt = _canonical_read(client, transport)
    second_result = _assess(second_receipt, Decimal("2.00"))

    assert second_result.admissible is True
    assert first_result.admissible is True


def test_incompatible_market_definition_revision_invalidates_old_generation_and_aba():
    transport = PriceLadderTransport("CLASSIC")
    first_receipt, client = _canonical_receipt(transport)
    first_result = _assess(first_receipt, Decimal("2.00"))
    assert first_result.admissible is True

    transport.ladder_type = "FINEST"
    second_receipt = _canonical_read(client, transport)
    second_result = _assess(second_receipt, Decimal("2.01"))
    assert second_result.admissible is True

    assert (
        first_result.state
        is BetfairPriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    )
    assert first_result.admissible is False
    with pytest.raises(BetfairReadOnlyError, match="canonical direct Betfair provider IO origin"):
        _assess(first_receipt, Decimal("2.00"))

    transport.ladder_type = "CLASSIC"
    third_receipt = _canonical_read(client, transport)
    third_result = _assess(third_receipt, Decimal("2.00"))
    assert third_result.admissible is True

    assert first_result.admissible is False
    assert second_result.admissible is False
    with pytest.raises(BetfairReadOnlyError, match="canonical direct Betfair provider IO origin"):
        _assess(first_receipt, Decimal("2.00"))
    with pytest.raises(BetfairReadOnlyError, match="canonical direct Betfair provider IO origin"):
        _assess(second_receipt, Decimal("2.01"))


def test_line_range_metadata_revision_invalidates_prior_receipt():
    transport = PriceLadderTransport(
        "LINE_RANGE",
        line_range=("1.5", "10.5", "0.5", "points"),
    )
    first_receipt, client = _canonical_receipt(transport)
    first_result = _assess(first_receipt, Decimal("2.0"))
    assert (
        first_result.state
        is BetfairPriceLadderAdmissionState.UNSUPPORTED_LADDER_SEMANTICS
    )

    transport.line_range = ("1.5", "10.5", "1.0", "points")
    second_receipt = _canonical_read(client, transport)
    second_result = _assess(second_receipt, Decimal("2.0"))
    assert (
        second_result.state
        is BetfairPriceLadderAdmissionState.UNSUPPORTED_LADDER_SEMANTICS
    )

    with pytest.raises(BetfairReadOnlyError, match="canonical direct Betfair provider IO origin"):
        _assess(first_receipt, Decimal("2.0"))


def test_line_range_is_explicitly_unsupported_even_with_complete_metadata():
    receipt, client = _canonical_receipt(
        PriceLadderTransport(
            "LINE_RANGE",
            line_range=("1.5", "10.5", "0.5", "points"),
        )
    )

    result = _assess(receipt, Decimal("2.0"))

    assert (
        result.state
        is BetfairPriceLadderAdmissionState.UNSUPPORTED_LADDER_SEMANTICS
    )
    assert result.admissible is False
    assert result.reasons == ("LINE_RANGE_EXECUTION_SEMANTICS_UNBOUND",)


def test_line_range_without_metadata_is_explicitly_unsupported():
    receipt, client = _canonical_receipt(PriceLadderTransport("LINE_RANGE"))

    result = _assess(receipt, Decimal("2.0"))

    assert (
        result.state
        is BetfairPriceLadderAdmissionState.UNSUPPORTED_LADDER_SEMANTICS
    )
    assert result.admissible is False
    assert result.reasons == ("LINE_RANGE_EXECUTION_SEMANTICS_UNBOUND",)
    assert client is not None


def test_unknown_future_ladder_type_never_defaults_to_classic():
    receipt, client = _canonical_receipt(
        PriceLadderTransport("FUTURE_PROVIDER_LADDER")
    )

    result = _assess(receipt, Decimal("2.00"))

    assert (
        result.state
        is BetfairPriceLadderAdmissionState.UNSUPPORTED_LADDER_SEMANTICS
    )
    assert result.admissible is False
    assert result.reasons == ("UNSUPPORTED_PRICE_LADDER_TYPE",)


@pytest.mark.parametrize(
    "value",
    [
        2.0,
        True,
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("0"),
        Decimal("-1"),
    ],
)
def test_noncanonical_price_ingress_is_rejected(value):
    receipt, client = _canonical_receipt(PriceLadderTransport("CLASSIC"))

    with pytest.raises(ValueError, match="positive finite exact Decimal"):
        assess_betfair_price_ladder_admission(
            receipt,
            value,
            max_evidence_age=timedelta(seconds=5),
        )


@pytest.mark.parametrize(
    "age",
    [timedelta(0), timedelta(seconds=-1), 5, True],
)
def test_noncanonical_freshness_policy_is_rejected(age):
    receipt, client = _canonical_receipt(PriceLadderTransport("CLASSIC"))

    with pytest.raises(ValueError, match="positive exact timedelta"):
        assess_betfair_price_ladder_admission(
            receipt,
            Decimal("2.00"),
            max_evidence_age=age,
        )


def test_injected_transport_receipt_cannot_mint_provider_authority():
    receipt = _injected_receipt(PriceLadderTransport("CLASSIC"))

    with pytest.raises(
        BetfairReadOnlyError,
        match="lacks canonical direct Betfair provider IO origin",
    ):
        _assess(receipt, Decimal("2.00"))


def test_provider_parser_rebinding_during_io_cannot_mint_ladder_authority(
    monkeypatch: pytest.MonkeyPatch,
):
    original_provider_text = readonly._provider_text

    class RebindingTransport(PriceLadderTransport):
        def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            body: bytes,
            timeout_seconds: float,
        ) -> bytes:
            payload = super().post(
                url,
                headers=headers,
                body=body,
                timeout_seconds=timeout_seconds,
            )

            def forged_provider_text(value, key, field):
                result = original_provider_text(value, key, field)
                if field == "price_ladder_type":
                    return "FINEST"
                return result

            monkeypatch.setattr(
                readonly,
                "_provider_text",
                forged_provider_text,
            )
            return payload

    receipt, _client = _canonical_receipt(
        RebindingTransport("CLASSIC")
    )
    assert receipt.ladder_type == "FINEST"

    with pytest.raises(
        BetfairReadOnlyError,
        match="lacks canonical direct Betfair provider IO origin",
    ):
        _assess(receipt, Decimal("2.01"))


def test_post_issue_hash_helper_rebinding_cannot_preserve_forged_receipt(
    monkeypatch: pytest.MonkeyPatch,
):
    receipt, _client = _canonical_receipt(
        PriceLadderTransport("CLASSIC")
    )
    original_fingerprint = readonly._market_price_ladder_fingerprint(receipt)

    object.__setattr__(receipt, "ladder_type", "FINEST")
    monkeypatch.setattr(
        readonly,
        "_canonical_sha256",
        lambda _value: original_fingerprint,
    )

    with pytest.raises(
        BetfairReadOnlyError,
        match="lacks canonical direct Betfair provider IO origin",
    ):
        _assess(receipt, Decimal("2.01"))


def test_failed_same_market_refresh_revokes_prior_positive_authority():
    class FailingTransport(PriceLadderTransport):
        def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            body: bytes,
            timeout_seconds: float,
        ) -> bytes:
            raise OSError("provider unavailable")

    receipt, client = _canonical_receipt(
        PriceLadderTransport("CLASSIC")
    )
    result = _assess(receipt, Decimal("2.00"))
    assert result.admissible is True

    with pytest.raises(
        BetfairReadOnlyError,
        match="network request failed",
    ):
        _canonical_read(client, FailingTransport("CLASSIC"))

    assert result.admissible is False
    with pytest.raises(
        BetfairReadOnlyError,
        match="lacks canonical direct Betfair provider IO origin",
    ):
        _assess(receipt, Decimal("2.00"))


def test_failed_other_market_refresh_does_not_revoke_unrelated_market():
    class FailingTransport(PriceLadderTransport):
        def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            body: bytes,
            timeout_seconds: float,
        ) -> bytes:
            raise OSError("provider unavailable")

    receipt, client = _canonical_receipt(
        PriceLadderTransport("CLASSIC")
    )
    result = _assess(receipt, Decimal("2.00"))
    assert result.admissible is True

    with pytest.raises(
        BetfairReadOnlyError,
        match="network request failed",
    ):
        _canonical_read(
            client,
            FailingTransport("CLASSIC"),
            market_id="1.999",
        )

    assert result.admissible is True


def test_structurally_copied_receipt_cannot_mint_provider_authority():
    receipt, client = _canonical_receipt(PriceLadderTransport("CLASSIC"))
    copied = replace(receipt)

    with pytest.raises(
        BetfairReadOnlyError,
        match="lacks canonical direct Betfair provider IO origin",
    ):
        _assess(copied, Decimal("2.00"))


def test_canonical_receipt_with_non_betfair_venue_cannot_pass_admission():
    receipt, client = _canonical_receipt(
        PriceLadderTransport("CLASSIC"),
        venue_id="configured-alias",
    )

    result = _assess(receipt, Decimal("2.00"))

    assert result.state is BetfairPriceLadderAdmissionState.UNKNOWN_UNPROVEN
    assert result.admissible is False
    assert result.reasons == ("PROVIDER_ID_MISMATCH",)


def test_market_catalogue_cross_market_substitution_fails_at_acquisition():
    transport = PriceLadderTransport(
        "CLASSIC",
        returned_market="9.999",
    )

    with pytest.raises(
        BetfairReadOnlyError,
        match="returned a different market",
    ):
        _injected_receipt(transport)


def test_caller_constructed_or_copied_result_cannot_mint_positive_authority():
    receipt, client = _canonical_receipt(PriceLadderTransport("CLASSIC"))
    result = _assess(receipt, Decimal("2.00"))
    copied = replace(result)

    assert result.admissible is True
    assert copied.admissible is False

    object.__setattr__(
        result,
        "market_id",
        "forged-market",
    )
    assert result.admissible is False


def test_assessor_dependency_rebinding_cannot_mint_positive_result(
    monkeypatch: pytest.MonkeyPatch,
):
    receipt, client = _canonical_receipt(PriceLadderTransport("CLASSIC"))
    monkeypatch.setattr(subject, "_classic_admissible", lambda price: True)

    with pytest.raises(
        RuntimeError,
        match="canonical Betfair price-ladder assessor changed",
    ):
        _assess(receipt, Decimal("2.01"))


def test_result_digest_binds_price_and_provider_evidence():
    receipt, client = _canonical_receipt(PriceLadderTransport("CLASSIC"))
    first = _assess(receipt, Decimal("2.00"))
    second = _assess(receipt, Decimal("2.02"))

    assert first.evidence_digest != second.evidence_digest
    assert first.source_payload_sha256 == second.source_payload_sha256


def test_numeric_decimal_scale_does_not_change_evidence_identity():
    receipt, client = _canonical_receipt(PriceLadderTransport("CLASSIC"))

    first = _assess(receipt, Decimal("2.0"))
    second = _assess(receipt, Decimal("2.00"))

    assert first.state is BetfairPriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    assert second.state is BetfairPriceLadderAdmissionState.PRICE_LADDER_ADMISSIBLE
    assert first.evidence_digest == second.evidence_digest


def test_extreme_decimal_exponent_fails_mechanically_without_expanded_digest():
    receipt, client = _canonical_receipt(PriceLadderTransport("CLASSIC"))

    result = _assess(receipt, Decimal("1E+100000"))

    assert result.state is BetfairPriceLadderAdmissionState.PRICE_LADDER_INVALID
    assert result.admissible is False
    assert len(result.evidence_digest) == 64


def test_provider_identity_failure_outranks_unsupported_ladder_classification():
    receipt, client = _canonical_receipt(
        PriceLadderTransport("FUTURE_PROVIDER_LADDER"),
        venue_id="configured-alias",
    )

    result = _assess(receipt, Decimal("2.00"))

    assert result.state is BetfairPriceLadderAdmissionState.UNKNOWN_UNPROVEN
    assert result.admissible is False
    assert "PROVIDER_ID_MISMATCH" in result.reasons
    assert "UNSUPPORTED_PRICE_LADDER_TYPE" in result.reasons


def test_missing_price_ladder_description_fails_at_provider_acquisition():
    class MissingPriceLadderDescriptionTransport(PriceLadderTransport):
        def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            body: bytes,
            timeout_seconds: float,
        ) -> bytes:
            request = json.loads(body.decode("utf-8"))
            return (
                '{"jsonrpc":"2.0","id":'
                + str(request["id"])
                + ',"result":[{"marketId":"1.234","description":{}}]}'
            ).encode("utf-8")

    with pytest.raises(
        BetfairReadOnlyError,
        match="priceLadderDescription must be a JSON object",
    ):
        _canonical_receipt(
            MissingPriceLadderDescriptionTransport("CLASSIC")
        )


def test_partial_line_range_metadata_fails_at_provider_acquisition():
    class PartialLineRangeTransport(PriceLadderTransport):
        def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            body: bytes,
            timeout_seconds: float,
        ) -> bytes:
            request = json.loads(body.decode("utf-8"))
            return (
                '{"jsonrpc":"2.0","id":'
                + str(request["id"])
                + ',"result":[{"marketId":"1.234","description":'
                + '{"priceLadderDescription":{"type":"LINE_RANGE"},'
                + '"lineRangeInfo":{"minUnitValue":1.5}}}]}'
            ).encode("utf-8")

    with pytest.raises(BetfairReadOnlyError):
        _injected_receipt(PartialLineRangeTransport("LINE_RANGE"))
