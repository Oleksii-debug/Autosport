from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)


FIXED_NOW = datetime(2026, 9, 21, 20, 40, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        self.calls += 1
        return self.payload


class CorrelatedErrorTransport:
    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        request = json.loads(body.decode("utf-8"))
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32099,
                    "message": "ANGX-0007",
                    "data": {
                        "AccountAPINGException": {
                            "errorCode": "INVALID_APP_KEY",
                            "errorDetails": "provider detail must stay out of evidence identity",
                        }
                    },
                },
                "id": request["id"],
            },
            separators=(",", ":"),
        ).encode("utf-8")


def _error_payload(*, data: object | None, code: int = -32099, message: str = "ANGX-0007") -> bytes:
    error: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return json.dumps(
        {"jsonrpc": "2.0", "error": error, "id": 1},
        separators=(",", ":"),
    ).encode("utf-8")


def _client(payload: bytes) -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=FakeTransport(payload),
        clock=lambda: FIXED_NOW,
    )


@pytest.mark.parametrize(
    ("exception_key", "provider_code", "read_method"),
    [
        ("AccountAPINGException", "INVALID_APP_KEY", "read_account_funds"),
        ("APINGException", "TOO_MANY_REQUESTS", "read_current_orders_page"),
    ],
)
def test_official_structured_error_data_preserves_typed_provider_code_without_details_leak(
    exception_key: str,
    provider_code: str,
    read_method: str,
) -> None:
    secret_detail = "errorDetails contains app-secret and session-secret"
    payload = _error_payload(
        data={
            exception_key: {
                "errorCode": provider_code,
                "errorDetails": secret_detail,
            }
        }
    )
    client = _client(payload)

    with pytest.raises(BetfairReadOnlyError) as raised:
        getattr(client, read_method)()

    error = raised.value
    assert error.json_rpc_code == -32099
    assert error.provider_error_code == provider_code
    rendered = str(error)
    assert "app-secret" not in rendered
    assert "session-secret" not in rendered
    assert secret_detail not in rendered


def test_unrelated_nested_known_token_cannot_mint_provider_error_authority() -> None:
    client = _client(
        _error_payload(
            data={
                "unrelated": {
                    "errorCode": "TOO_MANY_REQUESTS",
                    "errorDetails": "not an APING exception container",
                }
            }
        )
    )

    with pytest.raises(BetfairReadOnlyError) as raised:
        client.read_account_funds()

    assert raised.value.json_rpc_code == -32099
    assert raised.value.provider_error_code is None


@pytest.mark.parametrize("bad_code", [True, 17, " too_many_requests ", "TOO MANY REQUESTS"])
def test_malformed_structured_error_code_remains_unclassified(bad_code: object) -> None:
    client = _client(
        _error_payload(data={"APINGException": {"errorCode": bad_code}})
    )

    with pytest.raises(BetfairReadOnlyError) as raised:
        client.read_current_orders_page()

    assert raised.value.json_rpc_code == -32099
    assert raised.value.provider_error_code is None


def test_conflicting_supported_exception_containers_fail_closed() -> None:
    client = _client(
        _error_payload(
            data={
                "APINGException": {"errorCode": "TOO_MANY_REQUESTS"},
                "AccountAPINGException": {"errorCode": "INVALID_APP_KEY"},
            }
        )
    )

    with pytest.raises(BetfairReadOnlyError) as raised:
        client.read_account_funds()

    assert raised.value.json_rpc_code == -32099
    assert raised.value.provider_error_code is None


def test_generic_jsonrpc_error_keeps_rpc_code_without_inventing_provider_semantics() -> None:
    client = _client(_error_payload(data=None, code=-32603, message="Internal error"))

    with pytest.raises(BetfairReadOnlyError) as raised:
        client.read_account_funds()

    assert raised.value.json_rpc_code == -32603
    assert raised.value.provider_error_code is None


def test_error_envelope_wins_over_result_even_when_result_looks_valid() -> None:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "result": {
                "availableToBetBalance": 100,
                "exposure": 0,
                "retainedCommission": 0,
                "exposureLimit": -1000,
            },
            "error": {
                "code": -32099,
                "message": "ANGX-0007",
                "data": {"AccountAPINGException": {"errorCode": "INVALID_APP_KEY"}},
            },
            "id": 1,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    client = _client(payload)

    with pytest.raises(BetfairReadOnlyError, match="both error and result"):
        client.read_account_funds()


def test_known_provider_token_in_top_level_message_does_not_mint_typed_semantics() -> None:
    client = _client(
        _error_payload(
            data={"unrelated": {"note": "not provider authority"}},
            message="TOO_MANY_REQUESTS",
        )
    )

    with pytest.raises(BetfairReadOnlyError) as raised:
        client.read_current_orders_page()

    assert raised.value.json_rpc_code == -32099
    assert raised.value.provider_error_code is None


def test_duplicate_supported_exception_containers_are_ambiguous_even_with_same_code() -> None:
    client = _client(
        _error_payload(
            data={
                "APINGException": {"errorCode": "TOO_MANY_REQUESTS"},
                "AccountAPINGException": {"errorCode": "TOO_MANY_REQUESTS"},
            }
        )
    )

    with pytest.raises(BetfairReadOnlyError) as raised:
        client.read_current_orders_page()

    assert raised.value.json_rpc_code == -32099
    assert raised.value.provider_error_code is None


@pytest.mark.parametrize("bad_data", [[], "APINGException", 17, True])
def test_non_object_error_data_cannot_mint_provider_semantics(bad_data: object) -> None:
    client = _client(_error_payload(data=bad_data))

    with pytest.raises(BetfairReadOnlyError) as raised:
        client.read_account_funds()

    assert raised.value.json_rpc_code == -32099
    assert raised.value.provider_error_code is None


@pytest.mark.parametrize(
    "declared_exception",
    ["AccountAPINGException", "OtherException", 17, True, None],
)
def test_conflicting_or_malformed_exceptionname_fails_closed(
    declared_exception: object,
) -> None:
    client = _client(
        _error_payload(
            data={
                "exceptionname": declared_exception,
                "APINGException": {"errorCode": "TOO_MANY_REQUESTS"},
            }
        )
    )

    with pytest.raises(BetfairReadOnlyError) as raised:
        client.read_current_orders_page()

    assert raised.value.json_rpc_code == -32099
    assert raised.value.provider_error_code is None


def test_matching_exceptionname_preserves_provider_semantics() -> None:
    client = _client(
        _error_payload(
            data={
                "exceptionname": "APINGException",
                "APINGException": {"errorCode": "TIMEOUT_ERROR"},
            }
        )
    )

    with pytest.raises(BetfairReadOnlyError) as raised:
        client.read_current_orders_page()

    assert raised.value.json_rpc_code == -32099
    assert raised.value.provider_error_code == "TIMEOUT_ERROR"


@pytest.mark.parametrize(
    ("read_method", "wrong_exception"),
    [
        ("read_account_funds", "APINGException"),
        ("read_current_orders_page", "AccountAPINGException"),
    ],
)
def test_wrong_service_exception_container_cannot_mint_provider_semantics(
    read_method: str,
    wrong_exception: str,
) -> None:
    client = _client(
        _error_payload(
            data={
                "exceptionname": wrong_exception,
                wrong_exception: {"errorCode": "TOO_MANY_REQUESTS"},
            }
        )
    )

    with pytest.raises(BetfairReadOnlyError) as raised:
        getattr(client, read_method)()

    assert raised.value.json_rpc_code == -32099
    assert raised.value.provider_error_code is None


def test_typed_error_metadata_preserves_runtimeerror_args_contract() -> None:
    no_args = BetfairReadOnlyError()
    multiple_args = BetfairReadOnlyError("left", "right")

    assert no_args.args == ()
    assert no_args.json_rpc_code is None
    assert no_args.provider_error_code is None
    assert multiple_args.args == ("left", "right")
    assert multiple_args.json_rpc_code is None
    assert multiple_args.provider_error_code is None


@pytest.mark.parametrize("rpc_code", [-32603, -32602, 0])
def test_non_application_jsonrpc_code_cannot_mint_provider_semantics(
    rpc_code: int,
) -> None:
    client = _client(
        _error_payload(
            code=rpc_code,
            data={
                "exceptionname": "APINGException",
                "APINGException": {"errorCode": "TOO_MANY_REQUESTS"},
            },
        )
    )

    with pytest.raises(BetfairReadOnlyError) as raised:
        client.read_current_orders_page()

    assert raised.value.json_rpc_code == rpc_code
    assert raised.value.provider_error_code is None


def test_missing_jsonrpc_code_cannot_mint_provider_semantics() -> None:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "error": {
                "message": "ANGX-0007",
                "data": {
                    "exceptionname": "APINGException",
                    "APINGException": {"errorCode": "TOO_MANY_REQUESTS"},
                },
            },
            "id": 1,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    client = _client(payload)

    with pytest.raises(BetfairReadOnlyError) as raised:
        client.read_current_orders_page()

    assert raised.value.json_rpc_code is None
    assert raised.value.provider_error_code is None

@pytest.mark.parametrize("bad_message", [None, "", "   ", 17, True])
def test_malformed_or_missing_jsonrpc_error_message_cannot_mint_provider_semantics(
    bad_message: object,
) -> None:
    error = {
        "code": -32099,
        "data": {
            "exceptionname": "APINGException",
            "APINGException": {"errorCode": "TOO_MANY_REQUESTS"},
        },
    }
    if bad_message is not None:
        error["message"] = bad_message
    payload = json.dumps(
        {"jsonrpc": "2.0", "error": error, "id": 1},
        separators=(",", ":"),
    ).encode("utf-8")
    client = _client(payload)

    with pytest.raises(BetfairReadOnlyError, match="malformed error") as raised:
        client.read_current_orders_page()

    assert raised.value.json_rpc_code is None
    assert raised.value.provider_error_code is None
    assert raised.value.rpc_error_evidence_sha256 is None


def test_transport_error_evidence_binds_exact_correlated_request_identity() -> None:
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=CorrelatedErrorTransport(),
        clock=lambda: FIXED_NOW,
    )

    captured = []
    for _ in range(2):
        with pytest.raises(BetfairReadOnlyError) as raised:
            client.read_account_funds()
        captured.append(raised.value)

    first, second = captured
    assert first.request_id == 1
    assert second.request_id == 2
    assert first.operation == "AccountAPING/v1.0/getAccountFunds"
    assert second.operation == first.operation
    assert first.json_rpc_code == -32099
    assert second.json_rpc_code == -32099
    assert first.provider_error_code == "INVALID_APP_KEY"
    assert second.provider_error_code == "INVALID_APP_KEY"
    assert first.response_payload_sha256 != second.response_payload_sha256
    assert first.rpc_error_evidence_sha256 is not None
    assert second.rpc_error_evidence_sha256 is not None
    assert first.rpc_error_evidence_sha256 != second.rpc_error_evidence_sha256
    assert "provider detail" not in first.rpc_error_evidence_sha256
    assert "app-secret" not in str(first)
    assert "session-secret" not in str(first)


def test_rpc_error_evidence_requires_complete_transport_correlation_metadata() -> None:
    uncorrelated = BetfairReadOnlyError(
        "synthetic",
        json_rpc_code=-32099,
        provider_error_code="INVALID_APP_KEY",
    )

    assert uncorrelated.request_id is None
    assert uncorrelated.operation is None
    assert uncorrelated.response_payload_sha256 is None
    assert uncorrelated.rpc_error_evidence_sha256 is None

