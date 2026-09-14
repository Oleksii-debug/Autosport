from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .dataset import ReplayDataset
from .decision_ledger import DecisionLedgerIntegrityError, JsonlDecisionLedger
from .forecasting import ForecastRecord, parse_iso_timestamp


@dataclass(frozen=True, slots=True)
class ForecastOriginBinding:
    """Local durable artifacts used to bind forecasts to canonical research decisions."""

    decision_ledger_path: Path
    run_summary_paths: tuple[Path, ...]


class _RunSummaryJsonIntegrityError(ValueError):
    """Raised when durable forecast-origin summary JSON is non-canonical."""


def load_forecast_origin_binding(raw: Any, bundle_path: str | Path) -> ForecastOriginBinding:
    if not isinstance(raw, dict):
        raise ValueError("walk-forward forecast_origin must be an object")
    source = Path(bundle_path)
    parent = source.parent.resolve()
    ledger = _resolve_relative_file(parent, raw.get("decision_ledger_path"), "decision_ledger_path")
    summary_values = raw.get("run_summary_paths")
    if not isinstance(summary_values, list) or not summary_values:
        raise ValueError("walk-forward forecast_origin.run_summary_paths must be a non-empty list")
    summaries = tuple(
        _resolve_relative_file(parent, value, f"run_summary_paths[{index}]")
        for index, value in enumerate(summary_values)
    )
    if len(set(summaries)) != len(summaries):
        raise ValueError("walk-forward forecast_origin contains duplicate run summary paths")
    return ForecastOriginBinding(ledger, summaries)


def verify_forecast_origin_binding(
    binding: ForecastOriginBinding,
    dataset: ReplayDataset,
    forecasts: Iterable[ForecastRecord],
    evaluated_forecast_ids: set[str],
) -> dict[str, Any]:
    """Bind evaluated forecasts to canonical local research-ledger evidence.

    Each evaluated forecast must appear by canonical id + hash in a research
    pipeline decision whose ledger prefix hash is committed by a matching durable
    run summary for the exact governed dataset. ``recorded_at`` is a local
    wall-clock field and is reported separately from causal replay time. A
    retrospective replay may therefore be canonically origin-bound even when its
    ledger was physically written after the historical outcome reveal. Physical
    pre-outcome write claims still require an immutable external timestamp/anchor.
    """

    governance = dataset.governance
    if dataset.schema_version != 2 or governance is None or dataset.import_identity is None:
        raise ValueError("forecast origin proof requires a governed historical schema-v2 dataset")
    if not evaluated_forecast_ids:
        raise ValueError("forecast origin proof requires at least one evaluated forecast")

    by_id = {record.forecast_id: record for record in forecasts}
    unknown = sorted(evaluated_forecast_ids.difference(by_id))
    if unknown:
        raise ValueError("forecast origin proof references unknown evaluated forecasts: " + ",".join(unknown))

    summaries = [_load_summary(path, dataset) for path in binding.run_summary_paths]
    run_ids = [item["run_id"] for item in summaries]
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("forecast origin evidence contains duplicate run_id")
    expected_prefix_hashes = {item["decision_ledger_sha256"] for item in summaries}
    prefixes, ledger_sha256 = _validated_ledger_prefixes(
        binding.decision_ledger_path, expected_prefix_hashes
    )
    missing_prefixes = sorted(expected_prefix_hashes.difference(prefixes))
    if missing_prefixes:
        raise ValueError("canonical decision ledger does not contain a run-summary committed prefix")

    reveal_after = parse_iso_timestamp(governance.outcome_reveal_after)
    matched: dict[str, set[str]] = {forecast_id: set() for forecast_id in evaluated_forecast_ids}
    latest_recorded_at: str | None = None
    all_declared_record_times_before_reveal = True

    for summary in summaries:
        run_id = summary["run_id"]
        records = prefixes[summary["decision_ledger_sha256"]]
        for envelope in records:
            record = envelope["record"]
            if record.get("replay_run_id") != run_id:
                continue
            if record.get("agent") != "research-decision-pipeline":
                continue
            if record.get("action") not in {
                "OPEN_PAPER_RESEARCH_TICKET",
                "REJECT_PAPER_RESEARCH_CANDIDATE",
            }:
                continue
            recorded_at_value = record.get("recorded_at")
            if not isinstance(recorded_at_value, str):
                raise ValueError("research decision record lacks recorded_at")
            recorded_at = parse_iso_timestamp(recorded_at_value)
            observed_value = record.get("observed_ts")
            if not isinstance(observed_value, str):
                raise ValueError("research decision record lacks observed_ts")
            observed_at = parse_iso_timestamp(observed_value)
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("real_money_execution") is not False:
                raise ValueError("research decision payload violates paper-only truth boundary")
            forecast_values = payload.get("forecasts")
            if not isinstance(forecast_values, list):
                raise ValueError("research decision payload lacks forecasts list")

            for audit in forecast_values:
                if not isinstance(audit, dict):
                    raise ValueError("research decision forecast audit entry must be an object")
                forecast_id = audit.get("forecast_id")
                if forecast_id not in evaluated_forecast_ids:
                    continue
                forecast = by_id[str(forecast_id)]
                _verify_forecast_audit(audit, forecast)
                if parse_iso_timestamp(forecast.generated_at) > observed_at:
                    raise ValueError("forecast was generated after its canonical decision time")
                if parse_iso_timestamp(forecast.input_cutoff_ts) > observed_at:
                    raise ValueError("forecast input cutoff is after its canonical decision time")
                if recorded_at < observed_at:
                    raise ValueError("research decision recorded_at is before its canonical decision time")
                matched[forecast.forecast_id].add(run_id)
                if recorded_at >= reveal_after:
                    all_declared_record_times_before_reveal = False
                if latest_recorded_at is None or parse_iso_timestamp(latest_recorded_at) < recorded_at:
                    latest_recorded_at = recorded_at_value

    missing = sorted(forecast_id for forecast_id, matched_runs in matched.items() if not matched_runs)
    if missing:
        raise ValueError(
            "evaluated forecasts lack canonical decision-ledger origin: " + ",".join(missing)
        )

    return {
        "status": "CANONICAL_BINDING_VERIFIED",
        "decision_ledger_sha256": ledger_sha256,
        "run_ids": sorted({run_id for matched_runs in matched.values() for run_id in matched_runs}),
        "run_summary_count": len(summaries),
        "evaluated_forecast_count": len(evaluated_forecast_ids),
        "latest_declared_recorded_at": latest_recorded_at,
        "outcome_reveal_after": governance.outcome_reveal_after,
        "canonical_forecast_origin_verified": True,
        "declared_record_time_before_reveal_verified": all_declared_record_times_before_reveal,
        "pre_outcome_ledger_write_verified": False,
        "independent_time_anchor_verified": False,
        "real_money_execution": False,
    }


def _resolve_relative_file(parent: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"walk-forward forecast_origin.{label} must be a non-empty relative path")
    relative = Path(value.strip())
    if relative.is_absolute():
        raise ValueError(f"walk-forward forecast_origin.{label} must be relative to the bundle")
    candidate = (parent / relative).resolve()
    try:
        candidate.relative_to(parent)
    except ValueError as exc:
        raise ValueError(f"walk-forward forecast_origin.{label} escapes the bundle directory") from exc
    if not candidate.is_file():
        raise ValueError(f"walk-forward forecast_origin.{label} does not exist")
    return candidate


def _json_object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _RunSummaryJsonIntegrityError(
                f"forecast origin run summary contains duplicate JSON key {key!r}"
            )
        payload[key] = value
    return payload


def _reject_non_standard_json_constant(value: str) -> None:
    raise _RunSummaryJsonIntegrityError(
        f"forecast origin run summary contains non-standard JSON constant {value!r}"
    )


def _validate_summary_json_domain(value: Any, *, path: str = "$") -> None:
    """Reject JSON values that cannot have one canonical UTF-8 meaning."""

    if value is None or type(value) in {bool, int}:
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _RunSummaryJsonIntegrityError(
                f"forecast origin run summary contains non-UTF-8 text at {path}"
            ) from exc
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _RunSummaryJsonIntegrityError(
                f"forecast origin run summary contains non-finite number at {path}"
            )
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_summary_json_domain(child, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            try:
                key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise _RunSummaryJsonIntegrityError(
                    f"forecast origin run summary contains non-UTF-8 object key at {path}"
                ) from exc
            _validate_summary_json_domain(child, path=f"{path}.{key}")
        return
    raise _RunSummaryJsonIntegrityError(
        f"forecast origin run summary contains unsupported JSON value at {path}"
    )


def _load_strict_summary_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_bytes().decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_json_object_without_duplicate_keys,
            parse_constant=_reject_non_standard_json_constant,
        )
        if not isinstance(raw, dict):
            raise _RunSummaryJsonIntegrityError(
                "forecast origin run summary root must be a JSON object"
            )
        _validate_summary_json_domain(raw)
        return raw
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, _RunSummaryJsonIntegrityError) as exc:
        raise ValueError(
            f"forecast origin run summary is unreadable or invalid canonical UTF-8 JSON: {path}"
        ) from exc


def _load_summary(path: Path, dataset: ReplayDataset) -> dict[str, Any]:
    raw = _load_strict_summary_json(path)
    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version != 2:
        raise ValueError("forecast origin run summary must use schema_version 2")
    transaction_schema_version = raw.get("transaction_schema_version")
    if type(transaction_schema_version) is not int or transaction_schema_version != 1:
        raise ValueError("forecast origin run summary lacks canonical transaction precommit evidence")
    run_id = _required_text(raw, "run_id", "forecast origin run summary")
    if raw.get("transaction_run_id") != run_id:
        raise ValueError("forecast origin run summary transaction_run_id mismatch")
    if raw.get("real_money_execution") is not False:
        raise ValueError("forecast origin run summary violates REAL_MONEY_EXECUTION=false")
    dataset_schema_version = raw.get("dataset_schema_version")
    if type(dataset_schema_version) is not int or dataset_schema_version != 2:
        raise ValueError("forecast origin run summary is not bound to governed dataset schema v2")
    if raw.get("historical_import_identity") != dataset.import_identity:
        raise ValueError("forecast origin run summary historical import identity mismatch")
    if raw.get("market_sha256") != dataset.market_sha256:
        raise ValueError("forecast origin run summary market SHA mismatch")
    if raw.get("sealed_results_sha256") != dataset.results_sha256:
        raise ValueError("forecast origin run summary sealed results SHA mismatch")
    runtime = raw.get("strategy_runtime")
    if not isinstance(runtime, dict) or runtime.get("canonical_strategy_id") != "research-replay-v1":
        raise ValueError("forecast origin run summary is not a canonical research replay run")
    return {
        "run_id": run_id,
        "decision_ledger_sha256": _required_sha256(raw, "decision_ledger_sha256", "forecast origin run summary"),
    }


def _validated_ledger_prefixes(
    path: Path, expected_hashes: set[str]
) -> tuple[dict[str, list[dict[str, Any]]], str]:
    try:
        snapshot = JsonlDecisionLedger(path).verified_snapshot()
    except DecisionLedgerIntegrityError as exc:
        raise ValueError(
            "canonical decision ledger failed semantic integrity validation"
        ) from exc

    hasher = hashlib.sha256()
    records: list[dict[str, Any]] = []
    found: dict[str, list[dict[str, Any]]] = {}
    for raw_line in snapshot.payload.splitlines(keepends=True):
        hasher.update(raw_line)
        if not raw_line.endswith(b"\n"):
            raise ValueError("canonical decision ledger contains a non-terminated line")
        try:
            envelope = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("canonical decision ledger contains invalid UTF-8 JSON") from exc
        _validate_envelope(envelope)
        records.append(envelope)
        digest = hasher.hexdigest()
        if digest in expected_hashes:
            found[digest] = list(records)
    return found, snapshot.sha256


def _validate_envelope(envelope: Any) -> None:
    if not isinstance(envelope, dict) or set(envelope) != {"record", "sha256"}:
        raise ValueError("canonical decision ledger envelope shape is invalid")
    record = envelope.get("record")
    if not isinstance(record, dict):
        raise ValueError("canonical decision ledger record must be an object")
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if envelope.get("sha256") != expected:
        raise ValueError("canonical decision ledger record SHA mismatch")


def _verify_forecast_audit(audit: dict[str, Any], forecast: ForecastRecord) -> None:
    expected = {
        "quote_key": forecast.quote_key,
        "forecast_id": forecast.forecast_id,
        "forecast_hash": forecast.canonical_hash,
        "model_id": forecast.model_id,
        "model_version": forecast.model_version,
        "strategy_version": forecast.strategy_version,
        "input_cutoff_ts": forecast.input_cutoff_ts,
        "generated_at": forecast.generated_at,
        "uncertainty": str(forecast.uncertainty),
    }
    mismatches = [key for key, value in expected.items() if audit.get(key) != value]
    if mismatches:
        raise ValueError("canonical decision ledger forecast audit mismatch: " + ",".join(sorted(mismatches)))


def _required_text(raw: dict[str, Any], key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{context}.{key} must be canonical non-empty text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{context}.{key} must be canonical UTF-8 text") from exc
    return value


def _required_sha256(raw: dict[str, Any], key: str, context: str) -> str:
    value = raw.get(key)
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{context}.{key} must be a canonical lowercase SHA-256 hex digest")
    return value
