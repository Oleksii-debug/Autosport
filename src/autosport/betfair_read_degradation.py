"""Fail-closed Betfair read-only provider-error classification.

This module classifies provider exception codes for a bounded set of read-only API
operations. It deliberately does not perform retries, login, transport I/O, response
completeness validation, or any money-moving reconciliation. Write operations are
rejected so read-safe repeat semantics cannot leak into place/cancel/update/replace
execution paths.

The classifier is policy only: construction does not prove that Betfair emitted the
supplied error. A product-owned transport may bind this classification to separately
verified provider-response provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json


class BetfairReadDegradationError(ValueError):
    """Raised when a read-only error cannot be classified canonically."""


class ReadRecoveryAction(str, Enum):
    REAUTHENTICATE = "REAUTHENTICATE"
    FIX_CREDENTIALS_OR_CONFIG = "FIX_CREDENTIALS_OR_CONFIG"
    REPAIR_REQUEST = "REPAIR_REQUEST"
    RETRY_WITH_BACKOFF = "RETRY_WITH_BACKOFF"
    DO_NOT_RETRY = "DO_NOT_RETRY"


_BETTING_READ_ONLY_OPERATIONS = frozenset(
    {
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
    }
)
_ACCOUNT_READ_ONLY_OPERATIONS = frozenset(
    {
        "getAccountDetails",
        "getAccountFunds",
    }
)
_READ_ONLY_OPERATIONS = _BETTING_READ_ONLY_OPERATIONS | _ACCOUNT_READ_ONLY_OPERATIONS

# Current provider-documented APINGException categories are kept API-family specific.
# A code documented for one family must not inherit that disposition in another family.
_COMMON_ERROR_ACTIONS: dict[str, ReadRecoveryAction] = {
    "INVALID_SESSION_INFORMATION": ReadRecoveryAction.REAUTHENTICATE,
    "NO_SESSION": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
    "NO_APP_KEY": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
    "INVALID_APP_KEY": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
    "INVALID_INPUT_DATA": ReadRecoveryAction.REPAIR_REQUEST,
    "TOO_MANY_REQUESTS": ReadRecoveryAction.RETRY_WITH_BACKOFF,
    "SERVICE_BUSY": ReadRecoveryAction.RETRY_WITH_BACKOFF,
    "TIMEOUT_ERROR": ReadRecoveryAction.RETRY_WITH_BACKOFF,
    "UNEXPECTED_ERROR": ReadRecoveryAction.RETRY_WITH_BACKOFF,
}
_BETTING_ERROR_ACTIONS: dict[str, ReadRecoveryAction] = {
    **_COMMON_ERROR_ACTIONS,
    "TOO_MUCH_DATA": ReadRecoveryAction.REPAIR_REQUEST,
    "REQUEST_SIZE_EXCEEDS_LIMIT": ReadRecoveryAction.REPAIR_REQUEST,
    "ACCESS_DENIED": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
}

# listCompetitions is part of the canonical multi-sport discovery prefix, but the
# provider's operation-specific limit documentation does not establish the
# TOO_MUCH_DATA / REQUEST_SIZE_EXCEEDS_LIMIT / TOO_MANY_REQUESTS cases for it.
# Keep those claimed codes fail-closed rather than inheriting positive action only
# from the broader Betting family.
_LIST_COMPETITIONS_ERROR_ACTIONS: dict[str, ReadRecoveryAction] = {
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
_ACCOUNT_ERROR_ACTIONS: dict[str, ReadRecoveryAction] = {
    **_COMMON_ERROR_ACTIONS,
    "SUBSCRIPTION_EXPIRED": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
    "INVALID_SUBSCRIPTION_TOKEN": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
}


def _family_error_actions_for(operation: str) -> dict[str, ReadRecoveryAction]:
    if operation in _BETTING_READ_ONLY_OPERATIONS:
        return _BETTING_ERROR_ACTIONS
    if operation in _ACCOUNT_READ_ONLY_OPERATIONS:
        return _ACCOUNT_ERROR_ACTIONS
    raise BetfairReadDegradationError(
        "operation must be a supported read-only Betfair operation"
    )


def _error_actions_for(operation: str) -> dict[str, ReadRecoveryAction]:
    if operation == "listCompetitions":
        return _LIST_COMPETITIONS_ERROR_ACTIONS
    return _family_error_actions_for(operation)


@dataclass(frozen=True, slots=True)
class BetfairReadDegradation:
    """Deterministic policy classification for a claimed failed read-only call.

    This value is not provider-origin evidence. In particular, request_uuid is
    correlation metadata only; a caller supplying a UUID does not prove that Betfair
    emitted the error.
    """

    operation: str
    error_code: str
    request_uuid: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, str) or self.operation not in _READ_ONLY_OPERATIONS:
            raise BetfairReadDegradationError(
                "operation must be a supported read-only Betfair operation"
            )
        if not isinstance(self.error_code, str) or not self.error_code:
            raise BetfairReadDegradationError("error_code must be a non-empty string")
        if (
            self.error_code != self.error_code.strip()
            or self.error_code.upper() != self.error_code
        ):
            raise BetfairReadDegradationError(
                "error_code must be canonical uppercase text"
            )
        if self.request_uuid is not None:
            if not isinstance(self.request_uuid, str) or not self.request_uuid:
                raise BetfairReadDegradationError(
                    "request_uuid must be a non-empty string when set"
                )
            if self.request_uuid != self.request_uuid.strip():
                raise BetfairReadDegradationError(
                    "request_uuid must not contain surrounding whitespace"
                )

    @property
    def api_family(self) -> str:
        return "BETTING" if self.operation in _BETTING_READ_ONLY_OPERATIONS else "ACCOUNTS"

    @property
    def documented_for_api_family(self) -> bool:
        return self.error_code in _family_error_actions_for(self.operation)

    @property
    def documented_for_operation(self) -> bool:
        return self.error_code in _error_actions_for(self.operation)

    @property
    def action(self) -> ReadRecoveryAction:
        return _error_actions_for(self.operation).get(
            self.error_code, ReadRecoveryAction.DO_NOT_RETRY
        )

    @property
    def automatic_repeat_allowed(self) -> bool:
        """Whether a controller may consider repeating this read after its own gate.

        This is deliberately false for reauthentication/config/request-repair classes.
        RETRY_WITH_BACKOFF is semantically repeat-safe only because this class accepts
        read-only operations. This property does not prove provider error origin, choose
        delay/timing, or bypass an external retry/rate-limit controller.
        """

        return self.action is ReadRecoveryAction.RETRY_WITH_BACKOFF

    @property
    def requires_new_session(self) -> bool:
        return self.action is ReadRecoveryAction.REAUTHENTICATE

    @property
    def request_must_change(self) -> bool:
        return self.action is ReadRecoveryAction.REPAIR_REQUEST

    @property
    def evidence_payload(self) -> dict[str, object]:
        return {
            "provider": "BETFAIR",
            "api_family": self.api_family,
            "operation": self.operation,
            "error_code": self.error_code,
            "request_uuid": self.request_uuid,
            "documented_for_api_family": self.documented_for_api_family,
            "action": self.action.value,
            "automatic_repeat_allowed": self.automatic_repeat_allowed,
            "requires_new_session": self.requires_new_session,
            "request_must_change": self.request_must_change,
            "read_only": True,
            "transport_performed": False,
            "provider_error_origin_verified": False,
            "response_completeness_proven": False,
            "execution_authorized": False,
        }

    @property
    def evidence_id(self) -> str:
        encoded = json.dumps(
            self.evidence_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
