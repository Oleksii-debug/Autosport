from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

import autosport.betfair_account_readonly as betfair_account_readonly
import autosport.betfair_decision_quote_evidence as decision_quote
from autosport.betfair_account_readonly import (
    BETTING_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_decision_quote_evidence import (
    BetfairDecisionQuoteAssessment,
    BetfairDecisionQuoteError,
    read_betfair_decision_quote,
)


NOW = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
MONOTONIC_NS = 10_000_000_000


class FakeTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def post(self, url, *, headers, body, timeout_seconds):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.payload


class FakeNetworkResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, max_bytes: int) -> bytes:
        return self.payload[:max_bytes]


def _market_payload(
    *,
    request_id: int = 1,
    market_id: str = "1.234",
    market_status: object = "OPEN",
    runner_status: object = "ACTIVE",
    delayed: object = False,
    inplay: object = True,
    bet_delay: object = 5,
    version: object = 42,
    selection_id: object = 123,
    handicap: object = 0,
    back_levels=None,
    lay_levels=None,
    duplicate_runner: bool = False,
) -> bytes:
    if back_levels is None:
        back_levels = [
            {"price": 2.2, "size": 2},
            {"price": 2.1, "size": 3},
            {"price": 1.9, "size": 50},
        ]
    if lay_levels is None:
        lay_levels = [
            {"price": 1.9, "size": 2},
            {"price": 2.0, "size": 3},
            {"price": 2.1, "size": 50},
        ]
    runner = {
        "selectionId": selection_id,
        "handicap": handicap,
        "status": runner_status,
        "ex": {
            "availableToBack": back_levels,
            "availableToLay": lay_levels,
        },
    }
    runners = [runner, dict(runner)] if duplicate_runner else [runner]
    row = {
        "marketId": market_id,
        "isMarketDataDelayed": delayed,
        "status": market_status,
        "version": version,
        "inplay": inplay,
        "betDelay": bet_delay,
        "runners": runners,
    }
    return json.dumps(
        {"jsonrpc": "2.0", "result": [row], "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _injected_client(payload: bytes):
    transport = FakeTransport(payload)
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: NOW,
        venue_id="betfair-global",
        account_id="configured-account",
    )
    return client, transport


def _read(client, *, side="BACK", price="2.0", size="4", depth=3):
    return read_betfair_decision_quote(
        client,
        market_id="1.234",
        selection_id=123,
        handicap=Decimal("0"),
        side=side,
        decision_price=Decimal(price),
        requested_size=Decimal(size),
        best_prices_depth=depth,
    )


def _network_capture(
    monkeypatch,
    *,
    market_payload: bytes | None = None,
    app_delay_data: object = False,
    app_active: object = True,
    side: str = "BACK",
    price: str = "2.0",
    size: str = "4",
    depth: int = 3,
    client_sink=None,
):
    market_payload = market_payload or _market_payload()
    calls = []

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        calls.append((request.full_url, body))
        request_id = body["id"]
        if body["method"] == "SportsAPING/v1.0/listMarketBook":
            decoded = json.loads(market_payload.decode("utf-8"))
            decoded["id"] = request_id
            payload = decoded
        elif body["method"] == "AccountAPING/v1.0/getDeveloperAppKeys":
            payload = {
                "jsonrpc": "2.0",
                "result": [
                    {
                        "appName": "autosport-test",
                        "appId": 101,
                        "appVersions": [
                            {
                                "owner": "synthetic-owner",
                                "versionId": 202,
                                "version": "synthetic-1",
                                "applicationKey": "app-secret",
                                "delayData": app_delay_data,
                                "subscriptionRequired": False,
                                "ownerManaged": False,
                                "active": app_active,
                            }
                        ],
                    }
                ],
                "id": request_id,
            }
        else:
            raise AssertionError(f"unexpected method {body['method']}")
        return FakeNetworkResponse(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )

    monkeypatch.setattr(betfair_account_readonly, "urlopen", fake_urlopen)
    monkeypatch.setattr(decision_quote, "monotonic_ns", lambda: MONOTONIC_NS)
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        venue_id="betfair-global",
        account_id="configured-account",
    )
    if client_sink is not None:
        client_sink.append(client)
    evidence = _read(
        client,
        side=side,
        price=price,
        size=size,
        depth=depth,
    )
    return evidence, calls


def _assess(evidence, *, seconds=1, max_age="5"):
    observed = datetime.fromisoformat(evidence.observed_at)
    return evidence.assess(
        now_utc=observed + timedelta(seconds=seconds),
        now_monotonic_ns=evidence.received_monotonic_ns + seconds * 1_000_000_000,
        max_age_seconds=Decimal(max_age),
    )


def test_injected_transport_parses_exact_depth_but_cannot_mint_decision_authority(
    monkeypatch,
):
    monkeypatch.setattr(decision_quote, "monotonic_ns", lambda: MONOTONIC_NS)
    client, transport = _injected_client(_market_payload())

    evidence = _read(client)

    assert evidence.executable_size_at_or_better == Decimal("5")
    assert evidence.market_version == 42
    assert evidence.inplay is True
    assert evidence.bet_delay_seconds == 5
    assert evidence.stream_continuity_proven is False
    assert evidence.market_version_price_lock_proven is False
    assert evidence.execution_fill_proven is False
    with pytest.raises(
        BetfairDecisionQuoteError,
        match="lacks production network origin",
    ):
        evidence.assert_provider_authoritative()

    call = transport.calls[0]
    assert call["url"] == BETTING_JSON_RPC_ENDPOINT
    request = json.loads(call["body"])
    assert request["method"] == "SportsAPING/v1.0/listMarketBook"
    assert request["params"] == {
        "marketIds": ["1.234"],
        "priceProjection": {
            "priceData": ["EX_BEST_OFFERS"],
            "exBestOffersOverrides": {"bestPricesDepth": 3},
        },
    }
    assert b"app-secret" not in call["body"]
    assert b"session-secret" not in call["body"]


def test_canonical_network_live_app_snapshot_is_quantity_aware_and_authoritative(
    monkeypatch,
):
    evidence, calls = _network_capture(monkeypatch)

    evidence.assert_provider_authoritative()
    assessment = _assess(evidence)
    assessment.assert_authoritative()

    assert assessment.decision_eligible is True
    assert assessment.reasons == ()
    assert assessment.executable_size_at_or_better == Decimal("5")
    assert assessment.requested_size == Decimal("4")
    assert assessment.execution_delay_present is True
    assert assessment.market_version_material_change_guard_available is True
    assert assessment.market_version_price_lock_proven is False
    assert assessment.execution_fill_proven is False
    assert assessment.realized_price_proven is False
    assert assessment.real_money_authorized is False
    assert len(assessment.assessment_id) == 64
    assert len(evidence.evidence_id) == 64
    assert [body["method"] for _, body in calls] == [
        "SportsAPING/v1.0/listMarketBook",
        "AccountAPING/v1.0/getDeveloperAppKeys",
    ]


def test_positive_assessment_revalidates_provider_origin_at_use_time(monkeypatch):
    clients = []
    evidence, _ = _network_capture(monkeypatch, client_sink=clients)
    assessment = _assess(evidence)
    assessment.assert_authoritative()

    monkeypatch.setattr(
        clients[0],
        "_credentials",
        BetfairSessionCredentials("rotated-app-secret", "rotated-session-secret"),
    )
    with pytest.raises(BetfairDecisionQuoteError, match="current live App Key authority"):
        assessment.assert_authoritative()

    negative_clients = []
    negative_evidence, _ = _network_capture(
        monkeypatch,
        market_payload=_market_payload(delayed=True),
        client_sink=negative_clients,
    )
    negative_assessment = _assess(negative_evidence)
    assert negative_assessment.decision_eligible is False
    monkeypatch.setattr(
        negative_clients[0],
        "_credentials",
        BetfairSessionCredentials("rotated-app-secret", "rotated-session-secret"),
    )
    negative_assessment.assert_authoritative()


def test_direct_or_copied_values_cannot_mint_positive_authority(monkeypatch):
    evidence, _ = _network_capture(monkeypatch)
    assessment = _assess(evidence)

    copied_evidence = replace(evidence)
    with pytest.raises(BetfairDecisionQuoteError, match="not product-issued"):
        copied_evidence.assert_provider_authoritative()

    copied_assessment = replace(assessment)
    with pytest.raises(BetfairDecisionQuoteError, match="not product-issued"):
        copied_assessment.assert_authoritative()

    forged = BetfairDecisionQuoteAssessment(
        evidence_id=evidence.evidence_id,
        decision_eligible=True,
        reasons=(),
        executable_size_at_or_better=Decimal("999"),
        requested_size=Decimal("1"),
        monotonic_age_seconds=Decimal("0"),
        utc_age_seconds=Decimal("0"),
        assessed_at=evidence.observed_at,
        assessed_monotonic_ns=evidence.received_monotonic_ns,
        max_age_seconds=Decimal("5"),
        execution_delay_present=False,
        market_version_material_change_guard_available=True,
    )
    with pytest.raises(BetfairDecisionQuoteError, match="not product-issued"):
        forged.assert_authoritative()


def test_delayed_market_data_is_retained_but_not_decision_eligible(monkeypatch):
    evidence, _ = _network_capture(
        monkeypatch,
        market_payload=_market_payload(delayed=True),
    )

    assessment = _assess(evidence)

    assert assessment.decision_eligible is False
    assert "MARKET_DATA_DELAYED" in assessment.reasons
    assessment.assert_authoritative()


def test_delayed_application_key_cannot_mint_live_decision_authority(monkeypatch):
    evidence, _ = _network_capture(monkeypatch, app_delay_data=True)

    with pytest.raises(BetfairDecisionQuoteError, match="live App Key authority"):
        _assess(evidence)


def test_non_open_market_and_non_active_runner_fail_closed(monkeypatch):
    evidence, _ = _network_capture(
        monkeypatch,
        market_payload=_market_payload(
            market_status="SUSPENDED",
            runner_status="REMOVED",
        ),
    )

    assessment = _assess(evidence)

    assert assessment.decision_eligible is False
    assert "MARKET_NOT_OPEN" in assessment.reasons
    assert "RUNNER_NOT_ACTIVE" in assessment.reasons


def test_insufficient_displayed_depth_is_not_executable_quote_evidence(monkeypatch):
    evidence, _ = _network_capture(
        monkeypatch,
        market_payload=_market_payload(
            back_levels=[
                {"price": 2.2, "size": 1},
                {"price": 2.1, "size": 1},
                {"price": 1.9, "size": 100},
            ]
        ),
        size="3",
    )

    assessment = _assess(evidence)

    assert evidence.executable_size_at_or_better == Decimal("2")
    assert assessment.decision_eligible is False
    assert "INSUFFICIENT_DISPLAYED_DEPTH" in assessment.reasons


def test_back_and_lay_use_side_correct_at_or_better_depth(monkeypatch):
    back, _ = _network_capture(monkeypatch, side="BACK", price="2.0", size="5")
    assert back.executable_size_at_or_better == Decimal("5")

    lay, _ = _network_capture(monkeypatch, side="LAY", price="2.0", size="5")
    assert lay.executable_size_at_or_better == Decimal("5")


def test_stale_and_future_process_clock_fail_closed(monkeypatch):
    evidence, _ = _network_capture(monkeypatch)

    stale = _assess(evidence, seconds=6, max_age="5")
    assert stale.decision_eligible is False
    assert "MONOTONIC_STALE" in stale.reasons
    assert "UTC_STALE" in stale.reasons

    observed = datetime.fromisoformat(evidence.observed_at)
    future = evidence.assess(
        now_utc=observed - timedelta(seconds=1),
        now_monotonic_ns=evidence.received_monotonic_ns - 1_000_000_000,
        max_age_seconds=Decimal("5"),
    )
    assert future.decision_eligible is False
    assert "MONOTONIC_CLOCK_PRECEDES_CAPTURE" in future.reasons
    assert "UTC_CLOCK_PRECEDES_CAPTURE" in future.reasons


def test_caller_clock_cannot_redefine_product_assessment_time(monkeypatch):
    evidence, _ = _network_capture(monkeypatch)
    observed = datetime.fromisoformat(evidence.observed_at)
    caller_utc = observed + timedelta(seconds=1)
    caller_monotonic_ns = evidence.received_monotonic_ns + 1_000_000_000

    assessment = evidence.assess(
        now_utc=caller_utc,
        now_monotonic_ns=caller_monotonic_ns,
        max_age_seconds=Decimal("5"),
    )

    assessment.assert_authoritative()
    assert assessment.decision_eligible is True
    assert assessment.assessed_at != caller_utc.astimezone(timezone.utc).isoformat()
    assert assessment.assessed_monotonic_ns != caller_monotonic_ns
    assert assessment.utc_age_seconds >= Decimal("1")
    assert assessment.monotonic_age_seconds >= Decimal("1")


def test_caller_cannot_widen_product_freshness_ceiling(monkeypatch):
    evidence, _ = _network_capture(monkeypatch)
    observed = datetime.fromisoformat(evidence.observed_at)

    with pytest.raises(
        BetfairDecisionQuoteError,
        match="max_age_seconds exceeds product freshness ceiling",
    ):
        evidence.assess(
            now_utc=observed,
            now_monotonic_ns=evidence.received_monotonic_ns,
            max_age_seconds=Decimal("86400"),
        )


def test_bet_delay_is_preserved_as_execution_risk_not_data_staleness(monkeypatch):
    evidence, _ = _network_capture(
        monkeypatch,
        market_payload=_market_payload(bet_delay=8),
    )

    assessment = _assess(evidence)

    assert assessment.decision_eligible is True
    assert assessment.execution_delay_present is True
    assert evidence.bet_delay_seconds == 8


def test_exact_selection_and_handicap_must_resolve_once(monkeypatch):
    monkeypatch.setattr(decision_quote, "monotonic_ns", lambda: MONOTONIC_NS)
    duplicate_client, _ = _injected_client(_market_payload(duplicate_runner=True))

    with pytest.raises(BetfairDecisionQuoteError, match="ambiguous"):
        _read(duplicate_client)

    missing_client, _ = _injected_client(_market_payload(selection_id=999))
    with pytest.raises(BetfairDecisionQuoteError, match="ambiguous"):
        _read(missing_client)


def test_provider_exact_json_types_and_requested_depth_are_fail_closed(monkeypatch):
    monkeypatch.setattr(decision_quote, "monotonic_ns", lambda: MONOTONIC_NS)

    for kwargs in (
        {"selection_id": True},
        {"version": True},
        {"bet_delay": True},
        {"inplay": 1},
        {"delayed": 0},
    ):
        client, _ = _injected_client(_market_payload(**kwargs))
        with pytest.raises(
            (
                BetfairDecisionQuoteError,
                betfair_account_readonly.BetfairReadOnlyError,
            )
        ):
            _read(client)

    too_deep = _market_payload(
        back_levels=[
            {"price": 2.3, "size": 1},
            {"price": 2.2, "size": 1},
            {"price": 2.1, "size": 1},
        ]
    )
    client, _ = _injected_client(too_deep)
    with pytest.raises(BetfairDecisionQuoteError, match="more price levels"):
        _read(client, depth=2)


def test_duplicate_json_key_and_nonfinite_price_are_rejected(monkeypatch):
    monkeypatch.setattr(decision_quote, "monotonic_ns", lambda: MONOTONIC_NS)
    payload = _market_payload()
    duplicate = payload.replace(
        b'"marketId":"1.234"',
        b'"marketId":"1.234","marketId":"9.999"',
        1,
    )
    client, _ = _injected_client(duplicate)
    with pytest.raises(
        betfair_account_readonly.BetfairReadOnlyError,
        match="duplicate object key",
    ):
        _read(client)

    invalid = payload.replace(b'"price":2.2', b'"price":NaN', 1)
    client, _ = _injected_client(invalid)
    with pytest.raises(
        betfair_account_readonly.BetfairReadOnlyError,
        match="non-standard numeric",
    ):
        _read(client)


def test_secrets_are_absent_from_evidence_and_contract_claims_remain_narrow(monkeypatch):
    evidence, _ = _network_capture(monkeypatch)

    rendered = repr(evidence)
    assert "app-secret" not in rendered
    assert "session-secret" not in rendered
    assert evidence.provider_publish_time is None
    assert evidence.stream_continuity_proven is False
    assert evidence.market_version_price_lock_proven is False
    assert evidence.execution_fill_proven is False
    assert evidence.realized_price_proven is False
    assert evidence.real_money_authorized is False



def test_positive_assessment_use_time_freshness_is_capture_bounded():
    observed = NOW.isoformat()
    received_ns = MONOTONIC_NS
    max_age = Decimal("5")

    assert decision_quote._positive_assessment_current(
        observed_at=observed,
        received_monotonic_ns=received_ns,
        max_age_seconds=max_age,
        now_utc=NOW + timedelta(seconds=5),
        now_monotonic_ns=received_ns + 5_000_000_000,
    )

    assert not decision_quote._positive_assessment_current(
        observed_at=observed,
        received_monotonic_ns=received_ns,
        max_age_seconds=max_age,
        now_utc=NOW + timedelta(seconds=6),
        now_monotonic_ns=received_ns + 6_000_000_000,
    )

    assert not decision_quote._positive_assessment_current(
        observed_at=observed,
        received_monotonic_ns=received_ns,
        max_age_seconds=max_age,
        now_utc=NOW - timedelta(seconds=1),
        now_monotonic_ns=received_ns - 1,
    )
