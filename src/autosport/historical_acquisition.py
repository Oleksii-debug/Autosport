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

from .historical_matches import capture_historical_matches
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

    request_scope = {
        "provider": "parlayapi",
        "sport_key": provider.sport_key,
        "regions": list(provider.regions),
        "markets": list(provider.markets),
        "requested_snapshot_timestamps": list(canonical_requests),
        "coverage_preflight": coverage_request,
        "match_results": {
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
        result_report = capture_historical_matches(
            provider,
            requested_date=canonical_results_date,
            output_path=result_path,
            evidence_path=result_evidence_path,
            priced_only=results_priced_only,
        )
        result_entry = {
            "requested_date": result_report.requested_date,
            "priced_only": result_report.priced_only,
            "captured_at": result_report.captured_at,
            "capture_file": result_relative.as_posix(),
            "evidence_file": result_evidence_relative.as_posix(),
            "capture_sha256": result_report.capture_sha256,
            "evidence_sha256": sha256_file(result_evidence_path),
            "canonical_response_sha256": result_report.canonical_response_sha256,
            "historical_window_hours": result_report.historical_window_hours,
            "historical_window_from": result_report.historical_window_from,
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
            result_capture_sha256=result_report.capture_sha256,
        )
    except BaseException:
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
    print("licensing_or_retention_verified=false redistribution_verified=false real_money_execution=false")
    print(f"bundle={report.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
