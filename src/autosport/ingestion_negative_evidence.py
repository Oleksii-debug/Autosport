from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .json_integrity import strict_json_loads

_CONTINUOUS_STATUS_SCHEMA_VERSION = 1
_EVIDENCE_SCHEMA_VERSION = 1
_CONTINUOUS_STATUS_KIND = "autosport_continuous_local_observation"
_CONTINUOUS_LIFECYCLE_STATES = frozenset(
    {"starting", "running", "attempting", "provider_unavailable", "failed", "stopped"}
)


@dataclass(frozen=True, slots=True)
class IngestionNegativeEvidence:
    """Fail-closed projection of continuous-observation coverage.

    The projection intentionally counts unsuccessful attempts and expected-but-
    unobserved cycles in the denominator. Missing or malformed status is itself
    returned as negative evidence instead of disappearing from downstream data.
    """

    schema_version: int
    kind: str
    evidence_state: str
    status_path: str
    run_id: str | None
    source_id: str | None
    lifecycle_state: str | None
    attempted_cycles: int
    successful_cycles: int
    failed_cycles: int
    expected_cycles: int | None
    unobserved_expected_cycles: int
    denominator_cycles: int
    successful_fraction: float | None
    last_error_kind: str | None
    stop_reason: str | None
    has_negative_evidence: bool
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def _valid_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _trimmed_string(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _negative_shell(
    path: Path,
    *,
    evidence_state: str,
    expected_cycles: int | None,
    reason: str,
) -> IngestionNegativeEvidence:
    denominator = expected_cycles or 0
    return IngestionNegativeEvidence(
        schema_version=_EVIDENCE_SCHEMA_VERSION,
        kind="autosport_ingestion_negative_evidence",
        evidence_state=evidence_state,
        status_path=str(path),
        run_id=None,
        source_id=None,
        lifecycle_state=None,
        attempted_cycles=0,
        successful_cycles=0,
        failed_cycles=0,
        expected_cycles=expected_cycles,
        unobserved_expected_cycles=denominator,
        denominator_cycles=denominator,
        successful_fraction=0.0 if denominator else None,
        last_error_kind=None,
        stop_reason=None,
        has_negative_evidence=True,
        reason_codes=(reason,),
    )


def project_ingestion_negative_evidence(
    status_path: str | Path,
    *,
    expected_cycles: int | None = None,
) -> IngestionNegativeEvidence:
    """Project collector status into denominator-preserving evidence.

    ``expected_cycles`` is optional because the collector can be operator-stopped
    or runtime-bounded. When supplied by a scheduler/campaign authority, cycles
    that were expected but never attempted are kept in the denominator.

    Status-file absence, unreadability, malformed JSON, unsupported schemas and
    invalid counter relationships are returned as explicit negative evidence.
    Caller configuration errors (invalid ``expected_cycles``) still raise.
    """

    if expected_cycles is not None and not _valid_nonnegative_int(expected_cycles):
        raise ValueError("expected_cycles must be a non-negative integer or None")

    path = Path(status_path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _negative_shell(
            path,
            evidence_state="missing",
            expected_cycles=expected_cycles,
            reason="status_missing",
        )
    except (OSError, UnicodeError):
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_unreadable",
        )

    try:
        raw = strict_json_loads(raw_text)
    except ValueError:
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_invalid_json",
        )

    if not isinstance(raw, dict):
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_not_object",
        )

    schema_version = raw.get("schema_version")
    if not _valid_nonnegative_int(schema_version) or schema_version != _CONTINUOUS_STATUS_SCHEMA_VERSION:
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_unsupported_schema",
        )
    if raw.get("kind") != _CONTINUOUS_STATUS_KIND:
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_wrong_kind",
        )

    run_id = raw.get("run_id")
    source_id = raw.get("source_id")
    lifecycle_state = raw.get("state")
    attempted = raw.get("attempted_cycles")
    successful = raw.get("successful_cycles")
    if not _trimmed_string(run_id):
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_invalid_run_id",
        )
    if not _trimmed_string(source_id):
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_invalid_source_id",
        )
    if not _trimmed_string(lifecycle_state):
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_invalid_lifecycle_state",
        )
    if lifecycle_state not in _CONTINUOUS_LIFECYCLE_STATES:
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_unknown_lifecycle_state",
        )
    if not _valid_nonnegative_int(attempted) or not _valid_nonnegative_int(successful):
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_invalid_cycle_counter",
        )
    if successful > attempted:
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_success_exceeds_attempts",
        )

    last_error_kind = raw.get("last_error_kind")
    stop_reason = raw.get("stop_reason")
    if last_error_kind is not None and not _trimmed_string(last_error_kind):
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_invalid_last_error_kind",
        )
    if stop_reason is not None and not _trimmed_string(stop_reason):
        return _negative_shell(
            path,
            evidence_state="invalid",
            expected_cycles=expected_cycles,
            reason="status_invalid_stop_reason",
        )

    failed = attempted - successful
    unobserved = max((expected_cycles or 0) - attempted, 0)
    denominator = max(attempted, expected_cycles or 0)
    fraction = successful / denominator if denominator else None

    reasons: list[str] = []
    if failed:
        reasons.append("failed_attempts_present")
    if unobserved:
        reasons.append("expected_cycles_unobserved")
    if last_error_kind is not None:
        reasons.append("runtime_error_present")
    if lifecycle_state == "failed":
        reasons.append("lifecycle_failed")
    elif lifecycle_state in {"starting", "running", "attempting", "provider_unavailable"}:
        reasons.append("lifecycle_incomplete")

    return IngestionNegativeEvidence(
        schema_version=_EVIDENCE_SCHEMA_VERSION,
        kind="autosport_ingestion_negative_evidence",
        evidence_state="observed",
        status_path=str(path),
        run_id=run_id,
        source_id=source_id,
        lifecycle_state=lifecycle_state,
        attempted_cycles=attempted,
        successful_cycles=successful,
        failed_cycles=failed,
        expected_cycles=expected_cycles,
        unobserved_expected_cycles=unobserved,
        denominator_cycles=denominator,
        successful_fraction=fraction,
        last_error_kind=last_error_kind,
        stop_reason=stop_reason,
        has_negative_evidence=bool(reasons),
        reason_codes=tuple(reasons),
    )


def _nonnegative_int_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Project continuous-observation status into denominator-preserving "
            "ingestion evidence."
        )
    )
    parser.add_argument("status_path", type=Path)
    parser.add_argument("--expected-cycles", type=_nonnegative_int_arg, default=None)
    args = parser.parse_args(argv)

    evidence = project_ingestion_negative_evidence(
        args.status_path,
        expected_cycles=args.expected_cycles,
    )
    print(json.dumps(evidence.to_dict(), sort_keys=True))
    return 2 if evidence.has_negative_evidence else 0


if __name__ == "__main__":
    raise SystemExit(main())
