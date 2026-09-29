from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .integrity import atomic_write_json
from .parlayapi_provider import (
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
    ProviderTransportError,
)

TERMS_REFERENCE = "https://parlay-api.com/terms"
REQUEST_CONTRACT_REFERENCE = "https://api.parlay-api.com/docs"
_CAPTURE_PUBLISH_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class HistoricalMatchCapture:
    requested_date: str
    priced_only: bool
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
    requested_date: str,
    output_path: str | Path,
    evidence_path: str | Path | None = None,
    priced_only: bool = False,
) -> HistoricalMatchCapture:
    """Capture provider historical match/result evidence through the documented API surface.

    ParlayAPI documents ``/v1/historical/sports/{sport_key}/matches`` with one
    required ``date=YYYY-MM-DD`` query parameter and optional ``pricedOnly``.
    The result archive is intentionally kept opaque here: this capture proves a
    response identity and runtime entitlement metadata, not quote outcomes,
    historical market coverage, retention rights, or replay-corpus readiness.
    """

    if provider.public_preview or not provider.api_key:
        raise ValueError("historical match capture requires an authenticated API key")
    requested = _parse_date(requested_date, field="requested_date")
    if not isinstance(priced_only, bool):
        raise ValueError("priced_only must be boolean")

    output = Path(output_path)
    evidence = Path(evidence_path) if evidence_path is not None else output.with_suffix(output.suffix + ".evidence.json")
    if _paths_alias(output, evidence):
        raise ValueError("output_path and evidence_path must refer to different files")

    query_values = {
        "date": requested_date,
        "pricedOnly": "true" if priced_only else "false",
    }
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
    if requested < entitlement_from:
        raise ProviderPayloadError("historical matches response contradicts its entitlement-window header")

    payload = response.payload
    if not isinstance(payload, (dict, list)):
        raise ProviderPayloadError("historical matches response must be a JSON object or array")
    try:
        canonical_response = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        canonical_response_bytes = canonical_response.encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProviderPayloadError("historical matches response must contain strict UTF-8 JSON values") from exc
    canonical_response_sha256 = hashlib.sha256(canonical_response_bytes).hexdigest()

    capture_payload = {
        "schema_version": 1,
        "kind": "parlayapi_historical_match_result_capture",
        "provider": "parlayapi",
        "sport_key": provider.sport_key,
        "request": {
            "date": requested_date,
            "priced_only": priced_only,
        },
        "request_contract_reference": REQUEST_CONTRACT_REFERENCE,
        "captured_at": captured_at,
        "canonical_response_sha256": canonical_response_sha256,
        "payload": payload,
    }
    capture_sha256 = _atomic_write_capture_json(output, capture_payload)

    evidence_payload = {
        "schema_version": 1,
        "kind": "parlayapi_historical_match_result_evidence",
        "provider": "parlayapi",
        "sport_key": provider.sport_key,
        "requested_date": requested_date,
        "priced_only": priced_only,
        "request_contract_reference": REQUEST_CONTRACT_REFERENCE,
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
        requested_date=requested_date,
        priced_only=priced_only,
        captured_at=captured_at,
        canonical_response_sha256=canonical_response_sha256,
        capture_sha256=capture_sha256,
        historical_window_hours=window_hours,
        historical_window_from=window_from_raw,
        output_path=str(output),
        evidence_path=str(evidence),
    )


def _atomic_write_capture_json(path: str | Path, payload: dict[str, Any]) -> str:
    """Publish one deterministic JSON byte snapshot and return that snapshot's SHA-256."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        with _CAPTURE_PUBLISH_LOCK:
            os.replace(temporary, destination)
        return digest
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _paths_alias(first: Path, second: Path) -> bool:
    if first.resolve(strict=False) == second.resolve(strict=False):
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _header(headers: Any, name: str) -> str | None:
    expected = name.lower()
    for key, value in headers.items():
        if str(key).lower() == expected:
            text = str(value).strip()
            return text or None
    return None


def _parse_date(value: str, *, field: str) -> date:
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return parsed


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
    parser.add_argument("--date", required=True, help="provider game date, YYYY-MM-DD")
    parser.add_argument(
        "--priced-only",
        action="store_true",
        help="request only match rows that include real odds; this still does not prove historical market coverage",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".autosport-workspace/historical-matches.json"),
        help="opaque provider response capture",
    )
    parser.add_argument("--evidence", type=Path, default=None, help="optional machine evidence JSON path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("AUTOSPORT_PARLAYAPI_KEY")
    if not api_key:
        print("historical_matches=BLOCKED reason=AUTOSPORT_PARLAYAPI_KEY_not_set")
        return 2
    try:
        provider = ParlayApiTableTennisProvider(api_key)
        report = capture_historical_matches(
            provider,
            requested_date=args.date,
            output_path=args.output,
            evidence_path=args.evidence,
            priced_only=args.priced_only,
        )
    except (ProviderTransportError, ProviderPayloadError, ValueError, OSError) as exc:
        print(f"historical_matches=FAIL_CLOSED error={exc}")
        return 3

    print(
        f"historical_matches=CAPTURED date={report.requested_date} "
        f"priced_only={str(report.priced_only).lower()} window_hours={report.historical_window_hours}"
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
