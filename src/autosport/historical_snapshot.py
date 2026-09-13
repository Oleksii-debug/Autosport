from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

from .domain import MarketEvent
from .integrity import atomic_write_json
from .parlayapi_provider import (
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
    ProviderTransportError,
)
from .providers import CanonicalNormalizer, ProviderQuote


TERMS_REFERENCE = "https://parlay-api.com/terms"


@dataclass(frozen=True, slots=True)
class HistoricalSnapshotCapture:
    requested_at: str
    snapshot_at: str
    captured_at: str
    previous_snapshot_at: str | None
    next_snapshot_at: str | None
    response_sha256: str
    market_sha256: str
    quote_count: int
    snapshot_timestamp_fallback_count: int
    market_types: tuple[str, ...]
    provider_market_keys: tuple[str, ...]
    bookmaker_keys: tuple[str, ...]
    output_path: str
    evidence_path: str

    @property
    def has_data(self) -> bool:
        return self.quote_count > 0


def capture_historical_snapshot(
    provider: ParlayApiTableTennisProvider,
    *,
    requested_at: str,
    output_path: str | Path,
    evidence_path: str | Path | None = None,
) -> HistoricalSnapshotCapture:
    """Capture one authenticated point-in-time table-tennis odds snapshot.

    The provider's historical response timestamp is the causal snapshot boundary.
    Per-quote ``last_update`` is preferred when present. When the provider omits it,
    the provider snapshot timestamp is used explicitly as a *snapshot-boundary*
    source time and the row metadata records that weaker semantic. No wall-clock
    timestamp is substituted for historical market time.

    This creates canonical market rows plus machine evidence only. It deliberately
    does not fabricate results, settlement outcomes, licensing proof, requested-
    market completeness, historical-window completeness, or a replay-ready schema-v2
    dataset manifest.
    """

    if provider.public_preview or not provider.api_key:
        raise ValueError("historical snapshot capture requires an authenticated API key")

    requested_dt = _parse_timestamp(requested_at, field="requested_at")
    query = urlencode(
        {
            "date": requested_at,
            "regions": ",".join(provider.regions),
            "markets": ",".join(provider.markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
    )
    url = f"{provider.base_url}/v1/historical/sports/{provider.sport_key}/odds?{query}"
    response = provider._request(url)  # package-internal transport preserves secret/header policy and retries
    captured_at = provider.clock()
    captured_dt = _parse_timestamp(captured_at, field="captured_at")

    payload = response.payload
    if not isinstance(payload, dict):
        raise ProviderPayloadError("historical odds response must be an object")
    snapshot_at = _required_text(payload, "timestamp")
    snapshot_dt = _parse_timestamp(snapshot_at, field="historical response timestamp")
    if snapshot_dt > requested_dt:
        raise ProviderPayloadError("historical snapshot timestamp is after requested_at")
    if captured_dt < snapshot_dt:
        raise ProviderPayloadError("capture clock is before historical snapshot timestamp")

    data = payload.get("data")
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ProviderPayloadError("historical odds response requires data[] event objects")

    previous_snapshot_at = _optional_timestamp(payload.get("previous_timestamp"), "previous_timestamp")
    next_snapshot_at = _optional_timestamp(payload.get("next_timestamp"), "next_timestamp")
    if previous_snapshot_at is not None and _parse_timestamp(previous_snapshot_at, field="previous_timestamp") > snapshot_dt:
        raise ProviderPayloadError("previous_timestamp is after snapshot timestamp")
    if next_snapshot_at is not None and _parse_timestamp(next_snapshot_at, field="next_timestamp") < snapshot_dt:
        raise ProviderPayloadError("next_timestamp is before snapshot timestamp")

    normalizer = CanonicalNormalizer()
    events: list[MarketEvent] = []
    fallback_count = 0
    provider_market_keys: set[str] = set()
    bookmaker_keys: set[str] = set()
    dedupe_keys: set[str] = set()

    for raw_event in data:
        quotes = provider._event_quotes(raw_event, snapshot_at, response.status_code)
        for quote in quotes:
            causal_quote, used_fallback = _bind_quote_to_snapshot(quote, snapshot_at, snapshot_dt)
            if used_fallback:
                fallback_count += 1
            bookmaker = str(causal_quote.metadata.get("bookmaker_key") or "").strip()
            if bookmaker:
                bookmaker_keys.add(bookmaker)
            provider_market_key = str(causal_quote.metadata.get("market_key") or "").strip()
            if provider_market_key:
                provider_market_keys.add(provider_market_key)
            event = normalizer.normalize(provider.source_id, causal_quote)
            event = replace(event, ingest_ts=captured_at)
            if event.dedupe_key in dedupe_keys:
                raise ProviderPayloadError("historical snapshot contains duplicate canonical quote identity")
            dedupe_keys.add(event.dedupe_key)
            events.append(event)

    events.sort(key=lambda item: (item.observed_ts, item.sequence, item.event_id, item.market_id, item.selection_id))
    output = Path(output_path)
    evidence = Path(evidence_path) if evidence_path is not None else output.with_suffix(output.suffix + ".evidence.json")
    _atomic_write_jsonl(output, (event.to_dict() for event in events))
    market_sha256 = _sha256(output)
    canonical_response = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    response_sha256 = hashlib.sha256(canonical_response.encode("utf-8")).hexdigest()
    market_types = tuple(sorted({event.market_type.value for event in events}))

    evidence_payload = {
        "schema_version": 1,
        "kind": "parlayapi_point_in_time_historical_snapshot",
        "provider": "parlayapi",
        "sport_key": provider.sport_key,
        "requested_at": requested_at,
        "snapshot_at": snapshot_at,
        "captured_at": captured_at,
        "previous_snapshot_at": previous_snapshot_at,
        "next_snapshot_at": next_snapshot_at,
        "response_sha256": response_sha256,
        "market_sha256": market_sha256,
        "quote_count": len(events),
        "has_data": bool(events),
        "requested_regions": list(provider.regions),
        "requested_markets": list(provider.markets),
        "observed_market_types": list(market_types),
        "observed_provider_market_keys": sorted(provider_market_keys),
        "bookmaker_keys": sorted(bookmaker_keys),
        "snapshot_timestamp_fallback_count": fallback_count,
        "source_time_semantics": {
            "preferred": "provider_quote_last_update",
            "fallback": "provider_historical_snapshot_timestamp",
            "wall_clock_used_as_historical_market_time": False,
        },
        "point_in_time_snapshot_data_observed": bool(events),
        "requested_market_set_complete_verified": False,
        "historical_window_coverage_verified": False,
        "sealed_outcomes_present": False,
        "replay_corpus_ready": False,
        "terms_reference": TERMS_REFERENCE,
        "licensing_or_retention_verified": False,
        "redistribution_verified": False,
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }
    atomic_write_json(evidence, evidence_payload)

    return HistoricalSnapshotCapture(
        requested_at=requested_at,
        snapshot_at=snapshot_at,
        captured_at=captured_at,
        previous_snapshot_at=previous_snapshot_at,
        next_snapshot_at=next_snapshot_at,
        response_sha256=response_sha256,
        market_sha256=market_sha256,
        quote_count=len(events),
        snapshot_timestamp_fallback_count=fallback_count,
        market_types=market_types,
        provider_market_keys=tuple(sorted(provider_market_keys)),
        bookmaker_keys=tuple(sorted(bookmaker_keys)),
        output_path=str(output),
        evidence_path=str(evidence),
    )


def _bind_quote_to_snapshot(
    quote: ProviderQuote,
    snapshot_at: str,
    snapshot_dt: datetime,
) -> tuple[ProviderQuote, bool]:
    metadata = dict(quote.metadata)
    if quote.source_ts is None:
        metadata["source_time_semantics"] = "provider_historical_snapshot_timestamp"
        metadata["provider_quote_last_update_present"] = False
        return replace(quote, source_ts=snapshot_at, metadata=metadata), True

    quote_dt = _parse_timestamp(quote.source_ts, field="provider quote last_update")
    if quote_dt > snapshot_dt:
        raise ProviderPayloadError("provider quote last_update is after historical snapshot timestamp")
    metadata["source_time_semantics"] = "provider_quote_last_update"
    metadata["provider_quote_last_update_present"] = True
    return replace(quote, metadata=metadata), False


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProviderPayloadError(f"historical odds response requires non-empty {field}")
    return value.strip()


def _optional_timestamp(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProviderPayloadError(f"{field} must be null or a non-empty timestamp")
    parsed = value.strip()
    _parse_timestamp(parsed, field=field)
    return parsed


def _parse_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ProviderPayloadError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderPayloadError(f"{field} must include an explicit timezone")
    return parsed


def _atomic_write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m autosport.historical_snapshot",
        description="Capture one authenticated point-in-time table-tennis historical odds snapshot.",
    )
    parser.add_argument("--at", required=True, help="requested historical snapshot timestamp (ISO-8601 with timezone)")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".autosport-workspace/historical-snapshot.jsonl"),
        help="canonical market-event JSONL output",
    )
    parser.add_argument("--evidence", type=Path, default=None, help="optional machine evidence JSON path")
    parser.add_argument("--regions", default="us", help="comma-separated provider regions")
    parser.add_argument("--markets", default="h2h,spreads,totals", help="comma-separated historical game-line markets")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("AUTOSPORT_PARLAYAPI_KEY")
    if not api_key:
        print("historical_snapshot=BLOCKED reason=AUTOSPORT_PARLAYAPI_KEY_not_set")
        return 2
    regions = tuple(value.strip() for value in args.regions.split(",") if value.strip())
    markets = tuple(value.strip() for value in args.markets.split(",") if value.strip())
    try:
        provider = ParlayApiTableTennisProvider(api_key, regions=regions, markets=markets)
        report = capture_historical_snapshot(
            provider,
            requested_at=args.at,
            output_path=args.output,
            evidence_path=args.evidence,
        )
    except (ProviderTransportError, ProviderPayloadError, ValueError, OSError) as exc:
        print(f"historical_snapshot=FAIL_CLOSED error={exc}")
        return 3

    status = "DATA_AVAILABLE" if report.has_data else "NO_DATA"
    print(
        f"historical_snapshot={status} quotes={report.quote_count} "
        f"snapshot_at={report.snapshot_at} fallback_source_times={report.snapshot_timestamp_fallback_count}"
    )
    print(
        "point_in_time_snapshot_data_observed=" + str(report.has_data).lower()
        + " requested_market_set_complete_verified=false historical_window_coverage_verified=false"
        + " sealed_outcomes_present=false replay_corpus_ready=false"
    )
    print("licensing_or_retention_verified=false redistribution_verified=false real_money_execution=false")
    print(f"market={report.output_path}")
    print(f"evidence={report.evidence_path}")
    return 0 if report.has_data else 6


if __name__ == "__main__":
    raise SystemExit(main())
