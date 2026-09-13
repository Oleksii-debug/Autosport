from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .dataset import load_dataset
from .domain import MarketEvent
from .integrity import atomic_write_json


_SNAPSHOT_KIND = "parlayapi_point_in_time_historical_snapshot"
_GOVERNANCE_KIND = "historical_corpus_governance_proof"
_ALLOWED_REDISTRIBUTION = {"prohibited", "internal_only", "permitted"}
_ALLOWED_OUTCOMES = {"win", "loss", "void"}


@dataclass(frozen=True, slots=True)
class HistoricalCorpusBuild:
    root: str
    import_identity: str
    snapshot_count: int
    event_count: int
    market_sha256: str
    results_sha256: str
    coverage_start_ts: str
    coverage_end_ts: str
    source_ids: tuple[str, ...]
    market_types: tuple[str, ...]
    redistribution_policy: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    return parsed


def _json_object(path: Path, *, context: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} is not readable valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a JSON object")
    return raw


def _text(raw: dict[str, Any], key: str, *, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _governance_proof(path: Path) -> dict[str, Any]:
    raw = _json_object(path, context="governance proof")
    if int(raw.get("schema_version", 0)) != 1:
        raise ValueError("governance proof schema_version must be 1")
    if raw.get("kind") != _GOVERNANCE_KIND:
        raise ValueError(f"governance proof kind must be {_GOVERNANCE_KIND}")
    if raw.get("licensing_or_retention_verified") is not True:
        raise ValueError("governance proof must explicitly set licensing_or_retention_verified=true")

    source_identity = _text(raw, "source_identity", context="governance proof")
    source_ids_raw = raw.get("source_ids")
    if not isinstance(source_ids_raw, list) or not source_ids_raw:
        raise ValueError("governance proof.source_ids must be a non-empty list")
    source_ids = tuple(sorted(str(value).strip() for value in source_ids_raw))
    if any(not value for value in source_ids) or len(set(source_ids)) != len(source_ids):
        raise ValueError("governance proof.source_ids must contain unique non-empty strings")
    terms_reference = _text(raw, "terms_reference", context="governance proof")
    retention_basis = _text(raw, "retention_basis", context="governance proof")
    authority_reference = _text(raw, "authority_reference", context="governance proof")
    verified_at = _text(raw, "verified_at", context="governance proof")
    _timestamp(verified_at, field="governance proof.verified_at")

    policy = _text(raw, "redistribution_policy", context="governance proof")
    if policy not in _ALLOWED_REDISTRIBUTION:
        raise ValueError(
            "governance proof.redistribution_policy must be prohibited, internal_only, or permitted"
        )
    redistribution_verified = raw.get("redistribution_verified")
    if not isinstance(redistribution_verified, bool):
        raise ValueError("governance proof.redistribution_verified must be boolean")
    if policy == "permitted" and redistribution_verified is not True:
        raise ValueError("redistribution_policy=permitted requires redistribution_verified=true")

    return {
        **raw,
        "source_identity": source_identity,
        "source_ids": source_ids,
        "terms_reference": terms_reference,
        "retention_basis": retention_basis,
        "authority_reference": authority_reference,
        "verified_at": verified_at,
        "redistribution_policy": policy,
        "redistribution_verified": redistribution_verified,
    }


def _snapshot(
    market_path: Path,
    evidence_path: Path,
    *,
    expected_terms_reference: str,
) -> tuple[list[tuple[MarketEvent, dict[str, Any]]], dict[str, Any]]:
    evidence = _json_object(evidence_path, context="snapshot evidence")
    if int(evidence.get("schema_version", 0)) != 1:
        raise ValueError("snapshot evidence schema_version must be 1")
    if evidence.get("kind") != _SNAPSHOT_KIND:
        raise ValueError(f"snapshot evidence kind must be {_SNAPSHOT_KIND}")
    if evidence.get("sport_key") != "table_tennis":
        raise ValueError("snapshot evidence sport_key must be table_tennis")
    if evidence.get("has_data") is not True:
        raise ValueError("snapshot evidence must prove has_data=true")
    if evidence.get("point_in_time_snapshot_contains_odds") is not True:
        raise ValueError("snapshot evidence must prove point_in_time_snapshot_contains_odds=true")
    # A single provider response is intentionally narrow evidence after #55. Never
    # convert it into a historical-window coverage claim during corpus assembly.
    if evidence.get("point_in_time_odds_market_coverage_verified") is not False:
        raise ValueError("snapshot evidence must keep point_in_time_odds_market_coverage_verified=false")
    if evidence.get("historical_window_market_coverage_verified") is not False:
        raise ValueError("snapshot evidence must keep historical_window_market_coverage_verified=false")
    if evidence.get("sealed_outcomes_present") is not False:
        raise ValueError("snapshot evidence must keep sealed_outcomes_present=false")
    if evidence.get("replay_corpus_ready") is not False:
        raise ValueError("snapshot evidence must keep replay_corpus_ready=false before assembly")
    if evidence.get("real_money_execution") is not False:
        raise ValueError("snapshot evidence must keep real_money_execution=false")
    if evidence.get("terms_reference") != expected_terms_reference:
        raise ValueError("snapshot evidence terms_reference does not match governance proof")
    provider = _text(evidence, "provider", context="snapshot evidence")

    expected_sha = _text(evidence, "market_sha256", context="snapshot evidence")
    if _sha256(market_path) != expected_sha:
        raise ValueError("snapshot market_sha256 does not match captured market file")

    requested_at = _text(evidence, "requested_at", context="snapshot evidence")
    snapshot_at = _text(evidence, "snapshot_at", context="snapshot evidence")
    captured_at = _text(evidence, "captured_at", context="snapshot evidence")
    requested_dt = _timestamp(requested_at, field="snapshot evidence.requested_at")
    snapshot_dt = _timestamp(snapshot_at, field="snapshot evidence.snapshot_at")
    captured_dt = _timestamp(captured_at, field="snapshot evidence.captured_at")
    if snapshot_dt > requested_dt:
        raise ValueError("snapshot evidence snapshot_at is after requested_at")
    if captured_dt < snapshot_dt:
        raise ValueError("snapshot evidence captured_at is before snapshot_at")

    rows: list[tuple[MarketEvent, dict[str, Any]]] = []
    with market_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"snapshot market line {line_number} is not valid JSON") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"snapshot market line {line_number} must be an object")
            event = MarketEvent.from_dict(raw)
            if event.source_id != provider:
                raise ValueError("snapshot evidence provider does not match captured market source_id")
            if event.source_ts is None or not event.ingest_ts:
                raise ValueError("historical snapshot rows require explicit source_ts and ingest_ts")
            source_dt = _timestamp(event.source_ts, field="historical snapshot source_ts")
            observed_dt = _timestamp(event.observed_ts, field="historical snapshot observed_ts")
            ingest_dt = _timestamp(event.ingest_ts, field="historical snapshot ingest_ts")
            if source_dt > observed_dt:
                raise ValueError("historical snapshot source_ts is after observed_ts")
            if observed_dt > snapshot_dt:
                raise ValueError("historical snapshot observed_ts is after snapshot boundary")
            if ingest_dt < observed_dt:
                raise ValueError("historical snapshot ingest_ts is before observed_ts")
            rows.append((event, raw))

    quote_count = evidence.get("quote_count")
    if not isinstance(quote_count, int) or quote_count != len(rows):
        raise ValueError("snapshot evidence quote_count does not match captured market rows")
    if not rows:
        raise ValueError("historical snapshot market file is empty")
    return rows, evidence


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def assemble_historical_corpus(
    snapshot_pairs: Sequence[tuple[str | Path, str | Path]],
    *,
    results_path: str | Path,
    governance_proof_path: str | Path,
    output_dir: str | Path,
    name: str,
    outcome_reveal_after: str,
    imported_at: str,
) -> HistoricalCorpusBuild:
    if not snapshot_pairs:
        raise ValueError("at least one historical snapshot pair is required")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("historical corpus name must be non-empty")

    output = Path(output_dir)
    if output.exists():
        raise ValueError("output_dir already exists; historical corpus assembly never overwrites")
    output.parent.mkdir(parents=True, exist_ok=True)

    proof_path = Path(governance_proof_path)
    proof = _governance_proof(proof_path)
    imported_dt = _timestamp(imported_at, field="imported_at")
    reveal_dt = _timestamp(outcome_reveal_after, field="outcome_reveal_after")

    events: list[tuple[MarketEvent, dict[str, Any]]] = []
    seen_dedupe: set[str] = set()
    evidence_rows: list[dict[str, Any]] = []
    captures: list[tuple[datetime, str]] = []

    for market_value, evidence_value in snapshot_pairs:
        market_path = Path(market_value)
        evidence_path = Path(evidence_value)
        rows, evidence = _snapshot(
            market_path,
            evidence_path,
            expected_terms_reference=proof["terms_reference"],
        )
        for event, raw in rows:
            if event.dedupe_key in seen_dedupe:
                raise ValueError("historical snapshots contain duplicate canonical source event identity")
            seen_dedupe.add(event.dedupe_key)
            events.append((event, raw))
        captured_at = str(evidence["captured_at"])
        captures.append((_timestamp(captured_at, field="snapshot captured_at"), captured_at))
        evidence_rows.append(
            {
                "evidence_sha256": _sha256(evidence_path),
                "market_sha256": str(evidence["market_sha256"]),
                "response_sha256": str(evidence.get("response_sha256", "")),
                "requested_at": str(evidence["requested_at"]),
                "snapshot_at": str(evidence["snapshot_at"]),
                "captured_at": captured_at,
                "quote_count": int(evidence["quote_count"]),
                "snapshot_timestamp_fallback_count": int(
                    evidence.get("snapshot_timestamp_fallback_count", 0)
                ),
            }
        )

    latest_capture_dt, latest_capture = max(captures, key=lambda item: item[0])
    if imported_dt < latest_capture_dt:
        raise ValueError("imported_at must not precede completion of snapshot acquisition")

    events.sort(
        key=lambda item: (
            item[0].observed_ts,
            item[0].sequence,
            item[0].source_id,
            item[0].event_id,
            item[0].market_id,
            item[0].selection_id,
        )
    )
    coverage_start = min(event.observed_ts for event, _ in events)
    coverage_end = max(event.observed_ts for event, _ in events)
    if reveal_dt < _timestamp(coverage_end, field="coverage end"):
        raise ValueError("outcome_reveal_after must be at or after observed market coverage end")
    source_ids = tuple(sorted({event.source_id for event, _ in events}))
    if source_ids != proof["source_ids"]:
        raise ValueError("governance proof.source_ids do not match historical snapshot source_ids")
    market_types = tuple(sorted({event.market_type.value for event, _ in events}))

    results = _json_object(Path(results_path), context="sealed results")
    if int(results.get("schema_version", 0)) != 1:
        raise ValueError("sealed results schema_version must be 1")
    declared_reveal = results.get("outcome_reveal_after")
    if declared_reveal is not None:
        declared_reveal_dt = _timestamp(
            declared_reveal,
            field="sealed results.outcome_reveal_after",
        )
        if declared_reveal_dt != reveal_dt:
            raise ValueError(
                "sealed results outcome_reveal_after conflicts with requested causal reveal boundary"
            )
    # The canonical assembled package always content-binds the causal reveal instant
    # into the sealed-results file. results_sha256 therefore changes if this boundary
    # changes, and the shared schema-v2 loader checks it against governance.
    results = {**results, "outcome_reveal_after": outcome_reveal_after}
    outcomes = results.get("quote_outcomes")
    if not isinstance(outcomes, dict):
        raise ValueError("sealed results quote_outcomes must be an object")
    quote_keys = {event.quote_key for event, _ in events}
    outcome_keys = {str(key) for key in outcomes}
    missing_outcomes = sorted(quote_keys - outcome_keys)
    if missing_outcomes:
        raise ValueError("sealed results are missing quote outcomes for historical market corpus")
    unknown_outcomes = sorted(outcome_keys - quote_keys)
    if unknown_outcomes:
        raise ValueError("sealed results reference quote keys absent from historical market corpus")
    invalid_outcomes = sorted(
        str(key)
        for key, value in outcomes.items()
        if not isinstance(value, str) or value not in _ALLOWED_OUTCOMES
    )
    if invalid_outcomes:
        raise ValueError("sealed results contain unsupported outcome; allowed values are win, loss, void")

    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.build-", dir=str(output.parent)))
    try:
        market_destination = staging / "market.jsonl"
        results_destination = staging / "results.json"
        manifest_path = staging / "manifest.json"
        _write_jsonl(market_destination, (raw for _, raw in events))
        atomic_write_json(results_destination, results)
        market_sha = _sha256(market_destination)
        results_sha = _sha256(results_destination)

        governance = {
            "source_identity": proof["source_identity"],
            "terms_reference": proof["terms_reference"],
            "retention_basis": proof["retention_basis"],
            "redistribution_policy": proof["redistribution_policy"],
            "acquired_at": latest_capture,
            "imported_at": imported_at,
            "coverage": {
                "start_ts": coverage_start,
                "end_ts": coverage_end,
                "source_ids": list(source_ids),
                "market_types": list(market_types),
            },
            "causality": {
                "strategy_time_field": "observed_ts",
                "outcome_reveal_after": outcome_reveal_after,
            },
            "acquisition_evidence": {
                "scope": "selected_point_in_time_snapshots_only",
                "snapshot_count": len(evidence_rows),
                "snapshots": evidence_rows,
                "point_in_time_snapshot_contains_odds": True,
                "historical_window_market_coverage_verified": False,
                "governance_proof_sha256": _sha256(proof_path),
                "licensing_or_retention_verified": True,
                "rights_source_ids": list(proof["source_ids"]),
                "authority_reference": proof["authority_reference"],
                "verified_at": proof["verified_at"],
                "redistribution_verified": proof["redistribution_verified"],
            },
        }
        manifest = {
            "schema_version": 2,
            "dataset_kind": "historical",
            "name": name.strip(),
            "sport": "table_tennis",
            "market_file": market_destination.name,
            "results_file": results_destination.name,
            "market_sha256": market_sha,
            "results_sha256": results_sha,
            "governance": governance,
        }
        atomic_write_json(manifest_path, manifest)

        staged = load_dataset(staging)
        if staged.import_identity is None:
            raise ValueError("assembled historical corpus did not produce an import identity")
        manifest["import_identity"] = staged.import_identity
        atomic_write_json(manifest_path, manifest)
        load_dataset(staging)

        os.replace(staging, output)
        verified = load_dataset(output)
        if verified.import_identity is None:
            raise ValueError("final historical corpus lost its import identity")
        return HistoricalCorpusBuild(
            root=str(output),
            import_identity=verified.import_identity,
            snapshot_count=len(evidence_rows),
            event_count=len(events),
            market_sha256=verified.market_sha256,
            results_sha256=verified.results_sha256,
            coverage_start_ts=coverage_start,
            coverage_end_ts=coverage_end,
            source_ids=source_ids,
            market_types=market_types,
            redistribution_policy=str(proof["redistribution_policy"]),
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-build-historical-corpus",
        description=(
            "Assemble selected authenticated table-tennis historical snapshots and separate sealed "
            "outcomes into a governed schema-v2 replay corpus without claiming complete window coverage."
        ),
    )
    parser.add_argument(
        "--snapshot",
        action="append",
        nargs=2,
        metavar=("MARKET_JSONL", "EVIDENCE_JSON"),
        required=True,
        help="captured market JSONL plus matching machine evidence; repeat for each snapshot",
    )
    parser.add_argument("--results", type=Path, required=True, help="separate sealed results JSON")
    parser.add_argument(
        "--governance-proof",
        type=Path,
        required=True,
        help="external non-secret rights/retention verification JSON",
    )
    parser.add_argument("--output", type=Path, required=True, help="new output dataset directory; never overwritten")
    parser.add_argument("--name", required=True, help="human-readable corpus name")
    parser.add_argument("--outcome-reveal-after", required=True, help="ISO-8601 causal outcome reveal timestamp")
    parser.add_argument("--imported-at", required=True, help="explicit ISO-8601 import timestamp")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = assemble_historical_corpus(
            [(Path(market), Path(evidence)) for market, evidence in args.snapshot],
            results_path=args.results,
            governance_proof_path=args.governance_proof,
            output_dir=args.output,
            name=args.name,
            outcome_reveal_after=args.outcome_reveal_after,
            imported_at=args.imported_at,
        )
    except (OSError, ValueError) as exc:
        print(f"historical_corpus=FAIL_CLOSED error={exc}")
        return 3

    print(
        f"historical_corpus=ASSEMBLED snapshots={result.snapshot_count} events={result.event_count} "
        f"coverage={result.coverage_start_ts}..{result.coverage_end_ts}"
    )
    print(f"historical_import_identity={result.import_identity}")
    print(
        f"market_sha256={result.market_sha256} results_sha256={result.results_sha256} "
        f"redistribution_policy={result.redistribution_policy}"
    )
    print(
        "scope=selected_point_in_time_snapshots_only historical_window_market_coverage_verified=false "
        "licensing_or_retention_verified=true profitability_claim=false real_money_execution=false"
    )
    print(f"dataset={result.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
