"""Plan/batch retry admission projected onto canonical provider-recovery truth.

Provider-unavailable streak and deadline authority remain owned by
continuous_observation / SourceHealthStore (#1878).  This module never creates a
second provider streak, retry scheduler, timer, or sleep loop.  It only records
which canonical MarketBook batch is blocked after a classified provider failure,
projects a deadline from exact durable provider-health evidence through the
canonical provider-backoff function, and emits explicit non-dispatch gap truth.

Structurally incomplete/transport/protocol/unknown-provider outcomes remain
explicit historical gaps but are not automatically retried by this authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from hashlib import sha256
import json
from threading import RLock
from typing import Mapping, Sequence

from .betfair_marketbook_attempt_history import MarketBookAttemptOutcome
from .betfair_marketbook_batch_plan import MarketBookReadPlan
from .continuous_observation import (
    ContinuousObservationConfig,
    _provider_backoff_seconds,
)
from .ingestion_health import (
    SourceHealthState,
    SourceHealthStore,
    parse_source_timestamp,
)


MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION = (
    "betfair.list-market-book.retry-backoff-projection.v3"
)
_TRANSIENT_PROVIDER_CODES = frozenset(
    {"TOO_MANY_REQUESTS", "SERVICE_BUSY", "TIMEOUT_ERROR"}
)
_LOCAL_NON_DISPATCH_OUTCOMES = frozenset(
    {
        MarketBookAttemptOutcome.NOT_DISPATCHED_RATE,
        MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY,
        MarketBookAttemptOutcome.NOT_DISPATCHED_BACKOFF,
    }
)


class MarketBookRetryBackoffError(ValueError):
    """Raised when MarketBook retry projection evidence is noncanonical."""


class MarketBookRetryDisposition(str, Enum):
    READY = "READY"
    BACKOFF = "BACKOFF"
    PROVIDER_RECOVERY_REQUIRED = "PROVIDER_RECOVERY_REQUIRED"
    TERMINAL = "TERMINAL"


_EPOCH_NAIVE = datetime(1970, 1, 1)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MarketBookRetryBackoffError(
            "retry projection evidence must be canonical JSON data"
        ) from exc


def _sha(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_token(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise MarketBookRetryBackoffError(
            f"{name} must be lowercase SHA-256"
        )
    return value


def _source_id(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MarketBookRetryBackoffError(
            "provider_source_id must be a non-empty exact string"
        )
    return value


def _provider_code(value: object, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or value != value.upper()
        or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
            for char in value
        )
    ):
        raise MarketBookRetryBackoffError(
            "provider_error_code must be a canonical uppercase provider token"
        )
    return value


def _utc_microseconds(value: object, name: str) -> int:
    if type(value) is not datetime:
        raise MarketBookRetryBackoffError(
            f"{name} must be an exact datetime"
        )
    if value.tzinfo is None:
        raise MarketBookRetryBackoffError(
            f"{name} must be timezone-aware"
        )
    offset = value.utcoffset()
    if type(offset) is not timedelta:
        raise MarketBookRetryBackoffError(
            f"{name} UTC offset must be an exact timedelta"
        )
    local_naive = value.replace(tzinfo=None)
    utc_naive = local_naive - offset
    delta = utc_naive - _EPOCH_NAIVE
    return (
        delta.days * 86_400 * 1_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


@dataclass(frozen=True, slots=True)
class MarketBookRetryBatchState:
    batch_id: str
    provider_recovery_required: bool
    provider_failure_observed_at_utc_us: int | None
    terminal_failure: bool
    last_outcome: MarketBookAttemptOutcome
    last_provider_error_code: str | None = None

    def __post_init__(self) -> None:
        _validate_batch_state(self)


@dataclass(frozen=True, slots=True)
class MarketBookRetryBackoffState:
    policy_version: str
    plan_id: str
    request_contract_id: str
    provider_source_id: str
    last_observed_at_utc_us: int | None
    batches: tuple[MarketBookRetryBatchState, ...]

    def __post_init__(self) -> None:
        _validate_state(self)

    @property
    def evidence_payload(self) -> dict[str, object]:
        return _state_evidence_payload(self)

    @property
    def state_id(self) -> str:
        return _sha(self.evidence_payload)

    def to_json(self) -> str:
        return _canonical_json(
            {"evidence": self.evidence_payload, "state_id": self.state_id}
        )

    @classmethod
    def from_json(
        cls,
        plan: MarketBookReadPlan,
        encoded: str,
    ) -> "MarketBookRetryBackoffState":
        return _restore_state(plan, encoded)


@dataclass(frozen=True, slots=True)
class MarketBookRetryDecision:
    batch_id: str
    observed_at_utc_us: int
    allowed: bool
    disposition: MarketBookRetryDisposition
    provider_recovery_streak: int = 0
    next_eligible_at_utc_us: int | None = None
    provider_recovery_projection_applied: bool = False
    provider_limit_coverage_complete: bool = False
    provider_dispatch_authorized: bool = False
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        _validate_decision(self)


def _validate_batch_state(state: MarketBookRetryBatchState) -> None:
    _sha256_token(state.batch_id, "batch_id")
    if type(state.provider_recovery_required) is not bool:
        raise MarketBookRetryBackoffError(
            "provider_recovery_required must be exact bool"
        )
    if (
        state.provider_failure_observed_at_utc_us is not None
        and type(state.provider_failure_observed_at_utc_us) is not int
    ):
        raise MarketBookRetryBackoffError(
            "provider_failure_observed_at_utc_us must be exact int or None"
        )
    if type(state.terminal_failure) is not bool:
        raise MarketBookRetryBackoffError(
            "terminal_failure must be exact bool"
        )
    if type(state.last_outcome) is not MarketBookAttemptOutcome:
        raise MarketBookRetryBackoffError(
            "last_outcome must be MarketBookAttemptOutcome"
        )
    code = _provider_code(state.last_provider_error_code)
    if state.provider_recovery_required is state.terminal_failure:
        raise MarketBookRetryBackoffError(
            "retry state must be exactly provider-recovery or terminal"
        )
    if state.provider_recovery_required:
        if (
            state.last_outcome is not MarketBookAttemptOutcome.PROVIDER_FAILURE
            or code not in _TRANSIENT_PROVIDER_CODES
            or type(state.provider_failure_observed_at_utc_us) is not int
        ):
            raise MarketBookRetryBackoffError(
                "provider recovery state is contradictory"
            )
    else:
        if state.provider_failure_observed_at_utc_us is not None:
            raise MarketBookRetryBackoffError(
                "terminal state cannot retain provider failure instant"
            )
        if (
            state.last_outcome is MarketBookAttemptOutcome.EXACT_RESPONSE
            or state.last_outcome in _LOCAL_NON_DISPATCH_OUTCOMES
        ):
            raise MarketBookRetryBackoffError(
                "terminal state cannot represent success/local non-dispatch"
            )
        if (
            state.last_outcome is not MarketBookAttemptOutcome.PROVIDER_FAILURE
            and code is not None
        ):
            raise MarketBookRetryBackoffError(
                "provider_error_code is only valid for provider failure"
            )


def _validate_state(state: MarketBookRetryBackoffState) -> None:
    if state.policy_version != MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION:
        raise MarketBookRetryBackoffError(
            "unsupported MarketBook retry projection policy version"
        )
    _sha256_token(state.plan_id, "plan_id")
    _sha256_token(state.request_contract_id, "request_contract_id")
    _source_id(state.provider_source_id)
    if (
        state.last_observed_at_utc_us is not None
        and type(state.last_observed_at_utc_us) is not int
    ):
        raise MarketBookRetryBackoffError(
            "last_observed_at_utc_us must be exact int or None"
        )
    if type(state.batches) is not tuple:
        raise MarketBookRetryBackoffError(
            "batches must be an exact tuple"
        )
    ids: list[str] = []
    for batch in state.batches:
        if type(batch) is not MarketBookRetryBatchState:
            raise MarketBookRetryBackoffError(
                "batches must contain exact MarketBookRetryBatchState values"
            )
        _validate_batch_state(batch)
        if (
            state.last_observed_at_utc_us is not None
            and batch.provider_failure_observed_at_utc_us is not None
            and batch.provider_failure_observed_at_utc_us
            > state.last_observed_at_utc_us
        ):
            raise MarketBookRetryBackoffError(
                "provider failure instant cannot follow last observed instant"
            )
        ids.append(batch.batch_id)
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise MarketBookRetryBackoffError(
            "retry batch state must be unique and sorted by batch_id"
        )


def _validate_decision(decision: MarketBookRetryDecision) -> None:
    _sha256_token(decision.batch_id, "batch_id")
    if type(decision.observed_at_utc_us) is not int:
        raise MarketBookRetryBackoffError(
            "observed_at_utc_us must be exact int"
        )
    if type(decision.allowed) is not bool:
        raise MarketBookRetryBackoffError("allowed must be exact bool")
    if type(decision.disposition) is not MarketBookRetryDisposition:
        raise MarketBookRetryBackoffError(
            "disposition must be MarketBookRetryDisposition"
        )
    if (
        type(decision.provider_recovery_streak) is not int
        or decision.provider_recovery_streak < 0
    ):
        raise MarketBookRetryBackoffError(
            "provider_recovery_streak must be a non-negative exact int"
        )
    if (
        decision.next_eligible_at_utc_us is not None
        and type(decision.next_eligible_at_utc_us) is not int
    ):
        raise MarketBookRetryBackoffError(
            "next_eligible_at_utc_us must be exact int or None"
        )
    if type(decision.provider_recovery_projection_applied) is not bool:
        raise MarketBookRetryBackoffError(
            "provider_recovery_projection_applied must be exact bool"
        )
    if decision.allowed is not (
        decision.disposition is MarketBookRetryDisposition.READY
    ):
        raise MarketBookRetryBackoffError(
            "retry decision allowed/disposition is contradictory"
        )
    if decision.disposition is MarketBookRetryDisposition.BACKOFF:
        if (
            type(decision.next_eligible_at_utc_us) is not int
            or not decision.provider_recovery_projection_applied
            or decision.provider_recovery_streak < 1
        ):
            raise MarketBookRetryBackoffError(
                "BACKOFF requires projected provider recovery evidence"
            )
    elif decision.disposition is MarketBookRetryDisposition.READY:
        if decision.provider_recovery_projection_applied:
            if (
                type(decision.next_eligible_at_utc_us) is not int
                or decision.provider_recovery_streak < 1
            ):
                raise MarketBookRetryBackoffError(
                    "provider-recovery READY decision lacks projection evidence"
                )
        elif (
            decision.next_eligible_at_utc_us is not None
            or decision.provider_recovery_streak != 0
        ):
            raise MarketBookRetryBackoffError(
                "plain READY decision cannot carry provider recovery evidence"
            )
    elif (
        decision.next_eligible_at_utc_us is not None
        or decision.provider_recovery_projection_applied
        or decision.provider_recovery_streak != 0
    ):
        raise MarketBookRetryBackoffError(
            "blocked non-backoff decision cannot carry recovery deadline"
        )
    if (
        decision.provider_limit_coverage_complete is not False
        or decision.provider_dispatch_authorized is not False
        or decision.execution_authorized is not False
    ):
        raise MarketBookRetryBackoffError(
            "retry decision cannot claim provider/execution authority"
        )


def _state_evidence_payload(
    state: MarketBookRetryBackoffState,
) -> dict[str, object]:
    _validate_state(state)
    return {
        "schema": "betfair-marketbook-retry-backoff-projection-v3",
        "policy_version": state.policy_version,
        "plan_id": state.plan_id,
        "request_contract_id": state.request_contract_id,
        "provider_source_id": state.provider_source_id,
        "last_observed_at_utc_us": state.last_observed_at_utc_us,
        "batches": [
            {
                "batch_id": batch.batch_id,
                "provider_recovery_required": (
                    batch.provider_recovery_required
                ),
                "provider_failure_observed_at_utc_us": (
                    batch.provider_failure_observed_at_utc_us
                ),
                "terminal_failure": batch.terminal_failure,
                "last_outcome": batch.last_outcome.value,
                "last_provider_error_code": (
                    batch.last_provider_error_code
                ),
            }
            for batch in state.batches
        ],
        "provider_recovery_deadline_is_authoritative": False,
        "provider_recovery_streak_is_authoritative": False,
        "provider_limit_coverage_complete": False,
        "provider_dispatch_authorized": False,
        "provider_observation_authenticated": False,
        "provider_freshness_proven": False,
        "execution_authorized": False,
    }


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MarketBookRetryBackoffError(
                f"encoded retry state contains duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise MarketBookRetryBackoffError(
        f"encoded retry state contains noncanonical JSON constant: {value}"
    )


def _restore_state(
    plan: MarketBookReadPlan,
    encoded: str,
) -> MarketBookRetryBackoffState:
    if type(plan) is not MarketBookReadPlan:
        raise MarketBookRetryBackoffError(
            "plan must be an exact MarketBookReadPlan"
        )
    if type(encoded) is not str:
        raise MarketBookRetryBackoffError(
            "encoded retry state must be exact str"
        )
    try:
        envelope = json.loads(
            encoded,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise MarketBookRetryBackoffError(
            "encoded retry state is not valid JSON"
        ) from exc
    if (
        type(envelope) is not dict
        or set(envelope) != {"evidence", "state_id"}
    ):
        raise MarketBookRetryBackoffError(
            "encoded retry state has a noncanonical envelope"
        )
    evidence = envelope["evidence"]
    if type(evidence) is not dict:
        raise MarketBookRetryBackoffError(
            "encoded retry evidence must be an object"
        )
    expected_keys = {
        "schema",
        "policy_version",
        "plan_id",
        "request_contract_id",
        "provider_source_id",
        "last_observed_at_utc_us",
        "batches",
        "provider_recovery_deadline_is_authoritative",
        "provider_recovery_streak_is_authoritative",
        "provider_limit_coverage_complete",
        "provider_dispatch_authorized",
        "provider_observation_authenticated",
        "provider_freshness_proven",
        "execution_authorized",
    }
    if set(evidence) != expected_keys:
        raise MarketBookRetryBackoffError(
            "encoded retry evidence has a noncanonical shape"
        )
    if evidence["schema"] != (
        "betfair-marketbook-retry-backoff-projection-v3"
    ):
        raise MarketBookRetryBackoffError(
            "encoded retry evidence has unsupported schema"
        )
    if any(
        evidence[name] is not False
        for name in (
            "provider_recovery_deadline_is_authoritative",
            "provider_recovery_streak_is_authoritative",
            "provider_limit_coverage_complete",
            "provider_dispatch_authorized",
            "provider_observation_authenticated",
            "provider_freshness_proven",
            "execution_authorized",
        )
    ):
        raise MarketBookRetryBackoffError(
            "retry projection cannot claim provider/execution authority"
        )
    raw_batches = evidence["batches"]
    if type(raw_batches) is not list:
        raise MarketBookRetryBackoffError(
            "encoded retry batches must be an exact JSON array"
        )
    expected_batch_keys = {
        "batch_id",
        "provider_recovery_required",
        "provider_failure_observed_at_utc_us",
        "terminal_failure",
        "last_outcome",
        "last_provider_error_code",
    }
    batches: list[MarketBookRetryBatchState] = []
    for raw in raw_batches:
        if type(raw) is not dict or set(raw) != expected_batch_keys:
            raise MarketBookRetryBackoffError(
                "encoded retry batch has a noncanonical shape"
            )
        try:
            outcome = MarketBookAttemptOutcome(raw["last_outcome"])
            batch = MarketBookRetryBatchState(
                batch_id=raw["batch_id"],
                provider_recovery_required=raw[
                    "provider_recovery_required"
                ],
                provider_failure_observed_at_utc_us=raw[
                    "provider_failure_observed_at_utc_us"
                ],
                terminal_failure=raw["terminal_failure"],
                last_outcome=outcome,
                last_provider_error_code=raw[
                    "last_provider_error_code"
                ],
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, MarketBookRetryBackoffError):
                raise
            raise MarketBookRetryBackoffError(
                "encoded retry batch cannot be restored"
            ) from exc
        batches.append(batch)
    state = MarketBookRetryBackoffState(
        policy_version=evidence["policy_version"],
        plan_id=evidence["plan_id"],
        request_contract_id=evidence["request_contract_id"],
        provider_source_id=evidence["provider_source_id"],
        last_observed_at_utc_us=evidence["last_observed_at_utc_us"],
        batches=tuple(batches),
    )
    if (
        state.plan_id != plan.plan_id
        or state.request_contract_id != plan.request_contract_id
    ):
        raise MarketBookRetryBackoffError(
            "retry state is bound to another MarketBook plan"
        )
    if (
        state.evidence_payload != evidence
        or state.state_id != envelope["state_id"]
    ):
        raise MarketBookRetryBackoffError(
            "encoded retry state does not match canonical recomputation"
        )
    return state


class MarketBookRetryBackoffGate:
    """Batch-local projection over canonical provider recovery evidence."""


def _install_retry_projection_authority() -> None:
    """Seal retry projection semantics against late module/class rebinding."""

    gate_type = MarketBookRetryBackoffGate
    batch_type = MarketBookRetryBatchState
    state_type = MarketBookRetryBackoffState
    decision_type = MarketBookRetryDecision
    plan_type = MarketBookReadPlan
    health_type = SourceHealthState
    health_store_type = SourceHealthStore
    config_type = ContinuousObservationConfig
    outcome_type = MarketBookAttemptOutcome
    disposition_type = MarketBookRetryDisposition
    error_type = MarketBookRetryBackoffError

    exact_response = outcome_type.EXACT_RESPONSE
    provider_failure = outcome_type.PROVIDER_FAILURE
    not_dispatched_rate = outcome_type.NOT_DISPATCHED_RATE
    not_dispatched_concurrency = outcome_type.NOT_DISPATCHED_CONCURRENCY
    not_dispatched_backoff = outcome_type.NOT_DISPATCHED_BACKOFF
    local_non_dispatch = frozenset(
        {
            not_dispatched_rate,
            not_dispatched_concurrency,
            not_dispatched_backoff,
        }
    )
    transient_codes = frozenset(
        {"TOO_MANY_REQUESTS", "SERVICE_BUSY", "TIMEOUT_ERROR"}
    )
    ready_disposition = disposition_type.READY
    backoff_disposition = disposition_type.BACKOFF
    recovery_required_disposition = (
        disposition_type.PROVIDER_RECOVERY_REQUIRED
    )
    terminal_disposition = disposition_type.TERMINAL
    policy_version = MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION
    evidence_schema = "betfair-marketbook-retry-backoff-projection-v3"

    datetime_type = datetime
    timedelta_type = timedelta
    epoch_naive = datetime_type(1970, 1, 1)
    lock_type = RLock
    hash_factory = sha256
    json_dumps = json.dumps
    json_loads = json.loads
    json_decode_error = json.JSONDecodeError
    parse_health_timestamp = parse_source_timestamp
    provider_backoff_seconds = _provider_backoff_seconds
    canonical_health_validate = SourceHealthState.validate
    canonical_health_get = SourceHealthStore.get
    canonical_health_writer_guard = SourceHealthStore._writer_guard

    object_new = object.__new__
    object_setattr = object.__setattr__
    object_getattribute = object.__getattribute__
    type_of = type
    tuple_type = tuple
    dict_type = dict
    list_type = list
    str_type = str
    int_type = int
    bool_type = bool
    set_type = set
    sorted_fn = sorted
    len_fn = len
    any_fn = any
    frozenset_type = frozenset
    type_error = TypeError
    value_error = ValueError

    plan_id_getter = plan_type.__dict__["plan_id"].fget
    request_contract_id_getter = plan_type.__dict__["request_contract_id"].fget
    plan_batches_getter = plan_type.__dict__["batches"].fget

    health_source = health_type.__dict__["source_id"]
    health_status = health_type.__dict__["status"]
    health_last_failure_kind = health_type.__dict__["last_failure_kind"]
    health_failure_count = health_type.__dict__[
        "consecutive_failure_kind_count"
    ]
    health_last_error_at = health_type.__dict__["last_error_at"]
    config_interval = config_type.__dict__["interval_seconds"]
    config_max_backoff = config_type.__dict__["max_backoff_seconds"]

    class BackoffConfigProjection:
        __slots__ = ("interval_seconds", "max_backoff_seconds")

        def __init__(
            self,
            interval_seconds: float,
            max_backoff_seconds: float,
        ) -> None:
            self.interval_seconds = interval_seconds
            self.max_backoff_seconds = max_backoff_seconds

    def canonical_json(value: object) -> str:
        try:
            return json_dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (type_error, value_error) as exc:
            raise error_type(
                "retry projection evidence must be canonical JSON data"
            ) from exc

    def hash_value(value: object) -> str:
        return hash_factory(
            canonical_json(value).encode("utf-8")
        ).hexdigest()

    def sha_token(value: object, name: str) -> str:
        if (
            type_of(value) is not str_type
            or len_fn(value) != 64
            or any_fn(
                char not in "0123456789abcdef"
                for char in value
            )
        ):
            raise error_type(
                f"{name} must be lowercase SHA-256"
            )
        return value

    def source_id(value: object) -> str:
        if (
            type_of(value) is not str_type
            or not value
            or value != value.strip()
        ):
            raise error_type(
                "provider_source_id must be a non-empty exact string"
            )
        return value

    def provider_code(
        value: object,
        *,
        optional: bool = True,
    ) -> str | None:
        if value is None and optional:
            return None
        if (
            type_of(value) is not str_type
            or not value
            or value != value.strip()
            or value != value.upper()
            or any_fn(
                char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
                for char in value
            )
        ):
            raise error_type(
                "provider_error_code must be a canonical uppercase provider token"
            )
        return value

    def utc_microseconds(value: object, name: str) -> int:
        if type_of(value) is not datetime_type:
            raise error_type(
                f"{name} must be an exact datetime"
            )
        if value.tzinfo is None:
            raise error_type(f"{name} must be timezone-aware")
        offset = value.utcoffset()
        if type_of(offset) is not timedelta_type:
            raise error_type(
                f"{name} UTC offset must be an exact timedelta"
            )
        local_naive = value.replace(tzinfo=None)
        utc_naive = local_naive - offset
        delta = utc_naive - epoch_naive
        return (
            delta.days * 86_400 * 1_000_000
            + delta.seconds * 1_000_000
            + delta.microseconds
        )

    def outcome_value(value: MarketBookAttemptOutcome) -> str:
        if type_of(value) is not outcome_type:
            raise error_type(
                "last_outcome must be MarketBookAttemptOutcome"
            )
        return object_getattribute(value, "_value_")

    def validate_batch(
        state: MarketBookRetryBatchState,
    ) -> None:
        if type_of(state) is not batch_type:
            raise error_type(
                "retry batch must be exact MarketBookRetryBatchState"
            )
        batch_id_value = object_getattribute(state, "batch_id")
        recovery_required = object_getattribute(
            state, "provider_recovery_required"
        )
        failure_observed = object_getattribute(
            state, "provider_failure_observed_at_utc_us"
        )
        terminal = object_getattribute(state, "terminal_failure")
        outcome = object_getattribute(state, "last_outcome")
        code = provider_code(
            object_getattribute(state, "last_provider_error_code")
        )
        sha_token(batch_id_value, "batch_id")
        if type_of(recovery_required) is not bool_type:
            raise error_type(
                "provider_recovery_required must be exact bool"
            )
        if (
            failure_observed is not None
            and type_of(failure_observed) is not int_type
        ):
            raise error_type(
                "provider_failure_observed_at_utc_us must be exact int or None"
            )
        if type_of(terminal) is not bool_type:
            raise error_type(
                "terminal_failure must be exact bool"
            )
        if type_of(outcome) is not outcome_type:
            raise error_type(
                "last_outcome must be MarketBookAttemptOutcome"
            )
        if recovery_required is terminal:
            raise error_type(
                "retry state must be exactly provider-recovery or terminal"
            )
        if recovery_required:
            if (
                outcome is not provider_failure
                or code not in transient_codes
                or type_of(failure_observed) is not int_type
            ):
                raise error_type(
                    "provider recovery state is contradictory"
                )
        else:
            if failure_observed is not None:
                raise error_type(
                    "terminal state cannot retain provider failure instant"
                )
            if (
                outcome is exact_response
                or outcome in local_non_dispatch
            ):
                raise error_type(
                    "terminal state cannot represent success/local non-dispatch"
                )
            if outcome is not provider_failure and code is not None:
                raise error_type(
                    "provider_error_code is only valid for provider failure"
                )

    def validate_state(
        state: MarketBookRetryBackoffState,
    ) -> None:
        if type_of(state) is not state_type:
            raise error_type(
                "retry state must be exact MarketBookRetryBackoffState"
            )
        if object_getattribute(state, "policy_version") != policy_version:
            raise error_type(
                "unsupported MarketBook retry projection policy version"
            )
        sha_token(object_getattribute(state, "plan_id"), "plan_id")
        sha_token(
            object_getattribute(state, "request_contract_id"),
            "request_contract_id",
        )
        source_id(object_getattribute(state, "provider_source_id"))
        last_observed = object_getattribute(
            state, "last_observed_at_utc_us"
        )
        if (
            last_observed is not None
            and type_of(last_observed) is not int_type
        ):
            raise error_type(
                "last_observed_at_utc_us must be exact int or None"
            )
        batches = object_getattribute(state, "batches")
        if type_of(batches) is not tuple_type:
            raise error_type("batches must be an exact tuple")
        ids: list[str] = []
        for batch in batches:
            validate_batch(batch)
            failure_observed = object_getattribute(
                batch, "provider_failure_observed_at_utc_us"
            )
            if (
                last_observed is not None
                and failure_observed is not None
                and failure_observed > last_observed
            ):
                raise error_type(
                    "provider failure instant cannot follow last observed instant"
                )
            ids.append(object_getattribute(batch, "batch_id"))
        if (
            ids != sorted_fn(ids)
            or len_fn(ids) != len_fn(set_type(ids))
        ):
            raise error_type(
                "retry batch state must be unique and sorted by batch_id"
            )

    def validate_decision(
        decision: MarketBookRetryDecision,
    ) -> None:
        if type_of(decision) is not decision_type:
            raise error_type(
                "retry decision must be exact MarketBookRetryDecision"
            )
        sha_token(
            object_getattribute(decision, "batch_id"),
            "batch_id",
        )
        observed = object_getattribute(
            decision, "observed_at_utc_us"
        )
        allowed = object_getattribute(decision, "allowed")
        disposition = object_getattribute(decision, "disposition")
        streak = object_getattribute(
            decision, "provider_recovery_streak"
        )
        next_eligible = object_getattribute(
            decision, "next_eligible_at_utc_us"
        )
        projected = object_getattribute(
            decision, "provider_recovery_projection_applied"
        )
        if type_of(observed) is not int_type:
            raise error_type(
                "observed_at_utc_us must be exact int"
            )
        if type_of(allowed) is not bool_type:
            raise error_type("allowed must be exact bool")
        if type_of(disposition) is not disposition_type:
            raise error_type(
                "disposition must be MarketBookRetryDisposition"
            )
        if type_of(streak) is not int_type or streak < 0:
            raise error_type(
                "provider_recovery_streak must be non-negative exact int"
            )
        if (
            next_eligible is not None
            and type_of(next_eligible) is not int_type
        ):
            raise error_type(
                "next_eligible_at_utc_us must be exact int or None"
            )
        if type_of(projected) is not bool_type:
            raise error_type(
                "provider_recovery_projection_applied must be exact bool"
            )
        if allowed is not (disposition is ready_disposition):
            raise error_type(
                "retry decision allowed/disposition is contradictory"
            )
        if disposition is backoff_disposition:
            if (
                type_of(next_eligible) is not int_type
                or not projected
                or streak < 1
            ):
                raise error_type(
                    "BACKOFF requires projected provider recovery evidence"
                )
        elif disposition is ready_disposition:
            if projected:
                if (
                    type_of(next_eligible) is not int_type
                    or streak < 1
                ):
                    raise error_type(
                        "provider-recovery READY lacks projection evidence"
                    )
            elif next_eligible is not None or streak != 0:
                raise error_type(
                    "plain READY cannot carry provider recovery evidence"
                )
        elif (
            next_eligible is not None
            or projected
            or streak != 0
        ):
            raise error_type(
                "blocked non-backoff decision cannot carry recovery deadline"
            )
        if (
            object_getattribute(
                decision, "provider_limit_coverage_complete"
            ) is not False
            or object_getattribute(
                decision, "provider_dispatch_authorized"
            ) is not False
            or object_getattribute(
                decision, "execution_authorized"
            ) is not False
        ):
            raise error_type(
                "retry decision cannot claim provider/execution authority"
            )

    def make_batch(
        *,
        batch_id: str,
        provider_recovery_required: bool,
        provider_failure_observed_at_utc_us: int | None,
        terminal_failure: bool,
        last_outcome: MarketBookAttemptOutcome,
        last_provider_error_code: str | None,
    ) -> MarketBookRetryBatchState:
        value = object_new(batch_type)
        object_setattr(value, "batch_id", batch_id)
        object_setattr(
            value,
            "provider_recovery_required",
            provider_recovery_required,
        )
        object_setattr(
            value,
            "provider_failure_observed_at_utc_us",
            provider_failure_observed_at_utc_us,
        )
        object_setattr(value, "terminal_failure", terminal_failure)
        object_setattr(value, "last_outcome", last_outcome)
        object_setattr(
            value,
            "last_provider_error_code",
            last_provider_error_code,
        )
        validate_batch(value)
        return value

    def copy_batch(
        value: MarketBookRetryBatchState,
    ) -> MarketBookRetryBatchState:
        validate_batch(value)
        return make_batch(
            batch_id=object_getattribute(value, "batch_id"),
            provider_recovery_required=object_getattribute(
                value, "provider_recovery_required"
            ),
            provider_failure_observed_at_utc_us=object_getattribute(
                value, "provider_failure_observed_at_utc_us"
            ),
            terminal_failure=object_getattribute(
                value, "terminal_failure"
            ),
            last_outcome=object_getattribute(value, "last_outcome"),
            last_provider_error_code=object_getattribute(
                value, "last_provider_error_code"
            ),
        )

    def make_state(
        *,
        plan_id: str,
        request_contract_id: str,
        provider_source_id: str,
        last_observed_at_utc_us: int | None,
        batches: tuple[MarketBookRetryBatchState, ...],
    ) -> MarketBookRetryBackoffState:
        value = object_new(state_type)
        object_setattr(value, "policy_version", policy_version)
        object_setattr(value, "plan_id", plan_id)
        object_setattr(value, "request_contract_id", request_contract_id)
        object_setattr(value, "provider_source_id", provider_source_id)
        object_setattr(
            value,
            "last_observed_at_utc_us",
            last_observed_at_utc_us,
        )
        object_setattr(value, "batches", batches)
        validate_state(value)
        return value

    def make_decision(
        batch_id: str,
        observed_us: int,
        allowed: bool,
        disposition: MarketBookRetryDisposition,
        *,
        streak: int = 0,
        next_eligible_us: int | None = None,
        projection_applied: bool = False,
    ) -> MarketBookRetryDecision:
        value = object_new(decision_type)
        object_setattr(value, "batch_id", batch_id)
        object_setattr(value, "observed_at_utc_us", observed_us)
        object_setattr(value, "allowed", allowed)
        object_setattr(value, "disposition", disposition)
        object_setattr(value, "provider_recovery_streak", streak)
        object_setattr(
            value, "next_eligible_at_utc_us", next_eligible_us
        )
        object_setattr(
            value,
            "provider_recovery_projection_applied",
            projection_applied,
        )
        object_setattr(
            value, "provider_limit_coverage_complete", False
        )
        object_setattr(value, "provider_dispatch_authorized", False)
        object_setattr(value, "execution_authorized", False)
        validate_decision(value)
        return value

    def evidence_payload(
        state: MarketBookRetryBackoffState,
    ) -> dict[str, object]:
        validate_state(state)
        batches = object_getattribute(state, "batches")
        return {
            "schema": evidence_schema,
            "policy_version": object_getattribute(
                state, "policy_version"
            ),
            "plan_id": object_getattribute(state, "plan_id"),
            "request_contract_id": object_getattribute(
                state, "request_contract_id"
            ),
            "provider_source_id": object_getattribute(
                state, "provider_source_id"
            ),
            "last_observed_at_utc_us": object_getattribute(
                state, "last_observed_at_utc_us"
            ),
            "batches": [
                {
                    "batch_id": object_getattribute(
                        batch, "batch_id"
                    ),
                    "provider_recovery_required": object_getattribute(
                        batch, "provider_recovery_required"
                    ),
                    "provider_failure_observed_at_utc_us": object_getattribute(
                        batch,
                        "provider_failure_observed_at_utc_us",
                    ),
                    "terminal_failure": object_getattribute(
                        batch, "terminal_failure"
                    ),
                    "last_outcome": outcome_value(
                        object_getattribute(batch, "last_outcome")
                    ),
                    "last_provider_error_code": object_getattribute(
                        batch, "last_provider_error_code"
                    ),
                }
                for batch in batches
            ],
            "provider_recovery_deadline_is_authoritative": False,
            "provider_recovery_streak_is_authoritative": False,
            "provider_limit_coverage_complete": False,
            "provider_dispatch_authorized": False,
            "provider_observation_authenticated": False,
            "provider_freshness_proven": False,
            "execution_authorized": False,
        }

    def state_id(
        state: MarketBookRetryBackoffState,
    ) -> str:
        return hash_value(evidence_payload(state))

    def to_json(
        state: MarketBookRetryBackoffState,
    ) -> str:
        return canonical_json(
            {
                "evidence": evidence_payload(state),
                "state_id": state_id(state),
            }
        )

    def strict_json_object(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise error_type(
                    "encoded retry state contains duplicate JSON key: "
                    + key
                )
            result[key] = value
        return result

    def reject_json_constant(value: str) -> None:
        raise error_type(
            "encoded retry state contains noncanonical JSON constant: "
            + value
        )

    def from_json(
        cls: type[MarketBookRetryBackoffState],
        plan: MarketBookReadPlan,
        encoded: str,
    ) -> MarketBookRetryBackoffState:
        if cls is not state_type:
            raise error_type(
                "retry state restoration requires exact state type"
            )
        if type_of(plan) is not plan_type:
            raise error_type("plan must be exact MarketBookReadPlan")
        if type_of(encoded) is not str_type:
            raise error_type("encoded retry state must be exact str")
        try:
            envelope = json_loads(
                encoded,
                object_pairs_hook=strict_json_object,
                parse_constant=reject_json_constant,
            )
        except json_decode_error as exc:
            raise error_type(
                "encoded retry state is not valid JSON"
            ) from exc
        if (
            type_of(envelope) is not dict_type
            or set_type(envelope) != {"evidence", "state_id"}
        ):
            raise error_type(
                "encoded retry state has a noncanonical envelope"
            )
        evidence = envelope["evidence"]
        if type_of(evidence) is not dict_type:
            raise error_type(
                "encoded retry evidence must be an object"
            )
        expected_keys = {
            "schema",
            "policy_version",
            "plan_id",
            "request_contract_id",
            "provider_source_id",
            "last_observed_at_utc_us",
            "batches",
            "provider_recovery_deadline_is_authoritative",
            "provider_recovery_streak_is_authoritative",
            "provider_limit_coverage_complete",
            "provider_dispatch_authorized",
            "provider_observation_authenticated",
            "provider_freshness_proven",
            "execution_authorized",
        }
        if set_type(evidence) != expected_keys:
            raise error_type(
                "encoded retry evidence has a noncanonical shape"
            )
        if evidence["schema"] != evidence_schema:
            raise error_type(
                "encoded retry evidence has unsupported schema"
            )
        if any_fn(
            evidence[name] is not False
            for name in (
                "provider_recovery_deadline_is_authoritative",
                "provider_recovery_streak_is_authoritative",
                "provider_limit_coverage_complete",
                "provider_dispatch_authorized",
                "provider_observation_authenticated",
                "provider_freshness_proven",
                "execution_authorized",
            )
        ):
            raise error_type(
                "retry projection cannot claim provider/execution authority"
            )
        raw_batches = evidence["batches"]
        if type_of(raw_batches) is not list_type:
            raise error_type(
                "encoded retry batches must be exact JSON array"
            )
        expected_batch_keys = {
            "batch_id",
            "provider_recovery_required",
            "provider_failure_observed_at_utc_us",
            "terminal_failure",
            "last_outcome",
            "last_provider_error_code",
        }
        restored_batches: list[MarketBookRetryBatchState] = []
        for raw in raw_batches:
            if (
                type_of(raw) is not dict_type
                or set_type(raw) != expected_batch_keys
            ):
                raise error_type(
                    "encoded retry batch has noncanonical shape"
                )
            try:
                outcome = outcome_type(raw["last_outcome"])
            except (type_error, value_error) as exc:
                raise error_type(
                    "encoded retry batch has invalid outcome"
                ) from exc
            restored_batches.append(
                make_batch(
                    batch_id=raw["batch_id"],
                    provider_recovery_required=raw[
                        "provider_recovery_required"
                    ],
                    provider_failure_observed_at_utc_us=raw[
                        "provider_failure_observed_at_utc_us"
                    ],
                    terminal_failure=raw["terminal_failure"],
                    last_outcome=outcome,
                    last_provider_error_code=raw[
                        "last_provider_error_code"
                    ],
                )
            )
        state = make_state(
            plan_id=evidence["plan_id"],
            request_contract_id=evidence["request_contract_id"],
            provider_source_id=evidence["provider_source_id"],
            last_observed_at_utc_us=evidence[
                "last_observed_at_utc_us"
            ],
            batches=tuple_type(restored_batches),
        )
        current_plan_id = plan_id_getter(plan)
        current_contract_id = request_contract_id_getter(plan)
        if (
            object_getattribute(state, "plan_id") != current_plan_id
            or object_getattribute(
                state, "request_contract_id"
            ) != current_contract_id
        ):
            raise error_type(
                "retry state is bound to another MarketBook plan"
            )
        if (
            evidence_payload(state) != evidence
            or state_id(state) != envelope["state_id"]
        ):
            raise error_type(
                "encoded retry state does not match canonical recomputation"
            )
        return state

    def gate_init(
        self: MarketBookRetryBackoffGate,
        plan: MarketBookReadPlan,
        *,
        provider_source_id: str,
        state: MarketBookRetryBackoffState | None = None,
    ) -> None:
        if type_of(plan) is not plan_type:
            raise error_type("plan must be exact MarketBookReadPlan")
        source = source_id(provider_source_id)
        current_plan_id = plan_id_getter(plan)
        current_contract_id = request_contract_id_getter(plan)
        planned_batches = plan_batches_getter(plan)
        object_setattr(self, "_lock", lock_type())
        object_setattr(self, "_plan_id", current_plan_id)
        object_setattr(
            self, "_request_contract_id", current_contract_id
        )
        object_setattr(self, "_provider_source_id", source)
        object_setattr(
            self,
            "_batch_ids",
            frozenset_type(
                object_getattribute(batch, "batch_id")
                for batch in planned_batches
            ),
        )
        object_setattr(self, "_entries", {})
        object_setattr(self, "_last_observed_at_utc_us", None)
        object_setattr(self, "_provider_deadlines", {})
        if state is not None:
            if type_of(state) is not state_type:
                raise error_type(
                    "state must be exact MarketBookRetryBackoffState or None"
                )
            validate_state(state)
            if (
                object_getattribute(state, "plan_id")
                != current_plan_id
                or object_getattribute(
                    state, "request_contract_id"
                )
                != current_contract_id
                or object_getattribute(
                    state, "provider_source_id"
                )
                != source
            ):
                raise error_type(
                    "retry state is bound to another MarketBook plan/source"
                )
            entries: dict[str, MarketBookRetryBatchState] = {}
            batch_ids = object_getattribute(self, "_batch_ids")
            for batch in object_getattribute(state, "batches"):
                item = object_getattribute(batch, "batch_id")
                if item not in batch_ids:
                    raise error_type(
                        "retry state references unknown planned batch"
                    )
                entries[item] = copy_batch(batch)
            object_setattr(self, "_entries", entries)
            object_setattr(
                self,
                "_last_observed_at_utc_us",
                object_getattribute(
                    state, "last_observed_at_utc_us"
                ),
            )

    def batch_id(
        self: MarketBookRetryBackoffGate,
        raw_batch_id: object,
    ) -> str:
        token = sha_token(raw_batch_id, "batch_id")
        if token not in object_getattribute(self, "_batch_ids"):
            raise error_type(
                "batch_id is not part of bound MarketBook plan"
            )
        return token

    def observe(
        self: MarketBookRetryBackoffGate,
        observed_at: object,
    ) -> int:
        observed_us = utc_microseconds(observed_at, "observed_at")
        last = object_getattribute(
            self, "_last_observed_at_utc_us"
        )
        if last is not None and observed_us < last:
            raise error_type("observed_at must not move backwards")
        object_setattr(
            self, "_last_observed_at_utc_us", observed_us
        )
        return observed_us

    def snapshot(
        self: MarketBookRetryBackoffGate,
    ) -> MarketBookRetryBackoffState:
        lock = object_getattribute(self, "_lock")
        with lock:
            entries = object_getattribute(self, "_entries")
            return make_state(
                plan_id=object_getattribute(self, "_plan_id"),
                request_contract_id=object_getattribute(
                    self, "_request_contract_id"
                ),
                provider_source_id=object_getattribute(
                    self, "_provider_source_id"
                ),
                last_observed_at_utc_us=object_getattribute(
                    self, "_last_observed_at_utc_us"
                ),
                batches=tuple_type(
                    copy_batch(entries[item])
                    for item in sorted_fn(entries)
                ),
            )

    def admit(
        self: MarketBookRetryBackoffGate,
        raw_batch_id: str,
        *,
        observed_at: datetime,
    ) -> MarketBookRetryDecision:
        batch = batch_id(self, raw_batch_id)
        lock = object_getattribute(self, "_lock")
        with lock:
            observed_us = observe(self, observed_at)
            current = object_getattribute(
                self, "_entries"
            ).get(batch)
            if current is None:
                return make_decision(
                    batch,
                    observed_us,
                    True,
                    ready_disposition,
                )
            if object_getattribute(current, "terminal_failure"):
                return make_decision(
                    batch,
                    observed_us,
                    False,
                    terminal_disposition,
                )
            projected = object_getattribute(
                self, "_provider_deadlines"
            ).get(batch)
            if projected is None:
                return make_decision(
                    batch,
                    observed_us,
                    False,
                    recovery_required_disposition,
                )
            deadline_us, streak = projected
            if observed_us < deadline_us:
                return make_decision(
                    batch,
                    observed_us,
                    False,
                    backoff_disposition,
                    streak=streak,
                    next_eligible_us=deadline_us,
                    projection_applied=True,
                )
            return make_decision(
                batch,
                observed_us,
                True,
                ready_disposition,
                streak=streak,
                next_eligible_us=deadline_us,
                projection_applied=True,
            )

    def apply_provider_recovery(
        self: MarketBookRetryBackoffGate,
        raw_batch_id: str,
        *,
        observed_at: datetime,
        health_store: SourceHealthStore,
        config: ContinuousObservationConfig,
    ) -> MarketBookRetryDecision:
        batch = batch_id(self, raw_batch_id)
        if type_of(health_store) is not health_store_type:
            raise error_type(
                "health_store must be exact SourceHealthStore"
            )
        if type_of(config) is not config_type:
            raise error_type(
                "config must be exact ContinuousObservationConfig"
            )
        source = object_getattribute(self, "_provider_source_id")

        # #1878 owns durable provider-recovery truth. Hold its canonical writer
        # lock while resolving current typed health and while committing this
        # non-authoritative batch projection so a concurrent success/failure
        # transition cannot be raced into a stale retry deadline.
        with canonical_health_writer_guard(health_store):
            health = canonical_health_get(health_store, source)
            if type_of(health) is not health_type:
                raise error_type(
                    "SourceHealthStore returned noncanonical health state"
                )
            canonical_health_validate(health)
            if health_source.__get__(health, health_type) != source:
                raise error_type(
                    "provider health is bound to another source"
                )
            if (
                health_status.__get__(health, health_type) != "failed"
                or health_last_failure_kind.__get__(
                    health, health_type
                )
                != "provider_unavailable"
            ):
                raise error_type(
                    "provider recovery requires durable provider_unavailable health"
                )
            streak = health_failure_count.__get__(health, health_type)
            last_error_at = health_last_error_at.__get__(
                health, health_type
            )
            if type_of(streak) is not int_type or streak <= 0:
                raise error_type(
                    "provider recovery requires positive durable typed streak"
                )
            if (
                type_of(last_error_at) is not str_type
                or not last_error_at
            ):
                raise error_type(
                    "provider recovery requires durable failure timestamp"
                )
            failure_at = parse_health_timestamp(last_error_at)
            failure_at_us = utc_microseconds(
                failure_at, "provider_health_failure_at"
            )

            # Snapshot #1878 config through captured slot descriptors, then
            # invoke its captured canonical backoff function.
            interval = config_interval.__get__(config, config_type)
            max_backoff = config_max_backoff.__get__(
                config, config_type
            )
            backoff_config = BackoffConfigProjection(
                interval, max_backoff
            )
            backoff_seconds = provider_backoff_seconds(
                streak, backoff_config
            )
            deadline = failure_at + timedelta_type(
                seconds=backoff_seconds
            )
            deadline_us = utc_microseconds(
                deadline, "provider_retry_not_before"
            )

            lock = object_getattribute(self, "_lock")
            with lock:
                current = object_getattribute(
                    self, "_entries"
                ).get(batch)
                if (
                    current is None
                    or not object_getattribute(
                        current, "provider_recovery_required"
                    )
                ):
                    raise error_type(
                        "batch is not awaiting provider recovery authority"
                    )
                failure_observed = object_getattribute(
                    current,
                    "provider_failure_observed_at_utc_us",
                )
                if (
                    failure_observed is None
                    or failure_at_us < failure_observed
                ):
                    raise error_type(
                        "provider health predates current MarketBook failure"
                    )
                observed_us = observe(self, observed_at)
                object_getattribute(
                    self, "_provider_deadlines"
                )[batch] = (deadline_us, streak)
                if observed_us < deadline_us:
                    return make_decision(
                        batch,
                        observed_us,
                        False,
                        backoff_disposition,
                        streak=streak,
                        next_eligible_us=deadline_us,
                        projection_applied=True,
                    )
                return make_decision(
                    batch,
                    observed_us,
                    True,
                    ready_disposition,
                    streak=streak,
                    next_eligible_us=deadline_us,
                    projection_applied=True,
                )

    def record_outcome(
        self: MarketBookRetryBackoffGate,
        raw_batch_id: str,
        *,
        observed_at: datetime,
        outcome: MarketBookAttemptOutcome,
        provider_error_code: str | None = None,
    ) -> MarketBookRetryBackoffState:
        batch = batch_id(self, raw_batch_id)
        if type_of(outcome) is not outcome_type:
            raise error_type(
                "outcome must be MarketBookAttemptOutcome"
            )
        code = provider_code(provider_error_code)
        if outcome is not provider_failure and code is not None:
            raise error_type(
                "provider_error_code is only valid for PROVIDER_FAILURE"
            )
        lock = object_getattribute(self, "_lock")
        with lock:
            observed_us = observe(self, observed_at)
            entries = object_getattribute(self, "_entries")
            deadlines = object_getattribute(
                self, "_provider_deadlines"
            )
            if outcome is exact_response:
                entries.pop(batch, None)
                deadlines.pop(batch, None)
                return snapshot(self)
            if outcome in local_non_dispatch:
                return snapshot(self)
            deadlines.pop(batch, None)
            if (
                outcome is provider_failure
                and code in transient_codes
            ):
                entries[batch] = make_batch(
                    batch_id=batch,
                    provider_recovery_required=True,
                    provider_failure_observed_at_utc_us=observed_us,
                    terminal_failure=False,
                    last_outcome=outcome,
                    last_provider_error_code=code,
                )
            else:
                entries[batch] = make_batch(
                    batch_id=batch,
                    provider_recovery_required=False,
                    provider_failure_observed_at_utc_us=None,
                    terminal_failure=True,
                    last_outcome=outcome,
                    last_provider_error_code=code,
                )
            return snapshot(self)

    def policy_version_property(
        self: MarketBookRetryBackoffGate,
    ) -> str:
        return policy_version

    batch_type.__post_init__ = validate_batch
    state_type.__post_init__ = validate_state
    decision_type.__post_init__ = validate_decision
    state_type.evidence_payload = property(evidence_payload)
    state_type.state_id = property(state_id)
    state_type.to_json = to_json
    state_type.from_json = classmethod(from_json)
    gate_type.__init__ = gate_init
    gate_type.snapshot = snapshot
    gate_type.admit = admit
    gate_type.apply_provider_recovery = apply_provider_recovery
    gate_type.record_outcome = record_outcome
    gate_type.policy_version = property(policy_version_property)


_install_retry_projection_authority()
del _install_retry_projection_authority
