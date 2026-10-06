"""Deterministic, restartable bounded backoff for canonical Betfair MarketBook reads.

This module owns only local retry timing after explicit structural/provider outcomes.
It never sleeps, schedules timers, performs network I/O, infers provider freshness,
or authorizes dispatch/execution. Callers supply an exact causal instant.
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


MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION = (
    "betfair.list-market-book.retry-backoff.v1"
)
MARKETBOOK_RETRY_BASE_DELAY_US = 250_000
MARKETBOOK_RETRY_MAX_DELAY_US = 4_000_000
MARKETBOOK_MAX_AUTOMATIC_RETRIES = 5

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
    """Raised when bounded-retry state or input is noncanonical."""


class MarketBookRetryDisposition(str, Enum):
    READY = "READY"
    BACKOFF = "BACKOFF"
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
            "retry backoff evidence must be canonical JSON data"
        ) from exc


def _sha(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_token(value: object, name: str) -> str:
    if type(value) is not str or len(value) != 64:
        raise MarketBookRetryBackoffError(f"{name} must be lowercase SHA-256")
    if any(char not in "0123456789abcdef" for char in value):
        raise MarketBookRetryBackoffError(f"{name} must be lowercase SHA-256")
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


def _retry_delay_us(failure_count: int) -> int:
    if type(failure_count) is not int or failure_count < 1:
        raise MarketBookRetryBackoffError(
            "failure_count must be a positive exact integer"
        )
    multiplier = 1 << (failure_count - 1)
    return min(
        MARKETBOOK_RETRY_BASE_DELAY_US * multiplier,
        MARKETBOOK_RETRY_MAX_DELAY_US,
    )


@dataclass(frozen=True, slots=True)
class MarketBookRetryBatchState:
    batch_id: str
    consecutive_retryable_failures: int
    next_eligible_at_utc_us: int | None
    automatic_retry_exhausted: bool
    terminal_failure: bool
    last_outcome: MarketBookAttemptOutcome
    last_provider_error_code: str | None = None

    def __post_init__(self) -> None:
        _sha256_token(self.batch_id, "batch_id")
        if (
            type(self.consecutive_retryable_failures) is not int
            or self.consecutive_retryable_failures < 0
        ):
            raise MarketBookRetryBackoffError(
                "consecutive_retryable_failures must be a non-negative exact integer"
            )
        if (
            self.next_eligible_at_utc_us is not None
            and type(self.next_eligible_at_utc_us) is not int
        ):
            raise MarketBookRetryBackoffError(
                "next_eligible_at_utc_us must be an exact integer or None"
            )
        if type(self.automatic_retry_exhausted) is not bool:
            raise MarketBookRetryBackoffError(
                "automatic_retry_exhausted must be exact bool"
            )
        if type(self.terminal_failure) is not bool:
            raise MarketBookRetryBackoffError(
                "terminal_failure must be exact bool"
            )
        if type(self.last_outcome) is not MarketBookAttemptOutcome:
            raise MarketBookRetryBackoffError(
                "last_outcome must be MarketBookAttemptOutcome"
            )
        _provider_code(self.last_provider_error_code)

        if self.automatic_retry_exhausted and self.terminal_failure:
            raise MarketBookRetryBackoffError(
                "retry state cannot be both exhausted and terminal"
            )
        if self.automatic_retry_exhausted:
            if (
                self.consecutive_retryable_failures
                <= MARKETBOOK_MAX_AUTOMATIC_RETRIES
                or self.next_eligible_at_utc_us is not None
            ):
                raise MarketBookRetryBackoffError(
                    "exhausted retry state is contradictory"
                )
        elif self.terminal_failure:
            if self.next_eligible_at_utc_us is not None:
                raise MarketBookRetryBackoffError(
                    "terminal retry state cannot carry next eligibility"
                )
        else:
            if self.consecutive_retryable_failures < 1:
                raise MarketBookRetryBackoffError(
                    "active backoff state requires at least one retryable failure"
                )
            if type(self.next_eligible_at_utc_us) is not int:
                raise MarketBookRetryBackoffError(
                    "active backoff state requires next eligibility"
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
        consecutive_retryable_failures=batch.consecutive_retryable_failures,
        next_eligible_at_utc_us=batch.next_eligible_at_utc_us,
        automatic_retry_exhausted=batch.automatic_retry_exhausted,
        terminal_failure=batch.terminal_failure,
        last_outcome=batch.last_outcome,
        last_provider_error_code=batch.last_provider_error_code,
    )


@dataclass(frozen=True, slots=True)
class MarketBookRetryBackoffState:
    policy_version: str
    plan_id: str
    request_contract_id: str
    last_observed_at_utc_us: int | None
    batches: tuple[MarketBookRetryBatchState, ...]

    def __post_init__(self) -> None:
        if self.policy_version != MARKETBOOK_RETRY_BACKOFF_POLICY_VERSION:
            raise MarketBookRetryBackoffError(
                "unsupported MarketBook retry backoff policy version"
            )
        _sha256_token(self.plan_id, "plan_id")
        _sha256_token(self.request_contract_id, "request_contract_id")
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
            "schema": "betfair-marketbook-retry-backoff-state-v1",
            "policy_version": self.policy_version,
            "plan_id": self.plan_id,
            "request_contract_id": self.request_contract_id,
            "last_observed_at_utc_us": self.last_observed_at_utc_us,
            "batches": [
                {
                    "batch_id": batch.batch_id,
                    "consecutive_retryable_failures": (
                        batch.consecutive_retryable_failures
                    ),
                    "next_eligible_at_utc_us": batch.next_eligible_at_utc_us,
                    "automatic_retry_exhausted": batch.automatic_retry_exhausted,
                    "terminal_failure": batch.terminal_failure,
                    "last_outcome": batch.last_outcome.value,
                    "last_provider_error_code": batch.last_provider_error_code,
                }
                for batch in self.batches
            ],
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
            "last_observed_at_utc_us",
            "batches",
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
        if evidence["schema"] != "betfair-marketbook-retry-backoff-state-v1":
            raise MarketBookRetryBackoffError(
                "encoded retry evidence has unsupported schema"
            )
        if any(
            evidence[name] is not False
            for name in (
                "provider_limit_coverage_complete",
                "provider_dispatch_authorized",
                "provider_observation_authenticated",
                "provider_freshness_proven",
                "execution_authorized",
            )
        ):
            raise MarketBookRetryBackoffError(
                "retry state cannot claim provider or execution authority"
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
            "consecutive_retryable_failures",
            "next_eligible_at_utc_us",
            "automatic_retry_exhausted",
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
                    consecutive_retryable_failures=raw[
                        "consecutive_retryable_failures"
                    ],
                    next_eligible_at_utc_us=raw["next_eligible_at_utc_us"],
                    automatic_retry_exhausted=raw[
                        "automatic_retry_exhausted"
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
                "local retry decision cannot claim provider/execution authority"
            )


class MarketBookRetryBackoffGate:
    """Plan-bound deterministic backoff state with no timers or network I/O."""

    def __init__(
        self,
        plan: MarketBookReadPlan,
        *,
        state: MarketBookRetryBackoffState | None = None,
    ) -> None:
        if type(plan) is not MarketBookReadPlan:
            raise MarketBookRetryBackoffError(
                "plan must be an exact MarketBookReadPlan"
            )
        self._lock = RLock()
        self._plan_id = plan.plan_id
        self._request_contract_id = plan.request_contract_id
        self._batch_ids = frozenset(batch.batch_id for batch in plan.batches)
        self._entries: dict[str, MarketBookRetryBatchState] = {}
        self._last_observed_at_utc_us: int | None = None
        if state is not None:
            if type(state) is not MarketBookRetryBackoffState:
                raise MarketBookRetryBackoffError(
                    "state must be MarketBookRetryBackoffState or None"
                )
            state.__post_init__()
            if (
                state.plan_id != self._plan_id
                or state.request_contract_id != self._request_contract_id
            ):
                raise MarketBookRetryBackoffError(
                    "retry state is bound to another MarketBook plan"
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
                    state.consecutive_retryable_failures,
                    None,
                )
            if state.automatic_retry_exhausted:
                return MarketBookRetryDecision(
                    batch,
                    observed_us,
                    False,
                    MarketBookRetryDisposition.EXHAUSTED,
                    state.consecutive_retryable_failures,
                    None,
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
                    state.consecutive_retryable_failures,
                    state.next_eligible_at_utc_us,
                )
            return MarketBookRetryDecision(
                batch,
                observed_us,
                True,
                MarketBookRetryDisposition.READY,
                state.consecutive_retryable_failures,
                state.next_eligible_at_utc_us,
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
                return self.snapshot()

            if outcome in _LOCAL_NO_FAILURE_OUTCOMES:
                return self.snapshot()

            retryable = (
                outcome is MarketBookAttemptOutcome.INCOMPLETE_RESPONSE
                or (
                    outcome is MarketBookAttemptOutcome.PROVIDER_FAILURE
                    and code in _TRANSIENT_PROVIDER_CODES
                )
            )
            if retryable:
                previous_count = (
                    previous.consecutive_retryable_failures
                    if previous is not None
                    and not previous.terminal_failure
                    else 0
                )
                failure_count = previous_count + 1
                if failure_count > MARKETBOOK_MAX_AUTOMATIC_RETRIES:
                    self._entries[batch] = MarketBookRetryBatchState(
                        batch_id=batch,
                        consecutive_retryable_failures=failure_count,
                        next_eligible_at_utc_us=None,
                        automatic_retry_exhausted=True,
                        terminal_failure=False,
                        last_outcome=outcome,
                        last_provider_error_code=code,
                    )
                else:
                    self._entries[batch] = MarketBookRetryBatchState(
                        batch_id=batch,
                        consecutive_retryable_failures=failure_count,
                        next_eligible_at_utc_us=(
                            observed_us + _retry_delay_us(failure_count)
                        ),
                        automatic_retry_exhausted=False,
                        terminal_failure=False,
                        last_outcome=outcome,
                        last_provider_error_code=code,
                    )
                return self.snapshot()

            self._entries[batch] = MarketBookRetryBatchState(
                batch_id=batch,
                consecutive_retryable_failures=(
                    previous.consecutive_retryable_failures
                    if previous is not None
                    else 0
                ),
                next_eligible_at_utc_us=None,
                automatic_retry_exhausted=False,
                terminal_failure=True,
                last_outcome=outcome,
                last_provider_error_code=code,
            )
            return self.snapshot()
