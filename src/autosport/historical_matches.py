from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .integrity import atomic_write_json, sha256_file
from .parlayapi_provider import (
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
    ProviderTransportError,
)

TERMS_REFERENCE = "https://parlay-api.com/terms"


@dataclass(frozen=True, slots=True)
class HistoricalMatchCapture:
    date_from: str
    date_to: str
    captured_at: str
    canonical_response_sha256: str
    capture_sha256: str
    historical_window_hours: int
    historical_window_from: str
    output_path: str
    evidence_path: str


def capture_historical_matches(
    provider: ParlayApiTableTennisProvider,
    *,
    date_from: str,
    date_to: str,
    output_path: str | Path,
    evidence_path: str | Path | None = None,
    sources: tuple[str, ...] = (),
    limit: int = 1000,
) -> HistoricalMatchCapture:
    """Capture provider historical match/result evidence without guessing its row schema.

    The public ParlayAPI contract identifies ``/matches`` as the schedules/scores/results
    archive and explicitly separates result-only rows from historical price evidence.
    The OpenAPI response schema is currently untyped, so this function deliberately
    preserves the provider JSON as an opaque payload. It does *not* derive quote
    outcomes, market coverage, licensing rights, or a replay-ready corpus.
    """

    if provider.public_preview or not provider.api_key:
        raise ValueError("historical match capture requires an authenticated API key")
    start = _parse_date(date_from, field="date_from")
    end = _parse_date(date_to, field="date_to")
    if end < start:
        raise ValueError("date_to must not precede date_from")
    if limit < 1 or limit > 5000:
        raise ValueError("limit must be between 1 and 5000")
    normalized_sources = tuple(sorted({value.strip() for value in sources if value.strip()}))

    query_values: dict[str, Any] = {
        "dateFrom": date_from,
        "dateTo": date_to,
        "pricedOnly": "false",
        "includeRaw": "false",
        "limit": str(limit),
    }
    if normalized_sources:
        query_values["sources"] = ",".join(normalized_sources)
    url = (
        f"{provider.base_url}/v1/historical/sports/{provider.sport_key}/matches?"
        + urlencode(query_values)
    )
    response = provider._request(url)
    captured_at = provider.clock()
    _parse_timestamp(captured_at, field="captured_at")

    window_hours_raw = _header(response.headers, "x-historical-window-hours")
    window_from_raw = _header(response.headers, "x-historical-window-from")
    if window_hours_raw is None or window_from_raw is None:
        raise ProviderPayloadError("historical matches response is missing entitlement-window headers")
    try:
        window_hours = int(window_hours_raw)
    except ValueError as exc:
        raise ProviderPayloadError("x-historical-window-hours must be an integer") from exc
    if window_hours <= 0:
        raise ProviderPayloadError("x-historical-window-hours must be positive")
    entitlement_from = _parse_provider_date(window_from_raw, field="x-historical-window-from")
    if start < entitlement_from:
        raise ProviderPayloadError("historical matches response contradicts its entitlement-window header")

    payload = response.payload
    if not isinstance(payload, (dict, list)):
        raise ProviderPayloadError("historical matches response must be a JSON object or array")
    canonical_response = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    canonical_response_sha256 = hashlib.sha256(canonical_response.encode("utf-8")).hexdigest()

    output = Path(output_path)
    evidence = Path(evidence_path) if evidence_path is not None else output.with_suffix(output.suffix + ".evidence.json")
    capture_payload = {
        "schema_version": 1,
        "kind": "parlayapi_historical_match_result_capture",
        "provider": "parlayapi",
        "sport_key": provider.sport_key,
        "request": {
            "date_from": date_from,
            "date_to": date_to,
            "sources": list(normalized_sources),
            "priced_only": False,
            "include_raw": False,
            "limit": limit,
        },
        "captured_at": captured_at,
        "canonical_response_sha256": canonical_response_sha256,
        "payload": payload,
    }
    atomic_write_json(output, capture_payload)
    capture_sha256 = sha256_file(output)

    evidence_payload = {
        "schema_version": 1,
        "kind": "parlayapi_historical_match_result_evidence",
        "provider": "parlayapi",
        "sport_key": provider.sport_key,
        "date_from": date_from,
        "date_to": date_to,
        "sources": list(normalized_sources),
        "captured_at": captured_at,
        "canonical_response_sha256": canonical_response_sha256,
        "capture_sha256": capture_sha256,
        "historical_window_hours": window_hours,
        "historical_window_from": window_from_raw,
        "api_version": _header(response.headers, "x-api-version"),
        "coverage_hint": _header(response.headers, "x-coverage-hint"),
        "provider_result_schema_parsed": False,
        "sealed_quote_outcomes_derived": False,
        "point_in_time_odds_market_coverage_verified": False,
        "historical_window_market_coverage_verified": False,
        "replay_corpus_ready": False,
        "terms_reference": TERMS_REFERENCE,
        "licensing_or_retention_verified": False,
        "redistribution_verified": False,
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }
    atomic_write_json(evidence, evidence_payload)

    return HistoricalMatchCapture(
        date_from=date_from,
        date_to=date_to,
        captured_at=captured_at,
        canonical_response_sha256=canonical_response_sha256,
        capture_sha256=capture_sha256,
        historical_window_hours=window_hours,
        historical_window_from=window_from_raw,
        output_path=str(output),
        evidence_path=str(evidence),
    )


def _header(headers: Any, name: str) -> str | None:
    expected = name.lower()
    for key, value in headers.items():
        if str(key).lower() == expected:
            text = str(value).strip()
            return text or None
    return None


def _parse_date(value: str, *, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _parse_provider_date(value: str, *, field: str) -> date:
    text = value.strip()
    try:
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError) as exc:
        raise ProviderPayloadError(f"{field} must start with an ISO date") from exc


def _parse_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ProviderPayloadError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderPayloadError(f"{field} must include an explicit timezone")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-capture-historical-matches",
        description="Capture authenticated table-tennis match/result archive evidence without deriving settlement outcomes.",
    )
    parser.add_argument("--from", dest="date_from", required=True, help="first provider date, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", required=True, help="last provider date, YYYY-MM-DD")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".autosport-workspace/historical-matches.json"),
        help="normalized provider response capture",
    )
    parser.add_argument("--evidence", type=Path, default=None, help="optional machine evidence JSON path")
    parser.add_argument("--sources", default="", help="optional comma-separated provider source filter")
    parser.add_argument("--limit", type=int, default=1000, help="provider row limit, 1..5000")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("AUTOSPORT_PARLAYAPI_KEY")
    if not api_key:
        print("historical_matches=BLOCKED reason=AUTOSPORT_PARLAYAPI_KEY_not_set")
        return 2
    sources = tuple(value.strip() for value in args.sources.split(",") if value.strip())
    try:
        provider = ParlayApiTableTennisProvider(api_key)
        report = capture_historical_matches(
            provider,
            date_from=args.date_from,
            date_to=args.date_to,
            output_path=args.output,
            evidence_path=args.evidence,
            sources=sources,
            limit=args.limit,
        )
    except (ProviderTransportError, ProviderPayloadError, ValueError, OSError) as exc:
        print(f"historical_matches=FAIL_CLOSED error={exc}")
        return 3

    print(
        f"historical_matches=CAPTURED range={report.date_from}..{report.date_to} "
        f"window_hours={report.historical_window_hours}"
    )
    print(
        "provider_result_schema_parsed=false sealed_quote_outcomes_derived=false "
        "point_in_time_odds_market_coverage_verified=false "
        "historical_window_market_coverage_verified=false replay_corpus_ready=false"
    )
    print("licensing_or_retention_verified=false real_money_execution=false")
    print(f"capture={report.output_path}")
    print(f"evidence={report.evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
