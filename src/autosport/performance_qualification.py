from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Mapping

BUDGET_SCHEMA = "autosport.endurance-performance-budget"
BUDGET_SCHEMA_VERSION = 1
QUALIFICATION_SCHEMA = "autosport.endurance-performance-qualification"
QUALIFICATION_SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")


class PerformanceQualificationError(ValueError):
    """Performance evidence or budget is malformed or not correctness-qualified."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PerformanceQualificationError("performance evidence is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _detached_report_snapshot(report: Mapping[str, object]) -> dict[str, object]:
    """Materialize one detached report state for validation and identity binding."""

    if not isinstance(report, Mapping):
        raise PerformanceQualificationError("endurance report must be an object")
    try:
        materialized = dict(report)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise PerformanceQualificationError(
            "endurance report could not be snapshotted"
        ) from exc

    active_containers: set[int] = set()

    def detach(value: object) -> object:
        if value is None or type(value) in (str, int, float, bool):
            return value

        if type(value) is dict:
            marker = id(value)
            if marker in active_containers:
                raise PerformanceQualificationError(
                    "endurance report contains a circular object"
                )
            active_containers.add(marker)
            try:
                detached: dict[str, object] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise PerformanceQualificationError(
                            "endurance report object keys must be strings"
                        )
                    try:
                        key.encode("utf-8", errors="strict")
                    except UnicodeEncodeError as exc:
                        raise PerformanceQualificationError(
                            "endurance report object keys must be valid UTF-8"
                        ) from exc
                    detached[key] = detach(item)
                return detached
            finally:
                active_containers.remove(marker)

        if type(value) is list:
            marker = id(value)
            if marker in active_containers:
                raise PerformanceQualificationError(
                    "endurance report contains a circular object"
                )
            active_containers.add(marker)
            try:
                return [detach(item) for item in value]
            finally:
                active_containers.remove(marker)

        if type(value) is tuple:
            marker = id(value)
            if marker in active_containers:
                raise PerformanceQualificationError(
                    "endurance report contains a circular object"
                )
            active_containers.add(marker)
            try:
                return tuple(detach(item) for item in value)
            finally:
                active_containers.remove(marker)

        raise PerformanceQualificationError(
            "endurance report contains an unsupported non-JSON value"
        )

    snapshot = detach(materialized)
    if type(snapshot) is not dict:
        raise PerformanceQualificationError("endurance report must snapshot to an object")
    _canonical_json(snapshot)
    return snapshot


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise PerformanceQualificationError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PerformanceQualificationError(f"{name} must be valid UTF-8") from exc
    return value


def _source_sha(value: object) -> str:
    sha = _text(value, "source_sha")
    if len(sha) != 40 or any(character not in _HEX for character in sha):
        raise PerformanceQualificationError("source_sha must be lowercase 40-character Git SHA-1 hex")
    return sha


def _sha256_hex(value: object, name: str) -> str:
    digest = _text(value, name)
    if len(digest) != 64 or any(character not in _HEX for character in digest):
        raise PerformanceQualificationError(f"{name} must be lowercase SHA-256 hex")
    return digest


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise PerformanceQualificationError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise PerformanceQualificationError(f"{name} must be a non-negative integer")
    return value


def _positive_float(value: object, name: str) -> float:
    if type(value) not in (int, float):
        raise PerformanceQualificationError(f"{name} must be a finite positive number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise PerformanceQualificationError(
            f"{name} must be a finite positive number"
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise PerformanceQualificationError(f"{name} must be a finite positive number")
    return number


def _optional_positive_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, name)


def _optional_positive_float(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _positive_float(value, name)


@dataclass(frozen=True, slots=True)
class PerformanceBudget:
    min_history_events: int | None = None
    min_accepted_events_per_second: float | None = None
    max_peak_traced_memory_bytes: int | None = None
    max_ingest_elapsed_seconds: float | None = None
    max_replay_elapsed_seconds: float | None = None
    max_restart_elapsed_seconds: float | None = None

    def __post_init__(self) -> None:
        normalized = {
            "min_history_events": _optional_positive_int(
                self.min_history_events, "min_history_events"
            ),
            "min_accepted_events_per_second": _optional_positive_float(
                self.min_accepted_events_per_second, "min_accepted_events_per_second"
            ),
            "max_peak_traced_memory_bytes": _optional_positive_int(
                self.max_peak_traced_memory_bytes, "max_peak_traced_memory_bytes"
            ),
            "max_ingest_elapsed_seconds": _optional_positive_float(
                self.max_ingest_elapsed_seconds, "max_ingest_elapsed_seconds"
            ),
            "max_replay_elapsed_seconds": _optional_positive_float(
                self.max_replay_elapsed_seconds, "max_replay_elapsed_seconds"
            ),
            "max_restart_elapsed_seconds": _optional_positive_float(
                self.max_restart_elapsed_seconds, "max_restart_elapsed_seconds"
            ),
        }
        if all(value is None for value in normalized.values()):
            raise PerformanceQualificationError("performance budget must constrain at least one metric")
        for name, value in normalized.items():
            object.__setattr__(self, name, value)

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PerformanceBudget":
        if not isinstance(raw, Mapping):
            raise PerformanceQualificationError("performance budget must be an object")
        expected = {
            "schema",
            "schema_version",
            "min_history_events",
            "min_accepted_events_per_second",
            "max_peak_traced_memory_bytes",
            "max_ingest_elapsed_seconds",
            "max_replay_elapsed_seconds",
            "max_restart_elapsed_seconds",
        }
        unexpected = set(raw) - expected
        if unexpected:
            raise PerformanceQualificationError(
                "performance budget has unexpected fields: " + ",".join(sorted(unexpected))
            )
        missing = expected - set(raw)
        if missing:
            raise PerformanceQualificationError(
                "performance budget is missing explicit fields: " + ",".join(sorted(missing))
            )
        if raw.get("schema") != BUDGET_SCHEMA:
            raise PerformanceQualificationError("unsupported performance budget schema")
        if (
            type(raw.get("schema_version")) is not int
            or raw.get("schema_version") != BUDGET_SCHEMA_VERSION
        ):
            raise PerformanceQualificationError("unsupported performance budget schema version")
        return cls(
            min_history_events=raw.get("min_history_events"),  # type: ignore[arg-type]
            min_accepted_events_per_second=raw.get("min_accepted_events_per_second"),  # type: ignore[arg-type]
            max_peak_traced_memory_bytes=raw.get("max_peak_traced_memory_bytes"),  # type: ignore[arg-type]
            max_ingest_elapsed_seconds=raw.get("max_ingest_elapsed_seconds"),  # type: ignore[arg-type]
            max_replay_elapsed_seconds=raw.get("max_replay_elapsed_seconds"),  # type: ignore[arg-type]
            max_restart_elapsed_seconds=raw.get("max_restart_elapsed_seconds"),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": BUDGET_SCHEMA,
            "schema_version": BUDGET_SCHEMA_VERSION,
            "min_history_events": self.min_history_events,
            "min_accepted_events_per_second": self.min_accepted_events_per_second,
            "max_peak_traced_memory_bytes": self.max_peak_traced_memory_bytes,
            "max_ingest_elapsed_seconds": self.max_ingest_elapsed_seconds,
            "max_replay_elapsed_seconds": self.max_replay_elapsed_seconds,
            "max_restart_elapsed_seconds": self.max_restart_elapsed_seconds,
        }


@dataclass(frozen=True, slots=True)
class MetricQualification:
    metric: str
    comparator: str
    observed: int | float
    threshold: int | float
    status: str

    def __post_init__(self) -> None:
        metric = _text(self.metric, "metric")
        if self.comparator not in (">=", "<="):
            raise PerformanceQualificationError("metric comparator must be >= or <=")
        if type(self.observed) not in (int, float) or type(self.threshold) not in (int, float):
            raise PerformanceQualificationError("metric observed/threshold must be numbers")
        try:
            observed = float(self.observed)
            threshold = float(self.threshold)
        except (OverflowError, TypeError, ValueError) as exc:
            raise PerformanceQualificationError(
                "metric observed/threshold must be finite"
            ) from exc
        if not math.isfinite(observed) or not math.isfinite(threshold):
            raise PerformanceQualificationError("metric observed/threshold must be finite")
        expected = (
            "PASS"
            if (observed >= threshold if self.comparator == ">=" else observed <= threshold)
            else "FAIL"
        )
        if self.status != expected:
            raise PerformanceQualificationError("metric status does not match observed threshold")
        object.__setattr__(self, "metric", metric)

    def to_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "comparator": self.comparator,
            "observed": self.observed,
            "threshold": self.threshold,
            "status": self.status,
        }


def _qualification_identity_payload(
    *,
    source_sha: str,
    machine_profile: str,
    report_sha256: str,
    budget: PerformanceBudget,
    checks: tuple[MetricQualification, ...],
    status: str,
) -> dict[str, object]:
    return {
        "schema": QUALIFICATION_SCHEMA,
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "source_sha": source_sha,
        "machine_profile": machine_profile,
        "report_sha256": report_sha256,
        "budget": budget.to_dict(),
        "checks": [check.to_dict() for check in checks],
        "status": status,
        "target_machine_acceptance": False,
    }


def _expected_check_roster(budget: PerformanceBudget) -> dict[str, tuple[str, int | float]]:
    roster: dict[str, tuple[str, int | float]] = {}
    candidates = (
        ("history_events", ">=", budget.min_history_events),
        ("accepted_events_per_second", ">=", budget.min_accepted_events_per_second),
        ("peak_traced_memory_bytes", "<=", budget.max_peak_traced_memory_bytes),
        ("ingest_elapsed_seconds", "<=", budget.max_ingest_elapsed_seconds),
        ("replay_elapsed_seconds", "<=", budget.max_replay_elapsed_seconds),
        ("restart_elapsed_seconds", "<=", budget.max_restart_elapsed_seconds),
    )
    for metric, comparator, threshold in candidates:
        if threshold is not None:
            roster[metric] = (comparator, threshold)
    return roster


def _validate_check_roster(
    budget: PerformanceBudget, checks: tuple[MetricQualification, ...]
) -> None:
    if type(checks) is not tuple or not checks:
        raise PerformanceQualificationError("checks must be a non-empty tuple")
    if any(not isinstance(check, MetricQualification) for check in checks):
        raise PerformanceQualificationError("checks must contain MetricQualification values")

    expected_roster = _expected_check_roster(budget)
    actual_metrics: set[str] = set()
    for check in checks:
        if check.metric in actual_metrics:
            raise PerformanceQualificationError("checks contain duplicate performance metric")
        expected = expected_roster.get(check.metric)
        if expected is None:
            raise PerformanceQualificationError("checks contain unconstrained performance metric")
        expected_comparator, expected_threshold = expected
        if check.comparator != expected_comparator:
            raise PerformanceQualificationError("check comparator does not match performance budget")
        if type(check.threshold) is not type(expected_threshold) or check.threshold != expected_threshold:
            raise PerformanceQualificationError("check threshold does not match performance budget")
        actual_metrics.add(check.metric)
    if actual_metrics != set(expected_roster):
        raise PerformanceQualificationError("checks do not exactly cover performance budget")


@dataclass(frozen=True, slots=True, init=False)
class PerformanceQualification:
    source_sha: str
    machine_profile: str
    report_sha256: str
    budget: PerformanceBudget
    checks: tuple[MetricQualification, ...]
    status: str = field(init=False)
    qualification_id: str = field(init=False)
    target_machine_acceptance: bool = field(default=False, init=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PerformanceQualificationError(
            "PerformanceQualification must be created by qualify_endurance_report"
        )

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(check.metric for check in self.checks if check.status != "PASS")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": QUALIFICATION_SCHEMA,
            "schema_version": QUALIFICATION_SCHEMA_VERSION,
            "source_sha": self.source_sha,
            "machine_profile": self.machine_profile,
            "report_sha256": self.report_sha256,
            "budget": self.budget.to_dict(),
            "checks": [check.to_dict() for check in self.checks],
            "status": self.status,
            "failures": list(self.failures),
            "qualification_id": self.qualification_id,
            "target_machine_acceptance": self.target_machine_acceptance,
        }


def _endurance_fingerprint_payload(raw: Mapping[str, object]) -> dict[str, object]:
    fields = (
        "config",
        "history_events",
        "current_quotes",
        "accepted_first_pass",
        "accepted_duplicate_pass",
        "replay_dataset_hash",
        "mirror_dataset_hash",
        "restart_hashes",
        "restart_projection_counts",
        "paper_tickets_opened",
        "paper_tickets_settled_first_pass",
        "paper_tickets_settled_second_pass",
        "paper_tickets_won",
        "paper_payout_total",
        "paper_expected_balance",
        "paper_balance_after_restart",
        "paper_economics_verified",
        "corrupt_health_rejected",
        "corrupt_paper_book_rejected",
        "real_money_execution",
    )
    missing = [field for field in fields if field not in raw]
    if missing:
        raise PerformanceQualificationError(
            "endurance report is missing invariant fields: " + ",".join(missing)
        )
    return {field: raw[field] for field in fields}


def _performance_observation_payload(
    *, source_sha: str, observed: Mapping[str, int | float]
) -> dict[str, object]:
    return {
        "source_sha": source_sha,
        "history_events": observed["history_events"],
        "accepted_events_per_second": observed["accepted_events_per_second"],
        "peak_traced_memory_bytes": observed["peak_traced_memory_bytes"],
        "ingest_elapsed_seconds": observed["ingest_elapsed_seconds"],
        "replay_elapsed_seconds": observed["replay_elapsed_seconds"],
        "restart_elapsed_seconds": observed["restart_elapsed_seconds"],
    }


def _validate_report(
    raw: Mapping[str, object], *, expected_source_sha: str
) -> dict[str, int | float]:
    if not isinstance(raw, Mapping):
        raise PerformanceQualificationError("endurance report must be an object")
    if raw.get("status") != "PASS":
        raise PerformanceQualificationError("endurance report correctness status must be PASS")
    failures = raw.get("failures")
    if type(failures) is not list or failures:
        raise PerformanceQualificationError("PASS endurance report must contain an empty failures list")
    if raw.get("real_money_execution") is not False:
        raise PerformanceQualificationError("endurance report must preserve real_money_execution=false")
    report_source_sha = _source_sha(raw.get("source_sha"))
    if report_source_sha != expected_source_sha:
        raise PerformanceQualificationError("endurance report source_sha does not match expected source")
    fingerprint = _sha256_hex(
        raw.get("stable_invariant_fingerprint"), "stable_invariant_fingerprint"
    )
    expected_fingerprint = _digest(_endurance_fingerprint_payload(raw))
    if fingerprint != expected_fingerprint:
        raise PerformanceQualificationError("endurance stable invariant fingerprint mismatch")
    observed: dict[str, int | float] = {
        "history_events": _positive_int(raw.get("history_events"), "history_events"),
        "accepted_events_per_second": _positive_float(
            raw.get("accepted_events_per_second"), "accepted_events_per_second"
        ),
        "peak_traced_memory_bytes": _nonnegative_int(
            raw.get("peak_traced_memory_bytes"), "peak_traced_memory_bytes"
        ),
        "ingest_elapsed_seconds": _positive_float(
            raw.get("ingest_elapsed_seconds"), "ingest_elapsed_seconds"
        ),
        "replay_elapsed_seconds": _positive_float(
            raw.get("replay_elapsed_seconds"), "replay_elapsed_seconds"
        ),
        "restart_elapsed_seconds": _positive_float(
            raw.get("restart_elapsed_seconds"), "restart_elapsed_seconds"
        ),
    }
    observation_fingerprint = _sha256_hex(
        raw.get("performance_observation_fingerprint"),
        "performance_observation_fingerprint",
    )
    expected_observation_fingerprint = _digest(
        _performance_observation_payload(
            source_sha=report_source_sha,
            observed=observed,
        )
    )
    if observation_fingerprint != expected_observation_fingerprint:
        raise PerformanceQualificationError("performance observation fingerprint mismatch")
    return observed


def qualify_endurance_report(
    report: Mapping[str, object],
    budget: PerformanceBudget,
    *,
    source_sha: str,
    machine_profile: str,
) -> PerformanceQualification:
    if not isinstance(budget, PerformanceBudget):
        raise PerformanceQualificationError("budget must be PerformanceBudget")
    canonical_source_sha = _source_sha(source_sha)
    canonical_machine_profile = _text(machine_profile, "machine_profile")
    report_snapshot = _detached_report_snapshot(report)
    observed = _validate_report(
        report_snapshot,
        expected_source_sha=canonical_source_sha,
    )
    report_sha256 = _digest(report_snapshot)

    checks: list[MetricQualification] = []

    def minimum(metric: str, threshold: int | float | None) -> None:
        if threshold is None:
            return
        value = observed[metric]
        checks.append(
            MetricQualification(
                metric=metric,
                comparator=">=",
                observed=value,
                threshold=threshold,
                status="PASS" if value >= threshold else "FAIL",
            )
        )

    def maximum(metric: str, threshold: int | float | None) -> None:
        if threshold is None:
            return
        value = observed[metric]
        checks.append(
            MetricQualification(
                metric=metric,
                comparator="<=",
                observed=value,
                threshold=threshold,
                status="PASS" if value <= threshold else "FAIL",
            )
        )

    minimum("history_events", budget.min_history_events)
    minimum("accepted_events_per_second", budget.min_accepted_events_per_second)
    maximum("peak_traced_memory_bytes", budget.max_peak_traced_memory_bytes)
    maximum("ingest_elapsed_seconds", budget.max_ingest_elapsed_seconds)
    maximum("replay_elapsed_seconds", budget.max_replay_elapsed_seconds)
    maximum("restart_elapsed_seconds", budget.max_restart_elapsed_seconds)

    frozen_checks = tuple(checks)
    _validate_check_roster(budget, frozen_checks)
    status = "PASS" if all(check.status == "PASS" for check in frozen_checks) else "FAIL"
    identity_payload = _qualification_identity_payload(
        source_sha=canonical_source_sha,
        machine_profile=canonical_machine_profile,
        report_sha256=report_sha256,
        budget=budget,
        checks=frozen_checks,
        status=status,
    )

    qualification = object.__new__(PerformanceQualification)
    object.__setattr__(qualification, "source_sha", canonical_source_sha)
    object.__setattr__(qualification, "machine_profile", canonical_machine_profile)
    object.__setattr__(qualification, "report_sha256", report_sha256)
    object.__setattr__(qualification, "budget", budget)
    object.__setattr__(qualification, "checks", frozen_checks)
    object.__setattr__(qualification, "status", status)
    object.__setattr__(qualification, "qualification_id", _digest(identity_payload))
    object.__setattr__(qualification, "target_machine_acceptance", False)
    return qualification
