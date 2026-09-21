import itertools

import pytest

from autosport.betfair_read_degradation import (
    BetfairReadDegradation,
    BetfairReadDegradationError,
    ReadRecoveryAction,
)


@pytest.mark.parametrize(
    ("error_code", "action"),
    [
        ("INVALID_SESSION_INFORMATION", ReadRecoveryAction.REAUTHENTICATE),
        ("NO_SESSION", ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG),
        ("NO_APP_KEY", ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG),
        ("INVALID_APP_KEY", ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG),
        ("SUBSCRIPTION_EXPIRED", ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG),
        ("INVALID_SUBSCRIPTION_TOKEN", ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG),
        ("INVALID_INPUT_DATA", ReadRecoveryAction.REPAIR_REQUEST),
        ("TOO_MUCH_DATA", ReadRecoveryAction.REPAIR_REQUEST),
        ("TOO_MANY_REQUESTS", ReadRecoveryAction.RETRY_WITH_BACKOFF),
        ("SERVICE_BUSY", ReadRecoveryAction.RETRY_WITH_BACKOFF),
        ("TIMEOUT_ERROR", ReadRecoveryAction.RETRY_WITH_BACKOFF),
        ("UNEXPECTED_ERROR", ReadRecoveryAction.RETRY_WITH_BACKOFF),
    ],
)
def test_documented_error_codes_have_fail_closed_read_dispositions(error_code, action):
    result = BetfairReadDegradation("listMarketBook", error_code)
    assert result.action is action
    assert result.automatic_repeat_allowed is (action is ReadRecoveryAction.RETRY_WITH_BACKOFF)


def test_expired_session_requires_reauthentication_not_blind_repeat():
    result = BetfairReadDegradation("listMarketCatalogue", "INVALID_SESSION_INFORMATION")
    assert result.requires_new_session is True
    assert result.automatic_repeat_allowed is False
    assert result.request_must_change is False


def test_request_shape_failures_require_repair_not_retry():
    for code in ("INVALID_INPUT_DATA", "TOO_MUCH_DATA"):
        result = BetfairReadDegradation("listMarketBook", code)
        assert result.request_must_change is True
        assert result.automatic_repeat_allowed is False


def test_unknown_error_fails_closed_without_automatic_retry():
    result = BetfairReadDegradation("getAccountFunds", "FUTURE_PROVIDER_ERROR")
    assert result.action is ReadRecoveryAction.DO_NOT_RETRY
    assert result.automatic_repeat_allowed is False


@pytest.mark.parametrize(
    "operation",
    [
        "placeOrders",
        "replaceOrders",
        "cancelOrders",
        "updateOrders",
        "heartbeat",
        "login",
        "keepAlive",
        "",
    ],
)
def test_non_read_operations_are_rejected(operation):
    with pytest.raises(BetfairReadDegradationError, match="read-only"):
        BetfairReadDegradation(operation, "TIMEOUT_ERROR")


@pytest.mark.parametrize(
    "operation",
    [
        "listMarketCatalogue",
        "listMarketBook",
        "listRunnerBook",
        "listCurrentOrders",
        "listClearedOrders",
        "listMarketProfitAndLoss",
        "getAccountDetails",
        "getAccountFunds",
    ],
)
def test_supported_reads_can_classify_temporary_provider_failures(operation):
    result = BetfairReadDegradation(operation, "SERVICE_BUSY")
    assert result.action is ReadRecoveryAction.RETRY_WITH_BACKOFF
    assert result.automatic_repeat_allowed is True
    assert result.evidence_payload["read_only"] is True


def test_error_code_must_be_canonical_uppercase_without_whitespace():
    for code in ("service_busy", " SERVICE_BUSY", "SERVICE_BUSY ", ""):
        with pytest.raises(BetfairReadDegradationError):
            BetfairReadDegradation("listMarketBook", code)


def test_request_uuid_is_provenance_and_part_of_evidence_identity():
    first = BetfairReadDegradation("listMarketBook", "SERVICE_BUSY", "req-a")
    same = BetfairReadDegradation("listMarketBook", "SERVICE_BUSY", "req-a")
    second = BetfairReadDegradation("listMarketBook", "SERVICE_BUSY", "req-b")
    assert first.evidence_id == same.evidence_id
    assert first.evidence_id != second.evidence_id
    assert first.evidence_payload["request_uuid"] == "req-a"


def test_operation_is_part_of_evidence_identity():
    catalogue = BetfairReadDegradation("listMarketCatalogue", "TIMEOUT_ERROR", "req")
    market_book = BetfairReadDegradation("listMarketBook", "TIMEOUT_ERROR", "req")
    assert catalogue.evidence_id != market_book.evidence_id


def test_classification_never_performs_transport_or_grants_execution():
    result = BetfairReadDegradation("listCurrentOrders", "TIMEOUT_ERROR")
    assert result.evidence_payload["transport_performed"] is False
    assert result.evidence_payload["execution_authorized"] is False


def test_all_temporary_errors_are_repeatable_only_for_supported_reads():
    temporary = ("TOO_MANY_REQUESTS", "SERVICE_BUSY", "TIMEOUT_ERROR", "UNEXPECTED_ERROR")
    operations = (
        "listMarketCatalogue",
        "listMarketBook",
        "listRunnerBook",
        "listCurrentOrders",
        "listClearedOrders",
        "listMarketProfitAndLoss",
        "getAccountDetails",
        "getAccountFunds",
    )
    for operation, error in itertools.product(operations, temporary):
        result = BetfairReadDegradation(operation, error)
        assert result.automatic_repeat_allowed is True
        assert result.action is ReadRecoveryAction.RETRY_WITH_BACKOFF


def test_credentials_and_config_errors_never_mint_reauth_or_repeat_authority():
    for code in (
        "NO_SESSION",
        "NO_APP_KEY",
        "INVALID_APP_KEY",
        "SUBSCRIPTION_EXPIRED",
        "INVALID_SUBSCRIPTION_TOKEN",
    ):
        result = BetfairReadDegradation("getAccountDetails", code)
        assert result.action is ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG
        assert result.requires_new_session is False
        assert result.automatic_repeat_allowed is False
