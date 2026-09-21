"""Fail-closed Betfair read-only degradation classification.

This module classifies provider errors for read-only API operations.  It deliberately
does not perform retries, login, transport I/O, or any money-moving reconciliation.
Write operations are rejected so the safe-to-repeat semantics here cannot leak into
place/cancel/update/replace execution paths.
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


_READ_ONLY_OPERATIONS = frozenset(
    {
        "listMarketCatalogue",
        "listMarketBook",
        "listRunnerBook",
        "listCurrentOrders",
        "listClearedOrders",
        "listMarketProfitAndLoss",
        "getAccountDetails",
        "getAccountFunds",
    }
)

# Provider-documented Betting/Accounts APINGException categories.  The output is
# intentionally a disposition, not a sleep duration or an executable retry command.
_ERROR_ACTIONS: dict[str, ReadRecoveryAction] = {
    "INVALID_SESSION_INFORMATION": ReadRecoveryAction.REAUTHENTICATE,
    "NO_SESSION": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
    "NO_APP_KEY": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
    "INVALID_APP_KEY": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
    "SUBSCRIPTION_EXPIRED": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
    "INVALID_SUBSCRIPTION_TOKEN": ReadRecoveryAction.FIX_CREDENTIALS_OR_CONFIG,
    "INVALID_INPUT_DATA": ReadRecoveryAction.REPAIR_REQUEST,
    "TOO_MUCH_DATA": ReadRecoveryAction.REPAIR_REQUEST,
    "TOO_MANY_REQUESTS": ReadRecoveryAction.RETRY_WITH_BACKOFF,
    "SERVICE_BUSY": ReadRecoveryAction.RETRY_WITH_BACKOFF,
    "TIMEOUT_ERROR": ReadRecoveryAction.RETRY_WITH_BACKOFF,
    "UNEXPECTED_ERROR": ReadRecoveryAction.RETRY_WITH_BACKOFF,
}


@dataclass(frozen=True, slots=True)
class BetfairReadDegradation:
    """Deterministic evidence for one failed read-only provider call."""

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
        if self.error_code != self.error_code.strip() or self.error_code.upper() != self.error_code:
            raise BetfairReadDegradationError("error_code must be canonical uppercase text")
        if self.request_uuid is not None:
            if not isinstance(self.request_uuid, str) or not self.request_uuid:
                raise BetfairReadDegradationError("request_uuid must be a non-empty string when set")
            if self.request_uuid != self.request_uuid.strip():
                raise BetfairReadDegradationError("request_uuid must not contain surrounding whitespace")

    @property
    def action(self) -> ReadRecoveryAction:
        return _ERROR_ACTIONS.get(self.error_code, ReadRecoveryAction.DO_NOT_RETRY)

    @property
    def automatic_repeat_allowed(self) -> bool:
        """Whether a controller may *consider* repeating this read after its own gate.

        This is deliberately false for reauthentication/config/request-repair classes.
        RETRY_WITH_BACKOFF is safe only because this class accepts read-only operations.
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
            "operation": self.operation,
            "error_code": self.error_code,
            "request_uuid": self.request_uuid,
            "action": self.action.value,
            "automatic_repeat_allowed": self.automatic_repeat_allowed,
            "requires_new_session": self.requires_new_session,
            "request_must_change": self.request_must_change,
            "read_only": True,
            "transport_performed": False,
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
