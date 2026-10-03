from __future__ import annotations

"""Deterministic operator-responsiveness evidence over supplied samples.

The module validates and summarizes monotonic timing samples.  It does not capture
Windows/UIA/NVDA timings itself and therefore never upgrades caller-supplied samples
into human, NVDA, release, or real-world measurement authority.
"""

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from math import ceil
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
MAX_SAMPLES = 100_000


class ResponsivenessEvidenceError(ValueError):
    """Responsiveness evidence is malformed or internally inconsistent."""


class ResponsivenessStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"


class ResponsivenessReason(StrEnum):
    WITHIN_BUDGET = "WITHIN_BUDGET"
    P95_BUDGET_EXCEEDED = "P95_BUDGET_EXCEEDED"
    MAX_BUDGET_EXCEEDED = "MAX_BUDGET_EXCEEDED"
    BOTH_BUDGETS_EXCEEDED = "BOTH_BUDGETS_EXCEEDED"
    SAMPLE_NOT_COMPLETED = "SAMPLE_NOT_COMPLETED"
    MALFORMED_EVIDENCE = "MALFORMED_EVIDENCE"


@dataclass(frozen=True, order=True, slots=True)
class ResponsivenessSample:
    sample_id: str
    process_instance_id: str
    clock_domain: str
    started_ns: int
    completed_ns: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", _text(self.sample_id, "sample_id"))
        object.__setattr__(
            self,
            "process_instance_id",
            _text(self.process_instance_id, "process_instance_id"),
        )
        object.__setattr__(self, "clock_domain", _text(self.clock_domain, "clock_domain"))
        object.__setattr__(self, "started_ns", _nonnegative_int(self.started_ns, "started_ns"))
        if self.completed_ns is not None:
            completed = _nonnegative_int(self.completed_ns, "completed_ns")
            if completed < self.started_ns:
                raise ResponsivenessEvidenceError(
                    "completed_ns cannot precede started_ns"
                )
            object.__setattr__(self, "completed_ns", completed)

    @property
    def duration_ns(self) -> int | None:
        if self.completed_ns is None:
            return None
        return self.completed_ns - self.started_ns

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "process_instance_id": self.process_instance_id,
            "clock_domain": self.clock_domain,
            "started_ns": self.started_ns,
            "completed_ns": self.completed_ns,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResponsivenessSample":
        _exact_keys(
            raw,
            {
                "sample_id",
                "process_instance_id",
                "clock_domain",
                "started_ns",
                "completed_ns",
            },
            "ResponsivenessSample",
        )
        completed = raw["completed_ns"]
        return cls(
            sample_id=_string(raw["sample_id"], "sample_id"),
            process_instance_id=_string(
                raw["process_instance_id"], "process_instance_id"
            ),
            clock_domain=_string(raw["clock_domain"], "clock_domain"),
            started_ns=_int(raw["started_ns"], "started_ns"),
            completed_ns=None
            if completed is None
            else _int(completed, "completed_ns"),
        )


@dataclass(frozen=True, slots=True)
class ResponsivenessBudget:
    p95_ns: int
    max_ns: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "p95_ns", _positive_int(self.p95_ns, "p95_ns"))
        object.__setattr__(self, "max_ns", _positive_int(self.max_ns, "max_ns"))
        if self.p95_ns > self.max_ns:
            raise ResponsivenessEvidenceError("p95_ns budget cannot exceed max_ns budget")

    def to_dict(self) -> dict[str, int]:
        return {"p95_ns": self.p95_ns, "max_ns": self.max_ns}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResponsivenessBudget":
        _exact_keys(raw, {"p95_ns", "max_ns"}, "ResponsivenessBudget")
        return cls(
            p95_ns=_int(raw["p95_ns"], "p95_ns"),
            max_ns=_int(raw["max_ns"], "max_ns"),
        )


@dataclass(frozen=True, slots=True)
class ResponsivenessEvaluation:
    status: ResponsivenessStatus
    reason: ResponsivenessReason
    sample_count: int
    completed_count: int
    process_instance_id: str | None
    clock_domain: str | None
    p95_ns: int | None
    max_ns: int | None
    budget: ResponsivenessBudget | None
    evidence_sha256: str | None
    measurement_authoritative: bool = False
    human_tested: bool = False
    nvda_verified: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not ResponsivenessStatus:
            raise ResponsivenessEvidenceError("status must be ResponsivenessStatus")
        if type(self.reason) is not ResponsivenessReason:
            raise ResponsivenessEvidenceError("reason must be ResponsivenessReason")
        _nonnegative_int(self.sample_count, "sample_count")
        _nonnegative_int(self.completed_count, "completed_count")
        if self.completed_count > self.sample_count:
            raise ResponsivenessEvidenceError("completed_count cannot exceed sample_count")
        for name in ("measurement_authoritative", "human_tested", "nvda_verified"):
            if getattr(self, name) is not False:
                raise ResponsivenessEvidenceError(
                    f"{name} cannot be asserted by this evidence contract"
                )
        if self.status is ResponsivenessStatus.INVALID_EVIDENCE:
            if self.reason is not ResponsivenessReason.MALFORMED_EVIDENCE:
                raise ResponsivenessEvidenceError(
                    "invalid evidence requires MALFORMED_EVIDENCE reason"
                )
            if any(
                value is not None
                for value in (
                    self.process_instance_id,
                    self.clock_domain,
                    self.p95_ns,
                    self.max_ns,
                    self.budget,
                    self.evidence_sha256,
                )
            ):
                raise ResponsivenessEvidenceError(
                    "invalid evidence cannot expose trusted metrics or digest"
                )
            return

        if self.process_instance_id is None or self.clock_domain is None:
            raise ResponsivenessEvidenceError(
                "valid evidence must bind process and clock identity"
            )
        _text(self.process_instance_id, "process_instance_id")
        _text(self.clock_domain, "clock_domain")
        if type(self.budget) is not ResponsivenessBudget:
            raise ResponsivenessEvidenceError("valid evidence must bind exact budget")
        _sha256(self.evidence_sha256, "evidence_sha256")

        if self.status is ResponsivenessStatus.INCOMPLETE_EVIDENCE:
            if self.reason is not ResponsivenessReason.SAMPLE_NOT_COMPLETED:
                raise ResponsivenessEvidenceError(
                    "incomplete evidence requires SAMPLE_NOT_COMPLETED"
                )
            if self.completed_count >= self.sample_count:
                raise ResponsivenessEvidenceError(
                    "incomplete evidence requires an unfinished sample"
                )
            if self.p95_ns is not None or self.max_ns is not None:
                raise ResponsivenessEvidenceError(
                    "incomplete evidence cannot publish partial latency metrics"
                )
            return

        if self.completed_count != self.sample_count or self.sample_count <= 0:
            raise ResponsivenessEvidenceError(
                "PASS/FAIL requires a non-empty fully completed sample set"
            )
        p95 = _nonnegative_int(self.p95_ns, "p95_ns")
        maximum = _nonnegative_int(self.max_ns, "max_ns")
        if p95 > maximum:
            raise ResponsivenessEvidenceError("p95_ns cannot exceed max_ns")
        p95_failed = p95 > self.budget.p95_ns
        max_failed = maximum > self.budget.max_ns
        expected_status = ResponsivenessStatus.FAIL if p95_failed or max_failed else ResponsivenessStatus.PASS
        if self.status is not expected_status:
            raise ResponsivenessEvidenceError("status disagrees with measured budgets")
        expected_reason = _budget_reason(p95_failed=p95_failed, max_failed=max_failed)
        if self.reason is not expected_reason:
            raise ResponsivenessEvidenceError("reason disagrees with measured budgets")

    @property
    def within_budget(self) -> bool:
        return self.status is ResponsivenessStatus.PASS


SampleInput = ResponsivenessSample | Mapping[str, Any]
BudgetInput = ResponsivenessBudget | Mapping[str, Any]


def evaluate_operator_responsiveness(
    samples: Sequence[SampleInput],
    *,
    budget: BudgetInput,
) -> ResponsivenessEvaluation:
    """Validate and summarize one bounded same-process monotonic sample set.

    The evaluator intentionally treats every sample as caller-supplied evidence.
    It establishes structural consistency and deterministic budget comparison only;
    it does not prove how, where, or by whom the timestamps were measured.
    """

    try:
        checked_budget = _coerce_budget(budget)
        checked_samples = _coerce_samples(samples)
        process_instance_id, clock_domain = _same_measurement_domain(checked_samples)
        evidence_sha256 = _evidence_digest(checked_samples, checked_budget)
    except (ResponsivenessEvidenceError, TypeError, ValueError, KeyError):
        return _invalid_evaluation()

    completed = tuple(item for item in checked_samples if item.completed_ns is not None)
    if len(completed) != len(checked_samples):
        return ResponsivenessEvaluation(
            status=ResponsivenessStatus.INCOMPLETE_EVIDENCE,
            reason=ResponsivenessReason.SAMPLE_NOT_COMPLETED,
            sample_count=len(checked_samples),
            completed_count=len(completed),
            process_instance_id=process_instance_id,
            clock_domain=clock_domain,
            p95_ns=None,
            max_ns=None,
            budget=checked_budget,
            evidence_sha256=evidence_sha256,
        )

    durations = sorted(item.duration_ns for item in checked_samples)
    # Fully completed above, so all values are integers.
    concrete = tuple(int(value) for value in durations if value is not None)
    p95 = concrete[ceil(0.95 * len(concrete)) - 1]
    maximum = concrete[-1]
    p95_failed = p95 > checked_budget.p95_ns
    max_failed = maximum > checked_budget.max_ns
    return ResponsivenessEvaluation(
        status=ResponsivenessStatus.FAIL
        if p95_failed or max_failed
        else ResponsivenessStatus.PASS,
        reason=_budget_reason(p95_failed=p95_failed, max_failed=max_failed),
        sample_count=len(checked_samples),
        completed_count=len(checked_samples),
        process_instance_id=process_instance_id,
        clock_domain=clock_domain,
        p95_ns=p95,
        max_ns=maximum,
        budget=checked_budget,
        evidence_sha256=evidence_sha256,
    )


def _coerce_samples(samples: object) -> tuple[ResponsivenessSample, ...]:
    if isinstance(samples, (str, bytes, bytearray)) or not isinstance(samples, Sequence):
        raise ResponsivenessEvidenceError("samples must be a finite sequence")
    if not samples:
        raise ResponsivenessEvidenceError("samples must not be empty")
    if len(samples) > MAX_SAMPLES:
        raise ResponsivenessEvidenceError("sample count exceeds bounded maximum")
    checked = tuple(_coerce_sample(value) for value in samples)
    canonical = tuple(sorted(checked, key=lambda value: value.sample_id))
    if len({value.sample_id for value in canonical}) != len(canonical):
        raise ResponsivenessEvidenceError("sample_id values must be unique")
    return canonical


def _coerce_sample(value: object) -> ResponsivenessSample:
    if type(value) is ResponsivenessSample:
        return ResponsivenessSample(
            sample_id=value.sample_id,
            process_instance_id=value.process_instance_id,
            clock_domain=value.clock_domain,
            started_ns=value.started_ns,
            completed_ns=value.completed_ns,
        )
    if isinstance(value, Mapping):
        return ResponsivenessSample.from_dict(value)
    raise ResponsivenessEvidenceError("sample must be ResponsivenessSample or mapping")


def _coerce_budget(value: object) -> ResponsivenessBudget:
    if type(value) is ResponsivenessBudget:
        return ResponsivenessBudget(p95_ns=value.p95_ns, max_ns=value.max_ns)
    if isinstance(value, Mapping):
        return ResponsivenessBudget.from_dict(value)
    raise ResponsivenessEvidenceError("budget must be ResponsivenessBudget or mapping")


def _same_measurement_domain(samples: tuple[ResponsivenessSample, ...]) -> tuple[str, str]:
    process_ids = {item.process_instance_id for item in samples}
    clock_domains = {item.clock_domain for item in samples}
    if len(process_ids) != 1:
        raise ResponsivenessEvidenceError(
            "all samples must use the same process_instance_id"
        )
    if len(clock_domains) != 1:
        raise ResponsivenessEvidenceError("all samples must use the same clock_domain")
    return next(iter(process_ids)), next(iter(clock_domains))


def _budget_reason(*, p95_failed: bool, max_failed: bool) -> ResponsivenessReason:
    if p95_failed and max_failed:
        return ResponsivenessReason.BOTH_BUDGETS_EXCEEDED
    if p95_failed:
        return ResponsivenessReason.P95_BUDGET_EXCEEDED
    if max_failed:
        return ResponsivenessReason.MAX_BUDGET_EXCEEDED
    return ResponsivenessReason.WITHIN_BUDGET


def _evidence_digest(
    samples: tuple[ResponsivenessSample, ...], budget: ResponsivenessBudget
) -> str:
    return _digest(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "operator-responsiveness-evidence-v1",
            "samples": [value.to_dict() for value in samples],
            "budget": budget.to_dict(),
        }
    )


def _invalid_evaluation() -> ResponsivenessEvaluation:
    return ResponsivenessEvaluation(
        status=ResponsivenessStatus.INVALID_EVIDENCE,
        reason=ResponsivenessReason.MALFORMED_EVIDENCE,
        sample_count=0,
        completed_count=0,
        process_instance_id=None,
        clock_domain=None,
        p95_ns=None,
        max_ns=None,
        budget=None,
        evidence_sha256=None,
    )


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ResponsivenessEvidenceError(f"{field} must be non-empty canonical text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ResponsivenessEvidenceError(f"{field} must be UTF-8 encodable") from exc
    return value


def _string(value: object, field: str) -> str:
    if type(value) is not str:
        raise ResponsivenessEvidenceError(f"{field} must be a string")
    return value


def _int(value: object, field: str) -> int:
    if type(value) is not int:
        raise ResponsivenessEvidenceError(f"{field} must be an exact integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    result = _int(value, field)
    if result < 0:
        raise ResponsivenessEvidenceError(f"{field} must be non-negative")
    return result


def _positive_int(value: object, field: str) -> int:
    result = _int(value, field)
    if result <= 0:
        raise ResponsivenessEvidenceError(f"{field} must be positive")
    return result


def _sha256(value: object, field: str) -> str:
    raw = _text(value, field)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise ResponsivenessEvidenceError(f"{field} must be lowercase SHA-256 hex")
    return raw


def _exact_keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    if type(raw) is not dict:
        raise ResponsivenessEvidenceError(f"{label} must be a plain object")
    if set(raw) != expected:
        raise ResponsivenessEvidenceError(f"{label} keys mismatch")


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
        raise ResponsivenessEvidenceError(
            "responsiveness evidence must be canonical finite JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "MAX_SAMPLES",
    "SCHEMA_VERSION",
    "ResponsivenessBudget",
    "ResponsivenessEvidenceError",
    "ResponsivenessEvaluation",
    "ResponsivenessReason",
    "ResponsivenessSample",
    "ResponsivenessStatus",
    "evaluate_operator_responsiveness",
]
