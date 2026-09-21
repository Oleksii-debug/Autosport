from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping


_SCHEMA_VERSION = 1
_PLAN_KIND = "forward-provider-acquisition-plan-v1"
_TERMINAL_KIND = "forward-provider-acquisition-terminal-v1"
_PLAN_FILENAME = "forward-acquisition-plan.json"
_TERMINAL_DIRNAME = "forward-acquisition-terminals"


class ForwardAcquisitionError(ValueError):
    """Forward provider acquisition evidence is malformed or incomplete."""


class ForwardAcquisitionIntegrityError(ForwardAcquisitionError):
    """Durable forward acquisition evidence is corrupt or conflicts with frozen truth."""


class AcquisitionTerminalState(str, Enum):
    SUCCEEDED_NONEMPTY = "SUCCEEDED_NONEMPTY"
    SUCCEEDED_EMPTY = "SUCCEEDED_EMPTY"
    FAILED_RETRYABLE_EXHAUSTED = "FAILED_RETRYABLE_EXHAUSTED"
    FAILED_PERMANENT = "FAILED_PERMANENT"


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ForwardAcquisitionError(f"{field} must be non-empty canonical text")
    value.encode("utf-8")
    return value


def _sha(value: object, field: str) -> str:
    raw = _text(value, field)
    if raw != raw.lower() or len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise ForwardAcquisitionError(f"{field} must be canonical SHA-256 hex")
    return raw


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ForwardAcquisitionError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ForwardAcquisitionError(f"{field} must be a non-negative integer")
    return value


def _instant(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForwardAcquisitionError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardAcquisitionError(f"{field} must include a timezone")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if raw != canonical:
        raise ForwardAcquisitionError(f"{field} must be canonical UTC with Z suffix")
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ForwardAcquisitionError("forward acquisition evidence must be finite JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _optional_sha(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _sha(value, field)


@dataclass(frozen=True, slots=True)
class ProviderOpportunitySpec:
    """One prospectively frozen provider acquisition opportunity."""

    provider_id: str
    opportunity_id: str
    request_sha256: str
    config_sha256: str
    retry_policy_sha256: str
    max_attempts: int

    def __post_init__(self) -> None:
        _text(self.provider_id, "provider_id")
        _text(self.opportunity_id, "opportunity_id")
        _sha(self.request_sha256, "request_sha256")
        _sha(self.config_sha256, "config_sha256")
        _sha(self.retry_policy_sha256, "retry_policy_sha256")
        _positive_int(self.max_attempts, "max_attempts")

    def to_payload(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "opportunity_id": self.opportunity_id,
            "request_sha256": self.request_sha256,
            "config_sha256": self.config_sha256,
            "retry_policy_sha256": self.retry_policy_sha256,
            "max_attempts": self.max_attempts,
        }

    @property
    def spec_sha256(self) -> str:
        return _digest({"kind": "provider-opportunity-spec-v1", **self.to_payload()})

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ProviderOpportunitySpec":
        if type(payload) is not dict:
            raise ForwardAcquisitionIntegrityError("opportunity spec must be an object")
        required = {
            "provider_id",
            "opportunity_id",
            "request_sha256",
            "config_sha256",
            "retry_policy_sha256",
            "max_attempts",
        }
        if set(payload) != required:
            raise ForwardAcquisitionIntegrityError("opportunity spec fields are not canonical")
        try:
            return cls(**payload)  # type: ignore[arg-type]
        except (TypeError, ForwardAcquisitionError) as exc:
            raise ForwardAcquisitionIntegrityError("opportunity spec is invalid") from exc


@dataclass(frozen=True, slots=True)
class ForwardAcquisitionPlan:
    """Prospective provider universe frozen before forward acquisition begins."""

    evaluation_id: str
    frozen_at: str
    opportunities: tuple[ProviderOpportunitySpec, ...]

    def __post_init__(self) -> None:
        _text(self.evaluation_id, "evaluation_id")
        _instant(self.frozen_at, "frozen_at")
        if type(self.opportunities) is not tuple or not self.opportunities:
            raise ForwardAcquisitionError("opportunities must be a non-empty tuple")
        if not all(type(item) is ProviderOpportunitySpec for item in self.opportunities):
            raise ForwardAcquisitionError("opportunities must contain exact ProviderOpportunitySpec values")
        scopes = [(item.provider_id, item.opportunity_id) for item in self.opportunities]
        if len(set(scopes)) != len(scopes):
            raise ForwardAcquisitionError("provider opportunity scopes must be unique")
        hashes = [item.spec_sha256 for item in self.opportunities]
        if len(set(hashes)) != len(hashes):
            raise ForwardAcquisitionError("provider opportunity identities must be unique")

    def to_payload(self) -> dict[str, object]:
        ordered = sorted(self.opportunities, key=lambda item: (item.provider_id, item.opportunity_id))
        return {
            "schema_version": _SCHEMA_VERSION,
            "kind": _PLAN_KIND,
            "evaluation_id": self.evaluation_id,
            "frozen_at": self.frozen_at,
            "opportunities": [item.to_payload() for item in ordered],
        }

    @property
    def plan_sha256(self) -> str:
        return _digest(self.to_payload())

    def find_spec(self, spec_sha256: str) -> ProviderOpportunitySpec:
        wanted = _sha(spec_sha256, "spec_sha256")
        matches = [item for item in self.opportunities if item.spec_sha256 == wanted]
        if len(matches) != 1:
            raise ForwardAcquisitionIntegrityError(
                "terminal record does not resolve to frozen provider opportunity"
            )
        return matches[0]

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ForwardAcquisitionPlan":
        if type(payload) is not dict:
            raise ForwardAcquisitionIntegrityError("forward acquisition plan must be an object")
        required = {
            "schema_version",
            "kind",
            "evaluation_id",
            "frozen_at",
            "opportunities",
            "plan_sha256",
        }
        if set(payload) != required:
            raise ForwardAcquisitionIntegrityError("forward acquisition plan fields are not canonical")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != _SCHEMA_VERSION:
            raise ForwardAcquisitionIntegrityError("unsupported forward acquisition plan schema")
        if payload["kind"] != _PLAN_KIND:
            raise ForwardAcquisitionIntegrityError("wrong forward acquisition plan kind")
        raw_opportunities = payload["opportunities"]
        if type(raw_opportunities) is not list or not raw_opportunities:
            raise ForwardAcquisitionIntegrityError(
                "forward acquisition plan opportunities must be a non-empty list"
            )
        try:
            plan = cls(
                evaluation_id=payload["evaluation_id"],  # type: ignore[arg-type]
                frozen_at=payload["frozen_at"],  # type: ignore[arg-type]
                opportunities=tuple(
                    ProviderOpportunitySpec.from_payload(item) for item in raw_opportunities
                ),
            )
        except ForwardAcquisitionError as exc:
            raise ForwardAcquisitionIntegrityError("forward acquisition plan is invalid") from exc
        stored_sha = _sha(payload["plan_sha256"], "plan_sha256")
        if stored_sha != plan.plan_sha256:
            raise ForwardAcquisitionIntegrityError("forward acquisition plan digest mismatch")
        return plan


def terminal_idempotency_key(
    plan: ForwardAcquisitionPlan,
    spec: ProviderOpportunitySpec,
) -> str:
    if type(plan) is not ForwardAcquisitionPlan or type(spec) is not ProviderOpportunitySpec:
        raise ForwardAcquisitionError("idempotency key requires canonical plan and opportunity spec")
    plan.find_spec(spec.spec_sha256)
    return _digest(
        {
            "kind": "forward-provider-acquisition-idempotency-v1",
            "plan_sha256": plan.plan_sha256,
            "spec_sha256": spec.spec_sha256,
        }
    )


@dataclass(frozen=True, slots=True)
class TerminalAcquisitionRecord:
    """Terminal evidence for one exact frozen provider opportunity."""

    plan_sha256: str
    spec_sha256: str
    idempotency_key: str
    state: AcquisitionTerminalState
    attempt_count: int
    started_at: str
    terminal_at: str
    response_evidence_sha256: str | None
    failure_evidence_sha256: str | None
    provider_row_count: int
    accepted_row_count: int
    rejected_row_count: int
    rejection_evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        _sha(self.plan_sha256, "plan_sha256")
        _sha(self.spec_sha256, "spec_sha256")
        _sha(self.idempotency_key, "idempotency_key")
        if type(self.state) is not AcquisitionTerminalState:
            raise ForwardAcquisitionError("state must be an exact AcquisitionTerminalState")
        _positive_int(self.attempt_count, "attempt_count")
        started = _instant(self.started_at, "started_at")
        terminal = _instant(self.terminal_at, "terminal_at")
        if terminal < started:
            raise ForwardAcquisitionError("terminal_at cannot precede started_at")
        response = _optional_sha(self.response_evidence_sha256, "response_evidence_sha256")
        failure = _optional_sha(self.failure_evidence_sha256, "failure_evidence_sha256")
        rejection = _optional_sha(
            self.rejection_evidence_sha256,
            "rejection_evidence_sha256",
        )
        provider_rows = _nonnegative_int(self.provider_row_count, "provider_row_count")
        accepted_rows = _nonnegative_int(self.accepted_row_count, "accepted_row_count")
        rejected_rows = _nonnegative_int(self.rejected_row_count, "rejected_row_count")
        if accepted_rows + rejected_rows != provider_rows:
            raise ForwardAcquisitionError(
                "accepted_row_count + rejected_row_count must equal provider_row_count"
            )
        if rejected_rows == 0 and rejection is not None:
            raise ForwardAcquisitionError("rejection evidence requires rejected provider rows")
        if rejected_rows > 0 and rejection is None:
            raise ForwardAcquisitionError(
                "rejected provider rows require explicit rejection evidence"
            )

        success = self.state in {
            AcquisitionTerminalState.SUCCEEDED_NONEMPTY,
            AcquisitionTerminalState.SUCCEEDED_EMPTY,
        }
        if success:
            if response is None:
                raise ForwardAcquisitionError(
                    "successful acquisition requires positive response evidence"
                )
            if failure is not None:
                raise ForwardAcquisitionError(
                    "successful acquisition cannot carry failure evidence"
                )
            if (
                self.state is AcquisitionTerminalState.SUCCEEDED_EMPTY
                and provider_rows != 0
            ):
                raise ForwardAcquisitionError(
                    "SUCCEEDED_EMPTY requires provider_row_count == 0"
                )
            if (
                self.state is AcquisitionTerminalState.SUCCEEDED_NONEMPTY
                and provider_rows == 0
            ):
                raise ForwardAcquisitionError(
                    "SUCCEEDED_NONEMPTY requires provider_row_count > 0"
                )
        else:
            if failure is None:
                raise ForwardAcquisitionError("failed acquisition requires failure evidence")
            if response is not None:
                raise ForwardAcquisitionError(
                    "failed acquisition cannot masquerade as positive response evidence"
                )
            if provider_rows or accepted_rows or rejected_rows or rejection is not None:
                raise ForwardAcquisitionError(
                    "failed acquisition cannot carry materialized provider rows"
                )

    def validate_against(
        self,
        plan: ForwardAcquisitionPlan,
    ) -> ProviderOpportunitySpec:
        if type(plan) is not ForwardAcquisitionPlan:
            raise ForwardAcquisitionIntegrityError(
                "terminal validation requires canonical frozen plan"
            )
        if self.plan_sha256 != plan.plan_sha256:
            raise ForwardAcquisitionIntegrityError(
                "terminal record is bound to a different frozen plan"
            )
        spec = plan.find_spec(self.spec_sha256)
        if self.idempotency_key != terminal_idempotency_key(plan, spec):
            raise ForwardAcquisitionIntegrityError(
                "terminal idempotency key does not match frozen request/config/retry identity"
            )
        if self.attempt_count > spec.max_attempts:
            raise ForwardAcquisitionIntegrityError(
                "terminal attempt count exceeds frozen retry policy"
            )
        if (
            self.state is AcquisitionTerminalState.FAILED_RETRYABLE_EXHAUSTED
            and self.attempt_count != spec.max_attempts
        ):
            raise ForwardAcquisitionIntegrityError(
                "retryable exhausted state requires exactly the frozen maximum attempt count"
            )
        if _instant(self.started_at, "started_at") < _instant(plan.frozen_at, "frozen_at"):
            raise ForwardAcquisitionIntegrityError(
                "provider acquisition cannot precede prospective plan freeze"
            )
        return spec

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "kind": _TERMINAL_KIND,
            "plan_sha256": self.plan_sha256,
            "spec_sha256": self.spec_sha256,
            "idempotency_key": self.idempotency_key,
            "state": self.state.value,
            "attempt_count": self.attempt_count,
            "started_at": self.started_at,
            "terminal_at": self.terminal_at,
            "response_evidence_sha256": self.response_evidence_sha256,
            "failure_evidence_sha256": self.failure_evidence_sha256,
            "provider_row_count": self.provider_row_count,
            "accepted_row_count": self.accepted_row_count,
            "rejected_row_count": self.rejected_row_count,
            "rejection_evidence_sha256": self.rejection_evidence_sha256,
        }

    @property
    def record_sha256(self) -> str:
        return _digest(self.to_payload())

    def durable_payload(self) -> dict[str, object]:
        return {**self.to_payload(), "record_sha256": self.record_sha256}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "TerminalAcquisitionRecord":
        if type(payload) is not dict:
            raise ForwardAcquisitionIntegrityError(
                "terminal acquisition record must be an object"
            )
        required = {
            "schema_version",
            "kind",
            "plan_sha256",
            "spec_sha256",
            "idempotency_key",
            "state",
            "attempt_count",
            "started_at",
            "terminal_at",
            "response_evidence_sha256",
            "failure_evidence_sha256",
            "provider_row_count",
            "accepted_row_count",
            "rejected_row_count",
            "rejection_evidence_sha256",
            "record_sha256",
        }
        if set(payload) != required:
            raise ForwardAcquisitionIntegrityError(
                "terminal acquisition fields are not canonical"
            )
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != _SCHEMA_VERSION
        ):
            raise ForwardAcquisitionIntegrityError(
                "unsupported terminal acquisition schema"
            )
        if payload["kind"] != _TERMINAL_KIND:
            raise ForwardAcquisitionIntegrityError("wrong terminal acquisition kind")
        try:
            state = AcquisitionTerminalState(payload["state"])
        except (TypeError, ValueError) as exc:
            raise ForwardAcquisitionIntegrityError(
                "unknown terminal acquisition state"
            ) from exc
        try:
            record = cls(
                plan_sha256=payload["plan_sha256"],  # type: ignore[arg-type]
                spec_sha256=payload["spec_sha256"],  # type: ignore[arg-type]
                idempotency_key=payload["idempotency_key"],  # type: ignore[arg-type]
                state=state,
                attempt_count=payload["attempt_count"],  # type: ignore[arg-type]
                started_at=payload["started_at"],  # type: ignore[arg-type]
                terminal_at=payload["terminal_at"],  # type: ignore[arg-type]
                response_evidence_sha256=payload[
                    "response_evidence_sha256"
                ],  # type: ignore[arg-type]
                failure_evidence_sha256=payload[
                    "failure_evidence_sha256"
                ],  # type: ignore[arg-type]
                provider_row_count=payload["provider_row_count"],  # type: ignore[arg-type]
                accepted_row_count=payload["accepted_row_count"],  # type: ignore[arg-type]
                rejected_row_count=payload["rejected_row_count"],  # type: ignore[arg-type]
                rejection_evidence_sha256=payload[
                    "rejection_evidence_sha256"
                ],  # type: ignore[arg-type]
            )
        except (TypeError, ForwardAcquisitionError) as exc:
            raise ForwardAcquisitionIntegrityError(
                "terminal acquisition record is invalid"
            ) from exc
        stored_sha = _sha(payload["record_sha256"], "record_sha256")
        if stored_sha != record.record_sha256:
            raise ForwardAcquisitionIntegrityError(
                "terminal acquisition record digest mismatch"
            )
        return record


@dataclass(frozen=True, slots=True)
class ForwardAcquisitionCompleteness:
    plan_sha256: str
    expected_count: int
    terminal_count: int
    succeeded_nonempty: tuple[str, ...]
    succeeded_empty: tuple[str, ...]
    failed_retryable_exhausted: tuple[str, ...]
    failed_permanent: tuple[str, ...]
    missing: tuple[str, ...]
    rejected_provider_rows: int
    exhaustive_terminal: bool
    all_successful: bool
    forward_evidence_ready: bool


class ForwardAcquisitionStore:
    """Create-only durable evidence store for one prospectively frozen forward plan.

    The plan is written before terminal evidence. Each provider opportunity then owns one
    immutable terminal file named by its frozen spec digest. Existing bytes are never
    overwritten: an exact replay is idempotent; a conflicting replay fails closed.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.plan_path = self.root / _PLAN_FILENAME
        self.terminal_dir = self.root / _TERMINAL_DIRNAME

    @staticmethod
    def _create_only_json(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (_canonical_json(payload) + "\n").encode("utf-8")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            # A crash/short write may leave a file behind. It is deliberately not
            # removed here: restart must observe corruption instead of silently
            # recreating authority from later caller input.
            raise

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ForwardAcquisitionIntegrityError(
                f"cannot read durable forward evidence: {path.name}"
            ) from exc
        if type(payload) is not dict:
            raise ForwardAcquisitionIntegrityError(
                "durable forward evidence must be a JSON object"
            )
        return payload

    def freeze_plan(self, plan: ForwardAcquisitionPlan) -> str:
        if type(plan) is not ForwardAcquisitionPlan:
            raise ForwardAcquisitionError(
                "freeze_plan requires exact ForwardAcquisitionPlan"
            )
        payload = {**plan.to_payload(), "plan_sha256": plan.plan_sha256}
        try:
            self._create_only_json(self.plan_path, payload)
        except FileExistsError:
            existing = self.load_plan()
            if existing.plan_sha256 != plan.plan_sha256:
                raise ForwardAcquisitionIntegrityError(
                    "forward acquisition plan is already frozen to different authority"
                )
        return plan.plan_sha256

    def load_plan(self) -> ForwardAcquisitionPlan:
        if not self.plan_path.is_file():
            raise ForwardAcquisitionIntegrityError(
                "forward acquisition plan is missing"
            )
        return ForwardAcquisitionPlan.from_payload(self._read_json(self.plan_path))

    def append_terminal(self, record: TerminalAcquisitionRecord) -> str:
        if type(record) is not TerminalAcquisitionRecord:
            raise ForwardAcquisitionError(
                "append_terminal requires exact TerminalAcquisitionRecord"
            )
        plan = self.load_plan()
        record.validate_against(plan)
        path = self.terminal_dir / f"{record.spec_sha256}.json"
        payload = record.durable_payload()
        try:
            self._create_only_json(path, payload)
        except FileExistsError:
            existing = TerminalAcquisitionRecord.from_payload(
                self._read_json(path)
            )
            existing.validate_against(plan)
            if existing.record_sha256 != record.record_sha256:
                raise ForwardAcquisitionIntegrityError(
                    "conflicting terminal replay for frozen provider opportunity"
                )
        return record.record_sha256

    def load_terminals(self) -> tuple[TerminalAcquisitionRecord, ...]:
        plan = self.load_plan()
        expected = {item.spec_sha256 for item in plan.opportunities}
        if not self.terminal_dir.exists():
            return ()
        if not self.terminal_dir.is_dir():
            raise ForwardAcquisitionIntegrityError(
                "forward acquisition terminal path is not a directory"
            )
        files = tuple(
            sorted(self.terminal_dir.iterdir(), key=lambda path: path.name)
        )
        unexpected = [
            path.name
            for path in files
            if path.suffix != ".json" or path.stem not in expected
        ]
        if unexpected:
            raise ForwardAcquisitionIntegrityError(
                "unexpected durable terminal evidence is present"
            )
        records: list[TerminalAcquisitionRecord] = []
        seen: set[str] = set()
        for path in files:
            record = TerminalAcquisitionRecord.from_payload(
                self._read_json(path)
            )
            record.validate_against(plan)
            if record.spec_sha256 != path.stem:
                raise ForwardAcquisitionIntegrityError(
                    "terminal filename does not match frozen opportunity identity"
                )
            if record.spec_sha256 in seen:
                raise ForwardAcquisitionIntegrityError(
                    "duplicate terminal provider opportunity evidence"
                )
            seen.add(record.spec_sha256)
            records.append(record)
        return tuple(records)

    def completeness(self, *, as_of: str) -> ForwardAcquisitionCompleteness:
        plan = self.load_plan()
        boundary = _instant(as_of, "as_of")
        if boundary < _instant(plan.frozen_at, "frozen_at"):
            raise ForwardAcquisitionIntegrityError(
                "completeness boundary cannot precede frozen plan"
            )
        records = self.load_terminals()
        by_spec = {record.spec_sha256: record for record in records}

        partitions: dict[AcquisitionTerminalState, list[str]] = {
            state: [] for state in AcquisitionTerminalState
        }
        rejected_provider_rows = 0
        for record in records:
            if _instant(record.terminal_at, "terminal_at") > boundary:
                raise ForwardAcquisitionIntegrityError(
                    "future terminal evidence cannot satisfy current completeness"
                )
            spec = plan.find_spec(record.spec_sha256)
            scope = f"{spec.provider_id}:{spec.opportunity_id}"
            partitions[record.state].append(scope)
            rejected_provider_rows += record.rejected_row_count

        missing = []
        for spec in plan.opportunities:
            if spec.spec_sha256 not in by_spec:
                missing.append(f"{spec.provider_id}:{spec.opportunity_id}")

        failed_retry = tuple(
            sorted(
                partitions[
                    AcquisitionTerminalState.FAILED_RETRYABLE_EXHAUSTED
                ]
            )
        )
        failed_permanent = tuple(
            sorted(partitions[AcquisitionTerminalState.FAILED_PERMANENT])
        )
        exhaustive = not missing and len(records) == len(plan.opportunities)
        all_successful = exhaustive and not failed_retry and not failed_permanent
        ready = all_successful and rejected_provider_rows == 0
        return ForwardAcquisitionCompleteness(
            plan_sha256=plan.plan_sha256,
            expected_count=len(plan.opportunities),
            terminal_count=len(records),
            succeeded_nonempty=tuple(
                sorted(partitions[AcquisitionTerminalState.SUCCEEDED_NONEMPTY])
            ),
            succeeded_empty=tuple(
                sorted(partitions[AcquisitionTerminalState.SUCCEEDED_EMPTY])
            ),
            failed_retryable_exhausted=failed_retry,
            failed_permanent=failed_permanent,
            missing=tuple(sorted(missing)),
            rejected_provider_rows=rejected_provider_rows,
            exhaustive_terminal=exhaustive,
            all_successful=all_successful,
            forward_evidence_ready=ready,
        )


def build_terminal_record(
    plan: ForwardAcquisitionPlan,
    spec: ProviderOpportunitySpec,
    *,
    state: AcquisitionTerminalState,
    attempt_count: int,
    started_at: str,
    terminal_at: str,
    response_evidence_sha256: str | None = None,
    failure_evidence_sha256: str | None = None,
    provider_row_count: int = 0,
    accepted_row_count: int = 0,
    rejected_row_count: int = 0,
    rejection_evidence_sha256: str | None = None,
) -> TerminalAcquisitionRecord:
    """Bind one terminal record to exact prospectively frozen plan/spec identity."""

    if type(plan) is not ForwardAcquisitionPlan or type(spec) is not ProviderOpportunitySpec:
        raise ForwardAcquisitionError(
            "terminal record builder requires canonical plan/spec"
        )
    plan.find_spec(spec.spec_sha256)
    record = TerminalAcquisitionRecord(
        plan_sha256=plan.plan_sha256,
        spec_sha256=spec.spec_sha256,
        idempotency_key=terminal_idempotency_key(plan, spec),
        state=state,
        attempt_count=attempt_count,
        started_at=started_at,
        terminal_at=terminal_at,
        response_evidence_sha256=response_evidence_sha256,
        failure_evidence_sha256=failure_evidence_sha256,
        provider_row_count=provider_row_count,
        accepted_row_count=accepted_row_count,
        rejected_row_count=rejected_row_count,
        rejection_evidence_sha256=rejection_evidence_sha256,
    )
    record.validate_against(plan)
    return record


__all__ = [
    "AcquisitionTerminalState",
    "ForwardAcquisitionCompleteness",
    "ForwardAcquisitionError",
    "ForwardAcquisitionIntegrityError",
    "ForwardAcquisitionPlan",
    "ForwardAcquisitionStore",
    "ProviderOpportunitySpec",
    "TerminalAcquisitionRecord",
    "build_terminal_record",
    "terminal_idempotency_key",
]
