from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .historical_matches import capture_historical_matches, historical_match_request_url
from .historical_snapshot import capture_historical_snapshot
from .integrity import atomic_write_json, sha256_file
from .parlayapi_provider import (
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
    ProviderTransportError,
)


_BUNDLE_KIND = "parlayapi_historical_acquisition_bundle"


@dataclass(frozen=True, slots=True)
class HistoricalAcquisitionBundle:
    root: str
    request_identity: str
    evidence_identity: str
    bundle_sha256: str
    snapshot_count: int
    snapshots_with_odds: int
    result_capture_sha256: str


def _canonical_timestamp(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_date(value: str, *, field: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_staged_digest(path: Path, expected_sha256: str, *, field: str) -> str:
    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise ProviderPayloadError(f"{field} bytes changed after child capture")
    return actual_sha256


def _read_strict_json_object_with_sha256(
    path: Path,
    *,
    field: str,
) -> tuple[dict[str, Any], str]:
    try:
        raw_bytes = path.read_bytes()
        raw = raw_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ProviderPayloadError(f"{field} must be readable UTF-8 JSON") from exc

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise ProviderPayloadError(f"{field} contains duplicate JSON key {key!r}")
            payload[key] = value
        return payload

    def reject_non_finite(value: str) -> None:
        raise ProviderPayloadError(f"{field} contains non-finite JSON constant {value!r}")

    try:
        payload = json.loads(
            raw,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite,
        )
    except json.JSONDecodeError as exc:
        raise ProviderPayloadError(f"{field} must be valid JSON") from exc
    if type(payload) is not dict:
        raise ProviderPayloadError(f"{field} must be a JSON object")
    return payload, hashlib.sha256(raw_bytes).hexdigest()


def _read_strict_json_object(path: Path, *, field: str) -> dict[str, Any]:
    payload, _ = _read_strict_json_object_with_sha256(path, field=field)
    return payload


def _require_match_result_evidence_semantics(
    payload: dict[str, Any],
    *,
    expected_sport_key: str,
    expected_date: str,
    expected_priced_only: bool,
    expected_request_url: str,
) -> dict[str, Any]:
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ProviderPayloadError("match_results.evidence schema_version mismatch")
    if payload.get("kind") != "parlayapi_historical_match_result_evidence":
        raise ProviderPayloadError("match_results.evidence kind mismatch")
    if payload.get("provider") != "parlayapi":
        raise ProviderPayloadError("match_results.evidence provider identity mismatch")
    if payload.get("sport_key") != expected_sport_key:
        raise ProviderPayloadError("match_results.evidence sport identity mismatch")
    if payload.get("requested_date") != expected_date:
        raise ProviderPayloadError("match_results.evidence requested_date mismatch")
    if type(payload.get("priced_only")) is not bool or payload["priced_only"] is not expected_priced_only:
        raise ProviderPayloadError("match_results.evidence priced_only mismatch")
    if payload.get("request_url") != expected_request_url:
        raise ProviderPayloadError("match_results.evidence request_url mismatch")

    captured_at = payload.get("captured_at")
    if type(captured_at) is not str:
        raise ProviderPayloadError("match_results.evidence captured_at must be text")
    try:
        _canonical_timestamp(captured_at, field="match_results.evidence.captured_at")
    except ValueError as exc:
        raise ProviderPayloadError(
            "match_results.evidence captured_at must be a timezone-aware ISO timestamp"
        ) from exc

    canonical_response_sha256 = payload.get("canonical_response_sha256")
    capture_sha256 = payload.get("capture_sha256")
    for field_name, value in (
        ("canonical_response_sha256", canonical_response_sha256),
        ("capture_sha256", capture_sha256),
    ):
        if (
            type(value) is not str
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ProviderPayloadError(
                f"match_results.evidence {field_name} must be lowercase SHA-256 hex"
            )

    historical_window_hours = payload.get("historical_window_hours")
    if (
        type(historical_window_hours) is not int
        or historical_window_hours <= 0
    ):
        raise ProviderPayloadError(
            "match_results.evidence historical_window_hours must be a positive integer"
        )
    historical_window_from = payload.get("historical_window_from")
    if type(historical_window_from) is not str or not historical_window_from.strip():
        raise ProviderPayloadError(
            "match_results.evidence historical_window_from must be text"
        )
    try:
        date.fromisoformat(historical_window_from.strip()[:10])
    except ValueError as exc:
        raise ProviderPayloadError(
            "match_results.evidence historical_window_from must start with an ISO date"
        ) from exc

    trust_fields = (
        "product_owned_request_path_verified",
        "product_owned_acquisition_clock_verified",
        "provider_response_origin_verified",
        "trusted_outcome_source_admissible",
    )
    for field_name in trust_fields:
        if payload.get(field_name) is not False:
            raise ProviderPayloadError(
                f"match_results.evidence {field_name} must remain false on this authority"
            )

    return {
        "requested_date": expected_date,
        "priced_only": expected_priced_only,
        "request_url": expected_request_url,
        "captured_at": captured_at,
        "capture_sha256": capture_sha256,
        "canonical_response_sha256": canonical_response_sha256,
        "historical_window_hours": historical_window_hours,
        "historical_window_from": historical_window_from,
        **{field_name: False for field_name in trust_fields},
    }


def capture_historical_acquisition_bundle(
    provider: ParlayApiTableTennisProvider,
    *,
    requested_at: Sequence[str],
    results_date: str,
    output_dir: str | Path,
    results_priced_only: bool = False,
) -> HistoricalAcquisitionBundle:
    """Atomically capture selected historical odds snapshots and match/result evidence.

    A one-credit authenticated coverage preflight runs before the more expensive
    point-in-time snapshot calls. Its exact request window and validated provider
    summary are content-bound into the bundle identities. This proves only runtime
    entitlement/source-row evidence for that request window; it does not promote the
    selected snapshots to complete historical market coverage or derive outcomes.

    Match-result logical request provenance and fail-closed trust facts from the
    canonical capture boundary are content-bound into the bundle as well.  The
    intended request URL is not promoted into proof that a mutable provider object
    actually used a product-owned transport/clock, and the response envelope cannot
    prove final response origin or trusted outcome-source authority.
    """

    if provider.public_preview or not provider.api_key:
        raise ValueError("historical acquisition bundle requires an authenticated API key")
    if not requested_at:
        raise ValueError("at least one requested historical snapshot timestamp is required")
    if not isinstance(results_priced_only, bool):
        raise ValueError("results_priced_only must be boolean")

    canonical_requests = tuple(
        sorted(_canonical_timestamp(value, field="requested_at") for value in requested_at)
    )
    if len(set(canonical_requests)) != len(canonical_requests):
        raise ValueError("requested historical snapshot timestamps must be unique instants")
    canonical_results_date = _canonical_date(results_date, field="results_date").isoformat()
    requested_dates = tuple(
        datetime.fromisoformat(value.replace("Z", "+00:00")).date() for value in canonical_requests
    )
    coverage_dates = (*requested_dates, date.fromisoformat(canonical_results_date))
    coverage_from = min(coverage_dates).isoformat()
    coverage_to = max(coverage_dates).isoformat()

    output = Path(output_dir)
    if output.exists():
        raise ValueError("output_dir already exists; historical acquisition bundles never overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)

    coverage_report = provider.historical_coverage(coverage_from, coverage_to)
    if not coverage_report.has_data:
        raise ProviderPayloadError("historical coverage preflight returned no source rows")
    coverage_request = {
        "date_from": coverage_from,
        "date_to": coverage_to,
    }
    coverage_evidence = {
        "date_from": coverage_report.date_from,
        "date_to": coverage_report.date_to,
        "observed_at": coverage_report.observed_at,
        "historical_window_hours": coverage_report.historical_window_hours,
        "historical_window_from": coverage_report.historical_window_from,
        "response_sha256": coverage_report.response_sha256,
        "api_version": coverage_report.api_version,
        "source_count": len(coverage_report.sources),
        "total_rows": coverage_report.total_rows,
        "total_priced_rows": coverage_report.total_priced_rows,
        "sources": [
            {
                "source": item.source,
                "rows": item.rows,
                "first_date": item.first_date,
                "last_date": item.last_date,
                "priced_rows": item.priced_rows,
            }
            for item in coverage_report.sources
        ],
        "historical_window_market_coverage_verified": False,
        "licensing_or_retention_verified": False,
        "redistribution_verified": False,
    }

    results_request_url = historical_match_request_url(
        provider,
        requested_date=canonical_results_date,
        priced_only=results_priced_only,
    )
    request_scope = {
        "provider": "parlayapi",
        "sport_key": provider.sport_key,
        "regions": list(provider.regions),
        "markets": list(provider.markets),
        "requested_snapshot_timestamps": list(canonical_requests),
        "coverage_preflight": coverage_request,
        "match_results": {
            "url": results_request_url,
            "date": canonical_results_date,
            "priced_only": results_priced_only,
        },
    }
    request_identity = _canonical_hash(request_scope)

    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.acquiring-", dir=str(output.parent)))
    try:
        snapshot_dir = staging / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=False)
        snapshot_entries: list[dict[str, Any]] = []
        snapshots_with_odds = 0

        for index, instant in enumerate(canonical_requests, start=1):
            market_relative = Path("snapshots") / f"{index:04d}-market.jsonl"
            evidence_relative = Path("snapshots") / f"{index:04d}-evidence.json"
            market_path = staging / market_relative
            evidence_path = staging / evidence_relative
            report = capture_historical_snapshot(
                provider,
                requested_at=instant,
                output_path=market_path,
                evidence_path=evidence_path,
            )
            if report.has_data:
                snapshots_with_odds += 1
            snapshot_entries.append(
                {
                    "requested_at": report.requested_at,
                    "snapshot_at": report.snapshot_at,
                    "captured_at": report.captured_at,
                    "market_file": market_relative.as_posix(),
                    "evidence_file": evidence_relative.as_posix(),
                    "market_sha256": report.market_sha256,
                    "evidence_sha256": sha256_file(evidence_path),
                    "provider_response_sha256": report.response_sha256,
                    "quote_count": report.quote_count,
                    "point_in_time_snapshot_contains_odds": report.has_data,
                }
            )

        result_relative = Path("match-results.json")
        result_evidence_relative = Path("match-results.evidence.json")
        result_path = staging / result_relative
        result_evidence_path = staging / result_evidence_relative
        capture_historical_matches(
            provider,
            requested_date=canonical_results_date,
            output_path=result_path,
            evidence_path=result_evidence_path,
            priced_only=results_priced_only,
        )

        # Re-resolve every child byte set at the bundle publication boundary.
        # Returned report digests are authority claims, not permission to trust a
        # pathname that may have been replaced after the child function returned.
        for index, entry in enumerate(snapshot_entries, start=1):
            market_path = staging / str(entry["market_file"])
            snapshot_evidence_path = staging / str(entry["evidence_file"])
            market_sha256 = _require_staged_digest(
                market_path,
                str(entry["market_sha256"]),
                field=f"snapshot[{index}].market",
            )
            evidence_sha256 = _require_staged_digest(
                snapshot_evidence_path,
                str(entry["evidence_sha256"]),
                field=f"snapshot[{index}].evidence",
            )
            snapshot_evidence = _read_strict_json_object(
                snapshot_evidence_path,
                field=f"snapshot[{index}].evidence",
            )
            if snapshot_evidence.get("market_sha256") != market_sha256:
                raise ProviderPayloadError(
                    f"snapshot[{index}].evidence market_sha256 does not bind staged market bytes"
                )
            entry["market_sha256"] = market_sha256
            entry["evidence_sha256"] = evidence_sha256

        result_evidence, result_evidence_sha256 = _read_strict_json_object_with_sha256(
            result_evidence_path,
            field="match_results.evidence",
        )
        result_semantics = _require_match_result_evidence_semantics(
            result_evidence,
            expected_sport_key=str(request_scope["sport_key"]),
            expected_date=canonical_results_date,
            expected_priced_only=results_priced_only,
            expected_request_url=results_request_url,
        )
        result_capture_sha256 = _require_staged_digest(
            result_path,
            str(result_semantics["capture_sha256"]),
            field="match_results.capture",
        )
        if result_evidence.get("capture_sha256") != result_capture_sha256:
            raise ProviderPayloadError(
                "match_results.evidence capture_sha256 does not bind staged capture bytes"
            )
        result_entry = {
            **result_semantics,
            "capture_file": result_relative.as_posix(),
            "evidence_file": result_evidence_relative.as_posix(),
            "capture_sha256": result_capture_sha256,
            "evidence_sha256": result_evidence_sha256,
            "coverage_preflight": coverage_evidence,
        }

        identity_payload = {
            "schema_version": 1,
            "kind": _BUNDLE_KIND,
            "request_identity": request_identity,
            "snapshots": snapshot_entries,
            "match_results": result_entry,
        }
        evidence_identity = _canonical_hash(identity_payload)
        bundle = {
            **identity_payload,
            "evidence_identity": evidence_identity,
            "request_scope": request_scope,
            "acquisition_scope": "selected_point_in_time_snapshots_plus_match_result_archive",
            "snapshot_count": len(snapshot_entries),
            "snapshots_with_odds": snapshots_with_odds,
            "all_requested_snapshots_returned_odds": snapshots_with_odds == len(snapshot_entries),
            "match_result_product_owned_request_path_verified": result_entry["product_owned_request_path_verified"],
            "match_result_product_owned_acquisition_clock_verified": result_entry["product_owned_acquisition_clock_verified"],
            "match_result_provider_response_origin_verified": result_entry["provider_response_origin_verified"],
            "trusted_outcome_source_admissible": result_entry["trusted_outcome_source_admissible"],
            "provider_result_schema_parsed": False,
            "sealed_quote_outcomes_derived": False,
            "point_in_time_odds_market_coverage_verified": False,
            "historical_window_market_coverage_verified": False,
            "licensing_or_retention_verified": False,
            "redistribution_verified": False,
            "replay_corpus_ready": False,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        bundle_path = staging / "bundle.json"
        atomic_write_json(bundle_path, bundle)
        staging.rename(output)
        final_bundle = output / "bundle.json"
        return HistoricalAcquisitionBundle(
            root=str(output),
            request_identity=request_identity,
            evidence_identity=evidence_identity,
            bundle_sha256=sha256_file(final_bundle),
            snapshot_count=len(snapshot_entries),
            snapshots_with_odds=snapshots_with_odds,
            result_capture_sha256=result_capture_sha256,
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-acquire-historical-evidence",
        description=(
            "Preflight authenticated historical coverage, then atomically capture selected odds snapshots plus "
            "opaque match/result evidence without promoting them to complete coverage or outcomes."
        ),
    )
    parser.add_argument(
        "--at",
        action="append",
        required=True,
        dest="requested_at",
        help="requested point-in-time snapshot timestamp; repeat for each selected instant",
    )
    parser.add_argument("--results-date", required=True, help="documented match/result archive date, YYYY-MM-DD")
    parser.add_argument(
        "--results-priced-only",
        action="store_true",
        help="request match rows with real odds where supported; this still does not prove market coverage",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".autosport-workspace/historical-acquisition"),
        help="new immutable acquisition-bundle directory",
    )
    parser.add_argument("--regions", default="us", help="comma-separated historical odds regions")
    parser.add_argument("--markets", default="h2h,spreads,totals", help="comma-separated historical odds markets")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("AUTOSPORT_PARLAYAPI_KEY")
    if not api_key:
        print("historical_acquisition=BLOCKED reason=AUTOSPORT_PARLAYAPI_KEY_not_set")
        return 2
    regions = tuple(value.strip() for value in args.regions.split(",") if value.strip())
    markets = tuple(value.strip() for value in args.markets.split(",") if value.strip())
    try:
        provider = ParlayApiTableTennisProvider(api_key, regions=regions, markets=markets)
        report = capture_historical_acquisition_bundle(
            provider,
            requested_at=args.requested_at,
            results_date=args.results_date,
            output_dir=args.output_dir,
            results_priced_only=args.results_priced_only,
        )
    except (ProviderTransportError, ProviderPayloadError, ValueError, OSError) as exc:
        print(f"historical_acquisition=FAIL_CLOSED error={exc}")
        return 3

    print(
        f"historical_acquisition=CAPTURED snapshots={report.snapshot_count} "
        f"snapshots_with_odds={report.snapshots_with_odds}"
    )
    print(f"request_identity={report.request_identity}")
    print(f"evidence_identity={report.evidence_identity}")
    print(f"bundle_sha256={report.bundle_sha256}")
    print(
        "provider_result_schema_parsed=false sealed_quote_outcomes_derived=false "
        "point_in_time_odds_market_coverage_verified=false "
        "historical_window_market_coverage_verified=false replay_corpus_ready=false"
    )
    print("match_result_provider_response_origin_verified=false trusted_outcome_source_admissible=false")
    print("licensing_or_retention_verified=false redistribution_verified=false real_money_execution=false")
    print(f"bundle={report.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
