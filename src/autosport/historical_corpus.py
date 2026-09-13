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
from .parlayapi_provider import ParlayApiTableTennisProvider


_SNAPSHOT_KIND = "parlayapi_point_in_time_historical_snapshot"
_GOVERNANCE_KIND = "historical_corpus_governance_proof"
_OUTCOME_PROVENANCE_KIND = "historical_outcome_provenance"
_ALLOWED_REDISTRIBUTION = {"prohibited", "internal_only", "permitted"}
_REDISTRIBUTION_RANK = {"prohibited": 0, "internal_only": 1, "permitted": 2}
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


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bytes(path: Path, *, context: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{context} is not readable: {path}") from exc


def _json_object_bytes(
    payload: bytes,
    *,
    path: Path,
    context: str,
) -> dict[str, Any]:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} is not readable valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a JSON object")
    return raw


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    payload = _read_bytes(path, context=context)
    return _json_object_bytes(payload, path=path, context=context)


def _text(raw: dict[str, Any], key: str, *, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _governance_proof(path: Path) -> dict[str, Any]:
    payload = _read_bytes(path, context="governance proof")
    raw = _json_object_bytes(payload, path=path, context="governance proof")
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
        "_artifact_sha256": _sha256_bytes(payload),
    }


def _outcome_provenance(
    results: dict[str, Any],
    *,
    source_root: Path,
    reveal_dt: datetime,
    imported_dt: datetime,
) -> dict[str, Any]:
    raw = results.get("outcome_provenance")
    if not isinstance(raw, dict):
        raise ValueError("sealed results outcome_provenance must be an object")
    if int(raw.get("schema_version", 0)) != 1:
        raise ValueError("sealed results outcome_provenance.schema_version must be 1")
    if raw.get("kind") != _OUTCOME_PROVENANCE_KIND:
        raise ValueError(
            f"sealed results outcome_provenance.kind must be {_OUTCOME_PROVENANCE_KIND}"
        )
    if raw.get("licensing_or_retention_verified") is not True:
        raise ValueError(
            "sealed results outcome_provenance must explicitly set licensing_or_retention_verified=true"
        )

    source_identity = _text(raw, "source_identity", context="sealed results outcome_provenance")
    source_record_file = _text(
        raw,
        "source_record_file",
        context="sealed results outcome_provenance",
    )
    relative_source_record = Path(source_record_file)
    if (
        relative_source_record.is_absolute()
        or len(relative_source_record.parts) != 1
        or source_record_file in {".", ".."}
    ):
        raise ValueError(
            "sealed results outcome_provenance.source_record_file must name one direct sibling artifact"
        )
    source_record_sha256 = _text(
        raw,
        "source_record_sha256",
        context="sealed results outcome_provenance",
    ).lower()
    if len(source_record_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_record_sha256
    ):
        raise ValueError(
            "sealed results outcome_provenance.source_record_sha256 must be a 64-character SHA-256 hex digest"
        )
    source_record_path = source_root / relative_source_record
    source_record_bytes = _read_bytes(
        source_record_path,
        context="sealed results outcome provenance source record",
    )
    actual_source_record_sha256 = _sha256_bytes(source_record_bytes)
    if actual_source_record_sha256 != source_record_sha256:
        raise ValueError(
            "sealed results outcome_provenance.source_record_sha256 does not match source record artifact"
        )

    source_record = _json_object_bytes(
        source_record_bytes,
        path=source_record_path,
        context="sealed outcome source record",
    )
    source_record_outcomes = source_record.get("quote_outcomes")
    if not isinstance(source_record_outcomes, dict):
        raise ValueError("sealed outcome source record quote_outcomes must be an object")
    sealed_outcomes = results.get("quote_outcomes")
    if not isinstance(sealed_outcomes, dict):
        raise ValueError("sealed results quote_outcomes must be an object")
    if source_record_outcomes != sealed_outcomes:
        raise ValueError(
            "sealed results quote_outcomes do not match hashed source record outcomes"
        )
    quote_outcomes_sha256 = _canonical_json_sha256(sealed_outcomes)

    terms_reference = _text(raw, "terms_reference", context="sealed results outcome_provenance")
    retention_basis = _text(raw, "retention_basis", context="sealed results outcome_provenance")
    authority_reference = _text(raw, "authority_reference", context="sealed results outcome_provenance")

    available_at = _text(raw, "available_at", context="sealed results outcome_provenance")
    acquired_at = _text(raw, "acquired_at", context="sealed results outcome_provenance")
    verified_at = _text(raw, "verified_at", context="sealed results outcome_provenance")
    available_dt = _timestamp(
        available_at,
        field="sealed results outcome_provenance.available_at",
    )
    acquired_dt = _timestamp(
        acquired_at,
        field="sealed results outcome_provenance.acquired_at",
    )
    verified_dt = _timestamp(
        verified_at,
        field="sealed results outcome_provenance.verified_at",
    )
    if reveal_dt < available_dt:
        raise ValueError(
            "outcome_reveal_after must not precede sealed outcome source availability"
        )
    if acquired_dt < available_dt:
        raise ValueError(
            "sealed results outcome_provenance.acquired_at must not precede available_at"
        )
    if verified_dt < acquired_dt:
        raise ValueError(
            "sealed results outcome_provenance.verified_at must not precede acquired_at"
        )
    if imported_dt < acquired_dt:
        raise ValueError("imported_at must not precede sealed outcome acquisition")
    if imported_dt < verified_dt:
        raise ValueError("imported_at must not precede sealed outcome provenance verification")

    policy = _text(raw, "redistribution_policy", context="sealed results outcome_provenance")
    if policy not in _ALLOWED_REDISTRIBUTION:
        raise ValueError(
            "sealed results outcome_provenance.redistribution_policy must be prohibited, internal_only, or permitted"
        )
    redistribution_verified = raw.get("redistribution_verified")
    if not isinstance(redistribution_verified, bool):
        raise ValueError(
            "sealed results outcome_provenance.redistribution_verified must be boolean"
        )
    if policy == "permitted" and redistribution_verified is not True:
        raise ValueError(
            "sealed results outcome_provenance redistribution_policy=permitted requires redistribution_verified=true"
        )

    return {
        **raw,
        "source_identity": source_identity,
        "source_record_file": source_record_file,
        "source_record_sha256": source_record_sha256,
        "quote_outcomes_sha256": quote_outcomes_sha256,
        "terms_reference": terms_reference,
        "retention_basis": retention_basis,
        "authority_reference": authority_reference,
        "available_at": available_at,
        "acquired_at": acquired_at,
        "verified_at": verified_at,
        "redistribution_policy": policy,
        "redistribution_verified": redistribution_verified,
        "licensing_or_retention_verified": True,
    }


def _snapshot(
    market_path: Path,
    evidence_path: Path,
    *,
    expected_terms_reference: str,
) -> tuple[list[tuple[MarketEvent, dict[str, Any]]], dict[str, Any]]:
    evidence_bytes = _read_bytes(evidence_path, context="snapshot evidence")
    evidence = _json_object_bytes(
        evidence_bytes,
        path=evidence_path,
        context="snapshot evidence",
    )
    evidence["_artifact_sha256"] = _sha256_bytes(evidence_bytes)
    if int(evidence.get("schema_version", 0)) != 1:
        raise ValueError("snapshot evidence schema_version must be 1")
    if evidence.get("kind") != _SNAPSHOT_KIND:
        raise ValueError(f"snapshot evidence kind must be {_SNAPSHOT_KIND}")
    sport_key = _text(evidence, "sport_key", context="snapshot evidence")
    if sport_key != ParlayApiTableTennisProvider.sport_key:
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
    if provider != "parlayapi":
        raise ValueError("snapshot evidence provider must be parlayapi")
    canonical_source_id = ParlayApiTableTennisProvider.source_id

    expected_sha = _text(evidence, "market_sha256", context="snapshot evidence")
    market_bytes = _read_bytes(market_path, context="snapshot market")
    if _sha256_bytes(market_bytes) != expected_sha:
        raise ValueError("snapshot market_sha256 does not match captured market file")

    requested_at = _text(evidence, "requested_at", context="snapshot evidence")
    snapshot_at = _text(evidence, "snapshot_at", context="snapshot evidence")
    captured_at = _text(evidence, "captured_at", context="snapshot evidence")
    requested_dt = _timestamp(requested_at, field="snapshot evidence.requested_at")
    snapshot_dt = _timestamp(snapshot_at, field="snapshot evidence.snapshot_at")
    captured_dt = _timestamp(captured_at, field="snapshot evidence.captured_at")
    if snapshot_dt > requested_dt:
        raise ValueError("snapshot evidence snapshot_at is after requested_at")
    if captured_dt < requested_dt:
        raise ValueError("snapshot evidence captured_at is before requested_at")
    if captured_dt < snapshot_dt:
        raise ValueError("snapshot evidence captured_at is before snapshot_at")

    try:
        market_text = market_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"snapshot market is not readable UTF-8: {market_path}") from exc

    rows: list[tuple[MarketEvent, dict[str, Any]]] = []
    for line_number, line in enumerate(market_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"snapshot market line {line_number} is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"snapshot market line {line_number} must be an object")
        event = MarketEvent.from_dict(raw)
        if event.source_id != canonical_source_id:
            raise ValueError("snapshot evidence provider/sport does not match captured market source_id")
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
    governance_verified_dt = _timestamp(
        proof["verified_at"],
        field="governance proof.verified_at",
    )
    if imported_dt < governance_verified_dt:
        raise ValueError("imported_at must not precede governance proof verification")
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
        evidence_sha256 = evidence.get("_artifact_sha256")
        if (
            not isinstance(evidence_sha256, str)
            or len(evidence_sha256) != 64
            or any(character not in "0123456789abcdef" for character in evidence_sha256)
        ):
            raise ValueError(
                "snapshot evidence artifact digest was not preserved from its verified byte snapshot"
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
                "evidence_sha256": evidence_sha256,
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

    governance_proof_sha256 = proof.get("_artifact_sha256")
    if (
        not isinstance(governance_proof_sha256, str)
        or len(governance_proof_sha256) != 64
        or any(character not in "0123456789abcdef" for character in governance_proof_sha256)
    ):
        raise ValueError("governance proof artifact digest was not preserved from its verified byte snapshot")

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

    results_path_obj = Path(results_path)
    results = _json_object(results_path_obj, context="sealed results")
    if int(results.get("schema_version", 0)) != 1:
        raise ValueError("sealed results schema_version must be 1")
    outcomes = results.get("quote_outcomes")
    if not isinstance(outcomes, dict):
        raise ValueError("sealed results quote_outcomes must be an object")
    invalid_outcomes = sorted(
        str(key)
        for key, value in outcomes.items()
        if not isinstance(value, str) or value not in _ALLOWED_OUTCOMES
    )
    if invalid_outcomes:
        raise ValueError("sealed results contain unsupported outcome; allowed values are win, loss, void")
    outcome_provenance = _outcome_provenance(
        results,
        source_root=results_path_obj.parent,
        reveal_dt=reveal_dt,
        imported_dt=imported_dt,
    )
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
    # The canonical assembled package content-binds the causal reveal instant and
    # independently sourced outcome provenance into sealed results. The declared
    # source-record digest and its normalized outcome labels are verified against an
    # external sibling artifact before assembly; that source artifact is deliberately
    # not redistributed.
    results = {
        **results,
        "outcome_provenance": outcome_provenance,
        "outcome_reveal_after": outcome_reveal_after,
    }
    quote_keys = {event.quote_key for event, _ in events}
    outcome_keys = {str(key) for key in outcomes}
    missing_outcomes = sorted(quote_keys - outcome_keys)
    if missing_outcomes:
        raise ValueError("sealed results are missing quote outcomes for historical market corpus")
    unknown_outcomes = sorted(outcome_keys - quote_keys)
    if unknown_outcomes:
        raise ValueError("sealed results reference quote keys absent from historical market corpus")

    effective_redistribution_policy = min(
        (str(proof["redistribution_policy"]), str(outcome_provenance["redistribution_policy"])),
        key=lambda policy: _REDISTRIBUTION_RANK[policy],
    )

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
            "redistribution_policy": effective_redistribution_policy,
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
                "governance_proof_sha256": governance_proof_sha256,
                "licensing_or_retention_verified": True,
                "rights_source_ids": list(proof["source_ids"]),
                "authority_reference": proof["authority_reference"],
                "verified_at": proof["verified_at"],
                "redistribution_verified": proof["redistribution_verified"],
            },
            "outcome_evidence": {
                "source_identity": outcome_provenance["source_identity"],
                "source_record_file": outcome_provenance["source_record_file"],
                "source_record_sha256": outcome_provenance["source_record_sha256"],
                "source_record_checksum_verified": True,
                "quote_outcomes_bound_to_source_record": True,
                "quote_outcomes_sha256": outcome_provenance["quote_outcomes_sha256"],
                "source_record_redistributed": False,
                "terms_reference": outcome_provenance["terms_reference"],
                "retention_basis": outcome_provenance["retention_basis"],
                "authority_reference": outcome_provenance["authority_reference"],
                "available_at": outcome_provenance["available_at"],
                "acquired_at": outcome_provenance["acquired_at"],
                "verified_at": outcome_provenance["verified_at"],
                "licensing_or_retention_verified": True,
                "redistribution_policy": outcome_provenance["redistribution_policy"],
                "redistribution_verified": outcome_provenance["redistribution_verified"],
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
            redistribution_policy=effective_redistribution_policy,
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
    parser.add_argument(
        "--results",
        type=Path,
        required=True,
        help=(
            "separate sealed results JSON with outcome provenance naming a sibling source artifact "
            "whose SHA-256 and normalized outcome labels are verified during assembly"
        ),
    )
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
        "licensing_or_retention_verified=true outcome_source_checksum_verified=true "
        "outcome_labels_source_bound=true profitability_claim=false real_money_execution=false"
    )
    print(f"dataset={result.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
