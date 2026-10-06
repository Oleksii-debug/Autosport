"""MarketBook retry admission projected onto canonical provider recovery authority.

Provider-unavailable streak/backoff truth is owned by continuous_observation /
SourceHealthStore. This module does not create a second provider streak law. It
adds only plan/batch-local admission and explicit gap projection around that
authority, plus bounded retry for structurally incomplete MarketBook responses.
No sleeps, timers, provider I/O, freshness authority, or execution authority.
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
from .ingestion_health import SourceHealthState, parse_source_timestamp


MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION = (
    "betfair.list-market-book.retry-backoff-projection.v2"
)
MARKETBOOK_INCOMPLETE_RETRY_BASE_DELAY_US = 250_000
MARKETBOOK_INCOMPLETE_RETRY_MAX_DELAY_US = 4_000_000
MARKETBOOK_MAX_INCOMPLETE_AUTOMATIC_RETRIES = 5

_TRANSIENT_PROVIDER_CODES = frozenset(
    {"TOO_MANY_REQUESTS", "SERVICE_BUSY", "TIMEOUT_ERROR"}
)
_LOCAL_NO_FAILURE_OUTCOMES = frozenset(
    {
        MarketBookAttemptOutcome.NOT_DISPATCHED_RATE,
        MarketBookAttemptOutcome.NOT_DISPATCHED_CONCURRENCY,
        MarketBookAttemptOutcome.NOT_DISPATCHED_BACKOFF,
    }
)


class MarketBookRetryBackoffError(ValueError):
    """Raised when retry projection state/input is noncanonical."""


class MarketBookRetryDisposition(str, Enum):
    READY = "READY"
    BACKOFF = "BACKOFF"
    PROVIDER_RECOVERY_REQUIRED = "PROVIDER_RECOVERY_REQUIRED"
    EXHAUSTED = "EXHAUSTED"
    TERMINAL = "TERMINAL"


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
    if type(value) is not str or len(value) != 64:
        raise MarketBookRetryBackoffError(f"{name} must be lowercase SHA-256")
    if any(char not in "0123456789abcdef" for char in value):
        raise MarketBookRetryBackoffError(f"{name} must be lowercase SHA-256")
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


_EPOCH_NAIVE = datetime(1970, 1, 1)


def _utc_microseconds(value: object, name: str) -> int:
    if type(value) is not datetime:
        raise MarketBookRetryBackoffError(f"{name} must be an exact datetime")
    offset = value.utcoffset()
    if type(offset) is not timedelta:
        raise MarketBookRetryBackoffError(f"{name} must be timezone-aware")
    utc_naive = value.replace(tzinfo=None) - offset
    delta = utc_naive - _EPOCH_NAIVE
    return (
        delta.days * 86_400 * 1_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _incomplete_retry_delay_us(failure_count: int) -> int:
    if type(failure_count) is not int or failure_count < 1:
        raise MarketBookRetryBackoffError(
            "failure_count must be a positive exact integer"
        )
    multiplier = 1 << (failure_count - 1)
    return min(
        MARKETBOOK_INCOMPLETE_RETRY_BASE_DELAY_US * multiplier,
        MARKETBOOK_INCOMPLETE_RETRY_MAX_DELAY_US,
    )


@dataclass(frozen=True, slots=True)
class MarketBookRetryBatchState:
    batch_id: str
    consecutive_incomplete_failures: int
    next_eligible_at_utc_us: int | None
    automatic_retry_exhausted: bool
    provider_recovery_required: bool
    provider_failure_observed_at_utc_us: int | None
    terminal_failure: bool
    last_outcome: MarketBookAttemptOutcome
    last_provider_error_code: str | None = None

    def __post_init__(self) -> None:
        _sha256_token(self.batch_id, "batch_id")
        if (
            type(self.consecutive_incomplete_failures) is not int
            or self.consecutive_incomplete_failures < 0
        ):
            raise MarketBookRetryBackoffError(
                "consecutive_incomplete_failures must be a non-negative exact integer"
            )
        if (
            self.next_eligible_at_utc_us is not None
            and type(self.next_eligible_at_utc_us) is not int
        ):
            raise MarketBookRetryBackoffError(
                "next_eligible_at_utc_us must be an exact integer or None"
            )
        if (
            self.provider_failure_observed_at_utc_us is not None
            and type(self.provider_failure_observed_at_utc_us) is not int
        ):
            raise MarketBookRetryBackoffError(
                "provider_failure_observed_at_utc_us must be an exact integer or None"
            )
        for value, name in (
            (self.automatic_retry_exhausted, "automatic_retry_exhausted"),
            (self.provider_recovery_required, "provider_recovery_required"),
            (self.terminal_failure, "terminal_failure"),
        ):
            if type(value) is not bool:
                raise MarketBookRetryBackoffError(f"{name} must be exact bool")
        if type(self.last_outcome) is not MarketBookAttemptOutcome:
            raise MarketBookRetryBackoffError(
                "last_outcome must be MarketBookAttemptOutcome"
            )
        code = _provider_code(self.last_provider_error_code)

        modes = sum(
            (
                self.automatic_retry_exhausted,
                self.provider_recovery_required,
                self.terminal_failure,
            )
        )
        if modes > 1:
            raise MarketBookRetryBackoffError(
                "retry state cannot hold multiple blocking modes"
            )
        if self.provider_recovery_required:
            if (
                self.last_outcome is not MarketBookAttemptOutcome.PROVIDER_FAILURE
                or code not in _TRANSIENT_PROVIDER_CODES
                or self.next_eligible_at_utc_us is not None
                or self.consecutive_incomplete_failures != 0
                or type(self.provider_failure_observed_at_utc_us) is not int
            ):
                raise MarketBookRetryBackoffError(
                    "provider recovery projection state is contradictory"
                )
        elif self.automatic_retry_exhausted:
            if (
                self.last_outcome is not MarketBookAttemptOutcome.INCOMPLETE_RESPONSE
                or self.consecutive_incomplete_failures
                <= MARKETBOOK_MAX_INCOMPLETE_AUTOMATIC_RETRIES
                or self.next_eligible_at_utc_us is not None
                or code is not None
                or self.provider_failure_observed_at_utc_us is not None
            ):
                raise MarketBookRetryBackoffError(
                    "exhausted incomplete retry state is contradictory"
                )
        elif self.terminal_failure:
            if (
                self.next_eligible_at_utc_us is not None
                or self.provider_failure_observed_at_utc_us is not None
            ):
                raise MarketBookRetryBackoffError(
                    "terminal retry state cannot carry retry eligibility/failure instant"
                )
        else:
            if (
                self.last_outcome
                is not MarketBookAttemptOutcome.INCOMPLETE_RESPONSE
                or self.consecutive_incomplete_failures < 1
                or type(self.next_eligible_at_utc_us) is not int
                or code is not None
                or self.provider_failure_observed_at_utc_us is not None
            ):
                raise MarketBookRetryBackoffError(
                    "active incomplete-response backoff state is contradictory"
                )


def _copy_batch_state(
    batch: MarketBookRetryBatchState,
) -> MarketBookRetryBatchState:
    if type(batch) is not MarketBookRetryBatchState:
        raise MarketBookRetryBackoffError(
            "retry batch copy requires exact MarketBookRetryBatchState"
        )
    batch.__post_init__()
    return MarketBookRetryBatchState(
        batch_id=batch.batch_id,
        consecutive_incomplete_failures=batch.consecutive_incomplete_failures,
        next_eligible_at_utc_us=batch.next_eligible_at_utc_us,
        automatic_retry_exhausted=batch.automatic_retry_exhausted,
        provider_recovery_required=batch.provider_recovery_required,
        provider_failure_observed_at_utc_us=(
            batch.provider_failure_observed_at_utc_us
        ),
        terminal_failure=batch.terminal_failure,
        last_outcome=batch.last_outcome,
        last_provider_error_code=batch.last_provider_error_code,
    )


@dataclass(frozen=True, slots=True)
class MarketBookRetryBackoffState:
    policy_version: str
    plan_id: str
    request_contract_id: str
    provider_source_id: str
    last_observed_at_utc_us: int | None
    batches: tuple[MarketBookRetryBatchState, ...]

    def __post_init__(self) -> None:
        if self.policy_version != MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION:
            raise MarketBookRetryBackoffError(
                "unsupported MarketBook retry projection policy version"
            )
        _sha256_token(self.plan_id, "plan_id")
        _sha256_token(self.request_contract_id, "request_contract_id")
        _source_id(self.provider_source_id)
        if (
            self.last_observed_at_utc_us is not None
            and type(self.last_observed_at_utc_us) is not int
        ):
            raise MarketBookRetryBackoffError(
                "last_observed_at_utc_us must be an exact integer or None"
            )
        if type(self.batches) is not tuple:
            raise MarketBookRetryBackoffError("batches must be an exact tuple")
        ids: list[str] = []
        for batch in self.batches:
            if type(batch) is not MarketBookRetryBatchState:
                raise MarketBookRetryBackoffError(
                    "batches must contain MarketBookRetryBatchState values"
                )
            batch.__post_init__()
            ids.append(batch.batch_id)
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise MarketBookRetryBackoffError(
                "retry batch state must be unique and sorted by batch_id"
            )

    @property
    def evidence_payload(self) -> dict[str, object]:
        return {
            "schema": "betfair-marketbook-retry-backoff-projection-v2",
            "policy_version": self.policy_version,
            "plan_id": self.plan_id,
            "request_contract_id": self.request_contract_id,
            "provider_source_id": self.provider_source_id,
            "last_observed_at_utc_us": self.last_observed_at_utc_us,
            "batches": [
                {
                    "batch_id": batch.batch_id,
                    "consecutive_incomplete_failures": (
                        batch.consecutive_incomplete_failures
                    ),
                    "next_eligible_at_utc_us": batch.next_eligible_at_utc_us,
                    "automatic_retry_exhausted": batch.automatic_retry_exhausted,
                    "provider_recovery_required": batch.provider_recovery_required,
                    "provider_failure_observed_at_utc_us": (
                        batch.provider_failure_observed_at_utc_us
                    ),
                    "terminal_failure": batch.terminal_failure,
                    "last_outcome": batch.last_outcome.value,
                    "last_provider_error_code": batch.last_provider_error_code,
                }
                for batch in self.batches
            ],
            "provider_recovery_deadline_is_authoritative": False,
            "provider_limit_coverage_complete": False,
            "provider_dispatch_authorized": False,
            "provider_observation_authenticated": False,
            "provider_freshness_proven": False,
            "execution_authorized": False,
        }

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
        if type(plan) is not MarketBookReadPlan:
            raise MarketBookRetryBackoffError(
                "plan must be an exact MarketBookReadPlan"
            )
        try:
            envelope = json.loads(encoded) if type(encoded) is str else None
        except json.JSONDecodeError as exc:
            raise MarketBookRetryBackoffError(
                "encoded retry state is not valid JSON"
            ) from exc
        if (
            not isinstance(envelope, Mapping)
            or set(envelope) != {"evidence", "state_id"}
        ):
            raise MarketBookRetryBackoffError(
                "encoded retry state has a noncanonical envelope"
            )
        evidence = envelope["evidence"]
        if not isinstance(evidence, Mapping):
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
        if evidence["schema"] != "betfair-marketbook-retry-backoff-projection-v2":
            raise MarketBookRetryBackoffError(
                "encoded retry evidence has unsupported schema"
            )
        if any(
            evidence[name] is not False
            for name in (
                "provider_recovery_deadline_is_authoritative",
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
        if (
            isinstance(raw_batches, (str, bytes, Mapping))
            or not isinstance(raw_batches, Sequence)
        ):
            raise MarketBookRetryBackoffError(
                "encoded retry batches must be a sequence"
            )
        batches: list[MarketBookRetryBatchState] = []
        batch_keys = {
            "batch_id",
            "consecutive_incomplete_failures",
            "next_eligible_at_utc_us",
            "automatic_retry_exhausted",
            "provider_recovery_required",
            "provider_failure_observed_at_utc_us",
            "terminal_failure",
            "last_outcome",
            "last_provider_error_code",
        }
        for raw in raw_batches:
            if not isinstance(raw, Mapping) or set(raw) != batch_keys:
                raise MarketBookRetryBackoffError(
                    "encoded retry batch has a noncanonical shape"
                )
            try:
                outcome = MarketBookAttemptOutcome(raw["last_outcome"])
            except (TypeError, ValueError) as exc:
                raise MarketBookRetryBackoffError(
                    "encoded retry batch has invalid outcome"
                ) from exc
            batches.append(
                MarketBookRetryBatchState(
                    batch_id=raw["batch_id"],
                    consecutive_incomplete_failures=raw[
                        "consecutive_incomplete_failures"
                    ],
                    next_eligible_at_utc_us=raw["next_eligible_at_utc_us"],
                    automatic_retry_exhausted=raw[
                        "automatic_retry_exhausted"
                    ],
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
        state = cls(
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
        if state.evidence_payload != evidence or state.state_id != envelope["state_id"]:
            raise MarketBookRetryBackoffError(
                "encoded retry state does not match canonical recomputation"
            )
        return state


@dataclass(frozen=True, slots=True)
class MarketBookRetryDecision:
    batch_id: str
    observed_at_utc_us: int
    allowed: bool
    disposition: MarketBookRetryDisposition
    consecutive_retryable_failures: int
    next_eligible_at_utc_us: int | None
    provider_recovery_projection_applied: bool = False
    provider_limit_coverage_complete: bool = False
    provider_dispatch_authorized: bool = False
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        _sha256_token(self.batch_id, "batch_id")
        if type(self.observed_at_utc_us) is not int:
            raise MarketBookRetryBackoffError(
                "observed_at_utc_us must be an exact integer"
            )
        if type(self.allowed) is not bool:
            raise MarketBookRetryBackoffError("allowed must be exact bool")
        if type(self.disposition) is not MarketBookRetryDisposition:
            raise MarketBookRetryBackoffError(
                "disposition must be MarketBookRetryDisposition"
            )
        if (
            type(self.consecutive_retryable_failures) is not int
            or self.consecutive_retryable_failures < 0
        ):
            raise MarketBookRetryBackoffError(
                "consecutive retryable failures must be non-negative"
            )
        if (
            self.next_eligible_at_utc_us is not None
            and type(self.next_eligible_at_utc_us) is not int
        ):
            raise MarketBookRetryBackoffError(
                "next_eligible_at_utc_us must be an exact integer or None"
            )
        if type(self.provider_recovery_projection_applied) is not bool:
            raise MarketBookRetryBackoffError(
                "provider_recovery_projection_applied must be exact bool"
            )
        if self.allowed is not (self.disposition is MarketBookRetryDisposition.READY):
            raise MarketBookRetryBackoffError(
                "retry decision allowed/disposition is contradictory"
            )
        if (
            self.provider_limit_coverage_complete is not False
            or self.provider_dispatch_authorized is not False
            or self.execution_authorized is not False
        ):
            raise MarketBookRetryBackoffError(
                "retry decision cannot claim provider/execution authority"
            )


class MarketBookRetryBackoffGate:
    """Batch-local projection over canonical provider recovery truth."""

    def __init__(
        self,
        plan: MarketBookReadPlan,
        *,
        provider_source_id: str,
        state: MarketBookRetryBackoffState | None = None,
    ) -> None:
        if type(plan) is not MarketBookReadPlan:
            raise MarketBookRetryBackoffError(
                "plan must be an exact MarketBookReadPlan"
            )
        source = _source_id(provider_source_id)
        self._lock = RLock()
        self._plan_id = plan.plan_id
        self._request_contract_id = plan.request_contract_id
        self._provider_source_id = source
        self._batch_ids = frozenset(batch.batch_id for batch in plan.batches)
        self._entries: dict[str, MarketBookRetryBatchState] = {}
        self._last_observed_at_utc_us: int | None = None
        # Volatile projection from canonical SourceHealthStore evidence. It is
        # deliberately absent from snapshots/JSON so restart must revalidate
        # durable provider recovery truth before a provider retry can dispatch.
        self._provider_deadlines: dict[str, tuple[int, int]] = {}
        if state is not None:
            if type(state) is not MarketBookRetryBackoffState:
                raise MarketBookRetryBackoffError(
                    "state must be MarketBookRetryBackoffState or None"
                )
            state.__post_init__()
            if (
                state.plan_id != self._plan_id
                or state.request_contract_id != self._request_contract_id
                or state.provider_source_id != self._provider_source_id
            ):
                raise MarketBookRetryBackoffError(
                    "retry state is bound to another MarketBook plan/source"
                )
            for batch in state.batches:
                if batch.batch_id not in self._batch_ids:
                    raise MarketBookRetryBackoffError(
                        "retry state references an unknown planned batch"
                    )
            self._entries = {
                batch.batch_id: _copy_batch_state(batch)
                for batch in state.batches
            }
            self._last_observed_at_utc_us = state.last_observed_at_utc_us

    @property
    def policy_version(self) -> str:
        return MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION

    def _batch_id(self, batch_id: object) -> str:
        token = _sha256_token(batch_id, "batch_id")
        if token not in self._batch_ids:
            raise MarketBookRetryBackoffError(
                "batch_id is not part of the bound MarketBook plan"
            )
        return token

    def _observe(self, observed_at: object) -> int:
        observed_us = _utc_microseconds(observed_at, "observed_at")
        if (
            self._last_observed_at_utc_us is not None
            and observed_us < self._last_observed_at_utc_us
        ):
            raise MarketBookRetryBackoffError(
                "observed_at must not move backwards"
            )
        self._last_observed_at_utc_us = observed_us
        return observed_us

    def snapshot(self) -> MarketBookRetryBackoffState:
        with self._lock:
            return MarketBookRetryBackoffState(
                policy_version=MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION,
                plan_id=self._plan_id,
                request_contract_id=self._request_contract_id,
                provider_source_id=self._provider_source_id,
                last_observed_at_utc_us=self._last_observed_at_utc_us,
                batches=tuple(
                    _copy_batch_state(self._entries[batch_id])
                    for batch_id in sorted(self._entries)
                ),
            )

    def admit(
        self,
        batch_id: str,
        *,
        observed_at: datetime,
    ) -> MarketBookRetryDecision:
        batch = self._batch_id(batch_id)
        with self._lock:
            observed_us = self._observe(observed_at)
            state = self._entries.get(batch)
            if state is None:
                return MarketBookRetryDecision(
                    batch,
                    observed_us,
                    True,
                    MarketBookRetryDisposition.READY,
                    0,
                    None,
                )
            if state.terminal_failure:
                return MarketBookRetryDecision(
                    batch,
                    observed_us,
                    False,
                    MarketBookRetryDisposition.TERMINAL,
                    state.consecutive_incomplete_failures,
                    None,
                )
            if state.automatic_retry_exhausted:
                return MarketBookRetryDecision(
                    batch,
                    observed_us,
                    False,
                    MarketBookRetryDisposition.EXHAUSTED,
                    state.consecutive_incomplete_failures,
                    None,
                )
            if state.provider_recovery_required:
                provider_projection = self._provider_deadlines.get(batch)
                if provider_projection is None:
                    return MarketBookRetryDecision(
                        batch,
                        observed_us,
                        False,
                        MarketBookRetryDisposition.PROVIDER_RECOVERY_REQUIRED,
                        0,
                        None,
                    )
                deadline_us, provider_streak = provider_projection
                if observed_us < deadline_us:
                    return MarketBookRetryDecision(
                        batch,
                        observed_us,
                        False,
                        MarketBookRetryDisposition.BACKOFF,
                        provider_streak,
                        deadline_us,
                        provider_recovery_projection_applied=True,
                    )
                return MarketBookRetryDecision(
                    batch,
                    observed_us,
                    True,
                    MarketBookRetryDisposition.READY,
                    provider_streak,
                    deadline_us,
                    provider_recovery_projection_applied=True,
                )
            if (
                state.next_eligible_at_utc_us is not None
                and observed_us < state.next_eligible_at_utc_us
            ):
                return MarketBookRetryDecision(
                    batch,
                    observed_us,
                    False,
                    MarketBookRetryDisposition.BACKOFF,
                    state.consecutive_incomplete_failures,
                    state.next_eligible_at_utc_us,
                )
            return MarketBookRetryDecision(
                batch,
                observed_us,
                True,
                MarketBookRetryDisposition.READY,
                state.consecutive_incomplete_failures,
                state.next_eligible_at_utc_us,
            )

    def apply_provider_recovery(
        self,
        batch_id: str,
        *,
        observed_at: datetime,
        health: SourceHealthState,
        config: ContinuousObservationConfig,
        health_type: type[SourceHealthState] = SourceHealthState,
        config_type: type[ContinuousObservationConfig] = ContinuousObservationConfig,
        validate_health=SourceHealthState.validate,
        parse_health_timestamp=parse_source_timestamp,
        provider_backoff_seconds=_provider_backoff_seconds,
    ) -> MarketBookRetryDecision:
        batch = self._batch_id(batch_id)
        if type(health) is not health_type:
            raise MarketBookRetryBackoffError(
                "health must be an exact SourceHealthState"
            )
        validate_health(health)
        if type(config) is not config_type:
            raise MarketBookRetryBackoffError(
                "config must be an exact ContinuousObservationConfig"
            )
        if health.source_id != self._provider_source_id:
            raise MarketBookRetryBackoffError(
                "provider health is bound to another source"
            )
        if (
            health.status != "failed"
            or health.last_failure_kind != "provider_unavailable"
            or health.consecutive_failure_kind_count <= 0
            or health.last_error_at is None
        ):
            raise MarketBookRetryBackoffError(
                "provider recovery requires durable provider_unavailable health"
            )

        with self._lock:
            state = self._entries.get(batch)
            if state is None or not state.provider_recovery_required:
                raise MarketBookRetryBackoffError(
                    "batch is not awaiting provider recovery authority"
                )
            failure_at = parse_health_timestamp(health.last_error_at)
            failure_at_us = _utc_microseconds(
                failure_at,
                "provider_health_failure_at",
            )
            if (
                state.provider_failure_observed_at_utc_us is None
                or failure_at_us
                < state.provider_failure_observed_at_utc_us
            ):
                raise MarketBookRetryBackoffError(
                    "provider health predates current MarketBook failure"
                )
            observed_us = self._observe(observed_at)
            backoff_seconds = provider_backoff_seconds(
                health.consecutive_failure_kind_count,
                config,
            )
            deadline = failure_at + timedelta(seconds=backoff_seconds)
            deadline_us = _utc_microseconds(deadline, "provider_retry_not_before")
            self._provider_deadlines[batch] = (
                deadline_us,
                health.consecutive_failure_kind_count,
            )
            if observed_us < deadline_us:
                return MarketBookRetryDecision(
                    batch,
                    observed_us,
                    False,
                    MarketBookRetryDisposition.BACKOFF,
                    health.consecutive_failure_kind_count,
                    deadline_us,
                    provider_recovery_projection_applied=True,
                )
            return MarketBookRetryDecision(
                batch,
                observed_us,
                True,
                MarketBookRetryDisposition.READY,
                health.consecutive_failure_kind_count,
                deadline_us,
                provider_recovery_projection_applied=True,
            )

    def record_outcome(
        self,
        batch_id: str,
        *,
        observed_at: datetime,
        outcome: MarketBookAttemptOutcome,
        provider_error_code: str | None = None,
    ) -> MarketBookRetryBackoffState:
        batch = self._batch_id(batch_id)
        if type(outcome) is not MarketBookAttemptOutcome:
            raise MarketBookRetryBackoffError(
                "outcome must be MarketBookAttemptOutcome"
            )
        code = _provider_code(provider_error_code)
        if outcome is not MarketBookAttemptOutcome.PROVIDER_FAILURE and code is not None:
            raise MarketBookRetryBackoffError(
                "provider_error_code is only valid for PROVIDER_FAILURE"
            )

        with self._lock:
            observed_us = self._observe(observed_at)
            previous = self._entries.get(batch)

            if outcome is MarketBookAttemptOutcome.EXACT_RESPONSE:
                self._entries.pop(batch, None)
                self._provider_deadlines.pop(batch, None)
                return self.snapshot()

            if outcome in _LOCAL_NO_FAILURE_OUTCOMES:
                return self.snapshot()

            if outcome is MarketBookAttemptOutcome.INCOMPLETE_RESPONSE:
                previous_count = (
                    previous.consecutive_incomplete_failures
                    if previous is not None
                    and previous.last_outcome
                    is MarketBookAttemptOutcome.INCOMPLETE_RESPONSE
                    and not previous.automatic_retry_exhausted
                    else 0
                )
                failure_count = previous_count + 1
                self._provider_deadlines.pop(batch, None)
                if failure_count > MARKETBOOK_MAX_INCOMPLETE_AUTOMATIC_RETRIES:
                    self._entries[batch] = MarketBookRetryBatchState(
                        batch_id=batch,
                        consecutive_incomplete_failures=failure_count,
                        next_eligible_at_utc_us=None,
                        automatic_retry_exhausted=True,
                        provider_recovery_required=False,
                        provider_failure_observed_at_utc_us=None,
                        terminal_failure=False,
                        last_outcome=outcome,
                    )
                else:
                    self._entries[batch] = MarketBookRetryBatchState(
                        batch_id=batch,
                        consecutive_incomplete_failures=failure_count,
                        next_eligible_at_utc_us=(
                            observed_us
                            + _incomplete_retry_delay_us(failure_count)
                        ),
                        automatic_retry_exhausted=False,
                        provider_recovery_required=False,
                        provider_failure_observed_at_utc_us=None,
                        terminal_failure=False,
                        last_outcome=outcome,
                    )
                return self.snapshot()

            if (
                outcome is MarketBookAttemptOutcome.PROVIDER_FAILURE
                and code in _TRANSIENT_PROVIDER_CODES
            ):
                self._provider_deadlines.pop(batch, None)
                self._entries[batch] = MarketBookRetryBatchState(
                    batch_id=batch,
                    consecutive_incomplete_failures=0,
                    next_eligible_at_utc_us=None,
                    automatic_retry_exhausted=False,
                    provider_recovery_required=True,
                    provider_failure_observed_at_utc_us=observed_us,
                    terminal_failure=False,
                    last_outcome=outcome,
                    last_provider_error_code=code,
                )
                return self.snapshot()

            self._provider_deadlines.pop(batch, None)
            self._entries[batch] = MarketBookRetryBatchState(
                batch_id=batch,
                consecutive_incomplete_failures=(
                    previous.consecutive_incomplete_failures
                    if previous is not None
                    and previous.last_outcome
                    is MarketBookAttemptOutcome.INCOMPLETE_RESPONSE
                    else 0
                ),
                next_eligible_at_utc_us=None,
                automatic_retry_exhausted=False,
                provider_recovery_required=False,
                provider_failure_observed_at_utc_us=None,
                terminal_failure=True,
                last_outcome=outcome,
                last_provider_error_code=code,
            )
            return self.snapshot()
