from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .forecasting import ForecastRecord, parse_iso_timestamp


_FORBIDDEN_OUTCOME_KEYS = {
    "final_result",
    "result",
    "winner",
    "settled_outcome",
    "future_quote",
    "outcome",
    "sealed_outcome",
}


@dataclass(frozen=True, slots=True)
class CausalForecastOriginEvidence:
    run_id: str
    experiment_key: str
    strategy_id: str
    market_sha256: str
    sealed_results_sha256: str
    historical_import_identity: str | None
    summary_sha256: str
    decision_ledger_sha256: str
    base_decision_ledger_sha256: str
    run_decisions_sha256: str
    forecasts: tuple[tuple[str, str, str, str, str], ...]

    @property
    def forecast_hashes(self) -> dict[str, str]:
        return {forecast_id: digest for forecast_id, digest, _quote, _generated, _observed in self.forecasts}

    @property
    def forecast_quote_keys(self) -> dict[str, str]:
        return {forecast_id: quote for forecast_id, _digest, quote, _generated, _observed in self.forecasts}

    def report(self, *, bound_forecast_count: int | None = None) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "causal_replay_forecast_origin_evidence",
            "status": "PASS",
            "run_id": self.run_id,
            "experiment_key": self.experiment_key,
            "strategy_id": self.strategy_id,
            "market_sha256": self.market_sha256,
            "sealed_results_sha256": self.sealed_results_sha256,
            "historical_import_identity": self.historical_import_identity,
            "run_summary_sha256": self.summary_sha256,
            "decision_ledger_sha256": self.decision_ledger_sha256,
            "base_decision_ledger_sha256": self.base_decision_ledger_sha256,
            "run_decisions_sha256": self.run_decisions_sha256,
            "forecast_count": len(self.forecasts),
            "bound_evaluation_forecast_count": bound_forecast_count,
            "forecasts": [
                {
                    "forecast_id": forecast_id,
                    "forecast_hash": digest,
                    "quote_key": quote_key,
                    "generated_at": generated_at,
                    "decision_observed_ts": observed_ts,
                }
                for forecast_id, digest, quote_key, generated_at, observed_ts in self.forecasts
            ],
            "truth": {
                "transaction_summary_hash_verified": True,
                "canonical_decision_append_chain_verified": True,
                "run_decision_envelopes_verified": True,
                "causal_replay_forecast_hashes_verified": True,
                "outcome_fields_absent_from_causal_decisions": True,
                "evaluation_bundle_forecasts_bound": bound_forecast_count is not None,
                # A causal replay audit proves which exact forecast bytes were consumed
                # by the runtime. It does not prove that an externally authored plan/model
                # was itself created without prior knowledge of historical outcomes.
                "independent_pre_outcome_model_generation_verified": False,
                "temporal_holdout_protocol_verified": False,
                "real_historical_oos_verified": False,
                "licensing_retention_verified": False,
                "profitability_claim": False,
                "predictive_superiority_claim": False,
                "real_money_execution": False,
            },
            "profitability_claim": False,
            "real_money_execution": False,
        }


def verify_workspace_forecast_origin(
    workspace: str | Path,
    run_id: str,
) -> CausalForecastOriginEvidence:
    """Verify one completed research run's durable causal forecast audit chain.

    This binds the exact ForecastRecord hashes found in the per-run decision ledger
    to the crash-recoverable transaction manifest, canonical run summary, and the
    append-only canonical Decision Ledger. It deliberately does *not* promote this
    evidence to independent pre-outcome model generation or holdout/OOS proof.
    """

    _validate_run_id(run_id)
    root = Path(workspace).resolve()
    tx_root = root / ".run-transactions" / run_id
    manifest_path = tx_root / "manifest.json"
    run_decisions_path = tx_root / "run-decisions.jsonl"
    summary_path = root / f"run-{run_id}.json"
    canonical_ledger_path = root / "decisions.jsonl"

    manifest = _read_json_object(manifest_path, "transaction manifest")
    if manifest.get("schema_version") != 1:
        raise ValueError("forecast-origin transaction manifest schema is invalid")
    if manifest.get("phase") != "completed":
        raise ValueError("forecast-origin transaction must be completed")
    if manifest.get("run_id") != run_id:
        raise ValueError("forecast-origin transaction run_id mismatch")
    if manifest.get("real_money_execution") is not False:
        raise ValueError("forecast-origin transaction violates real-money truth boundary")
    _validate_transaction_paths(manifest, run_id)

    experiment_key = _required_text(manifest, "experiment_key", "transaction manifest")
    strategy_id = _required_text(manifest, "strategy_id", "transaction manifest")
    market_sha256 = _required_sha256(manifest.get("market_sha256"), "transaction market_sha256")
    results_sha256 = _required_sha256(
        manifest.get("sealed_results_sha256"), "transaction sealed_results_sha256"
    )
    base_ledger_hash = _required_sha256(
        manifest.get("base", {}).get("decision_ledger_sha256"),
        "transaction base.decision_ledger_sha256",
    )
    new_ledger_hash = _required_sha256(
        manifest.get("new", {}).get("decision_ledger_sha256"),
        "transaction new.decision_ledger_sha256",
    )
    expected_summary_hash = _required_sha256(
        manifest.get("new", {}).get("summary_sha256"),
        "transaction new.summary_sha256",
    )

    if not summary_path.is_file():
        raise ValueError("forecast-origin canonical run summary is missing")
    summary_hash = _sha256_file(summary_path)
    if summary_hash != expected_summary_hash:
        raise ValueError("forecast-origin canonical run summary SHA-256 mismatch")
    summary = _read_json_object(summary_path, "run summary")
    _validate_summary_identity(
        summary,
        run_id=run_id,
        experiment_key=experiment_key,
        strategy_id=strategy_id,
        market_sha256=market_sha256,
        results_sha256=results_sha256,
        decision_ledger_sha256=new_ledger_hash,
    )

    if not run_decisions_path.is_file():
        raise ValueError("forecast-origin per-run Decision Ledger is missing")
    run_bytes = run_decisions_path.read_bytes()
    if not run_bytes:
        raise ValueError("forecast-origin per-run Decision Ledger is empty")
    if not canonical_ledger_path.is_file():
        raise ValueError("forecast-origin canonical Decision Ledger is missing")
    canonical_bytes = canonical_ledger_path.read_bytes()
    committed_prefix = _prefix_with_sha256(canonical_bytes, new_ledger_hash)
    if committed_prefix is None:
        raise ValueError(
            "forecast-origin canonical Decision Ledger no longer contains the transaction NEW state"
        )
    if not committed_prefix.endswith(run_bytes):
        raise ValueError("forecast-origin run decisions are not the transaction Decision Ledger suffix")
    base_prefix = committed_prefix[: -len(run_bytes)]
    if hashlib.sha256(base_prefix).hexdigest() != base_ledger_hash:
        raise ValueError("forecast-origin Decision Ledger BASE->RUN append identity mismatch")

    forecasts = _verified_forecast_entries(run_bytes, run_id)
    if not forecasts:
        raise ValueError("forecast-origin run contains no audited research ForecastRecord hashes")

    historical_import_identity = summary.get("historical_import_identity")
    if historical_import_identity is not None:
        historical_import_identity = _required_sha256(
            historical_import_identity, "run summary historical_import_identity"
        )

    return CausalForecastOriginEvidence(
        run_id=run_id,
        experiment_key=experiment_key,
        strategy_id=strategy_id,
        market_sha256=market_sha256,
        sealed_results_sha256=results_sha256,
        historical_import_identity=historical_import_identity,
        summary_sha256=summary_hash,
        decision_ledger_sha256=new_ledger_hash,
        base_decision_ledger_sha256=base_ledger_hash,
        run_decisions_sha256=hashlib.sha256(run_bytes).hexdigest(),
        forecasts=tuple(sorted(forecasts.values())),
    )


def bind_walk_forward_bundle_forecasts(
    evidence: CausalForecastOriginEvidence,
    bundle_path: str | Path,
) -> int:
    """Bind every evaluation ForecastRecord to an exact hash audited in the causal run."""

    bundle = _read_json_object(Path(bundle_path), "walk-forward bundle")
    forecasts_raw = bundle.get("forecasts")
    if not isinstance(forecasts_raw, list) or not forecasts_raw:
        raise ValueError("walk-forward bundle forecasts must be a non-empty list")

    dataset = bundle.get("dataset")
    if dataset is not None:
        if not isinstance(dataset, dict):
            raise ValueError("walk-forward bundle dataset must be an object")
        if _required_sha256(dataset.get("market_sha256"), "walk-forward dataset.market_sha256") != evidence.market_sha256:
            raise ValueError("walk-forward dataset market_sha256 does not match causal run")
        if _required_sha256(
            dataset.get("sealed_results_sha256"),
            "walk-forward dataset.sealed_results_sha256",
        ) != evidence.sealed_results_sha256:
            raise ValueError("walk-forward dataset sealed_results_sha256 does not match causal run")
        declared_import = _required_sha256(
            dataset.get("historical_import_identity"),
            "walk-forward dataset.historical_import_identity",
        )
        if evidence.historical_import_identity is None or declared_import != evidence.historical_import_identity:
            raise ValueError("walk-forward historical_import_identity does not match causal run")

    origin_hashes = evidence.forecast_hashes
    origin_quotes = evidence.forecast_quote_keys
    seen: set[str] = set()
    for raw in forecasts_raw:
        record = _forecast_from_dict(raw)
        if record.forecast_id in seen:
            raise ValueError(f"walk-forward bundle contains duplicate forecast_id: {record.forecast_id}")
        seen.add(record.forecast_id)
        expected_hash = origin_hashes.get(record.forecast_id)
        if expected_hash is None:
            raise ValueError(
                f"walk-forward forecast lacks causal run audit evidence: {record.forecast_id}"
            )
        if record.canonical_hash != expected_hash:
            raise ValueError(
                f"walk-forward ForecastRecord hash does not match causal run audit: {record.forecast_id}"
            )
        if origin_quotes[record.forecast_id] != record.quote_key:
            raise ValueError(
                f"walk-forward forecast quote_key does not match causal run audit: {record.forecast_id}"
            )
    return len(seen)


def verify_and_report(
    workspace: str | Path,
    run_id: str,
    *,
    bundle_path: str | Path | None = None,
) -> dict[str, Any]:
    evidence = verify_workspace_forecast_origin(workspace, run_id)
    bound = None
    if bundle_path is not None:
        bound = bind_walk_forward_bundle_forecasts(evidence, bundle_path)
    return evidence.report(bound_forecast_count=bound)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autosport-verify-forecast-origin",
        description=(
            "Verify durable causal replay forecast-hash evidence without promoting it "
            "to independent holdout/OOS or profitability proof."
        ),
    )
    parser.add_argument("workspace", nargs="?", help="Autosport strategy workspace")
    parser.add_argument("run_id", nargs="?", help="completed replay run id")
    parser.add_argument(
        "--bundle",
        help="optional walk-forward bundle whose ForecastRecords must match the audited run hashes",
    )
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args(argv)
    if not args.workspace or not args.run_id:
        parser.print_help()
        return 0 if (argv and any(value in {"-h", "--help"} for value in argv)) else 2

    try:
        report = verify_and_report(
            args.workspace,
            args.run_id,
            bundle_path=args.bundle,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FORECAST ORIGIN VERIFICATION FAILED: {exc}")
        return 2

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


def _verified_forecast_entries(
    run_bytes: bytes,
    run_id: str,
) -> dict[str, tuple[str, str, str, str, str]]:
    forecasts: dict[str, tuple[str, str, str, str, str]] = {}
    try:
        text = run_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("forecast-origin per-run Decision Ledger must be UTF-8 JSONL") from exc
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise ValueError("forecast-origin Decision Ledger contains blank JSONL line")
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"forecast-origin Decision Ledger line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(envelope, dict) or not isinstance(envelope.get("record"), dict):
            raise ValueError("forecast-origin Decision Ledger envelope is invalid")
        record = envelope["record"]
        canonical = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        expected = _required_sha256(
            envelope.get("sha256"), "forecast-origin Decision Ledger envelope sha256"
        )
        if actual != expected:
            raise ValueError("forecast-origin Decision Ledger envelope SHA-256 mismatch")
        if record.get("replay_run_id") != run_id:
            raise ValueError("forecast-origin Decision Ledger contains foreign replay_run_id")
        if _contains_forbidden_outcome_key(record):
            raise ValueError("forecast-origin causal Decision Ledger contains outcome/future-result fields")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("forecast-origin Decision Ledger payload is invalid")
        values = payload.get("forecasts")
        if values is None:
            continue
        if record.get("agent") != "research-decision-pipeline":
            raise ValueError("forecast-origin forecast audit came from an unexpected agent")
        if not isinstance(values, list) or not values:
            raise ValueError("forecast-origin research decision forecasts must be a non-empty list")
        observed_ts = _required_text(record, "observed_ts", "forecast-origin decision")
        observed_time = parse_iso_timestamp(observed_ts)
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("forecast-origin forecast audit entry must be an object")
            forecast_id = _required_text(item, "forecast_id", "forecast-origin forecast")
            digest = _required_sha256(item.get("forecast_hash"), "forecast-origin forecast_hash")
            quote_key = _required_text(item, "quote_key", "forecast-origin forecast")
            generated_at = _required_text(item, "generated_at", "forecast-origin forecast")
            input_cutoff = _required_text(item, "input_cutoff_ts", "forecast-origin forecast")
            generated_time = parse_iso_timestamp(generated_at)
            if parse_iso_timestamp(input_cutoff) > generated_time:
                raise ValueError("forecast-origin forecast input cutoff is after generation")
            if generated_time > observed_time:
                raise ValueError("forecast-origin forecast generation is after causal decision time")
            value = (forecast_id, digest, quote_key, generated_at, observed_ts)
            prior = forecasts.get(forecast_id)
            if prior is not None and prior != value:
                raise ValueError("forecast-origin forecast_id has conflicting durable audit evidence")
            forecasts[forecast_id] = value
    return forecasts


def _validate_summary_identity(
    summary: dict[str, Any],
    *,
    run_id: str,
    experiment_key: str,
    strategy_id: str,
    market_sha256: str,
    results_sha256: str,
    decision_ledger_sha256: str,
) -> None:
    expected = {
        "run_id": run_id,
        "transaction_run_id": run_id,
        "experiment_key": experiment_key,
        "strategy_id": strategy_id,
        "market_sha256": market_sha256,
        "sealed_results_sha256": results_sha256,
        "decision_ledger_sha256": decision_ledger_sha256,
    }
    mismatches = [key for key, value in expected.items() if summary.get(key) != value]
    if mismatches:
        raise ValueError(
            "forecast-origin run summary identity mismatch: " + ",".join(sorted(mismatches))
        )
    if summary.get("transaction_schema_version") != 1:
        raise ValueError("forecast-origin run summary transaction schema is invalid")
    if summary.get("real_money_execution") is not False:
        raise ValueError("forecast-origin run summary violates real-money truth boundary")
    runtime = summary.get("strategy_runtime")
    if not isinstance(runtime, dict) or runtime.get("canonical_strategy_id") != "research-replay-v1":
        raise ValueError("forecast-origin evidence requires canonical research-replay-v1 runtime")


def _validate_transaction_paths(manifest: dict[str, Any], run_id: str) -> None:
    expected_targets = {
        "paper_book": "paper_book.json",
        "decision_ledger": "decisions.jsonl",
        "summary": f"run-{run_id}.json",
    }
    expected_staged = {
        "paper_book": "paper_book.next.json",
        "decision_ledger": "decisions.next.jsonl",
        "summary": "run-summary.next.json",
        "run_decisions": "run-decisions.jsonl",
    }
    if manifest.get("targets") != expected_targets or manifest.get("staged") != expected_staged:
        raise ValueError("forecast-origin transaction paths are invalid")


def _prefix_with_sha256(payload: bytes, expected_sha256: str) -> bytes | None:
    hasher = hashlib.sha256()
    offset = 0
    for line in payload.splitlines(keepends=True):
        hasher.update(line)
        offset += len(line)
        if hasher.hexdigest() == expected_sha256:
            return payload[:offset]
    if not payload and hashlib.sha256(b"").hexdigest() == expected_sha256:
        return b""
    return None


def _forecast_from_dict(raw: Any) -> ForecastRecord:
    if not isinstance(raw, dict):
        raise ValueError("walk-forward forecast entry must be an object")
    return ForecastRecord(
        quote_key=str(raw["quote_key"]),
        probability=raw["probability"],
        model_id=str(raw["model_id"]),
        model_version=str(raw["model_version"]),
        strategy_version=str(raw["strategy_version"]),
        model_training_cutoff_ts=str(raw["model_training_cutoff_ts"]),
        input_cutoff_ts=str(raw["input_cutoff_ts"]),
        generated_at=str(raw["generated_at"]),
        uncertainty=raw.get("uncertainty", "0"),
        evidence_hashes=tuple(str(item) for item in raw.get("evidence_hashes", ())),
        market_snapshot_hash=(
            str(raw["market_snapshot_hash"])
            if raw.get("market_snapshot_hash") is not None
            else None
        ),
        provenance=dict(raw.get("provenance", {})),
        forecast_id=str(raw["forecast_id"]),
    )


def _contains_forbidden_outcome_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in _FORBIDDEN_OUTCOME_KEYS
            or _contains_forbidden_outcome_key(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_outcome_key(child) for child in value)
    return False


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is missing, unreadable, or invalid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a JSON object")
    return raw


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{label} must be a SHA-256 hex digest")
    lowered = value.lower()
    if any(char not in "0123456789abcdef" for char in lowered):
        raise ValueError(f"{label} must be a SHA-256 hex digest")
    return lowered


def _required_text(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return value


def _validate_run_id(run_id: str) -> None:
    if not run_id or run_id in {".", ".."} or "/" in run_id or "\\" in run_id:
        raise ValueError("run_id is not a safe workspace path component")


if __name__ == "__main__":
    raise SystemExit(main())
