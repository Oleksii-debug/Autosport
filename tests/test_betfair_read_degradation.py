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
        ("INVALID_INPUT_DATA", ReadRecoveryAction.REPAIR_REQUEST),
        ("TOO_MANY_REQUESTS", ReadRecoveryAction.RETRY_WITH_BACKOFF),
        ("SERVICE_BUSY", ReadRecoveryAction.RETRY_WITH_BACKOFF),
        ("TIMEOUT_ERROR", ReadRecoveryAction.RETRY_WITH_BACKOFF),
        ("UNEXPECTED_ERROR", ReadRecoveryAction.RETRY_WITH_BACKOFF),
    ],
)
def test_common_documented_errors_have_same_read_disposition(error_code, action):
    for operation in ("listMarketBook", "getAccountFunds"):
        result = BetfairReadDegradation(operation, error_code)
        assert result.action is action
        assert result.documented_for_api_family is True
        assert result.automatic_repeat_allowed is (
            action is ReadRecoveryAction.RETRY_WITH_BACKOFF
        )


@pytest.mark.parametrize(
    ("error_code", "action"),
    [
        ("TOO_MUCH_DATA", ReadRecoveryAction.REPAIR_REQUEST),
        ("REQUEST_SIZE_EXCEEDS_LIMIT", ReadRecoveryAction.REPAIR_REQUEST),
        ("ACCESS_DENIED", ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG),
    ],
)
def test_betting_specific_errors_are_documented_only_for_betting(error_code, action):
    betting = BetfairReadDegradation("listMarketBook", error_code)
    account = BetfairReadDegradation("getAccountFunds", error_code)

    assert betting.api_family == "BETTING"
    assert betting.documented_for_api_family is True
    assert betting.action is action

    assert account.api_family == "ACCOUNTS"
    assert account.documented_for_api_family is False
    assert account.action is ReadRecoveryAction.DO_NOT_RETRY
    assert account.automatic_repeat_allowed is False


@pytest.mark.parametrize(
    "error_code",
    ["SUBSCRIPTION_EXPIRED", "INVALID_SUBSCRIPTION_TOKEN"],
)
def test_accounts_specific_errors_are_not_rebound_to_betting(error_code):
    account = BetfairReadDegradation("getAccountDetails", error_code)
    betting = BetfairReadDegradation("listMarketBook", error_code)

    assert account.api_family == "ACCOUNTS"
    assert account.documented_for_api_family is True
    assert account.action is ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG

    assert betting.api_family == "BETTING"
    assert betting.documented_for_api_family is False
    assert betting.action is ReadRecoveryAction.DO_NOT_RETRY
    assert betting.automatic_repeat_allowed is False


def test_expired_session_requires_reauthentication_not_blind_repeat():
    result = BetfairReadDegradation(
        "listMarketCatalogue", "INVALID_SESSION_INFORMATION"
    )
    assert result.requires_new_session is True
    assert result.automatic_repeat_allowed is False
    assert result.request_must_change is False


def test_request_shape_failures_require_repair_not_retry():
    for code in (
        "INVALID_INPUT_DATA",
        "TOO_MUCH_DATA",
        "REQUEST_SIZE_EXCEEDS_LIMIT",
    ):
        result = BetfairReadDegradation("listMarketBook", code)
        assert result.request_must_change is True
        assert result.automatic_repeat_allowed is False


def test_unknown_error_fails_closed_without_automatic_retry():
    result = BetfairReadDegradation("getAccountFunds", "FUTURE_PROVIDER_ERROR")
    assert result.documented_for_api_family is False
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
        "listEventTypes",
        "listCompetitions",
        "listEvents",
        "listMarketTypes",
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
def test_supported_reads_can_classify_common_temporary_provider_failures(operation):
    result = BetfairReadDegradation(operation, "SERVICE_BUSY")
    assert result.action is ReadRecoveryAction.RETRY_WITH_BACKOFF
    assert result.automatic_repeat_allowed is True
    assert result.documented_for_api_family is True
    assert result.evidence_payload["read_only"] is True


def test_multisport_discovery_reads_share_betting_degradation_policy():
    for operation in ("listEventTypes", "listEvents", "listMarketTypes"):
        temporary = BetfairReadDegradation(operation, "SERVICE_BUSY")
        malformed_request = BetfairReadDegradation(operation, "TOO_MUCH_DATA")
        account_only = BetfairReadDegradation(operation, "SUBSCRIPTION_EXPIRED")

        assert temporary.api_family == "BETTING"
        assert temporary.action is ReadRecoveryAction.RETRY_WITH_BACKOFF
        assert temporary.automatic_repeat_allowed is True
        assert malformed_request.action is ReadRecoveryAction.REPAIR_REQUEST
        assert malformed_request.request_must_change is True
        assert account_only.documented_for_api_family is False
        assert account_only.action is ReadRecoveryAction.DO_NOT_RETRY


def test_list_competitions_uses_conservative_documented_failure_policy():
    expected = {
        "INVALID_SESSION_INFORMATION": ReadRecoveryAction.REAUTHENTICATE,
        "NO_SESSION": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
        "NO_APP_KEY": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
        "INVALID_APP_KEY": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
        "INVALID_INPUT_DATA": ReadRecoveryAction.REPAIR_REQUEST,
        "SERVICE_BUSY": ReadRecoveryAction.RETRY_WITH_BACKOFF,
        "TIMEOUT_ERROR": ReadRecoveryAction.RETRY_WITH_BACKOFF,
        "UNEXPECTED_ERROR": ReadRecoveryAction.RETRY_WITH_BACKOFF,
        "ACCESS_DENIED": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
    }
    for error_code, action in expected.items():
        result = BetfairReadDegradation("listCompetitions", error_code)
        assert result.api_family == "BETTING"
        assert result.documented_for_api_family is True
        assert result.documented_for_operation is True
        assert result.action is action
        assert result.automatic_repeat_allowed is (
            action is ReadRecoveryAction.RETRY_WITH_BACKOFF
        )


@pytest.mark.parametrize(
    "error_code",
    ["TOO_MUCH_DATA", "REQUEST_SIZE_EXCEEDS_LIMIT", "TOO_MANY_REQUESTS"],
)
def test_list_competitions_does_not_inherit_unestablished_limit_actions(error_code):
    result = BetfairReadDegradation("listCompetitions", error_code)
    assert result.api_family == "BETTING"
    assert result.documented_for_api_family is True
    assert result.documented_for_operation is False
    assert result.action is ReadRecoveryAction.DO_NOT_RETRY
    assert result.automatic_repeat_allowed is False
    assert result.request_must_change is False


def test_list_competitions_rejects_accounts_only_error_semantics():
    result = BetfairReadDegradation("listCompetitions", "SUBSCRIPTION_EXPIRED")
    assert result.documented_for_api_family is False
    assert result.documented_for_operation is False
    assert result.action is ReadRecoveryAction.DO_NOT_RETRY
    assert result.automatic_repeat_allowed is False


def test_error_code_must_be_canonical_uppercase_without_whitespace():
    for code in ("service_busy", " SERVICE_BUSY", "SERVICE_BUSY ", ""):
        with pytest.raises(BetfairReadDegradationError):
            BetfairReadDegradation("listMarketBook", code)


def test_request_uuid_is_correlation_metadata_and_part_of_identity():
    first = BetfairReadDegradation("listMarketBook", "SERVICE_BUSY", "req-a")
    same = BetfairReadDegradation("listMarketBook", "SERVICE_BUSY", "req-a")
    second = BetfairReadDegradation("listMarketBook", "SERVICE_BUSY", "req-b")
    assert first.evidence_id == same.evidence_id
    assert first.evidence_id != second.evidence_id
    assert first.evidence_payload["request_uuid"] == "req-a"
    assert first.evidence_payload["provider_error_origin_verified"] is False


def test_api_family_and_operation_are_part_of_evidence_identity():
    catalogue = BetfairReadDegradation(
        "listMarketCatalogue", "TIMEOUT_ERROR", "req"
    )
    market_book = BetfairReadDegradation("listMarketBook", "TIMEOUT_ERROR", "req")
    account = BetfairReadDegradation("getAccountDetails", "TIMEOUT_ERROR", "req")
    assert catalogue.evidence_id != market_book.evidence_id
    assert market_book.evidence_id != account.evidence_id
    assert market_book.api_family == "BETTING"
    assert account.api_family == "ACCOUNTS"


def test_classification_never_performs_transport_or_grants_execution():
    result = BetfairReadDegradation("listCurrentOrders", "TIMEOUT_ERROR")
    assert result.evidence_payload["transport_performed"] is False
    assert result.evidence_payload["provider_error_origin_verified"] is False
    assert result.evidence_payload["execution_authorized"] is False


def test_error_classification_never_claims_success_response_completeness():
    # Betfair documents that getAccountDetails can return a partial successful response
    # when only one of its two underlying services fails. This exception classifier
    # therefore cannot turn field absence into authoritative provider truth.
    result = BetfairReadDegradation("getAccountDetails", "UNEXPECTED_ERROR")
    assert result.evidence_payload["response_completeness_proven"] is False


def test_all_common_temporary_errors_are_repeatable_only_for_supported_reads():
    temporary = (
        "SERVICE_BUSY",
        "TIMEOUT_ERROR",
        "UNEXPECTED_ERROR",
    )
    operations = (
        "listEventTypes",
        "listCompetitions",
        "listEvents",
        "listMarketTypes",
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
        assert result.documented_for_api_family is True
        assert result.automatic_repeat_allowed is True
        assert result.action is ReadRecoveryAction.RETRY_WITH_BACKOFF


def test_accounts_credentials_and_config_errors_never_mint_reauth_or_repeat_authority():
    for code in (
        "NO_SESSION",
        "NO_APP_KEY",
        "INVALID_APP_KEY",
        "SUBSCRIPTION_EXPIRED",
        "INVALID_SUBSCRIPTION_TOKEN",
    ):
        result = BetfairReadDegradation("getAccountDetails", code)
        assert result.documented_for_api_family is True
        assert result.action is ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG
        assert result.requires_new_session is False
        assert result.automatic_repeat_allowed is False
