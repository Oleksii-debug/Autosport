from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport import betfair_stream_transport as stream
from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.betfair_authenticated_stream import (
    BetfairAuthenticatedMarketDefinitionEvidence,
    BetfairAuthenticatedStreamError,
    BetfairAuthenticatedStreamFreshnessRuntime,
    open_authenticated_market_subscription,
)
from autosport.betfair_stream_codec import BETFAIR_STREAM_SOURCE_ID
from autosport.domain import TicketLeg
from autosport.market_outcomes import (
    OutcomeAuthorityStatus,
    OutcomeRosterBasis,
    SettlementSemantics,
    assess_betfair_authenticated_market_definition_authority,
)
from autosport.paper import PaperBook
from autosport.scenario_search import ScenarioSearchEngine


class FakeSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False

    def recv(self, size: int) -> bytes:
        if not self.chunks:
            return b""
        head = self.chunks.pop(0)
        if len(head) <= size:
            return head
        self.chunks.insert(0, head[size:])
        return head[:size]

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def settimeout(self, _value: float) -> None:
        pass

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class Secrets:
    def get_session_lease(self) -> stream.BetfairStreamCredentialLease:
        return stream.BetfairStreamCredentialLease(
            account_id="acct-1",
            app_identity_id="betfair-app-1",
            app_key_class="LIVE",
            session_epoch=1,
            credentials=BetfairSessionCredentials(
                "TEST_APP_KEY_NOT_A_REAL_CREDENTIAL",
                "Bearer TEST_SESSION_NOT_A_REAL_CREDENTIAL",
            ),
        )


def _connection() -> bytes:
    return b'{"op":"connection","connectionId":"conn-1"}\r\n'


def _auth_status() -> bytes:
    return (
        b'{"op":"status","id":1,"statusCode":"SUCCESS",'
        b'"connectionClosed":false}\r\n'
    )


def _subscription_status() -> bytes:
    return (
        b'{"op":"status","id":7,"statusCode":"SUCCESS",'
        b'"connectionClosed":false}\r\n'
    )


def _market_definition(*, complete: bool = True) -> dict[str, object]:
    return {
        "eventId": "event-1",
        "eventTypeId": "2593174",
        "marketType": "MATCH_ODDS",
        "status": "OPEN",
        "complete": complete,
        "runners": [
            {"id": 101, "status": "ACTIVE"},
            {"id": 202, "status": "ACTIVE"},
        ],
    }


def _mcm(
    *,
    market_definition: dict[str, object] | None = None,
    market_id: str = "1.234",
) -> bytes:
    payload = {
        "op": "mcm",
        "id": 7,
        "ct": "SUB_IMAGE",
        "initialClk": "i1",
        "clk": "c1",
        "pt": time.time_ns() // 1_000_000,
        "conflateMs": 0,
        "heartbeatMs": 5000,
        "mc": [
            {
                "id": market_id,
                "img": True,
                "con": False,
                "marketDefinition": market_definition or _market_definition(),
                "rc": [{"id": 101, "hc": 0, "ltp": 2.0}],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"


def _runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: bytes,
    fields: tuple[str, ...] = ("EX_LTP", "EX_MARKET_DEF"),
) -> tuple[
    stream.BetfairStreamTlsTransport,
    BetfairAuthenticatedStreamFreshnessRuntime,
]:
    fake = FakeSocket(
        [
            _connection(),
            _auth_status() + _subscription_status() + payload,
        ]
    )
    monkeypatch.setattr(
        stream,
        "_open_verified_tls_socket",
        lambda _timeout, _cancel=None: fake,
    )
    transport = stream.BetfairStreamTlsTransport(
        identity=stream.BetfairStreamSessionIdentity(
            account_id="acct-1",
            app_identity_id="betfair-app-1",
            app_key_class="LIVE",
            session_epoch=1,
        ),
        secret_provider=Secrets(),
    )
    assert transport.connect() == "conn-1"
    subscription = open_authenticated_market_subscription(
        transport,
        provider_request_id=7,
        market_filter={"marketIds": ["1.234"]},
        market_data_fields=fields,
        ladder_levels=None,
        heartbeat_ms=5000,
        conflate_ms=0,
    )
    return (
        transport,
        BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription),
    )


def test_authenticated_complete_market_definition_mints_conservative_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = _runtime(monkeypatch, payload=_mcm())
    runtime.read_and_ingest()
    evidence = runtime.resolve_market_definition("1.234")

    assert type(evidence) is BetfairAuthenticatedMarketDefinitionEvidence
    evidence.assert_issued()
    assessment = assess_betfair_authenticated_market_definition_authority(
        evidence
    )

    assert assessment.status is OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE
    authority = assessment.authority
    assert authority is not None
    authority.assert_issued_integrity()
    assert authority.identity.source_id == BETFAIR_STREAM_SOURCE_ID
    assert authority.selection_ids == ("101", "202")
    assert authority.roster_basis is OutcomeRosterBasis.PROVIDER_MARKET_DEFINITION
    assert (
        authority.settlement_semantics
        is SettlementSemantics.CANONICAL_WIN_LOSS_VOID_SUPERSET
    )
    assert authority.terminal_space_exact is False
    assert authority.terminal_state_count == 9
    observed = datetime.fromisoformat(authority.observed_at.replace("Z", "+00:00"))
    authority.assert_available_as_of(observed)


def test_directly_constructed_evidence_cannot_mint_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = _runtime(monkeypatch, payload=_mcm())
    runtime.read_and_ingest()
    issued = runtime.resolve_market_definition("1.234")
    assert issued is not None

    forged = replace(issued)
    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="not issued",
    ):
        forged.assert_issued()
    with pytest.raises(BetfairAuthenticatedStreamError):
        assess_betfair_authenticated_market_definition_authority(forged)


def test_market_definition_requires_ex_market_def_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = _runtime(
        monkeypatch,
        payload=_mcm(),
        fields=("EX_LTP",),
    )

    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="EX_MARKET_DEF",
    ):
        runtime.read_and_ingest()
    assert runtime.resolve_market_definition("1.234") is None


def test_incomplete_authenticated_roster_remains_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = _runtime(
        monkeypatch,
        payload=_mcm(market_definition=_market_definition(complete=False)),
    )
    runtime.read_and_ingest()
    evidence = runtime.resolve_market_definition("1.234")
    assert evidence is not None

    assessment = assess_betfair_authenticated_market_definition_authority(
        evidence
    )
    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.authority is None
    assert (
        assessment.refusal_reason
        == "betfair_market_definition_runner_roster_is_not_complete"
    )


def test_disconnect_revokes_market_definition_evidence_before_issuance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, runtime = _runtime(monkeypatch, payload=_mcm())
    runtime.read_and_ingest()
    evidence = runtime.resolve_market_definition("1.234")
    assert evidence is not None

    transport.close()
    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="no longer current",
    ):
        evidence.assert_issued()
    with pytest.raises(BetfairAuthenticatedStreamError):
        assess_betfair_authenticated_market_definition_authority(evidence)


def test_new_definition_supersedes_prior_process_local_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _mcm()
    second_payload = json.loads(_mcm().decode("utf-8"))
    second_payload["ct"] = None
    second_payload.pop("initialClk")
    second_payload["clk"] = "c2"
    second_payload["pt"] += 1
    second_payload["mc"][0]["img"] = False
    second_payload["mc"][0]["marketDefinition"]["runners"][1]["status"] = "REMOVED"
    second = json.dumps(
        second_payload,
        separators=(",", ":"),
    ).encode("utf-8") + b"\r\n"

    transport, runtime = _runtime(monkeypatch, payload=first + second)
    runtime.read_and_ingest()
    prior = runtime.resolve_market_definition("1.234")
    assert prior is not None
    runtime.read_and_ingest()
    current = runtime.resolve_market_definition("1.234")
    assert current is not None
    assert current is not prior

    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="no longer current",
    ):
        prior.assert_issued()
    current.assert_issued()
    transport.close()


def test_authenticated_evidence_does_not_claim_execution_or_real_money(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = _runtime(monkeypatch, payload=_mcm())
    runtime.read_and_ingest()
    evidence = runtime.resolve_market_definition("1.234")
    assert evidence is not None

    assert evidence.grants_provider_write_authority is False
    assert evidence.grants_execution_authority is False
    assert evidence.real_money_authorized is False


def test_future_provider_publish_time_cannot_mint_causal_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.loads(_mcm().decode("utf-8"))
    raw["pt"] = time.time_ns() // 1_000_000 + 60_000
    payload = json.dumps(raw, separators=(",", ":")).encode("utf-8") + b"\r\n"
    _, runtime = _runtime(monkeypatch, payload=payload)
    runtime.read_and_ingest()
    evidence = runtime.resolve_market_definition("1.234")
    assert evidence is not None

    with pytest.raises(
        ValueError,
        match="provider_publish_at must not be after observed_at",
    ):
        assess_betfair_authenticated_market_definition_authority(evidence)


def test_market_definition_evidence_json_is_content_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = _runtime(monkeypatch, payload=_mcm())
    runtime.read_and_ingest()
    evidence = runtime.resolve_market_definition("1.234")
    assert evidence is not None

    object.__setattr__(
        evidence,
        "market_definition_json",
        evidence.market_definition_json.replace("event-1", "event-2"),
    )
    with pytest.raises(BetfairAuthenticatedStreamError):
        evidence.assert_issued()



def test_fake_module_registry_cannot_self_mint_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth_module

    _, runtime = _runtime(monkeypatch, payload=_mcm())
    runtime.read_and_ingest()
    issued = runtime.resolve_market_definition("1.234")
    assert issued is not None
    forged = replace(issued)

    auth_module._ISSUED_MARKET_DEFINITIONS = {  # type: ignore[attr-defined]
        forged: object(),
    }
    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="not issued",
    ):
        forged.assert_issued()
    with pytest.raises(BetfairAuthenticatedStreamError):
        assess_betfair_authenticated_market_definition_authority(forged)



def test_authenticated_authority_drives_conservative_scenario_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = _runtime(monkeypatch, payload=_mcm())
    runtime.read_and_ingest()
    evidence = runtime.resolve_market_definition("1.234")
    assert evidence is not None
    assessment = assess_betfair_authenticated_market_definition_authority(
        evidence
    )
    authority = assessment.authority
    assert authority is not None

    book = PaperBook("100")
    ticket = book.open_ticket(
        [
            TicketLeg(
                "event-1",
                "1.234",
                "101",
                Decimal("2.0"),
                sport="table_tennis",
            )
        ],
        "10",
        provider_source_ids=(BETFAIR_STREAM_SOURCE_ID,),
    )
    decision_as_of = datetime.fromisoformat(
        authority.observed_at.replace("Z", "+00:00")
    )
    report = ScenarioSearchEngine().analyse_authoritative(
        [ticket],
        [authority],
        decision_as_of=decision_as_of,
    )

    assert report.outcome_space_exhaustive is True
    assert report.outcome_space_exact is False
    assert report.total_states == 9
    assert report.outcome_authority_sha256s == (authority.authority_sha256,)


def test_evidence_class_dispatch_replacement_fails_before_positive_issuance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = _runtime(monkeypatch, payload=_mcm())
    runtime.read_and_ingest()
    evidence = runtime.resolve_market_definition("1.234")
    assert evidence is not None
    original = BetfairAuthenticatedMarketDefinitionEvidence.__getattribute__
    hostile_calls: list[str] = []

    def hostile(self, name):
        hostile_calls.append(name)
        return original(self, name)

    monkeypatch.setattr(
        BetfairAuthenticatedMarketDefinitionEvidence,
        "__getattribute__",
        hostile,
    )
    with pytest.raises(
        ValueError,
        match="evidence class dispatch was replaced",
    ):
        assess_betfair_authenticated_market_definition_authority(evidence)

    assert hostile_calls == []
