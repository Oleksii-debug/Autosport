from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from enum import Enum
import hashlib
import json
import math
from threading import RLock
from typing import Mapping, Optional


class Decision(str, Enum):
    ALLOW = "ALLOW"
    BLOCK_COOLDOWN = "BLOCK_COOLDOWN"
    BLOCK_HARD = "BLOCK_HARD"
    ALLOW_PROBE = "ALLOW_PROBE"
    BLOCK_PROBE_IN_FLIGHT = "BLOCK_PROBE_IN_FLIGHT"


class HardBlockReason(str, Enum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    FORBIDDEN = "FORBIDDEN"


@dataclass(frozen=True)
class GatePolicy:
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 300.0
    max_server_delay_seconds: float = 86_400.0
    jitter_fraction: float = 0.10

    def __post_init__(self) -> None:
        values = (
            self.base_backoff_seconds,
            self.max_backoff_seconds,
            self.max_server_delay_seconds,
            self.jitter_fraction,
        )
        if any(type(v) not in {int, float} for v in values):
            raise ValueError("policy values must be exact numbers")
        if not all(math.isfinite(float(v)) for v in values):
            raise ValueError("policy values must be finite")
        if not (self.base_backoff_seconds > 0.0):
            raise ValueError("base_backoff_seconds must be > 0")
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= base_backoff_seconds")
        if self.max_server_delay_seconds <= 0.0:
            raise ValueError("max_server_delay_seconds must be > 0")
        if not (0.0 <= self.jitter_fraction <= 0.5):
            raise ValueError("jitter_fraction must be in [0, 0.5]")


@dataclass
class GateState:
    schema_version: int = 2
    provider: str = ""
    jitter_seed: str = ""
    blocked_until_epoch: float = 0.0
    consecutive_transient_failures: int = 0
    hard_block_reason: Optional[str] = None
    issued_generation: int = 0
    throttle_fence_generation: int = 0
    probe_in_flight: bool = False
    active_probe_generation: int = 0

    def validate(self) -> None:
        if type(self.schema_version) is not int:
            raise ValueError("schema_version must be an exact integer")
        if type(self.provider) is not str:
            raise ValueError("provider must be an exact string")
        if type(self.jitter_seed) is not str:
            raise ValueError("jitter_seed must be an exact string")
        if type(self.blocked_until_epoch) not in {int, float}:
            raise ValueError("blocked_until_epoch must be an exact number")
        for name, value in (
            ("consecutive_transient_failures", self.consecutive_transient_failures),
            ("issued_generation", self.issued_generation),
            ("throttle_fence_generation", self.throttle_fence_generation),
            ("active_probe_generation", self.active_probe_generation),
        ):
            if type(value) is not int:
                raise ValueError(f"{name} must be an exact integer")
        if type(self.probe_in_flight) is not bool:
            raise ValueError("probe_in_flight must be an exact boolean")
        if self.hard_block_reason is not None and type(self.hard_block_reason) is not str:
            raise ValueError("hard_block_reason must be a string or null")
        if self.schema_version != 2:
            raise ValueError("unsupported schema_version")
        if not self.provider or not self.provider.strip():
            raise ValueError("provider must be non-empty")
        if not self.jitter_seed or not self.jitter_seed.strip():
            raise ValueError("jitter_seed must be non-empty")
        if not math.isfinite(self.blocked_until_epoch) or self.blocked_until_epoch < 0:
            raise ValueError("blocked_until_epoch must be finite and >= 0")
        if self.consecutive_transient_failures < 0:
            raise ValueError("consecutive_transient_failures must be >= 0")
        if self.issued_generation < 0 or self.throttle_fence_generation < 0:
            raise ValueError("generation counters must be >= 0")
        if self.throttle_fence_generation > self.issued_generation:
            raise ValueError("throttle fence cannot exceed issued generation")
        if self.active_probe_generation < 0 or self.active_probe_generation > self.issued_generation:
            raise ValueError("active_probe_generation is invalid")
        if self.probe_in_flight != (self.active_probe_generation != 0):
            raise ValueError("probe lease fields are inconsistent")
        if self.hard_block_reason is not None:
            HardBlockReason(self.hard_block_reason)
        if self.hard_block_reason is not None and self.probe_in_flight:
            raise ValueError("hard-blocked state cannot have probe_in_flight")


@dataclass(frozen=True)
class Admission:
    decision: Decision
    retry_at_epoch: Optional[float]
    request_token: Optional[str]
    reason: str


class ProviderAdmissionGate:
    """Fail-closed provider request admission state machine.

    Every allowed request receives a generation-bound token. The token is required
    when recording its result. A transient/hard result raises a generation fence;
    an older concurrent success can never clear newer throttle/hard-block state.
    """

    def __init__(self, state: GateState, policy: GatePolicy | None = None):
        self.state = state
        self.policy = policy or GatePolicy()
        self._lock = RLock()
        self.state.validate()

    @classmethod
    def fresh(
        cls,
        provider: str,
        jitter_seed: str,
        policy: GatePolicy | None = None,
    ) -> "ProviderAdmissionGate":
        return cls(GateState(provider=provider, jitter_seed=jitter_seed), policy)

    def snapshot_json(self) -> str:
        with self._lock:
            self.state.validate()
            return json.dumps(
                asdict(self.state),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

    @classmethod
    def restore_json(
        cls,
        payload: str,
        policy: GatePolicy | None = None,
    ) -> "ProviderAdmissionGate":
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid gate snapshot JSON") from exc
        expected = {
            "schema_version",
            "provider",
            "jitter_seed",
            "blocked_until_epoch",
            "consecutive_transient_failures",
            "hard_block_reason",
            "issued_generation",
            "throttle_fence_generation",
            "probe_in_flight",
            "active_probe_generation",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValueError("gate snapshot fields mismatch")
        state = GateState(**raw)
        # Validate persisted bytes before normalizing process-local probe ownership.
        # Otherwise malformed probe fields could be laundered by restart recovery.
        state.validate()
        # In-memory probe ownership cannot survive process death. Preserve all
        # causal/failure state, but release the lease so one new local probe may run.
        state.probe_in_flight = False
        state.active_probe_generation = 0
        state.validate()
        return cls(state, policy)

    def admit(self, now_epoch: float) -> Admission:
        with self._lock:
            return self._admit_unlocked(now_epoch)

    def _admit_unlocked(self, now_epoch: float) -> Admission:
        now = self._validate_now(now_epoch)
        if self.state.hard_block_reason is not None:
            return Admission(
                Decision.BLOCK_HARD,
                None,
                None,
                self.state.hard_block_reason,
            )

        if now < self.state.blocked_until_epoch:
            return Admission(
                Decision.BLOCK_COOLDOWN,
                self.state.blocked_until_epoch,
                None,
                "provider cooldown active",
            )

        if self.state.consecutive_transient_failures > 0:
            if self.state.probe_in_flight:
                return Admission(
                    Decision.BLOCK_PROBE_IN_FLIGHT,
                    None,
                    None,
                    "half-open probe already in flight",
                )
            generation = self._issue_generation()
            self.state.probe_in_flight = True
            self.state.active_probe_generation = generation
            return Admission(
                Decision.ALLOW_PROBE,
                None,
                self._request_token(generation),
                "single half-open probe",
            )

        generation = self._issue_generation()
        return Admission(
            Decision.ALLOW,
            None,
            self._request_token(generation),
            "provider gate open",
        )

    def record_http_result(
        self,
        *,
        now_epoch: float,
        status_code: int,
        request_token: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        with self._lock:
            self._record_http_result_unlocked(
                now_epoch=now_epoch,
                status_code=status_code,
                request_token=request_token,
                headers=headers,
            )

    def _record_http_result_unlocked(
        self,
        *,
        now_epoch: float,
        status_code: int,
        request_token: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        now = self._validate_now(now_epoch)
        if type(status_code) is not int or not (100 <= status_code <= 599):
            raise ValueError("status_code must be a valid exact integer HTTP status")
        generation = self._validate_request_token(request_token)
        norm_headers = {
            str(k).lower(): str(v).strip()
            for k, v in (headers or {}).items()
        }
        self._release_probe_if_owner(generation)

        if 200 <= status_code <= 399:
            self._record_reachable_response(generation)
            return

        if status_code == 401:
            self._hard_block(HardBlockReason.AUTH_REQUIRED, generation)
            return

        if status_code == 403:
            throttle_until = self._rate_limit_403_until(now, norm_headers)
            if throttle_until is None:
                self._hard_block(HardBlockReason.FORBIDDEN, generation)
            else:
                self._record_transient(
                    now,
                    generation,
                    explicit_until=throttle_until,
                )
            return

        if status_code == 429:
            explicit_until = self._retry_after_until(
                now,
                norm_headers.get("retry-after"),
            )
            self._record_transient(
                now,
                generation,
                explicit_until=explicit_until,
            )
            return

        if status_code in {408, 425} or 500 <= status_code <= 599:
            explicit_until = self._retry_after_until(
                now,
                norm_headers.get("retry-after"),
            )
            self._record_transient(
                now,
                generation,
                explicit_until=explicit_until,
            )
            return

        # A request-specific non-throttling response proves transport reachability.
        # Close only this transport circuit; generation fencing prevents the response
        # from erasing a newer throttle fence or hard block.
        self._record_reachable_response(generation)

    def record_transport_failure(
        self,
        *,
        now_epoch: float,
        request_token: str,
    ) -> None:
        with self._lock:
            now = self._validate_now(now_epoch)
            generation = self._validate_request_token(request_token)
            self._release_probe_if_owner(generation)
            self._record_transient(
                now,
                generation,
                explicit_until=None,
            )

    def clear_hard_block(self) -> None:
        """Explicit operator/configuration recovery after credentials/permission repair."""
        with self._lock:
            self.state.hard_block_reason = None
            self.state.blocked_until_epoch = 0.0
            self.state.consecutive_transient_failures = 0
            self.state.throttle_fence_generation = self.state.issued_generation
            self.state.probe_in_flight = False
            self.state.active_probe_generation = 0

    def _record_reachable_response(self, generation: int) -> None:
        # Hard blocks are operator-authoritative. An outstanding request that later
        # succeeds must never clear a 401/plain-403 observed from another request.
        if self.state.hard_block_reason is not None:
            return
        # Likewise, a success from an older concurrent request cannot clear a newer
        # 429/503/transport failure.
        if generation <= self.state.throttle_fence_generation:
            return
        self.state.blocked_until_epoch = 0.0
        self.state.consecutive_transient_failures = 0
        self.state.throttle_fence_generation = generation

    def _hard_block(
        self,
        reason: HardBlockReason,
        generation: int,
    ) -> None:
        self.state.hard_block_reason = reason.value
        self.state.throttle_fence_generation = max(
            self.state.throttle_fence_generation,
            self.state.issued_generation,
        )
        self.state.probe_in_flight = False
        self.state.active_probe_generation = 0

    def _record_transient(
        self,
        now: float,
        generation: int,
        explicit_until: float | None,
    ) -> None:
        self.state.consecutive_transient_failures += 1
        self.state.throttle_fence_generation = max(
            self.state.throttle_fence_generation,
            self.state.issued_generation,
        )
        computed_until = now + self._backoff_seconds(
            self.state.consecutive_transient_failures
        )
        target = (
            computed_until
            if explicit_until is None
            else max(computed_until, explicit_until)
        )
        self.state.blocked_until_epoch = max(
            self.state.blocked_until_epoch,
            target,
        )

    def _rate_limit_403_until(
        self,
        now: float,
        headers: Mapping[str, str],
    ) -> float | None:
        retry = self._retry_after_until(
            now,
            headers.get("retry-after"),
        )
        if retry is not None:
            return retry
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        if remaining is None or reset is None:
            return None
        try:
            remaining_n = int(remaining)
            reset_epoch = float(reset)
        except (TypeError, ValueError):
            return None
        if remaining_n != 0 or not math.isfinite(reset_epoch):
            return None
        delay = max(0.0, reset_epoch - now)
        delay = min(delay, self.policy.max_server_delay_seconds)
        return now + delay

    def _retry_after_until(
        self,
        now: float,
        raw: str | None,
    ) -> float | None:
        if raw is None:
            return None
        value = raw.strip()
        if not value:
            return None
        if value.isdigit():
            # Avoid converting attacker-controlled giant digit strings to int.
            # Compare decimal magnitude to the configured finite cap first.
            significant = value.lstrip("0") or "0"
            cap_digits = str(
                int(math.ceil(self.policy.max_server_delay_seconds))
            )
            if len(significant) > len(cap_digits):
                delay = self.policy.max_server_delay_seconds
            else:
                delay = min(
                    float(int(significant)),
                    self.policy.max_server_delay_seconds,
                )
            return now + float(delay)
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        try:
            target = dt.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
        if not math.isfinite(target):
            return None
        return now + min(
            max(0.0, target - now),
            self.policy.max_server_delay_seconds,
        )

    def _backoff_seconds(
        self,
        failure_count: int,
    ) -> float:
        exponent = max(0, failure_count - 1)
        ratio = (
            self.policy.max_backoff_seconds
            / self.policy.base_backoff_seconds
        )
        cap_exponent = (
            0
            if ratio <= 1.0
            else math.ceil(math.log2(ratio))
        )
        if exponent >= cap_exponent:
            raw = self.policy.max_backoff_seconds
        else:
            raw = (
                self.policy.base_backoff_seconds
                * (2.0 ** exponent)
            )
        if self.policy.jitter_fraction == 0.0:
            return raw
        digest = hashlib.sha256(
            (
                f"{self.state.provider}\0"
                f"{self.state.jitter_seed}\0"
                f"{failure_count}"
            ).encode("utf-8")
        ).digest()
        unit = (
            int.from_bytes(digest[:8], "big")
            / float((1 << 64) - 1)
        )
        signed = (unit * 2.0) - 1.0
        jittered = max(
            0.0,
            raw
            * (
                1.0
                + signed * self.policy.jitter_fraction
            ),
        )
        return min(self.policy.max_backoff_seconds, jittered)

    def _issue_generation(self) -> int:
        self.state.issued_generation += 1
        return self.state.issued_generation

    def _request_token(
        self,
        generation: int,
    ) -> str:
        digest = hashlib.sha256(
            (
                f"{self.state.provider}\0"
                f"{self.state.jitter_seed}\0"
                f"request\0{generation}"
            ).encode("utf-8")
        ).hexdigest()[:24]
        return f"{generation}:{digest}"

    def _validate_request_token(
        self,
        token: str,
    ) -> int:
        if not isinstance(token, str) or ":" not in token:
            raise ValueError("invalid request token")
        raw_generation, _ = token.split(":", 1)
        try:
            generation = int(raw_generation)
        except ValueError as exc:
            raise ValueError("invalid request token generation") from exc
        if (
            generation <= 0
            or generation > self.state.issued_generation
        ):
            raise ValueError(
                "request token generation was not issued"
            )
        if token != self._request_token(generation):
            raise ValueError(
                "request token integrity mismatch"
            )
        return generation

    def _release_probe_if_owner(
        self,
        generation: int,
    ) -> None:
        if (
            self.state.probe_in_flight
            and generation == self.state.active_probe_generation
        ):
            self.state.probe_in_flight = False
            self.state.active_probe_generation = 0

    @staticmethod
    def _validate_now(
        now_epoch: float,
    ) -> float:
        if type(now_epoch) not in {int, float}:
            raise ValueError("now_epoch must be an exact number")
        now = float(now_epoch)
        if not math.isfinite(now) or now < 0.0:
            raise ValueError(
                "now_epoch must be finite and >= 0"
            )
        return now
