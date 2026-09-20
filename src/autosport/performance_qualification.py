from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
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
    number = float(value)
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

    def to_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "comparator": self.comparator,
            "observed": self.observed,
            "threshold": self.threshold,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class PerformanceQualification:
    source_sha: str
    machine_profile: str
    report_sha256: str
    budget: PerformanceBudget
    checks: tuple[MetricQualification, ...]
    status: str
    qualification_id: str
    target_machine_acceptance: bool = False

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


def _validate_report(raw: Mapping[str, object]) -> dict[str, int | float]:
    if not isinstance(raw, Mapping):
        raise PerformanceQualificationError("endurance report must be an object")
    if raw.get("status") != "PASS":
        raise PerformanceQualificationError("endurance report correctness status must be PASS")
    failures = raw.get("failures")
    if type(failures) is not list or failures:
        raise PerformanceQualificationError("PASS endurance report must contain an empty failures list")
    if raw.get("real_money_execution") is not False:
        raise PerformanceQualificationError("endurance report must preserve real_money_execution=false")
    fingerprint = raw.get("stable_invariant_fingerprint")
    if (
        type(fingerprint) is not str
        or len(fingerprint) != 64
        or any(character not in _HEX for character in fingerprint)
    ):
        raise PerformanceQualificationError(
            "stable_invariant_fingerprint must be lowercase SHA-256 hex"
        )
    expected_fingerprint = _digest(_endurance_fingerprint_payload(raw))
    if fingerprint != expected_fingerprint:
        raise PerformanceQualificationError("endurance stable invariant fingerprint mismatch")
    return {
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
    observed = _validate_report(report)
    report_sha256 = _digest(dict(report))

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

    status = "PASS" if all(check.status == "PASS" for check in checks) else "FAIL"
    identity_payload = {
        "schema": QUALIFICATION_SCHEMA,
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "source_sha": canonical_source_sha,
        "machine_profile": canonical_machine_profile,
        "report_sha256": report_sha256,
        "budget": budget.to_dict(),
        "checks": [check.to_dict() for check in checks],
        "status": status,
        "target_machine_acceptance": False,
    }
    qualification_id = _digest(identity_payload)
    return PerformanceQualification(
        source_sha=canonical_source_sha,
        machine_profile=canonical_machine_profile,
        report_sha256=report_sha256,
        budget=budget,
        checks=tuple(checks),
        status=status,
        qualification_id=qualification_id,
    )
