"""Bounded supervised Betfair placeOrders execution-report vertical.

This module intentionally does not extend BetfairReadOnlyClient. The only provider
write method reachable here is SportsAPING/v1.0/placeOrders, and it is disabled
unless an explicit scoped supervised gate is enabled. The canonical
RealExecutionLedger remains the sole execution-effect authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .betfair_account_readonly import (
    BETTING_JSON_RPC_ENDPOINT,
    BetfairExecutionReadbackEnvelope,
    BetfairHttpTransport,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
    UrllibBetfairHttpTransport,
)
from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityError,
    BookmakerCapabilityProfile,
)
from .economic_goal import AutomationLevel, EconomicGoalContractError
from .economic_goal_provenance import provenance_for
from .economic_goal_store import EconomicGoalStore
from .workspace_lock import WorkspaceEconomicLock
from .real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    ExecutionAction,
    ExternalAcknowledgement,
    RealExecutionLedger,
)
from .supervised_execution import (
    BoundSupervisedExecutionPlan,
    SupervisedApproval,
    begin_supervised_attempt,
)

PLACE_ORDERS_METHOD = "SportsAPING/v1.0/placeOrders"
WRITE_ADAPTER_ID = "betfair-exchange-jsonrpc-supervised-placeorders"
WRITE_ADAPTER_VERSION = "1"

# Terminal provider-effect authority must not depend on caller-rebindable method
# dispatch.  These product-owned implementations are captured once and are used
# non-virtually by execute_betfair_supervised_action().
_CANONICAL_URLLIB_BETFAIR_HTTP_POST = UrllibBetfairHttpTransport.post
_CANONICAL_URLLIB_BETFAIR_HTTP_POST_CODE = (
    _CANONICAL_URLLIB_BETFAIR_HTTP_POST.__code__
)
_CANONICAL_URLLIB_BETFAIR_REQUEST = (
    _CANONICAL_URLLIB_BETFAIR_HTTP_POST.__globals__["Request"]
)
_CANONICAL_URLLIB_BETFAIR_URLOPEN = (
    _CANONICAL_URLLIB_BETFAIR_HTTP_POST.__globals__["urlopen"]
)


class BetfairSupervisedExecutionError(RuntimeError):
    """The bounded provider-write seam rejected an operation."""


class BetfairPlaceOrdersAmbiguous(BetfairSupervisedExecutionError):
    """The provider effect is unknown and requires readback before retry."""


class PlaceOrdersOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    PLACED_UNMATCHED = "PLACED_UNMATCHED"
    UNKNOWN = "UNKNOWN"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetfairSupervisedExecutionError(
            f"{name} must be non-empty canonical text"
        )
    return value


def _sha(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise BetfairSupervisedExecutionError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return raw


def _time(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairSupervisedExecutionError(
            f"{name} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairSupervisedExecutionError(
            f"{name} must be timezone-aware"
        )
    return parsed


def _positive_decimal(value: object, name: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BetfairSupervisedExecutionError(
            f"{name} must be exact Decimal-compatible"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise BetfairSupervisedExecutionError(
            f"{name} must be finite and > 0"
        )
    return parsed


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BetfairSupervisedExecutionError(
            f"{name} must be exact Decimal-compatible"
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise BetfairSupervisedExecutionError(
            f"{name} must be finite and >= 0"
        )
    return parsed


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairSupervisedExecutionError(
            "value is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _decode_provider_json(payload: bytes) -> object:
    """Decode provider JSON without silently accepting ambiguous object keys."""

    def reject_duplicate_pairs(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise BetfairPlaceOrdersAmbiguous(
                    "placeOrders response contains duplicate JSON object keys"
                )
            decoded[key] = value
        return decoded

    def reject_constant(value: str) -> object:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders response contains non-finite JSON number"
        )

    try:
        return json.loads(
            payload.decode("utf-8"),
            parse_float=Decimal,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders response is not valid UTF-8 JSON"
        ) from exc


@dataclass(frozen=True, slots=True)
class BetfairSupervisedExecutionGate:
    """Externally supplied enablement; disabled is the safe default."""

    enabled: bool = False
    bookmaker_id: str = "betfair"
    account_id: str | None = None
    profile_sha256: str | None = None
    authority_ref: str | None = None
    authority_sha256: str | None = None
    economic_goal_store: EconomicGoalStore | None = None
    economic_goal_workspace: Path | None = field(
        init=False,
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise BetfairSupervisedExecutionError(
                "gate enabled must be bool"
            )
        _text(self.bookmaker_id, "bookmaker_id")
        if not self.enabled:
            return
        if self.account_id is None or self.profile_sha256 is None:
            raise BetfairSupervisedExecutionError(
                "enabled gate requires exact account and capability-profile binding"
            )
        _text(self.account_id, "account_id")
        _sha(self.profile_sha256, "profile_sha256")
        if self.authority_ref is None or self.authority_sha256 is None:
            raise BetfairSupervisedExecutionError(
                "enabled gate requires external authority evidence"
            )
        _text(self.authority_ref, "authority_ref")
        _sha(self.authority_sha256, "authority_sha256")
        if not isinstance(self.economic_goal_store, EconomicGoalStore):
            raise BetfairSupervisedExecutionError(
                "enabled gate requires canonical EconomicGoalStore authority"
            )
        workspace = self.economic_goal_store.workspace.resolve()
        if self.economic_goal_store.path.resolve() != (
            workspace / EconomicGoalStore.FILE_NAME
        ):
            raise BetfairSupervisedExecutionError(
                "enabled gate requires canonical EconomicGoalStore path"
            )
        object.__setattr__(self, "economic_goal_workspace", workspace)

    @classmethod
    def from_economic_goal_store(
        cls,
        store: EconomicGoalStore,
        *,
        bookmaker_id: str,
        account_id: str,
        profile_sha256: str,
    ) -> "BetfairSupervisedExecutionGate":
        """Bind enablement to the exact current durable owner contract."""

        if not isinstance(store, EconomicGoalStore):
            raise BetfairSupervisedExecutionError(
                "store must be canonical EconomicGoalStore"
            )
        try:
            goal = store.load()
        except EconomicGoalContractError as exc:
            raise BetfairSupervisedExecutionError(
                "cannot load durable owner execution authority"
            ) from exc
        goal_sha256 = provenance_for(goal).contract_sha256
        return cls(
            enabled=True,
            bookmaker_id=bookmaker_id,
            account_id=account_id,
            profile_sha256=profile_sha256,
            authority_ref=(
                f"economic-goal:{goal.goal_id}:revision:{goal.revision}"
            ),
            authority_sha256=goal_sha256,
            economic_goal_store=store,
        )

    def _require_current_owner_authority(
        self,
        *,
        action: ExecutionAction,
        bound: BoundSupervisedExecutionPlan,
        execution_workspace: Path,
    ) -> None:
        if not isinstance(execution_workspace, Path):
            raise BetfairSupervisedExecutionError(
                "execution workspace must be a canonical Path"
            )
        canonical_workspace = execution_workspace.resolve()
        if self.economic_goal_workspace != canonical_workspace:
            raise BetfairSupervisedExecutionError(
                "owner authority is not bound to the trusted execution workspace"
            )
        store = EconomicGoalStore(canonical_workspace)
        try:
            goal = store.load()
        except EconomicGoalContractError as exc:
            raise BetfairSupervisedExecutionError(
                "cannot load durable owner execution authority"
            ) from exc
        goal_sha256 = provenance_for(goal).contract_sha256
        expected_ref = (
            f"economic-goal:{goal.goal_id}:revision:{goal.revision}"
        )
        if goal.automation_level < AutomationLevel.SUPERVISED_EXECUTION:
            raise BetfairSupervisedExecutionError(
                "owner authority does not permit supervised execution"
            )
        if goal.emergency_stop:
            raise BetfairSupervisedExecutionError(
                "owner emergency STOP is active"
            )
        if action.bookmaker_id in goal.blocked_providers:
            raise BetfairSupervisedExecutionError(
                "owner authority blocks this provider"
            )
        if action.market_id in goal.blocked_markets:
            raise BetfairSupervisedExecutionError(
                "owner authority blocks this market"
            )
        if (
            bound.constraint_for(action.action_id).max_slippage_fraction
            > goal.max_execution_slippage_fraction
        ):
            raise BetfairSupervisedExecutionError(
                "execution slippage exceeds current owner authority"
            )
        if (
            self.authority_ref != expected_ref
            or self.authority_sha256 != goal_sha256
            or bound.economic_goal_contract_sha256 != goal_sha256
        ):
            raise BetfairSupervisedExecutionError(
                "durable owner authority does not match bound execution plan"
            )

    def require(
        self,
        *,
        action: ExecutionAction,
        profile: BookmakerCapabilityProfile,
        bound: BoundSupervisedExecutionPlan,
        execution_workspace: Path,
    ) -> None:
        if not self.enabled:
            raise BetfairSupervisedExecutionError(
                "supervised Betfair placeOrders gate is disabled"
            )
        self._require_current_owner_authority(
            action=action,
            bound=bound,
            execution_workspace=execution_workspace,
        )
        if (
            action.bookmaker_id != self.bookmaker_id
            or action.account_id != self.account_id
            or profile.venue_id != action.bookmaker_id
            or profile.account_id != action.account_id
            or profile.profile_id != self.profile_sha256
        ):
            raise BetfairSupervisedExecutionError(
                "enabled gate does not bind exact execution action/profile"
            )
        binding = bound.profile_for(action.bookmaker_id, action.account_id)
        if (
            binding.profile_sha256 != profile.profile_id
            or binding.adapter_id != profile.adapter_id
            or binding.adapter_version != profile.adapter_version
            or binding.profile_version != profile.profile_version
        ):
            raise BetfairSupervisedExecutionError(
                "write profile does not match bound supervised plan"
            )
        try:
            profile.require(BookmakerCapability.BET_READBACK)
            profile.require(BookmakerCapability.PLACE_BET)
        except BookmakerCapabilityError as exc:
            raise BetfairSupervisedExecutionError(
                "bound profile must explicitly support BET_READBACK and PLACE_BET"
            ) from exc


@dataclass(frozen=True, slots=True)
class BetfairInstructionReport:
    status: str
    error_code: str | None
    bet_id: str | None
    placed_date: str | None
    average_price_matched: Decimal
    size_matched: Decimal
    order_status: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"SUCCESS", "FAILURE"}:
            raise BetfairSupervisedExecutionError(
                "unsupported Betfair place instruction status"
            )
        if self.error_code is not None:
            _text(self.error_code, "instruction error_code")
        if self.bet_id is not None:
            _text(self.bet_id, "bet_id")
        if self.order_status is not None:
            _text(self.order_status, "order_status")
            if self.order_status not in {"EXECUTABLE", "EXECUTION_COMPLETE"}:
                raise BetfairSupervisedExecutionError(
                    "unsupported Betfair order_status"
                )
        if self.placed_date is not None:
            _time(self.placed_date, "placed_date")
        object.__setattr__(
            self,
            "average_price_matched",
            _nonnegative_decimal(
                self.average_price_matched,
                "average_price_matched",
            ),
        )
        object.__setattr__(
            self,
            "size_matched",
            _nonnegative_decimal(self.size_matched, "size_matched"),
        )
        if self.status == "FAILURE":
            if self.error_code is None:
                raise BetfairSupervisedExecutionError(
                    "failed instruction requires provider error_code"
                )
            if self.size_matched != 0 or self.average_price_matched != 0:
                raise BetfairSupervisedExecutionError(
                    "failed instruction cannot claim matched economics"
                )
        elif self.error_code is not None:
            raise BetfairSupervisedExecutionError(
                "successful instruction cannot claim provider error_code"
            )


@dataclass(frozen=True, slots=True)
class BetfairPlaceExecutionReport:
    bookmaker_id: str
    account_id: str
    action_id: str
    provider_order_ref: str
    market_id: str
    request_id: int
    request_sha256: str
    response_sha256: str
    observed_at: str
    status: str
    error_code: str | None
    instruction: BetfairInstructionReport

    def __post_init__(self) -> None:
        for name in (
            "bookmaker_id",
            "account_id",
            "action_id",
            "provider_order_ref",
            "market_id",
        ):
            _text(getattr(self, name), name)
        if len(self.provider_order_ref) > 32 or any(
            character not in "0123456789abcdef"
            for character in self.provider_order_ref
        ):
            raise BetfairSupervisedExecutionError(
                "provider_order_ref must be <=32 lowercase hex characters"
            )
        if type(self.request_id) is not int or self.request_id < 1:
            raise BetfairSupervisedExecutionError(
                "request_id must be positive int"
            )
        _sha(self.request_sha256, "request_sha256")
        _sha(self.response_sha256, "response_sha256")
        _time(self.observed_at, "observed_at")
        if self.status not in {
            "SUCCESS",
            "FAILURE",
            "PROCESSED_WITH_ERRORS",
        }:
            raise BetfairSupervisedExecutionError(
                "unsupported Betfair execution report status"
            )
        if self.error_code is not None:
            _text(self.error_code, "execution error_code")
        if not isinstance(self.instruction, BetfairInstructionReport):
            raise BetfairSupervisedExecutionError(
                "instruction must be BetfairInstructionReport"
            )

    @property
    def evidence_id(self) -> str:
        return _digest(
            {
                "schema": "autosport.betfair_place_execution_report",
                "schema_version": 1,
                "bookmaker_id": self.bookmaker_id,
                "account_id": self.account_id,
                "action_id": self.action_id,
                "provider_order_ref": self.provider_order_ref,
                "market_id": self.market_id,
                "request_id": self.request_id,
                "request_sha256": self.request_sha256,
                "response_sha256": self.response_sha256,
                "observed_at": self.observed_at,
                "status": self.status,
                "error_code": self.error_code,
                "instruction": {
                    "status": self.instruction.status,
                    "error_code": self.instruction.error_code,
                    "bet_id": self.instruction.bet_id,
                    "order_status": self.instruction.order_status,
                    "placed_date": self.instruction.placed_date,
                    "average_price_matched": str(
                        self.instruction.average_price_matched
                    ),
                    "size_matched": str(
                        self.instruction.size_matched
                    ),
                },
            }
        )


@dataclass(frozen=True, slots=True)
class BetfairSupervisedExecutionResult:
    outcome: PlaceOrdersOutcome
    attempt_id: str
    attempt_state: AttemptState
    evidence_id: str | None
    external_receipt_id: str | None


def _validate_betfair_place_action(action: ExecutionAction) -> int:
    """Validate deterministic Betfair action shape before any durable attempt."""

    if not isinstance(action, ExecutionAction):
        raise BetfairSupervisedExecutionError(
            "action must be canonical ExecutionAction"
        )
    if action.side != "BACK":
        raise BetfairSupervisedExecutionError(
            "Betfair supervised write seam currently supports BACK only"
        )
    try:
        selection_id = int(action.selection_id)
    except (TypeError, ValueError) as exc:
        raise BetfairSupervisedExecutionError(
            "Betfair selection_id must be canonical positive integer text"
        ) from exc
    if str(selection_id) != action.selection_id or selection_id <= 0:
        raise BetfairSupervisedExecutionError(
            "Betfair selection_id must be canonical positive integer text"
        )
    return selection_id


class BetfairSupervisedPlaceOrdersClient:
    """Action-specific placeOrders client; no arbitrary write RPC is exposed."""

    def __init__(
        self,
        credentials: BetfairSessionCredentials,
        *,
        gate: BetfairSupervisedExecutionGate | None = None,
        transport: BetfairHttpTransport | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(credentials, BetfairSessionCredentials):
            raise TypeError(
                "credentials must be BetfairSessionCredentials"
            )
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self._credentials = credentials
        self._gate = gate or BetfairSupervisedExecutionGate()
        self._transport = transport or UrllibBetfairHttpTransport()
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock or _now
        self._request_id = 0

    def __repr__(self) -> str:
        return (
            "BetfairSupervisedPlaceOrdersClient("
            f"adapter_id={WRITE_ADAPTER_ID!r}, "
            f"adapter_version={WRITE_ADAPTER_VERSION!r}, "
            f"enabled={self._gate.enabled!r})"
        )

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def place_action(
        self,
        action: ExecutionAction,
        *,
        profile: BookmakerCapabilityProfile,
        bound: BoundSupervisedExecutionPlan,
        provider_order_ref: str,
        execution_workspace: Path,
        _transport_post: Callable[..., bytes] | None = None,
        _response_parser: Callable[..., BetfairPlaceExecutionReport] | None = None,
    ) -> BetfairPlaceExecutionReport:
        selection_id = _validate_betfair_place_action(action)
        self._gate.require(
            action=action,
            profile=profile,
            bound=bound,
            execution_workspace=execution_workspace,
        )
        provider_ref = _text(provider_order_ref, "provider_order_ref")
        if len(provider_ref) > 32 or any(
            character not in "0123456789abcdef"
            for character in provider_ref
        ):
            raise BetfairSupervisedExecutionError(
                "provider_order_ref must be <=32 lowercase hex characters"
            )

        request_id = self._next_request_id()
        customer_ref = sha256(
            f"placeOrders:{provider_ref}".encode("utf-8")
        ).hexdigest()[:32]
        instruction = {
            "selectionId": selection_id,
            "handicap": 0,
            "side": action.side,
            "orderType": "LIMIT",
            "limitOrder": {
                "size": str(action.requested_stake),
                "price": str(action.requested_odds),
                "persistenceType": "LAPSE",
            },
            "customerOrderRef": provider_ref,
        }
        params = {
            "marketId": action.market_id,
            "instructions": [instruction],
            "customerRef": customer_ref,
            "async": False,
        }
        envelope = {
            "jsonrpc": "2.0",
            "method": PLACE_ORDERS_METHOD,
            "params": params,
            "id": request_id,
        }
        body = _canonical_bytes(envelope)
        request_sha256 = sha256(body).hexdigest()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Application": self._credentials.application_key,
            "X-Authentication": self._credentials.session_token,
        }
        try:
            if _transport_post is None:
                payload = self._transport.post(
                    BETTING_JSON_RPC_ENDPOINT,
                    headers=headers,
                    body=body,
                    timeout_seconds=self._timeout_seconds,
                )
            else:
                payload = _transport_post(
                    self._transport,
                    BETTING_JSON_RPC_ENDPOINT,
                    headers=headers,
                    body=body,
                    timeout_seconds=self._timeout_seconds,
                )
        except (BetfairReadOnlyError, TimeoutError, OSError) as exc:
            raise BetfairPlaceOrdersAmbiguous(
                "placeOrders transport outcome is ambiguous; "
                "authoritative readback required"
            ) from exc
        if not isinstance(payload, bytes):
            raise BetfairPlaceOrdersAmbiguous(
                "placeOrders transport returned non-bytes response"
            )
        parser = _parse_place_orders_response if _response_parser is None else _response_parser
        return parser(
            payload,
            request_id=request_id,
            request_sha256=request_sha256,
            action=action,
            provider_order_ref=provider_ref,
            observed_at=self._clock(),
        )


_CANONICAL_BETFAIR_PLACE_ACTION = BetfairSupervisedPlaceOrdersClient.place_action
_CANONICAL_BETFAIR_PLACE_ACTION_CODE = _CANONICAL_BETFAIR_PLACE_ACTION.__code__


def _mapping(
    value: object,
    field: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BetfairPlaceOrdersAmbiguous(
            f"{field} is not a JSON object"
        )
    return value


def _sequence(
    value: object,
    field: str,
) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise BetfairPlaceOrdersAmbiguous(
            f"{field} is not a JSON array"
        )
    return value


def _optional_provider_text(
    value: object,
    field: str,
) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value:
        raise BetfairPlaceOrdersAmbiguous(
            f"{field} is malformed"
        )
    return value


def _provider_response_decimal(
    value: object,
    field: str,
    *,
    positive: bool,
) -> Decimal:
    """Accept only provider JSON numeric wire values, never coercible strings/bools."""

    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise BetfairPlaceOrdersAmbiguous(
            f"{field} must be a JSON number"
        )
    parsed = Decimal(value)
    if not parsed.is_finite() or (parsed <= 0 if positive else parsed < 0):
        constraint = "> 0" if positive else ">= 0"
        raise BetfairPlaceOrdersAmbiguous(
            f"{field} must be finite and {constraint}"
        )
    return parsed


def _parse_place_orders_response(
    payload: bytes,
    *,
    request_id: int,
    request_sha256: str,
    action: ExecutionAction,
    provider_order_ref: str,
    observed_at: str,
) -> BetfairPlaceExecutionReport:
    decoded = _decode_provider_json(payload)
    if isinstance(decoded, list):
        if len(decoded) != 1:
            raise BetfairPlaceOrdersAmbiguous(
                "placeOrders response must contain exactly one "
                "JSON-RPC envelope"
            )
        decoded = decoded[0]
    envelope = _mapping(decoded, "placeOrders response")
    response_id = envelope.get("id")
    response_id_matches = False
    if (
        not isinstance(response_id, bool)
        and isinstance(response_id, (int, Decimal))
    ):
        response_id_number = Decimal(response_id)
        response_id_matches = (
            response_id_number.is_finite()
            and response_id_number == response_id_number.to_integral_value()
            and int(response_id_number) == request_id
        )
    if (
        envelope.get("jsonrpc") != "2.0"
        or not response_id_matches
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders response does not bind exact JSON-RPC request"
        )
    if envelope.get("error") is not None:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders JSON-RPC error is not proof of zero external effect"
        )
    result = _mapping(
        envelope.get("result"),
        "placeOrders result",
    )
    if result.get("marketId") != action.market_id:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders report market does not match execution action"
        )
    status = result.get("status")
    if status not in {"SUCCESS", "FAILURE", "PROCESSED_WITH_ERRORS"}:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders report has unsupported/nonterminal status"
        )
    if status == "SUCCESS" and result.get("errorCode") is not None:
        raise BetfairPlaceOrdersAmbiguous(
            "successful placeOrders execution must not include errorCode"
        )
    reports = _sequence(
        result.get("instructionReports"),
        "instructionReports",
    )
    if len(reports) != 1:
        raise BetfairPlaceOrdersAmbiguous(
            "action-specific placeOrders requires exactly "
            "one instruction report"
        )
    item = _mapping(
        reports[0],
        "instruction report",
    )
    echoed = _mapping(
        item.get("instruction"),
        "echoed instruction",
    )
    limit = _mapping(
        echoed.get("limitOrder"),
        "echoed limitOrder",
    )
    if "customerOrderRef" in echoed:
        echoed_customer_order_ref = _optional_provider_text(
            echoed.get("customerOrderRef"),
            "echoed customerOrderRef",
        )
        if echoed_customer_order_ref != provider_order_ref:
            raise BetfairPlaceOrdersAmbiguous(
                "placeOrders echoed customerOrderRef does not bind exact request"
            )
    raw_selection = echoed.get("selectionId")
    if (
        isinstance(raw_selection, bool)
        or not isinstance(raw_selection, (int, Decimal))
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders echoed selection is malformed"
        )
    selection_number = Decimal(raw_selection)
    if (
        not selection_number.is_finite()
        or selection_number <= 0
        or selection_number != selection_number.to_integral_value()
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders echoed selection is malformed"
        )
    echoed_selection = int(selection_number)

    raw_handicap = echoed.get("handicap")
    handicap_matches = (
        not isinstance(raw_handicap, bool)
        and isinstance(raw_handicap, (int, Decimal))
        and Decimal(raw_handicap).is_finite()
        and Decimal(raw_handicap) == 0
    )
    try:
        exact_echo = (
            str(echoed_selection) == action.selection_id
            and echoed.get("side") == action.side
            and echoed.get("orderType") == "LIMIT"
            and handicap_matches
            and limit.get("persistenceType") == "LAPSE"
            and set(limit) == {"size", "price", "persistenceType"}
            and _provider_response_decimal(
                limit.get("price"),
                "echoed price",
                positive=True,
            )
            == action.requested_odds
            and _provider_response_decimal(
                limit.get("size"),
                "echoed size",
                positive=True,
            )
            == action.requested_stake
        )
    except BetfairSupervisedExecutionError as exc:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders instruction report is malformed"
        ) from exc
    if not exact_echo:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders instruction report does not bind exact action"
        )
    instruction_status = item.get("status")
    if instruction_status not in {"SUCCESS", "FAILURE"}:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders instruction status is unsupported/nonterminal"
        )
    if (
        status == "PROCESSED_WITH_ERRORS"
        and instruction_status != "FAILURE"
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders PROCESSED_WITH_ERRORS requires "
            "a failed sole instruction"
        )
    if "sizeMatched" not in item:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders instruction report omits sizeMatched; "
            "external effect is ambiguous"
        )
    try:
        instruction = BetfairInstructionReport(
            status=instruction_status,
            error_code=_optional_provider_text(
                item.get("errorCode"),
                "instruction errorCode",
            ),
            bet_id=_optional_provider_text(
                item.get("betId"),
                "betId",
            ),
            order_status=_optional_provider_text(
                item.get("orderStatus"),
                "orderStatus",
            ),
            placed_date=_optional_provider_text(
                item.get("placedDate"),
                "placedDate",
            ),
            average_price_matched=_provider_response_decimal(
                item.get("averagePriceMatched", 0),
                "averagePriceMatched",
                positive=False,
            ),
            size_matched=_provider_response_decimal(
                item["sizeMatched"],
                "sizeMatched",
                positive=False,
            ),
        )
    except BetfairSupervisedExecutionError as exc:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders instruction report is internally inconsistent"
        ) from exc
    if (
        instruction.bet_id is not None
        and instruction.placed_date is None
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "synchronous placeOrders report with betId omits placedDate"
        )
    if (
        (status == "SUCCESS" and instruction.status != "SUCCESS")
        or (
            status == "FAILURE"
            and instruction.status != "FAILURE"
        )
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders execution/instruction statuses conflict"
        )
    if (
        instruction.status == "FAILURE"
        and (
            instruction.size_matched != 0
            or instruction.average_price_matched != 0
        )
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "failed placeOrders instruction contradicts matched execution economics"
        )
    if (
        instruction.status == "FAILURE"
        and instruction.order_status == "EXECUTABLE"
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "failed placeOrders instruction contradicts live EXECUTABLE order state"
        )
    if status == "FAILURE" and result.get("errorCode") is None:
        raise BetfairPlaceOrdersAmbiguous(
            "failed placeOrders execution requires provider errorCode"
        )
    if instruction.size_matched > action.requested_stake:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders report matched stake exceeds requested stake"
        )
    if (
        instruction.size_matched == 0
        and instruction.average_price_matched != 0
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "zero matched stake cannot claim positive average price"
        )
    if (
        instruction.order_status == "EXECUTABLE"
        and instruction.size_matched == action.requested_stake
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders EXECUTABLE order cannot already be fully matched"
        )
    if (
        instruction.size_matched > 0
        and instruction.average_price_matched <= 0
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "matched placeOrders report lacks positive average price"
        )
    if (
        instruction.size_matched > 0
        and action.side == "BACK"
        and instruction.average_price_matched < action.requested_odds
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "matched BACK price is worse than requested limit"
        )
    if (
        instruction.size_matched > 0
        and action.side == "LAY"
        and instruction.average_price_matched > action.requested_odds
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "matched LAY price is worse than requested limit"
        )
    return BetfairPlaceExecutionReport(
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        action_id=action.action_id,
        provider_order_ref=provider_order_ref,
        market_id=action.market_id,
        request_id=request_id,
        request_sha256=request_sha256,
        response_sha256=sha256(payload).hexdigest(),
        observed_at=observed_at,
        status=status,
        error_code=_optional_provider_text(
            result.get("errorCode"),
            "execution errorCode",
        ),
        instruction=instruction,
    )


_CANONICAL_PARSE_PLACE_ORDERS_RESPONSE = _parse_place_orders_response
_CANONICAL_PARSE_PLACE_ORDERS_RESPONSE_CODE = (
    _CANONICAL_PARSE_PLACE_ORDERS_RESPONSE.__code__
)


def _report_outcome(
    report: BetfairPlaceExecutionReport,
    action: ExecutionAction,
) -> PlaceOrdersOutcome:
    instruction = report.instruction
    if instruction.status == "FAILURE":
        # A terminal REJECTED fact needs the narrow provider rejection shape
        # that is actually qualified for this synchronous one-instruction seam.
        # Availability/matcher/regulator failures and unknown/future code
        # combinations do not prove zero external effect, even if a betId-like
        # identity is present; keep those UNKNOWN until canonical readback.
        if (
            instruction.bet_id is None
            or report.error_code != "BET_ACTION_ERROR"
            or instruction.error_code != "BET_TAKEN_OR_LAPSED"
            or instruction.order_status != "EXECUTION_COMPLETE"
        ):
            return PlaceOrdersOutcome.UNKNOWN
        return PlaceOrdersOutcome.REJECTED
    if instruction.size_matched == action.requested_stake:
        return PlaceOrdersOutcome.ACCEPTED
    if instruction.size_matched > 0:
        # Standard LIMIT partial fills can leave a live unmatched remainder.
        # Only explicit provider proof that no unmatched part remains makes
        # the immediate report terminal PARTIAL.
        if instruction.order_status == "EXECUTION_COMPLETE":
            return PlaceOrdersOutcome.PARTIAL
        return PlaceOrdersOutcome.UNKNOWN
    if (
        instruction.bet_id is not None
        and instruction.order_status == "EXECUTABLE"
    ):
        # A provider order identity alone does not prove a live unmatched
        # remainder. EXECUTION_COMPLETE explicitly means there is no
        # remaining unmatched portion; missing status is also insufficient.
        return PlaceOrdersOutcome.PLACED_UNMATCHED
    return PlaceOrdersOutcome.UNKNOWN


def read_betfair_supervised_action_readback(
    client: BetfairReadOnlyClient,
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    *,
    attempt_id: str,
    page_size: int = 1000,
    max_pages: int = 100,
) -> BetfairExecutionReadbackEnvelope:
    """Query the exact durable provider order reference used by placeOrders."""

    if not isinstance(client, BetfairReadOnlyClient):
        raise TypeError("client must be BetfairReadOnlyClient")
    saga = ledger.saga(bound.execution_plan.plan_id)
    action_id = saga.attempt_action_ids.get(attempt_id)
    if action_id is None:
        raise BetfairSupervisedExecutionError(
            "attempt does not belong to bound supervised plan"
        )
    action = bound.action_for(action_id)
    provider_order_ref = ledger.provider_order_reference(
        attempt_id=attempt_id,
        provider_id=action.bookmaker_id,
    )
    if provider_order_ref is None:
        raise BetfairSupervisedExecutionError(
            "attempt lacks durable provider order reference"
        )
    return client.read_execution_readback(
        action_id=action.action_id,
        provider_order_ref=provider_order_ref,
        market_id=action.market_id,
        page_size=page_size,
        max_pages=max_pages,
    )


def execute_betfair_supervised_action(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    action_id: str,
    attempt_id: str,
    profile: BookmakerCapabilityProfile,
    client: BetfairSupervisedPlaceOrdersClient,
    clock: Callable[[], str] | None = None,
) -> BetfairSupervisedExecutionResult:
    """Reserve -> submit -> placeOrders -> report -> canonical ledger transition."""

    if not isinstance(ledger, RealExecutionLedger):
        raise TypeError("ledger must be RealExecutionLedger")
    if type(client) is not BetfairSupervisedPlaceOrdersClient:
        raise TypeError(
            "client must be exact BetfairSupervisedPlaceOrdersClient"
        )
    action = bound.action_for(action_id)
    _validate_betfair_place_action(action)
    now = clock or _now
    execution_workspace = ledger.path.parent.resolve()

    # Serialize the current owner authority through the actual provider-write
    # boundary, not just through local ledger preparation. EconomicGoalStore
    # successors use this same writer lock, so either a tighter owner revision
    # becomes durable first and the initial gate rejects before any attempt
    # mutation, or this already-authorized bounded call reaches placeOrders
    # before that successor can publish. The provider client still re-reads the
    # canonical owner contract immediately before transport while the fence is
    # held. This prevents a known local authority denial from being mislabeled
    # as provider-effect uncertainty.
    with WorkspaceEconomicLock(execution_workspace):
        client._gate.require(
            action=action,
            profile=profile,
            bound=bound,
            execution_workspace=execution_workspace,
        )
        if (
            type(client._transport) is not UrllibBetfairHttpTransport
            or BetfairSupervisedPlaceOrdersClient.place_action
            is not _CANONICAL_BETFAIR_PLACE_ACTION
            or _CANONICAL_BETFAIR_PLACE_ACTION.__code__
            is not _CANONICAL_BETFAIR_PLACE_ACTION_CODE
            or UrllibBetfairHttpTransport.post
            is not _CANONICAL_URLLIB_BETFAIR_HTTP_POST
            or _CANONICAL_URLLIB_BETFAIR_HTTP_POST.__code__
            is not _CANONICAL_URLLIB_BETFAIR_HTTP_POST_CODE
            or _CANONICAL_URLLIB_BETFAIR_HTTP_POST.__globals__.get("Request")
            is not _CANONICAL_URLLIB_BETFAIR_REQUEST
            or _CANONICAL_URLLIB_BETFAIR_HTTP_POST.__globals__.get("urlopen")
            is not _CANONICAL_URLLIB_BETFAIR_URLOPEN
            or _parse_place_orders_response
            is not _CANONICAL_PARSE_PLACE_ORDERS_RESPONSE
            or _CANONICAL_PARSE_PLACE_ORDERS_RESPONSE.__code__
            is not _CANONICAL_PARSE_PLACE_ORDERS_RESPONSE_CODE
        ):
            raise BetfairSupervisedExecutionError(
                "terminal Betfair execution requires canonical client, transport, parser, and code authority"
            )
        begin_supervised_attempt(
            ledger,
            bound,
            approval,
            action_id=action_id,
            attempt_id=attempt_id,
        )
        attempt_state = ledger.attempt_state(attempt_id)
        if attempt_state is AttemptState.SUBMITTED:
            # SUBMITTED is a durable uncertainty boundary. Re-entering the
            # same attempt after a crash must never transmit placeOrders again:
            # the previous process may have reached Betfair before dying.
            provider_order_ref = ledger.provider_order_reference(
                attempt_id=attempt_id,
                provider_id=action.bookmaker_id,
            )
            if provider_order_ref is None:
                raise BetfairSupervisedExecutionError(
                    "submitted Betfair attempt lacks durable provider order reference"
                )
            ledger.mark_unknown(
                attempt_id,
                reason=(
                    "betfair_placeOrders_existing_submitted_"
                    "requires_readback"
                ),
                observed_at=now(),
            )
            return BetfairSupervisedExecutionResult(
                PlaceOrdersOutcome.UNKNOWN,
                attempt_id,
                ledger.attempt_state(attempt_id),
                None,
                None,
            )
        provider_order_ref = ledger.bind_provider_order_reference(
            attempt_id=attempt_id,
            provider_id=action.bookmaker_id,
        )
        ledger.mark_submitted(
            attempt_id,
            submitted_at=now(),
        )
        try:
            report = _CANONICAL_BETFAIR_PLACE_ACTION(
                client,
                action,
                profile=profile,
                bound=bound,
                provider_order_ref=provider_order_ref,
                execution_workspace=execution_workspace,
                _transport_post=_CANONICAL_URLLIB_BETFAIR_HTTP_POST,
                _response_parser=_CANONICAL_PARSE_PLACE_ORDERS_RESPONSE,
            )
        except (
            BetfairPlaceOrdersAmbiguous,
            BetfairSupervisedExecutionError,
        ):
            ledger.mark_unknown(
                attempt_id,
                reason=(
                    "betfair_placeOrders_ambiguous_effect_"
                    "requires_readback"
                ),
                observed_at=now(),
            )
            return BetfairSupervisedExecutionResult(
                PlaceOrdersOutcome.UNKNOWN,
                attempt_id,
                ledger.attempt_state(attempt_id),
                None,
                None,
            )
        if (
            BetfairSupervisedPlaceOrdersClient.place_action
            is not _CANONICAL_BETFAIR_PLACE_ACTION
            or _CANONICAL_BETFAIR_PLACE_ACTION.__code__
            is not _CANONICAL_BETFAIR_PLACE_ACTION_CODE
            or UrllibBetfairHttpTransport.post
            is not _CANONICAL_URLLIB_BETFAIR_HTTP_POST
            or _CANONICAL_URLLIB_BETFAIR_HTTP_POST.__code__
            is not _CANONICAL_URLLIB_BETFAIR_HTTP_POST_CODE
            or _CANONICAL_URLLIB_BETFAIR_HTTP_POST.__globals__.get("Request")
            is not _CANONICAL_URLLIB_BETFAIR_REQUEST
            or _CANONICAL_URLLIB_BETFAIR_HTTP_POST.__globals__.get("urlopen")
            is not _CANONICAL_URLLIB_BETFAIR_URLOPEN
            or _parse_place_orders_response
            is not _CANONICAL_PARSE_PLACE_ORDERS_RESPONSE
            or _CANONICAL_PARSE_PLACE_ORDERS_RESPONSE.__code__
            is not _CANONICAL_PARSE_PLACE_ORDERS_RESPONSE_CODE
        ):
            ledger.mark_unknown(
                attempt_id,
                reason=(
                    "betfair_placeOrders_code_authority_changed_"
                    "requires_readback"
                ),
                observed_at=now(),
            )
            return BetfairSupervisedExecutionResult(
                PlaceOrdersOutcome.UNKNOWN,
                attempt_id,
                ledger.attempt_state(attempt_id),
                None,
                None,
            )

    evidence_id = report.evidence_id
    ledger.bind_provider_evidence(
        attempt_id=attempt_id,
        evidence_id=evidence_id,
        observed_at=report.observed_at,
        source=f"betfair:placeOrders:{report.response_sha256}",
    )
    outcome = _report_outcome(report, action)
    receipt = report.instruction.bet_id

    if outcome is PlaceOrdersOutcome.UNKNOWN:
        ledger.mark_unknown(
            attempt_id,
            reason="betfair_placeOrders_report_requires_readback",
            observed_at=report.observed_at,
        )
        return BetfairSupervisedExecutionResult(
            outcome,
            attempt_id,
            ledger.attempt_state(attempt_id),
            evidence_id,
            receipt,
        )

    if outcome is PlaceOrdersOutcome.PLACED_UNMATCHED:
        if receipt is None:
            raise BetfairSupervisedExecutionError(
                "placed-unmatched provider order requires betId identity"
            )
        ledger.mark_unknown(
            attempt_id,
            reason=(
                "betfair_placeOrders_known_unmatched_order_"
                "requires_readback"
            ),
            observed_at=report.observed_at,
        )
        return BetfairSupervisedExecutionResult(
            outcome,
            attempt_id,
            ledger.attempt_state(attempt_id),
            evidence_id,
            receipt,
        )

    acknowledgement_receipt = receipt
    if outcome is PlaceOrdersOutcome.REJECTED:
        # Rejection acknowledgement must carry a real provider identity.
        # Never launder Autosport's internal evidence hash into the external
        # receipt namespace.
        if acknowledgement_receipt is None:
            ledger.mark_unknown(
                attempt_id,
                reason=(
                    "betfair_placeOrders_rejection_missing_receipt_"
                    "requires_readback"
                ),
                observed_at=report.observed_at,
            )
            return BetfairSupervisedExecutionResult(
                PlaceOrdersOutcome.UNKNOWN,
                attempt_id,
                ledger.attempt_state(attempt_id),
                evidence_id,
                None,
            )
        acknowledgement = ExternalAcknowledgement(
            attempt_id=attempt_id,
            external_receipt_id=acknowledgement_receipt,
            status=AcknowledgementStatus.REJECTED,
            acknowledged_at=report.observed_at,
        )
    else:
        if receipt is None:
            ledger.mark_unknown(
                attempt_id,
                reason=(
                    "betfair_placeOrders_matched_report_"
                    "missing_receipt_requires_readback"
                ),
                observed_at=report.observed_at,
            )
            return BetfairSupervisedExecutionResult(
                PlaceOrdersOutcome.UNKNOWN,
                attempt_id,
                ledger.attempt_state(attempt_id),
                evidence_id,
                None,
            )
        acknowledgement = ExternalAcknowledgement(
            attempt_id=attempt_id,
            external_receipt_id=receipt,
            status=(
                AcknowledgementStatus.ACCEPTED
                if outcome is PlaceOrdersOutcome.ACCEPTED
                else AcknowledgementStatus.PARTIAL
            ),
            acknowledged_at=report.observed_at,
            accepted_odds=report.instruction.average_price_matched,
            accepted_stake=report.instruction.size_matched,
        )
    ledger.acknowledge(acknowledgement)
    return BetfairSupervisedExecutionResult(
        outcome,
        attempt_id,
        ledger.attempt_state(attempt_id),
        evidence_id,
        receipt,
    )