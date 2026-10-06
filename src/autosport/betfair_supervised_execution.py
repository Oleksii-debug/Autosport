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
from hmac import compare_digest as _hmac_compare_digest, digest as _hmac_digest
import json
from pathlib import Path
from secrets import token_bytes as _token_bytes
from typing import Callable, Mapping, Sequence

from .betfair_account_readonly import (
    BETTING_JSON_RPC_ENDPOINT,
    BetfairExecutionReadbackEnvelope,
    BetfairHttpTransport,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
    UrllibBetfairHttpTransport,
    _execution_provider_network_dispatch_is_current,
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
    EventType,
    ExecutionAction,
    ExecutionStateError,
    ExternalAcknowledgement,
    RealExecutionLedger,
)
from . import supervised_execution as _supervised_execution_runtime
from .supervised_execution import (
    BoundSupervisedExecutionPlan,
    SupervisedApproval,
    _require_approval,
    _require_durable_approval,
    begin_supervised_attempt,
)

PLACE_ORDERS_METHOD = "SportsAPING/v1.0/placeOrders"
WRITE_ADAPTER_ID = "betfair-exchange-jsonrpc-supervised-placeorders"
WRITE_ADAPTER_VERSION = "1"
_MAX_BETFAIR_SELECTION_ID = "9223372036854775807"


class BetfairSupervisedExecutionError(RuntimeError):
    """The bounded provider-write seam rejected an operation."""


class BetfairPlaceOrdersAmbiguous(BetfairSupervisedExecutionError):
    """The provider effect is unknown and requires readback before retry."""


class PlaceOrdersOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
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

    def __post_init__(self) -> None:
        if self.status not in {"SUCCESS", "FAILURE"}:
            raise BetfairSupervisedExecutionError(
                "unsupported Betfair place instruction status"
            )
        if self.error_code is not None:
            _text(self.error_code, "instruction error_code")
        if self.bet_id is not None:
            _text(self.bet_id, "bet_id")
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
            if self.size_matched != 0:
                raise BetfairSupervisedExecutionError(
                    "failed instruction cannot claim a matched stake"
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
    provider_origin_authoritative: bool = False

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
        if type(self.provider_origin_authoritative) is not bool:
            raise BetfairSupervisedExecutionError(
                "provider_origin_authoritative must be bool"
            )

    @property
    def evidence_id(self) -> str:
        return _digest(
            {
                "schema": "autosport.betfair_place_execution_report",
                "schema_version": 2,
                "provider_origin_authoritative": self.provider_origin_authoritative,
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
    raw_selection_id = action.selection_id
    if (
        not raw_selection_id.isascii()
        or not raw_selection_id.isdigit()
        or raw_selection_id.startswith("0")
    ):
        raise BetfairSupervisedExecutionError(
            "Betfair selection_id must be canonical positive integer text"
        )
    if (
        len(raw_selection_id) > len(_MAX_BETFAIR_SELECTION_ID)
        or (
            len(raw_selection_id) == len(_MAX_BETFAIR_SELECTION_ID)
            and raw_selection_id > _MAX_BETFAIR_SELECTION_ID
        )
    ):
        raise BetfairSupervisedExecutionError(
            "Betfair selection_id exceeds signed-long provider domain"
        )
    return int(raw_selection_id)


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
        _before_transport: Callable[[str], None] | None = None,
    ) -> BetfairPlaceExecutionReport:
        _canonical_place_client_preflight(self)
        response_parser = _parse_place_orders_response
        response_parser_code = getattr(response_parser, "__code__", None)
        if response_parser_code is None:
            raise BetfairSupervisedExecutionError(
                "canonical Betfair response parser is unavailable"
            )
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
        action_payload = ExecutionAction.to_dict(action)
        instruction = {
            "selectionId": selection_id,
            "handicap": 0,
            "side": action.side,
            "orderType": "LIMIT",
            "limitOrder": {
                "size": action_payload["requested_stake"],
                "price": action_payload["requested_odds"],
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
        transport = self._transport
        timeout_seconds = self._timeout_seconds
        observed_clock = self._clock
        if _before_transport is not None:
            _before_transport(request_sha256)

        canonical_transport = type(transport) is UrllibBetfairHttpTransport
        canonical_transport_post = (
            UrllibBetfairHttpTransport.__dict__.get("post")
            if canonical_transport
            else None
        )
        transport_namespace = (
            object.__getattribute__(transport, "__dict__")
            if canonical_transport
            else None
        )
        origin_candidate = (
            canonical_transport
            and type(transport_namespace) is dict
            and "post" not in transport_namespace
            and callable(canonical_transport_post)
            and getattr(canonical_transport_post, "__code__", None) is not None
            and _execution_provider_network_dispatch_is_current()
        )
        try:
            if canonical_transport:
                if not origin_candidate:
                    raise BetfairPlaceOrdersAmbiguous(
                        "canonical placeOrders transport dispatch changed; "
                        "authoritative readback required"
                    )
                # Call the exact class dispatch captured after the durable SUBMITTED
                # boundary.  Never re-enter instance attribute lookup here: a
                # same-process interleaving must not install transport.post after
                # preflight and fabricate provider-origin response authority.
                payload = canonical_transport_post(
                    transport,
                    BETTING_JSON_RPC_ENDPOINT,
                    headers=headers,
                    body=body,
                    timeout_seconds=timeout_seconds,
                )
            else:
                payload = transport.post(
                    BETTING_JSON_RPC_ENDPOINT,
                    headers=headers,
                    body=body,
                    timeout_seconds=timeout_seconds,
                )
        except BetfairPlaceOrdersAmbiguous:
            raise
        except (BetfairReadOnlyError, TimeoutError, OSError) as exc:
            raise BetfairPlaceOrdersAmbiguous(
                "placeOrders transport outcome is ambiguous; "
                "authoritative readback required"
            ) from exc
        if not isinstance(payload, bytes):
            raise BetfairPlaceOrdersAmbiguous(
                "placeOrders transport returned non-bytes response"
            )
        # Revalidate the entire client dependency binding after provider return.
        # A response can only carry provider-origin authority when its local
        # observation timestamp is also product-owned; caller/test clocks remain
        # valid for structural responses but cannot mint durable provider truth.
        _canonical_place_client_preflight(self)
        if (
            _parse_place_orders_response is not response_parser
            or getattr(response_parser, "__code__", None) is not response_parser_code
        ):
            raise BetfairPlaceOrdersAmbiguous(
                "canonical placeOrders response parser changed; "
                "authoritative readback required"
            )
        observation_clock_authoritative = observed_clock is _now
        provider_origin_authoritative = (
            origin_candidate
            and observation_clock_authoritative
            and type(self._transport) is UrllibBetfairHttpTransport
            and self._transport is transport
            and object.__getattribute__(transport, "__dict__")
            is transport_namespace
            and "post" not in transport_namespace
            and UrllibBetfairHttpTransport.__dict__.get("post")
            is canonical_transport_post
            and _execution_provider_network_dispatch_is_current()
        )
        return response_parser(
            payload,
            request_id=request_id,
            request_sha256=request_sha256,
            action=action,
            provider_order_ref=provider_ref,
            observed_at=observed_clock(),
            provider_origin_authoritative=provider_origin_authoritative,
        )


def _build_canonical_place_action_dispatch():
    """Capture provider-write dispatch and authenticate each client construction."""

    client_type = BetfairSupervisedPlaceOrdersClient
    object_getattribute = object.__getattribute__
    original_init = client_type.__dict__.get("__init__")
    if not callable(original_init) or getattr(original_init, "__code__", None) is None:
        raise RuntimeError("canonical Betfair client constructor is unavailable")
    original_init_code = original_init.__code__
    original_init_defaults = original_init.__defaults__
    original_init_kwdefaults = original_init.__kwdefaults__
    original_init_kwdefault_items = (
        tuple(original_init_kwdefaults.items())
        if original_init_kwdefaults is not None
        else ()
    )

    canonical_credentials_type = BetfairSessionCredentials
    canonical_gate_type = BetfairSupervisedExecutionGate
    canonical_default_transport_type = UrllibBetfairHttpTransport
    canonical_profile_type = BookmakerCapabilityProfile
    canonical_bound_type = BoundSupervisedExecutionPlan
    canonical_action_type = ExecutionAction
    canonical_ledger_type = RealExecutionLedger
    canonical_workspace_lock_type = WorkspaceEconomicLock
    canonical_instruction_report_type = BetfairInstructionReport
    canonical_execution_report_type = BetfairPlaceExecutionReport

    canonical_profile_require = canonical_profile_type.__dict__.get("require")
    canonical_profile_id_descriptor = canonical_profile_type.__dict__.get("profile_id")
    canonical_profile_id_getter = getattr(
        canonical_profile_id_descriptor,
        "fget",
        None,
    )
    canonical_action_to_dict = canonical_action_type.__dict__.get("to_dict")
    canonical_instruction_post_init = canonical_instruction_report_type.__dict__.get(
        "__post_init__"
    )
    canonical_execution_report_post_init = canonical_execution_report_type.__dict__.get(
        "__post_init__"
    )
    canonical_evidence_id_descriptor = canonical_execution_report_type.__dict__.get(
        "evidence_id"
    )
    canonical_evidence_id_getter = getattr(
        canonical_evidence_id_descriptor,
        "fget",
        None,
    )
    canonical_trusted_now = _supervised_execution_runtime._trusted_now
    canonical_workspace_lock_enter = canonical_workspace_lock_type.__dict__.get(
        "__enter__"
    )
    canonical_workspace_lock_exit = canonical_workspace_lock_type.__dict__.get(
        "__exit__"
    )
    canonical_profile_require_code = getattr(
        canonical_profile_require,
        "__code__",
        None,
    )
    canonical_profile_id_getter_code = getattr(
        canonical_profile_id_getter,
        "__code__",
        None,
    )
    canonical_action_to_dict_code = getattr(
        canonical_action_to_dict,
        "__code__",
        None,
    )
    canonical_instruction_post_init_code = getattr(
        canonical_instruction_post_init,
        "__code__",
        None,
    )
    canonical_execution_report_post_init_code = getattr(
        canonical_execution_report_post_init,
        "__code__",
        None,
    )
    canonical_evidence_id_getter_code = getattr(
        canonical_evidence_id_getter,
        "__code__",
        None,
    )
    canonical_trusted_now_code = getattr(
        canonical_trusted_now,
        "__code__",
        None,
    )
    canonical_workspace_lock_enter_code = getattr(
        canonical_workspace_lock_enter,
        "__code__",
        None,
    )
    canonical_workspace_lock_exit_code = getattr(
        canonical_workspace_lock_exit,
        "__code__",
        None,
    )
    bound_method_names = ("action_for", "profile_for", "constraint_for")
    bound_methods = tuple(
        (
            name,
            canonical_bound_type.__dict__.get(name),
            getattr(canonical_bound_type.__dict__.get(name), "__code__", None),
        )
        for name in bound_method_names
    )
    ledger_method_names = (
        "_append",
        "_mutate",
        "verified_execution_view",
        "bind_provider_order_reference",
        "provider_order_reference",
        "bind_provider_evidence",
        "mark_unknown",
        "acknowledge",
        "attempt_state",
        "saga",
    )
    ledger_methods = tuple(
        (
            name,
            canonical_ledger_type.__dict__.get(name),
            getattr(canonical_ledger_type.__dict__.get(name), "__code__", None),
        )
        for name in ledger_method_names
    )
    if (
        not callable(canonical_profile_require)
        or canonical_profile_require_code is None
        or not callable(canonical_profile_id_getter)
        or canonical_profile_id_getter_code is None
        or not callable(canonical_action_to_dict)
        or canonical_action_to_dict_code is None
        or not callable(canonical_instruction_post_init)
        or canonical_instruction_post_init_code is None
        or not callable(canonical_execution_report_post_init)
        or canonical_execution_report_post_init_code is None
        or not callable(canonical_evidence_id_getter)
        or canonical_evidence_id_getter_code is None
        or not callable(canonical_trusted_now)
        or canonical_trusted_now_code is None
        or not callable(canonical_workspace_lock_enter)
        or canonical_workspace_lock_enter_code is None
        or not callable(canonical_workspace_lock_exit)
        or canonical_workspace_lock_exit_code is None
        or any(method is None or code is None for _, method, code in bound_methods)
        or any(method is None or code is None for _, method, code in ledger_methods)
    ):
        raise RuntimeError("canonical Betfair execution class authority is unavailable")

    profile_field_names = (
        "venue_id",
        "account_id",
        "adapter_id",
        "adapter_version",
        "profile_version",
        "facts",
        "observed_at",
        "source_ref",
        "source_payload_sha256",
    )
    profile_field_descriptors = tuple(
        (name, canonical_profile_type.__dict__.get(name))
        for name in profile_field_names
    )
    action_field_names = (
        "action_id",
        "bookmaker_id",
        "account_id",
        "event_id",
        "market_id",
        "selection_id",
        "side",
        "requested_odds",
        "requested_stake",
        "quote_id",
        "quote_observed_at",
        "expires_at",
    )
    action_field_descriptors = tuple(
        (name, canonical_action_type.__dict__.get(name))
        for name in action_field_names
    )
    instruction_field_names = (
        "status",
        "error_code",
        "bet_id",
        "placed_date",
        "average_price_matched",
        "size_matched",
    )
    instruction_field_descriptors = tuple(
        (name, canonical_instruction_report_type.__dict__.get(name))
        for name in instruction_field_names
    )
    execution_report_field_names = (
        "bookmaker_id",
        "account_id",
        "action_id",
        "provider_order_ref",
        "market_id",
        "request_id",
        "request_sha256",
        "response_sha256",
        "observed_at",
        "status",
        "error_code",
        "instruction",
        "provider_origin_authoritative",
    )
    execution_report_field_descriptors = tuple(
        (name, canonical_execution_report_type.__dict__.get(name))
        for name in execution_report_field_names
    )
    canonical_default_clock = _now
    canonical_default_clock_code = getattr(canonical_default_clock, "__code__", None)
    canonical_client_getattribute = client_type.__getattribute__
    canonical_credentials_getattribute = canonical_credentials_type.__getattribute__
    canonical_gate_getattribute = canonical_gate_type.__getattribute__
    canonical_gate_method = canonical_gate_type.__dict__.get("require")
    gate_owner_authority_method = canonical_gate_type.__dict__.get(
        "_require_current_owner_authority"
    )
    if (
        not callable(canonical_gate_method)
        or getattr(canonical_gate_method, "__code__", None) is None
        or not callable(gate_owner_authority_method)
        or getattr(gate_owner_authority_method, "__code__", None) is None
    ):
        raise RuntimeError("canonical Betfair gate authority is unavailable")
    canonical_gate_method_code = canonical_gate_method.__code__
    gate_owner_authority_code = gate_owner_authority_method.__code__
    gate_field_names = (
        "enabled",
        "bookmaker_id",
        "account_id",
        "profile_sha256",
        "authority_ref",
        "authority_sha256",
        "economic_goal_store",
        "economic_goal_workspace",
    )
    gate_field_descriptors = tuple(
        (name, canonical_gate_type.__dict__.get(name))
        for name in gate_field_names
    )
    credential_field_names = ("application_key", "session_token")
    credential_field_descriptors = tuple(
        (name, canonical_credentials_type.__dict__.get(name))
        for name in credential_field_names
    )

    canonical_bytes = _canonical_bytes
    canonical_bytes_code = canonical_bytes.__code__
    canonical_response_parser = _parse_place_orders_response
    canonical_response_parser_code = getattr(
        canonical_response_parser,
        "__code__",
        None,
    )
    if canonical_response_parser_code is None:
        raise RuntimeError("canonical Betfair response parser is unavailable")
    binding_digest = _hmac_digest
    binding_compare_digest = _hmac_compare_digest
    binding_key = _token_bytes(32)
    if type(binding_key) is not bytes or len(binding_key) != 32:
        raise RuntimeError("Betfair client binding key is unavailable")
    binding_field = "_autosport_provider_write_dependency_proof"

    def init_metadata_current() -> bool:
        current_kwdefaults = original_init.__kwdefaults__
        return (
            original_init.__code__ is original_init_code
            and original_init.__defaults__ is original_init_defaults
            and current_kwdefaults is original_init_kwdefaults
            and (
                current_kwdefaults is None
                or (
                    len(current_kwdefaults) == len(original_init_kwdefault_items)
                    and all(
                        key in current_kwdefaults
                        and current_kwdefaults[key] is value
                        for key, value in original_init_kwdefault_items
                    )
                )
            )
        )

    def static_authority_current() -> bool:
        return (
            init_metadata_current()
            and BetfairSessionCredentials is canonical_credentials_type
            and BetfairSupervisedExecutionGate is canonical_gate_type
            and UrllibBetfairHttpTransport is canonical_default_transport_type
            and BookmakerCapabilityProfile is canonical_profile_type
            and BoundSupervisedExecutionPlan is canonical_bound_type
            and ExecutionAction is canonical_action_type
            and RealExecutionLedger is canonical_ledger_type
            and WorkspaceEconomicLock is canonical_workspace_lock_type
            and BetfairInstructionReport is canonical_instruction_report_type
            and BetfairPlaceExecutionReport is canonical_execution_report_type
            and canonical_profile_type.__dict__.get("require")
            is canonical_profile_require
            and getattr(canonical_profile_require, "__code__", None)
            is canonical_profile_require_code
            and canonical_profile_type.__dict__.get("profile_id")
            is canonical_profile_id_descriptor
            and getattr(canonical_profile_id_descriptor, "fget", None)
            is canonical_profile_id_getter
            and getattr(canonical_profile_id_getter, "__code__", None)
            is canonical_profile_id_getter_code
            and canonical_action_type.__dict__.get("to_dict")
            is canonical_action_to_dict
            and getattr(canonical_action_to_dict, "__code__", None)
            is canonical_action_to_dict_code
            and canonical_instruction_report_type.__dict__.get("__post_init__")
            is canonical_instruction_post_init
            and getattr(canonical_instruction_post_init, "__code__", None)
            is canonical_instruction_post_init_code
            and canonical_execution_report_type.__dict__.get("__post_init__")
            is canonical_execution_report_post_init
            and getattr(canonical_execution_report_post_init, "__code__", None)
            is canonical_execution_report_post_init_code
            and canonical_execution_report_type.__dict__.get("evidence_id")
            is canonical_evidence_id_descriptor
            and getattr(canonical_evidence_id_descriptor, "fget", None)
            is canonical_evidence_id_getter
            and getattr(canonical_evidence_id_getter, "__code__", None)
            is canonical_evidence_id_getter_code
            and _supervised_execution_runtime._trusted_now is canonical_trusted_now
            and getattr(canonical_trusted_now, "__code__", None)
            is canonical_trusted_now_code
            and canonical_workspace_lock_type.__dict__.get("__enter__")
            is canonical_workspace_lock_enter
            and getattr(canonical_workspace_lock_enter, "__code__", None)
            is canonical_workspace_lock_enter_code
            and canonical_workspace_lock_type.__dict__.get("__exit__")
            is canonical_workspace_lock_exit
            and getattr(canonical_workspace_lock_exit, "__code__", None)
            is canonical_workspace_lock_exit_code
            and all(
                canonical_bound_type.__dict__.get(name) is method
                and getattr(method, "__code__", None) is code
                for name, method, code in bound_methods
            )
            and all(
                canonical_ledger_type.__dict__.get(name) is method
                and getattr(method, "__code__", None) is code
                for name, method, code in ledger_methods
            )
            and all(
                canonical_profile_type.__dict__.get(name) is descriptor
                for name, descriptor in profile_field_descriptors
            )
            and all(
                canonical_action_type.__dict__.get(name) is descriptor
                for name, descriptor in action_field_descriptors
            )
            and all(
                canonical_instruction_report_type.__dict__.get(name) is descriptor
                for name, descriptor in instruction_field_descriptors
            )
            and all(
                canonical_execution_report_type.__dict__.get(name) is descriptor
                for name, descriptor in execution_report_field_descriptors
            )
            and _now is canonical_default_clock
            and getattr(canonical_default_clock, "__code__", None)
            is canonical_default_clock_code
            and client_type.__getattribute__ is canonical_client_getattribute
            and canonical_credentials_type.__getattribute__
            is canonical_credentials_getattribute
            and canonical_gate_type.__getattribute__ is canonical_gate_getattribute
            and canonical_gate_type.__dict__.get("require") is canonical_gate_method
            and getattr(canonical_gate_method, "__code__", None)
            is canonical_gate_method_code
            and canonical_gate_type.__dict__.get("_require_current_owner_authority")
            is gate_owner_authority_method
            and getattr(gate_owner_authority_method, "__code__", None)
            is gate_owner_authority_code
            and all(
                canonical_gate_type.__dict__.get(name) is descriptor
                for name, descriptor in gate_field_descriptors
            )
            and all(
                canonical_credentials_type.__dict__.get(name) is descriptor
                for name, descriptor in credential_field_descriptors
            )
            and _canonical_bytes is canonical_bytes
            and canonical_bytes.__code__ is canonical_bytes_code
            and _parse_place_orders_response is canonical_response_parser
            and getattr(canonical_response_parser, "__code__", None)
            is canonical_response_parser_code
        )

    def instance_dict(value: object) -> dict[str, object] | None:
        try:
            namespace = object_getattribute(value, "__dict__")
        except AttributeError:
            return None
        return namespace if type(namespace) is dict else None

    def dependency_payload(
        client: BetfairSupervisedPlaceOrdersClient,
        namespace: dict[str, object],
    ) -> bytes:
        gate = namespace.get("_gate")
        transport = namespace.get("_transport")
        credentials = namespace.get("_credentials")
        clock = namespace.get("_clock")
        timeout_seconds = namespace.get("_timeout_seconds")
        if (
            type(gate) is not canonical_gate_type
            or type(credentials) is not canonical_credentials_type
            or transport is None
            or not callable(clock)
            or type(timeout_seconds) is not float
            or timeout_seconds <= 0
        ):
            raise BetfairSupervisedExecutionError(
                "Betfair client dependencies were not initialized canonically"
            )
        transport_type = type(transport)
        transport_method = transport_type.__dict__.get("post")
        transport_getattribute = transport_type.__getattribute__
        if (
            not callable(transport_method)
            or getattr(transport_method, "__code__", None) is None
        ):
            raise BetfairSupervisedExecutionError(
                "Betfair client dependency dispatch is unavailable"
            )
        gate_namespace = instance_dict(gate)
        transport_namespace = instance_dict(transport)
        if (
            gate_namespace is not None
            and "require" in gate_namespace
        ) or (
            transport_namespace is not None
            and "post" in transport_namespace
        ):
            raise BetfairSupervisedExecutionError(
                "Betfair client dependency dispatch is shadowed"
            )
        gate_state = [
            object_getattribute(gate, name)
            for name in gate_field_names
        ]
        store = gate_state[6]
        workspace = gate_state[7]
        canonical_gate_state = [
            gate_state[0],
            gate_state[1],
            gate_state[2],
            gate_state[3],
            gate_state[4],
            gate_state[5],
            0 if store is None else id(store),
            None if workspace is None else str(workspace),
        ]
        credential_state = [
            object_getattribute(credentials, name)
            for name in credential_field_names
        ]
        clock_code = getattr(clock, "__code__", None)
        payload = {
            "schema": "autosport.betfair_provider_write_client_binding",
            "schema_version": 1,
            "client_identity": id(client),
            "gate_identity": id(gate),
            "gate_state": canonical_gate_state,
            "gate_method_identity": id(canonical_gate_method),
            "gate_method_code_identity": id(canonical_gate_method_code),
            "gate_owner_method_identity": id(gate_owner_authority_method),
            "gate_owner_method_code_identity": id(gate_owner_authority_code),
            "credentials_identity": id(credentials),
            "credential_state": credential_state,
            "transport_identity": id(transport),
            "transport_type_identity": id(transport_type),
            "transport_method_identity": id(transport_method),
            "transport_method_code_identity": id(transport_method.__code__),
            "transport_getattribute_identity": id(transport_getattribute),
            "clock_identity": id(clock),
            "clock_code_identity": 0 if clock_code is None else id(clock_code),
            "timeout_seconds": timeout_seconds,
        }
        return canonical_bytes(payload)

    def dependency_proof(
        client: BetfairSupervisedPlaceOrdersClient,
        namespace: dict[str, object],
    ) -> bytes:
        return binding_digest(
            binding_key,
            dependency_payload(client, namespace),
            "sha256",
        )

    def sealed_init(self, *args, **kwargs):
        if (
            client_type.__dict__.get("__init__") is not sealed_init
            or not static_authority_current()
        ):
            raise BetfairSupervisedExecutionError(
                "canonical Betfair client constructor authority changed"
            )
        original_init(self, *args, **kwargs)
        if not static_authority_current():
            raise BetfairSupervisedExecutionError(
                "canonical Betfair client constructor authority changed"
            )
        namespace = object_getattribute(self, "__dict__")
        if type(namespace) is not dict:
            raise BetfairSupervisedExecutionError(
                "Betfair client instance state is unavailable"
            )
        if type(namespace.get("_credentials")) is not canonical_credentials_type:
            raise BetfairSupervisedExecutionError(
                "Betfair client credentials must be exact canonical credentials"
            )
        if type(namespace.get("_gate")) is not canonical_gate_type:
            raise BetfairSupervisedExecutionError(
                "Betfair client gate must be exact canonical gate"
            )
        proof = dependency_proof(self, namespace)
        namespace[binding_field] = proof

    client_type.__init__ = sealed_init

    place_action = client_type.__dict__.get("place_action")
    if not callable(place_action) or getattr(place_action, "__code__", None) is None:
        raise RuntimeError("canonical Betfair place_action dispatch is unavailable")
    place_action_code = place_action.__code__
    sealed_init_code = sealed_init.__code__

    def preflight(client: BetfairSupervisedPlaceOrdersClient) -> None:
        if (
            type(client) is not client_type
            or client_type.__getattribute__ is not canonical_client_getattribute
            or client_type.__dict__.get("__init__") is not sealed_init
            or sealed_init.__code__ is not sealed_init_code
            or client_type.__dict__.get("place_action") is not place_action
            or place_action.__code__ is not place_action_code
            or not static_authority_current()
        ):
            raise BetfairSupervisedExecutionError(
                "canonical Betfair client dispatch changed"
            )
        namespace = object_getattribute(client, "__dict__")
        if (
            type(namespace) is not dict
            or "place_action" in namespace
            or binding_field not in namespace
        ):
            raise BetfairSupervisedExecutionError(
                "Betfair client has no canonical dependency binding"
            )
        proof = namespace.get(binding_field)
        if type(proof) is not bytes or len(proof) != 32:
            raise BetfairSupervisedExecutionError(
                "Betfair client dependency binding changed"
            )
        expected = dependency_proof(client, namespace)
        if not binding_compare_digest(proof, expected):
            raise BetfairSupervisedExecutionError(
                "Betfair client dependency binding changed"
            )

    def dispatch(
        client: BetfairSupervisedPlaceOrdersClient,
        action: ExecutionAction,
        *,
        profile: BookmakerCapabilityProfile,
        bound: BoundSupervisedExecutionPlan,
        provider_order_ref: str,
        execution_workspace: Path,
        _before_transport: Callable[[str], None] | None,
    ) -> BetfairPlaceExecutionReport:
        preflight(client)
        return place_action(
            client,
            action,
            profile=profile,
            bound=bound,
            provider_order_ref=provider_order_ref,
            execution_workspace=execution_workspace,
            _before_transport=_before_transport,
        )

    return dispatch, preflight


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


def _parse_place_orders_response(
    payload: bytes,
    *,
    request_id: int,
    request_sha256: str,
    action: ExecutionAction,
    provider_order_ref: str,
    observed_at: str,
    provider_origin_authoritative: bool = False,
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
    if (
        envelope.get("jsonrpc") != "2.0"
        or envelope.get("id") != request_id
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
    if status not in {
        "SUCCESS",
        "FAILURE",
        "PROCESSED_WITH_ERRORS",
    }:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders report has unsupported/nonterminal status"
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
    try:
        echoed_selection = int(echoed.get("selectionId"))
    except (TypeError, ValueError) as exc:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders echoed selection is malformed"
        ) from exc
    try:
        exact_echo = (
            str(echoed_selection) == action.selection_id
            and echoed.get("side") == action.side
            and echoed.get("orderType") == "LIMIT"
            and _positive_decimal(
                limit.get("price"),
                "echoed price",
            )
            == action.requested_odds
            and _positive_decimal(
                limit.get("size"),
                "echoed size",
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
            placed_date=_optional_provider_text(
                item.get("placedDate"),
                "placedDate",
            ),
            average_price_matched=_nonnegative_decimal(
                item.get("averagePriceMatched", 0),
                "averagePriceMatched",
            ),
            size_matched=_nonnegative_decimal(
                item.get("sizeMatched", 0),
                "sizeMatched",
            ),
        )
    except BetfairSupervisedExecutionError as exc:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders instruction report is internally inconsistent"
        ) from exc
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
    if status == "FAILURE" and result.get("errorCode") is None:
        raise BetfairPlaceOrdersAmbiguous(
            "failed placeOrders execution requires provider errorCode"
        )
    if instruction.size_matched > action.requested_stake:
        raise BetfairPlaceOrdersAmbiguous(
            "placeOrders report matched stake exceeds requested stake"
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
        and instruction.average_price_matched < action.requested_odds
    ):
        raise BetfairPlaceOrdersAmbiguous(
            "matched placeOrders report average price is below requested "
            "BACK LIMIT price"
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
        provider_origin_authoritative=provider_origin_authoritative,
    )


_canonical_place_action_dispatch, _canonical_place_client_preflight = (
    _build_canonical_place_action_dispatch()
)
del _build_canonical_place_action_dispatch


def _report_outcome(
    report: BetfairPlaceExecutionReport,
    action: ExecutionAction,
) -> PlaceOrdersOutcome:
    instruction = report.instruction
    if instruction.status == "FAILURE":
        return PlaceOrdersOutcome.REJECTED
    if instruction.size_matched == action.requested_stake:
        return PlaceOrdersOutcome.ACCEPTED
    if instruction.size_matched > 0:
        return PlaceOrdersOutcome.PARTIAL
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

    if type(client) is not BetfairReadOnlyClient:
        raise TypeError("client must be exact BetfairReadOnlyClient")
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
    capture = client.read_execution_readback(
        action_id=action.action_id,
        provider_order_ref=provider_order_ref,
        market_id=action.market_id,
        page_size=page_size,
        max_pages=max_pages,
    )
    try:
        capture.assert_authoritative()
    except BetfairReadOnlyError as exc:
        raise BetfairSupervisedExecutionError(
            "supervised Betfair readback lacks authenticated product origin"
        ) from exc
    return capture



def _place_action_with_final_durable_authority(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    action: ExecutionAction,
    attempt_id: str,
    profile: BookmakerCapabilityProfile,
    client: BetfairSupervisedPlaceOrdersClient,
    provider_order_ref: str,
    execution_workspace: Path,
) -> BetfairPlaceExecutionReport:
    """Hold durable approval stable and fsync SUBMITTED at the provider boundary."""

    def operation() -> BetfairPlaceExecutionReport:
        view = ledger.verified_execution_view(bound.execution_plan.plan_id)
        if view.plan_fingerprint != bound.execution_plan.fingerprint:
            raise ExecutionStateError(
                "durable execution-plan fingerprint changed before final send"
            )
        attempts = [
            item
            for item in view.attempts
            if item.attempt.attempt_id == attempt_id
        ]
        if len(attempts) != 1:
            raise ExecutionStateError(
                "final supervised send requires one durable attempt"
            )
        attempt = attempts[0]
        if (
            attempt.state is not AttemptState.RESERVED
            or attempt.attempt.action_id != action.action_id
            or attempt.action != action
        ):
            raise ExecutionStateError(
                "final supervised send requires the exact RESERVED action"
            )
        if attempt.provider_order_ref != provider_order_ref:
            raise ExecutionStateError(
                "final supervised send provider order reference drifted"
            )

        _require_durable_approval(ledger, bound, approval)
        _validate_betfair_place_action(action)
        _canonical_place_client_preflight(client)
        client._gate.require(
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

        submitted = False
        submitted_request_sha256: str | None = None

        def authorize_and_submit(request_sha256: str) -> None:
            nonlocal submitted, submitted_request_sha256
            _sha(request_sha256, "submitted_request_sha256")
            send_at = _supervised_execution_runtime._trusted_now()
            _require_approval(bound, approval, send_at)
            _require_durable_approval(ledger, bound, approval)
            if _time(send_at, "final send time") < _time(
                attempt.attempt.reserved_at,
                "attempt reserved_at",
            ):
                raise BetfairSupervisedExecutionError(
                    "final send time precedes attempt reservation"
                )
            if _time(send_at, "final send time") >= _time(
                action.expires_at,
                "action expires_at",
            ):
                raise BetfairSupervisedExecutionError(
                    "placeOrders final send is at/after quote expiry"
                )
            ledger._append(
                EventType.ATTEMPT_SUBMITTED,
                bound.execution_plan.plan_id,
                action.action_id,
                attempt_id,
                {
                    "submitted_at": send_at,
                    "request_sha256": request_sha256,
                },
            )
            submitted_request_sha256 = request_sha256
            submitted = True

        try:
            report = _canonical_place_action_dispatch(
                client,
                action,
                profile=profile,
                bound=bound,
                provider_order_ref=provider_ref,
                execution_workspace=execution_workspace,
                _before_transport=authorize_and_submit,
            )
            if (
                submitted_request_sha256 is None
                or report.request_sha256 != submitted_request_sha256
            ):
                raise BetfairSupervisedExecutionError(
                    "placeOrders report request digest mismatches durable submission"
                )
            return report
        except BetfairPlaceOrdersAmbiguous:
            raise
        except Exception as exc:
            if not submitted:
                raise
            raise BetfairPlaceOrdersAmbiguous(
                "placeOrders dispatch failed after durable submission; "
                "authoritative readback required"
            ) from exc

    return ledger._mutate(operation)

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

    if type(ledger) is not RealExecutionLedger:
        raise TypeError("ledger must be exact RealExecutionLedger")
    if type(bound) is not BoundSupervisedExecutionPlan:
        raise TypeError("bound must be exact BoundSupervisedExecutionPlan")
    if type(profile) is not BookmakerCapabilityProfile:
        raise TypeError("profile must be exact BookmakerCapabilityProfile")
    if type(client) is not BetfairSupervisedPlaceOrdersClient:
        raise TypeError(
            "client must be exact BetfairSupervisedPlaceOrdersClient"
        )
    _canonical_place_client_preflight(client)
    action = bound.action_for(action_id)
    _validate_betfair_place_action(action)
    # API compatibility only: execution-authority time is product-owned.
    _ = clock
    trusted_now = _supervised_execution_runtime._trusted_now
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
        _canonical_place_client_preflight(client)
        client._gate.require(
            action=action,
            profile=profile,
            bound=bound,
            execution_workspace=execution_workspace,
        )
        begin_supervised_attempt(
            ledger,
            bound,
            approval,
            action_id=action_id,
            attempt_id=attempt_id,
        )
        provider_order_ref = ledger.bind_provider_order_reference(
            attempt_id=attempt_id,
            provider_id=action.bookmaker_id,
        )
        try:
            report = _place_action_with_final_durable_authority(
                ledger,
                bound,
                approval,
                action=action,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                provider_order_ref=provider_order_ref,
                execution_workspace=execution_workspace,
            )
        except BetfairPlaceOrdersAmbiguous:
            ledger.mark_unknown(
                attempt_id,
                reason=(
                    "betfair_placeOrders_ambiguous_effect_"
                    "requires_readback"
                ),
                observed_at=trusted_now(),
            )
            return BetfairSupervisedExecutionResult(
                PlaceOrdersOutcome.UNKNOWN,
                attempt_id,
                ledger.attempt_state(attempt_id),
                None,
                None,
            )

    if not report.provider_origin_authoritative:
        ledger.mark_unknown(
            attempt_id,
            reason=(
                "betfair_placeOrders_non_authoritative_response_"
                "requires_authenticated_readback"
            ),
            observed_at=trusted_now(),
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
        request_sha256=report.request_sha256,
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

    if outcome is PlaceOrdersOutcome.REJECTED:
        receipt = receipt or provider_order_ref
        acknowledgement = ExternalAcknowledgement(
            attempt_id=attempt_id,
            external_receipt_id=receipt,
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
