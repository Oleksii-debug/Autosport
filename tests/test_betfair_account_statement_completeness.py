from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_account_statement_completeness import (
    BetfairAccountStatementCompletenessError,
    prove_betfair_account_statement_pagination_complete,
)
from autosport.betfair_provider_billing_inputs import (
    read_betfair_provider_billing_inputs,
)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self._value = start

    def __call__(self) -> datetime:
        self._value += timedelta(seconds=1)
        return self._value


class _Transport:
    def __init__(self, pages: dict[int, tuple[list[dict[str, object]], bool]]) -> None:
        self.pages = pages

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body)
        method = request["method"]
        if method == "AccountAPING/v1.0/getAccountDetails":
            result: object = {"currencyCode": "GBP"}
        elif method == "AccountAPING/v1.0/getDeveloperAppKeys":
            result = [
                {
                    "appId": 41,
                    "appName": "autosport",
                    "appVersions": [
                        {
                            "owner": "owner",
                            "versionId": 7,
                            "version": "1.0",
                            "applicationKey": "live-key-123",
                            "delayData": False,
                            "subscriptionRequired": False,
                            "ownerManaged": False,
                            "active": True,
                            "vendorId": "vendor-3",
                        }
                    ],
                }
            ]
        elif method == "AccountAPING/v1.0/getAccountStatement":
            offset = request["params"]["fromRecord"]
            rows, more = self.pages[offset]
            result = {"accountStatement": rows, "moreAvailable": more}
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _row(ref_id: str, minute: int) -> dict[str, object]:
    return {
        "refId": ref_id,
        "itemDate": f"2026-09-20T09:{minute:02d}:00Z",
        "amount": -1,
        "balance": 100,
        "itemClass": "UNKNOWN",
        "itemClassData": {"source": "provider", "ref": ref_id},
    }


def _client(
    pages: dict[int, tuple[list[dict[str, object]], bool]],
    *,
    start: datetime | None = None,
) -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("live-key-123", "session-secret"),
        transport=_Transport(pages),
        clock=_Clock(start or datetime(2026, 9, 21, 2, 10, tzinfo=timezone.utc)),
        venue_id="betfair",
        account_id="non-authoritative-test-label",
    )


def _read(
    client: BetfairReadOnlyClient,
    *,
    from_record: int,
    record_count: int = 2,
    statement_from: str = "2026-09-01T00:00:00Z",
    statement_to: str = "2026-09-21T00:00:00Z",
):
    return read_betfair_provider_billing_inputs(
        client,
        from_record=from_record,
        record_count=record_count,
        statement_from=statement_from,
        statement_to=statement_to,
    )


def _valid_pages():
    client = _client(
        {
            0: ([_row("ref-1", 0), _row("ref-2", 1)], True),
            2: ([_row("ref-3", 2)], False),
        }
    )
    return (_read(client, from_record=0), _read(client, from_record=2))


def test_proves_only_structural_pagination_completeness() -> None:
    proof = prove_betfair_account_statement_pagination_complete(_valid_pages())

    assert proof.pagination_complete is True
    assert proof.temporal_finality_attested is False
    assert proof.cost_scope_complete is False
    assert proof.missing_authorities == ("TEMPORAL_FINALITY_ATTESTATION",)
    assert proof.page_count == 2
    assert proof.row_count == 3
    assert proof.record_count == 2
    assert len(proof.evidence_sha256) == 64
    assert len(proof.query_fingerprint_sha256) == 64
    assert len(proof.ref_ids_sha256) == 64


def test_first_page_must_start_at_zero() -> None:
    pages = _valid_pages()
    with pytest.raises(BetfairAccountStatementCompletenessError, match="contiguous offset"):
        prove_betfair_account_statement_pagination_complete((pages[1],))


def test_gap_or_overlap_in_offsets_fails_closed() -> None:
    client = _client(
        {
            0: ([_row("ref-1", 0), _row("ref-2", 1)], True),
            1: ([_row("ref-3", 2)], False),
        }
    )
    pages = (_read(client, from_record=0), _read(client, from_record=1))
    with pytest.raises(BetfairAccountStatementCompletenessError, match="contiguous offset"):
        prove_betfair_account_statement_pagination_complete(pages)


def test_duplicate_ref_id_across_pages_fails_closed() -> None:
    client = _client(
        {
            0: ([_row("ref-1", 0), _row("ref-2", 1)], True),
            2: ([_row("ref-2", 2)], False),
        }
    )
    pages = (_read(client, from_record=0), _read(client, from_record=2))
    with pytest.raises(BetfairAccountStatementCompletenessError, match="duplicate statement ref_id"):
        prove_betfair_account_statement_pagination_complete(pages)


def test_more_available_requires_progress() -> None:
    client = _client({0: ([], True)})
    page = _read(client, from_record=0)
    with pytest.raises(BetfairAccountStatementCompletenessError, match="without pagination progress"):
        prove_betfair_account_statement_pagination_complete((page,))


def test_terminal_semantics_are_exact() -> None:
    client = _client(
        {
            0: ([_row("ref-1", 0)], False),
            1: ([_row("ref-2", 1)], False),
        }
    )
    pages = (_read(client, from_record=0), _read(client, from_record=1))
    with pytest.raises(BetfairAccountStatementCompletenessError, match="terminal before"):
        prove_betfair_account_statement_pagination_complete(pages)

    client = _client({0: ([_row("ref-1", 0)], True)})
    page = _read(client, from_record=0)
    with pytest.raises(BetfairAccountStatementCompletenessError, match="last page"):
        prove_betfair_account_statement_pagination_complete((page,))


def test_query_scope_and_provider_identity_must_stay_invariant() -> None:
    first_client = _client({0: ([_row("ref-1", 0)], True)})
    second_client = _client({1: ([_row("ref-2", 1)], False)})
    first = _read(first_client, from_record=0)
    second = _read(
        second_client,
        from_record=1,
        statement_to="2026-09-22T00:00:00Z",
    )
    with pytest.raises(BetfairAccountStatementCompletenessError, match="changed provider/query scope"):
        prove_betfair_account_statement_pagination_complete((first, second))


def test_record_count_change_fails_closed() -> None:
    first_client = _client({0: ([_row("ref-1", 0)], True)})
    second_client = _client({1: ([_row("ref-2", 1)], False)})
    first = _read(first_client, from_record=0, record_count=2)
    second = _read(second_client, from_record=1, record_count=1)
    with pytest.raises(BetfairAccountStatementCompletenessError, match="changed provider/query scope"):
        prove_betfair_account_statement_pagination_complete((first, second))


def test_observation_time_must_not_move_backwards() -> None:
    first_client = _client(
        {0: ([_row("ref-1", 0)], True)},
        start=datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc),
    )
    second_client = _client(
        {1: ([_row("ref-2", 1)], False)},
        start=datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc),
    )
    first = _read(first_client, from_record=0)
    second = _read(second_client, from_record=1)
    with pytest.raises(BetfairAccountStatementCompletenessError, match="moved backwards"):
        prove_betfair_account_statement_pagination_complete((first, second))


def test_cost_scope_and_finality_cannot_be_caller_promoted() -> None:
    proof = prove_betfair_account_statement_pagination_complete(_valid_pages())
    with pytest.raises(BetfairAccountStatementCompletenessError, match="temporal_finality_attested"):
        replace(proof, temporal_finality_attested=True)
    with pytest.raises(BetfairAccountStatementCompletenessError, match="cost_scope_complete"):
        replace(proof, cost_scope_complete=True)


def test_exact_same_pages_produce_deterministic_proof_identity() -> None:
    pages = _valid_pages()
    first = prove_betfair_account_statement_pagination_complete(pages)
    second = prove_betfair_account_statement_pagination_complete(pages)
    assert first == second
    assert first.evidence_sha256 == second.evidence_sha256
