"""Durable account-scoped Smarkets request-budget governance.

This module is deliberately transport-governance only.  It coordinates observed
provider rate-budget state across sessions/processes for one canonical account;
it does not establish provider origin, API entitlement, execution permission,
settlement truth, bankroll authority, or real-money readiness.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Final


_SCHEMA_VERSION: Final[int] = 1
_RATE_LIMIT_ERROR: Final[str] = "RATE_LIMIT_EXCEEDED"


class SmarketsRateBudgetError(ValueError):
    """Raised when rate-budget evidence or durable state is unsafe to use."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise SmarketsRateBudgetError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SmarketsRateBudgetError(f"{name} must be UTF-8") from exc
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise SmarketsRateBudgetError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise SmarketsRateBudgetError(f"{name} must be a non-negative integer")
    return value


def _utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise SmarketsRateBudgetError(f"{name} must be a UTC datetime")
    return value


def _dt_text(value: datetime) -> str:
    return _utc(value, "datetime").isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_dt(value: str, name: str) -> datetime:
    text = _text(value, name)
    if not text.endswith("Z"):
        raise SmarketsRateBudgetError(f"{name} must use canonical UTC Z notation")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise SmarketsRateBudgetError(f"{name} must be ISO-8601") from exc
    if _dt_text(parsed) != text:
        raise SmarketsRateBudgetError(f"{name} must use canonical microsecond UTC encoding")
    return parsed


def _digest(payload: dict[str, object]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class SmarketsRateBudgetObservation:
    """Normalized provider budget observation for one account/window.

    Header parsing stays in the HTTP adapter.  ``reset_at`` is the already
    normalized UTC boundary derived from the provider's reset header.  Keeping
    parsing out of this authority prevents accidental assumptions about raw
    header units while still making the durable decision deterministic.
    """

    account_id: str
    limit: int
    remaining: int
    window_seconds: int
    observed_at: datetime
    reset_at: datetime
    http_status: int
    error_type: str | None = None

    def __post_init__(self) -> None:
        _text(self.account_id, "account_id")
        _positive_int(self.limit, "limit")
        _nonnegative_int(self.remaining, "remaining")
        if self.remaining > self.limit:
            raise SmarketsRateBudgetError("remaining must not exceed limit")
        _positive_int(self.window_seconds, "window_seconds")
        observed_at = _utc(self.observed_at, "observed_at")
        reset_at = _utc(self.reset_at, "reset_at")
        if reset_at < observed_at:
            raise SmarketsRateBudgetError("reset_at must not precede observed_at")
        if type(self.http_status) is not int or isinstance(self.http_status, bool) or not 100 <= self.http_status <= 599:
            raise SmarketsRateBudgetError("http_status must be an integer HTTP status")
        if self.error_type is not None:
            _text(self.error_type, "error_type")
        if self.http_status == 429 and self.error_type != _RATE_LIMIT_ERROR:
            raise SmarketsRateBudgetError("HTTP 429 must carry RATE_LIMIT_EXCEEDED")
        if self.http_status != 429 and self.error_type == _RATE_LIMIT_ERROR:
            raise SmarketsRateBudgetError("RATE_LIMIT_EXCEEDED is only valid with HTTP 429")

    @property
    def evidence_sha256(self) -> str:
        return _digest(self.payload())

    def payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.smarkets-rate-budget-observation",
            "schema_version": _SCHEMA_VERSION,
            "account_id": self.account_id,
            "limit": self.limit,
            "remaining": self.remaining,
            "window_seconds": self.window_seconds,
            "observed_at": _dt_text(self.observed_at),
            "reset_at": _dt_text(self.reset_at),
            "http_status": self.http_status,
            "error_type": self.error_type,
        }


@dataclass(frozen=True, slots=True)
class SmarketsRateBudgetDecision:
    request_budget_available: bool
    reason: str
    account_id: str
    observed_remaining: int
    effective_remaining_before: int
    effective_remaining_after: int
    reset_at: datetime | None
    observation_sha256: str | None
    provider_origin_proven: bool = False
    execution_authorized: bool = False
    real_money_execution: bool = False

    def __post_init__(self) -> None:
        if type(self.request_budget_available) is not bool:
            raise SmarketsRateBudgetError("request_budget_available must be bool")
        _text(self.reason, "reason")
        _text(self.account_id, "account_id")
        _nonnegative_int(self.observed_remaining, "observed_remaining")
        _nonnegative_int(self.effective_remaining_before, "effective_remaining_before")
        _nonnegative_int(self.effective_remaining_after, "effective_remaining_after")
        if self.request_budget_available:
            if self.effective_remaining_before <= 0 or self.effective_remaining_after != self.effective_remaining_before - 1:
                raise SmarketsRateBudgetError("positive decision must consume exactly one reservation")
        elif self.effective_remaining_after != self.effective_remaining_before:
            raise SmarketsRateBudgetError("negative decision must not consume budget")
        if self.reset_at is not None:
            _utc(self.reset_at, "reset_at")
        if self.observation_sha256 is not None:
            if len(self.observation_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.observation_sha256):
                raise SmarketsRateBudgetError("observation_sha256 must be lowercase SHA-256 hex")
        if self.provider_origin_proven is not False or self.execution_authorized is not False or self.real_money_execution is not False:
            raise SmarketsRateBudgetError("rate-budget governance cannot grant higher authority")


class SmarketsAccountRateBudget:
    """SQLite-backed atomic reservations for one Smarkets account.

    The durable row is keyed only by canonical account identity.  Session
    generation is accepted for call-site auditing/validation but intentionally
    never participates in quota identity, so relogin/restart cannot mint a new
    pool.  Every positive reservation executes under ``BEGIN IMMEDIATE`` and is
    therefore serialized with other processes using the same SQLite authority.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        account_id: str,
        approved_limit: int,
        approved_window_seconds: int,
    ) -> None:
        self._path = Path(db_path)
        self._account_id = _text(account_id, "account_id")
        self._approved_limit = _positive_int(approved_limit, "approved_limit")
        self._approved_window_seconds = _positive_int(
            approved_window_seconds, "approved_window_seconds"
        )
        if self._path.exists() and self._path.is_symlink():
            raise SmarketsRateBudgetError("rate-budget database path must not be a symlink")
        if not self._path.parent.exists() or not self._path.parent.is_dir():
            raise SmarketsRateBudgetError("rate-budget database parent directory must exist")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._path), timeout=0.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS smarkets_rate_budget (
                        account_id TEXT PRIMARY KEY,
                        observation_sha256 TEXT NOT NULL,
                        provider_limit INTEGER NOT NULL,
                        provider_remaining INTEGER NOT NULL,
                        window_seconds INTEGER NOT NULL,
                        observed_at TEXT NOT NULL,
                        reset_at TEXT NOT NULL,
                        http_status INTEGER NOT NULL,
                        error_type TEXT,
                        reserved_count INTEGER NOT NULL CHECK (reserved_count >= 0)
                    )
                    """
                )
        except sqlite3.Error as exc:
            raise SmarketsRateBudgetError("rate-budget database initialization failed") from exc

    def record_observation(self, observation: SmarketsRateBudgetObservation) -> None:
        if not isinstance(observation, SmarketsRateBudgetObservation):
            raise SmarketsRateBudgetError("observation must be SmarketsRateBudgetObservation")
        if observation.account_id != self._account_id:
            raise SmarketsRateBudgetError("rate-budget observation account mismatch")
        if observation.window_seconds != self._approved_window_seconds:
            raise SmarketsRateBudgetError(
                "provider and approved rate windows must match before positive use"
            )

        digest = observation.evidence_sha256
        observed_text = _dt_text(observation.observed_at)
        reset_text = _dt_text(observation.reset_at)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT * FROM smarkets_rate_budget WHERE account_id = ?",
                    (self._account_id,),
                ).fetchone()
                if current is not None:
                    current_observed = _parse_dt(current["observed_at"], "stored observed_at")
                    current_reset = _parse_dt(current["reset_at"], "stored reset_at")
                    if observation.observed_at < current_observed:
                        raise SmarketsRateBudgetError("rate-budget observation regressed in time")
                    if observation.observed_at == current_observed:
                        if digest != current["observation_sha256"]:
                            raise SmarketsRateBudgetError(
                                "conflicting rate-budget observations share observed_at"
                            )
                        connection.execute("COMMIT")
                        return
                    if observation.reset_at == current_reset and observation.remaining > current["provider_remaining"]:
                        raise SmarketsRateBudgetError(
                            "remaining budget increased inside the same reset window"
                        )
                    if observation.reset_at < current_reset:
                        raise SmarketsRateBudgetError("rate-budget reset boundary regressed")

                connection.execute(
                    """
                    INSERT INTO smarkets_rate_budget (
                        account_id, observation_sha256, provider_limit,
                        provider_remaining, window_seconds, observed_at, reset_at,
                        http_status, error_type, reserved_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(account_id) DO UPDATE SET
                        observation_sha256 = excluded.observation_sha256,
                        provider_limit = excluded.provider_limit,
                        provider_remaining = excluded.provider_remaining,
                        window_seconds = excluded.window_seconds,
                        observed_at = excluded.observed_at,
                        reset_at = excluded.reset_at,
                        http_status = excluded.http_status,
                        error_type = excluded.error_type,
                        reserved_count = 0
                    """,
                    (
                        self._account_id,
                        digest,
                        observation.limit,
                        observation.remaining,
                        observation.window_seconds,
                        observed_text,
                        reset_text,
                        observation.http_status,
                        observation.error_type,
                    ),
                )
                connection.execute("COMMIT")
        except SmarketsRateBudgetError:
            raise
        except sqlite3.Error as exc:
            raise SmarketsRateBudgetError("rate-budget observation update failed closed") from exc

    def reserve_request(
        self,
        *,
        account_id: str,
        session_generation: str,
        now: datetime,
    ) -> SmarketsRateBudgetDecision:
        account = _text(account_id, "account_id")
        _text(session_generation, "session_generation")
        instant = _utc(now, "now")
        if account != self._account_id:
            raise SmarketsRateBudgetError("rate-budget reservation account mismatch")

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM smarkets_rate_budget WHERE account_id = ?",
                    (self._account_id,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return self._decision(False, "budget_unobserved", 0, 0, 0, None, None)

                reset_at = _parse_dt(row["reset_at"], "stored reset_at")
                observed_remaining = _nonnegative_int(row["provider_remaining"], "stored provider_remaining")
                provider_limit = _positive_int(row["provider_limit"], "stored provider_limit")
                reserved = _nonnegative_int(row["reserved_count"], "stored reserved_count")

                provider_used = provider_limit - observed_remaining
                if provider_used < 0:
                    raise SmarketsRateBudgetError("stored provider usage is invalid")
                approved_remaining = max(0, self._approved_limit - provider_used)
                base_remaining = min(observed_remaining, approved_remaining)
                effective_before = max(0, base_remaining - reserved)

                reason: str | None = None
                status = row["http_status"]
                error_type = row["error_type"]
                if instant >= reset_at:
                    reason = "reset_refresh_required"
                elif status == 429:
                    if error_type != _RATE_LIMIT_ERROR:
                        raise SmarketsRateBudgetError("stored 429 evidence is malformed")
                    reason = "provider_rate_limited"
                elif 500 <= status <= 599:
                    reason = "transport_ambiguous_wait"
                elif not 200 <= status <= 299:
                    reason = "non_success_budget_unknown"
                elif effective_before <= 0:
                    reason = "budget_exhausted"

                if reason is not None:
                    connection.execute("COMMIT")
                    return self._decision(
                        False,
                        reason,
                        observed_remaining,
                        effective_before,
                        effective_before,
                        reset_at,
                        row["observation_sha256"],
                    )

                connection.execute(
                    "UPDATE smarkets_rate_budget SET reserved_count = reserved_count + 1 WHERE account_id = ?",
                    (self._account_id,),
                )
                connection.execute("COMMIT")
                return self._decision(
                    True,
                    "budget_reserved",
                    observed_remaining,
                    effective_before,
                    effective_before - 1,
                    reset_at,
                    row["observation_sha256"],
                )
        except SmarketsRateBudgetError:
            raise
        except sqlite3.Error as exc:
            raise SmarketsRateBudgetError("rate-budget reservation failed closed") from exc

    def _decision(
        self,
        available: bool,
        reason: str,
        observed_remaining: int,
        before: int,
        after: int,
        reset_at: datetime | None,
        observation_sha256: str | None,
    ) -> SmarketsRateBudgetDecision:
        return SmarketsRateBudgetDecision(
            request_budget_available=available,
            reason=reason,
            account_id=self._account_id,
            observed_remaining=observed_remaining,
            effective_remaining_before=before,
            effective_remaining_after=after,
            reset_at=reset_at,
            observation_sha256=observation_sha256,
        )
