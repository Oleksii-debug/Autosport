from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .historical_corpus import HistoricalCorpusBuild, assemble_historical_corpus


_BUNDLE_KIND = "parlayapi_historical_acquisition_bundle"
_SNAPSHOT_KIND = "parlayapi_point_in_time_historical_snapshot"
_RESULT_CAPTURE_KIND = "parlayapi_historical_match_result_capture"
_RESULT_EVIDENCE_KIND = "parlayapi_historical_match_result_evidence"
_BOUNDED_FALSE_FIELDS = (
    "provider_result_schema_parsed",
    "sealed_quote_outcomes_derived",
    "point_in_time_odds_market_coverage_verified",
    "historical_window_market_coverage_verified",
    "licensing_or_retention_verified",
    "redistribution_verified",
    "replay_corpus_ready",
    "real_money_execution",
    "human_tested",
    "nvda_verified",
)


@dataclass(frozen=True, slots=True)
class VerifiedAcquisitionBundle:
    root: str
    bundle_sha256: str
    request_identity: str
    evidence_identity: str
    snapshot_pairs: tuple[tuple[str, str], ...]
    result_capture_path: str
    result_evidence_path: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _object(path: Path, *, context: str) -> dict[str, Any]:
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


def _digest(raw: dict[str, Any], key: str, *, context: str) -> str:
    value = _text(raw, key, context=context)
    if len(value) != 64 or value != value.lower() or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{context}.{key} must be a canonical lowercase SHA-256")
    return value


def _false(raw: dict[str, Any], key: str, *, context: str) -> None:
    if raw.get(key) is not False:
        raise ValueError(f"{context}.{key} must remain false")


def _member(root: Path, value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"{field} must be relative to the acquisition bundle")
    root_resolved = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"{field} escapes the acquisition bundle") from exc
    if not candidate.is_file():
        raise ValueError(f"{field} does not resolve to a regular file inside the bundle")
    return candidate


def verify_acquisition_bundle(
    bundle_dir: str | Path,
    *,
    expected_bundle_sha256: str,
) -> VerifiedAcquisitionBundle:
    root = Path(bundle_dir)
    if not root.is_dir():
        raise ValueError("bundle_dir must be an existing acquisition-bundle directory")
    expected = expected_bundle_sha256.strip()
    if len(expected) != 64 or expected != expected.lower() or any(
        ch not in "0123456789abcdef" for ch in expected
    ):
        raise ValueError("expected_bundle_sha256 must be a canonical lowercase SHA-256")

    bundle_path = root / "bundle.json"
    if not bundle_path.is_file():
        raise ValueError("acquisition bundle is missing bundle.json")
    actual_bundle_sha = _sha256(bundle_path)
    if actual_bundle_sha != expected:
        raise ValueError("bundle.json SHA-256 does not match expected_bundle_sha256")

    bundle = _object(bundle_path, context="acquisition bundle")
    if int(bundle.get("schema_version", 0)) != 1:
        raise ValueError("acquisition bundle schema_version must be 1")
    if bundle.get("kind") != _BUNDLE_KIND:
        raise ValueError(f"acquisition bundle kind must be {_BUNDLE_KIND}")
    if bundle.get("acquisition_scope") != "selected_point_in_time_snapshots_plus_match_result_archive":
        raise ValueError("acquisition bundle has unsupported acquisition_scope")
    for field in _BOUNDED_FALSE_FIELDS:
        _false(bundle, field, context="acquisition bundle")

    request_scope = bundle.get("request_scope")
    if not isinstance(request_scope, dict):
        raise ValueError("acquisition bundle.request_scope must be an object")
    if request_scope.get("provider") != "parlayapi":
        raise ValueError("acquisition bundle.request_scope.provider must be parlayapi")
    if request_scope.get("sport_key") != "table_tennis":
        raise ValueError("acquisition bundle.request_scope.sport_key must be table_tennis")
    request_identity = _digest(bundle, "request_identity", context="acquisition bundle")
    if request_identity != _canonical_hash(request_scope):
        raise ValueError("acquisition bundle request_identity does not match request_scope")

    snapshots = bundle.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots or not all(isinstance(item, dict) for item in snapshots):
        raise ValueError("acquisition bundle.snapshots must be a non-empty list of objects")
    if bundle.get("snapshot_count") != len(snapshots):
        raise ValueError("acquisition bundle.snapshot_count does not match snapshots")
    snapshots_with_odds = sum(
        1 for item in snapshots if item.get("point_in_time_snapshot_contains_odds") is True
    )
    if bundle.get("snapshots_with_odds") != snapshots_with_odds:
        raise ValueError("acquisition bundle.snapshots_with_odds does not match snapshots")
    if snapshots_with_odds != len(snapshots) or bundle.get("all_requested_snapshots_returned_odds") is not True:
        raise ValueError(
            "acquisition bundle cannot feed a replay corpus unless every requested snapshot returned odds"
        )

    requested_scope = request_scope.get("requested_snapshot_timestamps")
    requested_from_entries = [item.get("requested_at") for item in snapshots]
    if requested_scope != requested_from_entries:
        raise ValueError("acquisition bundle request_scope timestamps do not match snapshot entries")

    match_results = bundle.get("match_results")
    if not isinstance(match_results, dict):
        raise ValueError("acquisition bundle.match_results must be an object")
    result_scope = request_scope.get("match_results")
    if not isinstance(result_scope, dict):
        raise ValueError("acquisition bundle.request_scope.match_results must be an object")
    if result_scope.get("date") != match_results.get("requested_date"):
        raise ValueError("acquisition bundle match-result date does not match request scope")
    if result_scope.get("priced_only") is not match_results.get("priced_only"):
        raise ValueError("acquisition bundle match-result priced_only does not match request scope")

    coverage_scope = request_scope.get("coverage_preflight")
    if not isinstance(coverage_scope, dict):
        raise ValueError("acquisition bundle.request_scope.coverage_preflight must be an object")
    coverage_evidence = match_results.get("coverage_preflight")
    if not isinstance(coverage_evidence, dict):
        raise ValueError("acquisition bundle.match_results.coverage_preflight must be an object")
    for field in ("date_from", "date_to"):
        scoped = _text(coverage_scope, field, context="acquisition bundle.request_scope.coverage_preflight")
        evidenced = _text(
            coverage_evidence,
            field,
            context="acquisition bundle.match_results.coverage_preflight",
        )
        if scoped != evidenced:
            raise ValueError(f"acquisition bundle coverage preflight {field} does not match request scope")
    _text(coverage_evidence, "observed_at", context="acquisition bundle.match_results.coverage_preflight")
    _text(
        coverage_evidence,
        "historical_window_from",
        context="acquisition bundle.match_results.coverage_preflight",
    )
    _text(coverage_evidence, "api_version", context="acquisition bundle.match_results.coverage_preflight")
    _digest(
        coverage_evidence,
        "response_sha256",
        context="acquisition bundle.match_results.coverage_preflight",
    )
    historical_window_hours = coverage_evidence.get("historical_window_hours")
    if (
        not isinstance(historical_window_hours, int)
        or isinstance(historical_window_hours, bool)
        or historical_window_hours <= 0
    ):
        raise ValueError(
            "acquisition bundle.match_results.coverage_preflight.historical_window_hours must be a positive integer"
        )
    sources = coverage_evidence.get("sources")
    if not isinstance(sources, list) or not sources or not all(isinstance(item, dict) for item in sources):
        raise ValueError("acquisition bundle.match_results.coverage_preflight.sources must be a non-empty list of objects")
    source_count = coverage_evidence.get("source_count")
    if (
        not isinstance(source_count, int)
        or isinstance(source_count, bool)
        or source_count != len(sources)
    ):
        raise ValueError("acquisition bundle coverage preflight source_count does not match sources")
    total_rows = coverage_evidence.get("total_rows")
    total_priced_rows = coverage_evidence.get("total_priced_rows")
    if not isinstance(total_rows, int) or isinstance(total_rows, bool) or total_rows <= 0:
        raise ValueError("acquisition bundle coverage preflight total_rows must be a positive integer")
    if (
        not isinstance(total_priced_rows, int)
        or isinstance(total_priced_rows, bool)
        or total_priced_rows < 0
        or total_priced_rows > total_rows
    ):
        raise ValueError("acquisition bundle coverage preflight total_priced_rows is invalid")
    summed_rows = 0
    summed_priced_rows = 0
    seen_sources: set[str] = set()
    for index, source in enumerate(sources, start=1):
        context = f"acquisition bundle.match_results.coverage_preflight.sources[{index}]"
        source_name = _text(source, "source", context=context)
        if source_name in seen_sources:
            raise ValueError("acquisition bundle coverage preflight source identities must be unique")
        seen_sources.add(source_name)
        rows = source.get("rows")
        priced_rows = source.get("priced_rows")
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            raise ValueError(f"{context}.rows must be a non-negative integer")
        if (
            not isinstance(priced_rows, int)
            or isinstance(priced_rows, bool)
            or priced_rows < 0
            or priced_rows > rows
        ):
            raise ValueError(f"{context}.priced_rows must be between zero and rows")
        if rows > 0:
            _text(source, "first_date", context=context)
            _text(source, "last_date", context=context)
        summed_rows += rows
        summed_priced_rows += priced_rows
    if summed_rows != total_rows:
        raise ValueError("acquisition bundle coverage preflight total_rows does not match sources")
    if summed_priced_rows != total_priced_rows:
        raise ValueError("acquisition bundle coverage preflight total_priced_rows does not match sources")
    for field in (
        "historical_window_market_coverage_verified",
        "licensing_or_retention_verified",
        "redistribution_verified",
    ):
        _false(
            coverage_evidence,
            field,
            context="acquisition bundle.match_results.coverage_preflight",
        )

    evidence_identity = _digest(bundle, "evidence_identity", context="acquisition bundle")
    identity_payload = {
        "schema_version": 1,
        "kind": _BUNDLE_KIND,
        "request_identity": request_identity,
        "snapshots": snapshots,
        "match_results": match_results,
    }
    if evidence_identity != _canonical_hash(identity_payload):
        raise ValueError("acquisition bundle evidence_identity does not match bound evidence")

    seen_paths: set[Path] = set()
    snapshot_pairs: list[tuple[str, str]] = []
    for index, entry in enumerate(snapshots, start=1):
        context = f"acquisition bundle.snapshots[{index}]"
        if entry.get("point_in_time_snapshot_contains_odds") is not True:
            raise ValueError(f"{context} must contain odds before corpus assembly")
        market_path = _member(root, entry.get("market_file"), field=f"{context}.market_file")
        evidence_path = _member(root, entry.get("evidence_file"), field=f"{context}.evidence_file")
        for path in (market_path, evidence_path):
            if path in seen_paths:
                raise ValueError("acquisition bundle file paths must be unique")
            seen_paths.add(path)

        market_sha = _digest(entry, "market_sha256", context=context)
        evidence_sha = _digest(entry, "evidence_sha256", context=context)
        response_sha = _digest(entry, "provider_response_sha256", context=context)
        if _sha256(market_path) != market_sha:
            raise ValueError(f"{context}.market_sha256 does not match market file")
        if _sha256(evidence_path) != evidence_sha:
            raise ValueError(f"{context}.evidence_sha256 does not match evidence file")

        evidence = _object(evidence_path, context=f"{context} evidence")
        if int(evidence.get("schema_version", 0)) != 1 or evidence.get("kind") != _SNAPSHOT_KIND:
            raise ValueError(f"{context} evidence is not canonical historical snapshot evidence")
        if evidence.get("provider") != "parlayapi" or evidence.get("sport_key") != "table_tennis":
            raise ValueError(f"{context} evidence provider/sport identity mismatch")
        if evidence.get("requested_at") != entry.get("requested_at"):
            raise ValueError(f"{context} requested_at does not match evidence")
        if evidence.get("snapshot_at") != entry.get("snapshot_at"):
            raise ValueError(f"{context} snapshot_at does not match evidence")
        if evidence.get("captured_at") != entry.get("captured_at"):
            raise ValueError(f"{context} captured_at does not match evidence")
        if evidence.get("market_sha256") != market_sha:
            raise ValueError(f"{context} market hash does not match evidence")
        if evidence.get("response_sha256") != response_sha:
            raise ValueError(f"{context} provider response hash does not match evidence")
        if evidence.get("quote_count") != entry.get("quote_count"):
            raise ValueError(f"{context} quote_count does not match evidence")
        if evidence.get("has_data") is not True or evidence.get("point_in_time_snapshot_contains_odds") is not True:
            raise ValueError(f"{context} evidence must prove a non-empty selected snapshot")
        for field in (
            "point_in_time_odds_market_coverage_verified",
            "historical_window_market_coverage_verified",
            "sealed_outcomes_present",
            "replay_corpus_ready",
            "licensing_or_retention_verified",
            "redistribution_verified",
            "real_money_execution",
            "human_tested",
            "nvda_verified",
        ):
            _false(evidence, field, context=f"{context} evidence")
        snapshot_pairs.append((str(market_path), str(evidence_path)))

    result_capture = _member(
        root,
        match_results.get("capture_file"),
        field="acquisition bundle.match_results.capture_file",
    )
    result_evidence = _member(
        root,
        match_results.get("evidence_file"),
        field="acquisition bundle.match_results.evidence_file",
    )
    for path in (result_capture, result_evidence):
        if path in seen_paths:
            raise ValueError("acquisition bundle file paths must be unique")
        seen_paths.add(path)

    capture_sha = _digest(match_results, "capture_sha256", context="acquisition bundle.match_results")
    result_evidence_sha = _digest(
        match_results, "evidence_sha256", context="acquisition bundle.match_results"
    )
    canonical_response_sha = _digest(
        match_results, "canonical_response_sha256", context="acquisition bundle.match_results"
    )
    if _sha256(result_capture) != capture_sha:
        raise ValueError("acquisition bundle match-result capture hash does not match file")
    if _sha256(result_evidence) != result_evidence_sha:
        raise ValueError("acquisition bundle match-result evidence hash does not match file")

    capture = _object(result_capture, context="match-result capture")
    if int(capture.get("schema_version", 0)) != 1 or capture.get("kind") != _RESULT_CAPTURE_KIND:
        raise ValueError("match-result capture is not canonical")
    if capture.get("provider") != "parlayapi" or capture.get("sport_key") != "table_tennis":
        raise ValueError("match-result capture provider/sport identity mismatch")
    capture_request = capture.get("request")
    if not isinstance(capture_request, dict):
        raise ValueError("match-result capture.request must be an object")
    if capture_request.get("date") != match_results.get("requested_date"):
        raise ValueError("match-result capture date does not match bundle")
    if capture_request.get("priced_only") is not match_results.get("priced_only"):
        raise ValueError("match-result capture priced_only does not match bundle")
    if capture.get("canonical_response_sha256") != canonical_response_sha:
        raise ValueError("match-result capture response hash does not match bundle")
    if _canonical_hash(capture.get("payload")) != canonical_response_sha:
        raise ValueError("match-result capture payload does not match canonical response hash")

    result_evidence_raw = _object(result_evidence, context="match-result evidence")
    if (
        int(result_evidence_raw.get("schema_version", 0)) != 1
        or result_evidence_raw.get("kind") != _RESULT_EVIDENCE_KIND
    ):
        raise ValueError("match-result evidence is not canonical")
    if result_evidence_raw.get("provider") != "parlayapi" or result_evidence_raw.get("sport_key") != "table_tennis":
        raise ValueError("match-result evidence provider/sport identity mismatch")
    if result_evidence_raw.get("requested_date") != match_results.get("requested_date"):
        raise ValueError("match-result evidence date does not match bundle")
    if result_evidence_raw.get("priced_only") is not match_results.get("priced_only"):
        raise ValueError("match-result evidence priced_only does not match bundle")
    if result_evidence_raw.get("capture_sha256") != capture_sha:
        raise ValueError("match-result evidence capture hash does not match bundle")
    if result_evidence_raw.get("canonical_response_sha256") != canonical_response_sha:
        raise ValueError("match-result evidence response hash does not match bundle")
    if result_evidence_raw.get("historical_window_hours") != match_results.get("historical_window_hours"):
        raise ValueError("match-result entitlement hours do not match bundle")
    if result_evidence_raw.get("historical_window_from") != match_results.get("historical_window_from"):
        raise ValueError("match-result entitlement start does not match bundle")
    for field in _BOUNDED_FALSE_FIELDS:
        _false(result_evidence_raw, field, context="match-result evidence")

    return VerifiedAcquisitionBundle(
        root=str(root),
        bundle_sha256=actual_bundle_sha,
        request_identity=request_identity,
        evidence_identity=evidence_identity,
        snapshot_pairs=tuple(snapshot_pairs),
        result_capture_path=str(result_capture),
        result_evidence_path=str(result_evidence),
    )


def assemble_historical_corpus_from_bundle(
    bundle_dir: str | Path,
    *,
    expected_bundle_sha256: str,
    results_path: str | Path,
    governance_proof_path: str | Path,
    output_dir: str | Path,
    name: str,
    outcome_reveal_after: str,
    imported_at: str,
) -> HistoricalCorpusBuild:
    verified = verify_acquisition_bundle(
        bundle_dir,
        expected_bundle_sha256=expected_bundle_sha256,
    )
    return assemble_historical_corpus(
        verified.snapshot_pairs,
        results_path=results_path,
        governance_proof_path=governance_proof_path,
        output_dir=output_dir,
        name=name,
        outcome_reveal_after=outcome_reveal_after,
        imported_at=imported_at,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-build-historical-corpus-from-bundle",
        description=(
            "Verify one immutable historical acquisition bundle end to end, then reuse the canonical "
            "governed corpus assembler with separate sealed outcomes and governance proof."
        ),
    )
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--expected-bundle-sha256", required=True)
    parser.add_argument("--results", type=Path, required=True, help="separate sealed quote-outcome JSON")
    parser.add_argument("--governance-proof", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--outcome-reveal-after", required=True)
    parser.add_argument("--imported-at", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        verified = verify_acquisition_bundle(
            args.bundle_dir,
            expected_bundle_sha256=args.expected_bundle_sha256,
        )
        report = assemble_historical_corpus(
            verified.snapshot_pairs,
            results_path=args.results,
            governance_proof_path=args.governance_proof,
            output_dir=args.output_dir,
            name=args.name,
            outcome_reveal_after=args.outcome_reveal_after,
            imported_at=args.imported_at,
        )
    except (ValueError, OSError) as exc:
        print(f"historical_bundle_corpus=FAIL_CLOSED error={exc}")
        return 3

    print(
        f"historical_bundle_corpus=BUILT events={report.event_count} "
        f"snapshots={report.snapshot_count}"
    )
    print(f"bundle_sha256={verified.bundle_sha256}")
    print(f"request_identity={verified.request_identity}")
    print(f"evidence_identity={verified.evidence_identity}")
    print(f"historical_import_identity={report.import_identity}")
    print(
        "historical_window_market_coverage_verified=false "
        "provider_result_schema_parsed=false sealed_quote_outcomes_derived=false"
    )
    print("real_money_execution=false human_tested=false nvda_verified=false")
    print(f"corpus={report.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
