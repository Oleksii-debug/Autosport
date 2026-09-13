from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TextIO

from .dataset import load_dataset
from .integrity import atomic_write_json


_SOURCE_ID = "betfair_exchange_historical"
_TABLE_TENNIS_EVENT_TYPE_ID = "2593174"
_ALLOWED_REDISTRIBUTION = {"prohibited", "internal_only", "permitted"}
_SUPPORTED_MARKET_TYPES = {"MATCH_ODDS": "winner"}
_SETTLED_OUTCOMES = {
    "WINNER": "win",
    "LOSER": "loss",
    "REMOVED": "void",
}


@dataclass(frozen=True, slots=True)
class BetfairHistoricalImportReport:
    root: str
    import_identity: str
    market_sha256: str
    results_sha256: str
    source_identity: str
    market_event_count: int
    quote_count: int
    settled_market_count: int


def _require_text(value: str, *, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} must be a non-empty string")
    return text


def _canonical_timestamp(value: str, *, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _timestamp_from_epoch_ms(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Betfair publish time pt must be epoch milliseconds")
    parsed = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _open_text(path: Path) -> TextIO:
    if path.suffix.lower() == ".bz2":
        return bz2.open(path, "rt", encoding="utf-8")
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def _iter_messages(paths: Iterable[Path]) -> Iterable[tuple[Path, int, dict[str, Any]]]:
    for path in paths:
        with _open_text(path) as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}: line {line_number} is not valid JSON") from exc
                if not isinstance(raw, dict):
                    raise ValueError(f"{path}: line {line_number} must be a JSON object")
                yield path, line_number, raw


def _market_type(value: Any, allowed: set[str]) -> str | None:
    market_type = str(value or "").strip().upper()
    if not market_type or market_type not in allowed:
        return None
    return _SUPPORTED_MARKET_TYPES[market_type]


def import_betfair_historical(
    inputs: Iterable[str | Path],
    output_dir: str | Path,
    *,
    acquired_at: str,
    terms_reference: str,
    retention_basis: str,
    redistribution_policy: str = "prohibited",
    dataset_name: str = "betfair-table-tennis-history",
    allowed_market_types: Iterable[str] = ("MATCH_ODDS",),
    imported_at: str | None = None,
) -> BetfairHistoricalImportReport:
    """Import user-supplied Betfair historical stream files into canonical schema-v2 history.

    This adapter never downloads Betfair data, never republishes source files, and never
    upgrades user-supplied rights metadata into a licensing/retention verification claim.
    It supports only explicitly mapped market types and requires settled runner statuses
    for every emitted quote so outcomes remain sealed and complete.
    """

    source_paths = tuple(Path(item) for item in inputs)
    if not source_paths:
        raise ValueError("at least one Betfair historical input file is required")
    for path in source_paths:
        if not path.is_file():
            raise ValueError(f"Betfair historical input does not exist: {path}")

    output = Path(output_dir)
    if output.exists():
        raise ValueError("output_dir already exists; historical imports never overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)

    acquired = _canonical_timestamp(acquired_at, field="acquired_at")
    imported = _canonical_timestamp(
        imported_at or datetime.now(timezone.utc).isoformat(),
        field="imported_at",
    )
    if _as_datetime(imported) < _as_datetime(acquired):
        raise ValueError("imported_at must not precede acquired_at")

    terms = _require_text(terms_reference, field="terms_reference")
    retention = _require_text(retention_basis, field="retention_basis")
    policy = str(redistribution_policy).strip()
    if policy not in _ALLOWED_REDISTRIBUTION:
        raise ValueError("redistribution_policy must be prohibited, internal_only, or permitted")
    name = _require_text(dataset_name, field="dataset_name")
    allowed = {str(value).strip().upper() for value in allowed_market_types if str(value).strip()}
    if not allowed:
        raise ValueError("at least one allowed Betfair market type is required")
    unsupported_market_types = sorted(allowed.difference(_SUPPORTED_MARKET_TYPES))
    if unsupported_market_types:
        raise ValueError(
            "unsupported Betfair market type(s): " + ",".join(unsupported_market_types)
        )

    source_hashes = tuple(_sha256_path(path) for path in source_paths)
    if len(set(source_hashes)) != len(source_hashes):
        raise ValueError("duplicate Betfair historical input content is not allowed")
    source_files = [
        {
            "ordinal": ordinal,
            "sha256": digest,
            "byte_size": path.stat().st_size,
        }
        for ordinal, (path, digest) in enumerate(zip(source_paths, source_hashes), start=1)
    ]
    source_identity = f"betfair-historical-files:{_canonical_hash(source_files)}"

    definitions: dict[str, dict[str, Any]] = {}
    runner_names: dict[str, dict[str, str]] = {}
    final_statuses: dict[str, dict[str, str]] = {}
    settlement_ts: dict[str, str] = {}
    last_publish_ts: dict[str, str] = {}
    market_source_path: dict[str, Path] = {}
    events: list[dict[str, Any]] = []
    source_event_ordinal = 0
    max_source_publish_ts: str | None = None

    for path, line_number, message in _iter_messages(source_paths):
        if message.get("op") != "mcm":
            continue
        observed = _timestamp_from_epoch_ms(message.get("pt"))
        if max_source_publish_ts is None or _as_datetime(observed) > _as_datetime(max_source_publish_ts):
            max_source_publish_ts = observed
        changes = message.get("mc")
        if not isinstance(changes, list):
            raise ValueError(f"{path}: line {line_number} mcm.mc must be a list")

        for change in changes:
            if not isinstance(change, dict):
                raise ValueError(f"{path}: line {line_number} market change must be an object")
            market_id = str(change.get("id") or "").strip()
            if not market_id:
                raise ValueError(f"{path}: line {line_number} market change id is required")

            previous_source = market_source_path.get(market_id)
            if previous_source is not None and previous_source != path:
                raise ValueError(
                    f"Betfair market {market_id} spans multiple input files; cross-file source order is ambiguous"
                )
            market_source_path[market_id] = path

            previous_ts = last_publish_ts.get(market_id)
            if previous_ts is not None and _as_datetime(observed) < _as_datetime(previous_ts):
                raise ValueError(f"{path}: line {line_number} market publish time moved backwards")
            last_publish_ts[market_id] = observed

            market_definition = change.get("marketDefinition")
            if market_definition is not None:
                if not isinstance(market_definition, dict):
                    raise ValueError(f"{path}: line {line_number} marketDefinition must be an object")
                prior = definitions.get(market_id, {})
                merged = {**prior, **market_definition}
                definitions[market_id] = merged
                names = runner_names.setdefault(market_id, {})
                runners = market_definition.get("runners")
                if isinstance(runners, list):
                    for runner in runners:
                        if not isinstance(runner, dict) or runner.get("id") is None:
                            continue
                        selection_id = str(runner["id"])
                        if runner.get("name") is not None:
                            names[selection_id] = str(runner["name"])

                closed = str(merged.get("status") or "").upper() == "CLOSED"
                if closed and isinstance(runners, list):
                    statuses: dict[str, str] = {}
                    for runner in runners:
                        if not isinstance(runner, dict) or runner.get("id") is None:
                            continue
                        status = str(runner.get("status") or "").upper()
                        if status in _SETTLED_OUTCOMES:
                            statuses[str(runner["id"])] = status
                    if statuses:
                        final_statuses[market_id] = statuses
                        settlement_ts[market_id] = observed

            definition = definitions.get(market_id)
            if definition is None:
                if change.get("rc"):
                    raise ValueError(
                        f"{path}: line {line_number} runner changes precede market definition"
                    )
                continue
            canonical_market_type = _market_type(definition.get("marketType"), allowed)
            if canonical_market_type is None:
                continue
            event_type_id = str(definition.get("eventTypeId") or "").strip()
            if event_type_id != _TABLE_TENNIS_EVENT_TYPE_ID:
                raise ValueError(
                    f"{path}: line {line_number} supported market is not Betfair Table Tennis "
                    f"eventTypeId={_TABLE_TENNIS_EVENT_TYPE_ID}"
                )

            market_status = str(definition.get("status") or "").upper()
            if market_status == "CLOSED":
                # Settlement facts stay outside strategy-visible market rows.
                continue
            if market_status != "OPEN":
                # SUSPENDED/unknown states are not rewritten as tradable/open history.
                continue

            runner_changes = change.get("rc")
            if runner_changes is None:
                continue
            if not isinstance(runner_changes, list):
                raise ValueError(f"{path}: line {line_number} rc must be a list")
            names = runner_names.get(market_id, {})
            event_id = str(definition.get("eventId") or "").strip()
            if not event_id:
                raise ValueError(
                    f"{path}: line {line_number} supported Betfair market requires source eventId"
                )
            event_name = str(definition.get("eventName") or "").strip()
            for runner_change in runner_changes:
                if not isinstance(runner_change, dict) or runner_change.get("id") is None:
                    raise ValueError(f"{path}: line {line_number} runner change requires id")
                if runner_change.get("ltp") is None:
                    continue
                odds = runner_change["ltp"]
                if isinstance(odds, bool) or not isinstance(odds, (int, float)) or float(odds) <= 1.0:
                    raise ValueError(f"{path}: line {line_number} ltp must be decimal odds > 1")
                selection_id = str(runner_change["id"])
                metadata = {
                    "provider": "betfair_exchange_historical",
                    "betfair_market_type": str(definition.get("marketType") or ""),
                }
                if event_name:
                    metadata["event_name"] = event_name
                selection_name = names.get(selection_id)
                if selection_name:
                    metadata["selection_name"] = selection_name
                source_event_ordinal += 1
                events.append(
                    {
                        "event_id": event_id,
                        "market_id": market_id,
                        "selection_id": selection_id,
                        "decimal_odds": str(odds),
                        "observed_ts": observed,
                        "source_id": _SOURCE_ID,
                        "market_type": canonical_market_type,
                        "status": "open",
                        "source_ts": observed,
                        "ingest_ts": imported,
                        "score_state": None,
                        "metadata": metadata,
                        "_source_event_ordinal": source_event_ordinal,
                    }
                )

    if not events:
        raise ValueError("Betfair inputs produced no supported historical market quotes")
    if max_source_publish_ts is None:
        raise ValueError("Betfair inputs contain no MarketChangeMessage publish timestamps")
    if _as_datetime(acquired) < _as_datetime(max_source_publish_ts):
        raise ValueError(
            "acquired_at must not precede the latest source publish time present in the supplied files"
        )

    semantic_keys: set[tuple[str, str, str, str]] = set()
    for event in events:
        semantic_key = (
            str(event["market_id"]),
            str(event["selection_id"]),
            str(event["observed_ts"]),
            str(event["decimal_odds"]),
        )
        if semantic_key in semantic_keys:
            raise ValueError("Betfair historical inputs contain duplicate market quote changes")
        semantic_keys.add(semantic_key)

    # Replay orders by observed_ts then sequence. Sequence therefore preserves the exact
    # provider source order for ties instead of introducing a price-based causal rewrite.
    events.sort(key=lambda item: (item["observed_ts"], item["_source_event_ordinal"]))
    for sequence, event in enumerate(events, start=1):
        event["sequence"] = sequence
        event.pop("_source_event_ordinal", None)

    observed_market_ids = {str(event["market_id"]) for event in events}
    missing_settlements = sorted(observed_market_ids.difference(settlement_ts))
    if missing_settlements:
        raise ValueError(
            "Betfair historical inputs lack final settlement for emitted markets: "
            + ",".join(missing_settlements)
        )

    outcomes: dict[str, str] = {}
    for event in events:
        market_id = str(event["market_id"])
        selection_id = str(event["selection_id"])
        statuses = final_statuses.get(market_id, {})
        status = statuses.get(selection_id)
        if status is None:
            raise ValueError(
                f"Betfair final settlement lacks emitted selection {selection_id} in market {market_id}"
            )
        quote_key = f"{event['event_id']}|{market_id}|{selection_id}"
        outcome = _SETTLED_OUTCOMES[status]
        previous = outcomes.get(quote_key)
        if previous is not None and previous != outcome:
            raise ValueError(f"Betfair settlement changed outcome for quote {quote_key}")
        outcomes[quote_key] = outcome

    coverage_start = min((str(event["observed_ts"]) for event in events), key=_as_datetime)
    coverage_end = max((str(event["observed_ts"]) for event in events), key=_as_datetime)
    outcome_reveal_after = max(
        (settlement_ts[market_id] for market_id in observed_market_ids), key=_as_datetime
    )
    if _as_datetime(outcome_reveal_after) < _as_datetime(coverage_end):
        raise ValueError("Betfair settlement boundary precedes emitted market coverage")
    if _as_datetime(imported) < _as_datetime(outcome_reveal_after):
        raise ValueError("imported_at must not precede the final settlement boundary")

    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.importing-", dir=str(output.parent)))
    try:
        market_path = staging / "market.jsonl"
        results_path = staging / "results.json"
        manifest_path = staging / "manifest.json"

        market_bytes = "".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for item in events
        ).encode("utf-8")
        market_path.write_bytes(market_bytes)
        results_payload = {
            "schema_version": 1,
            "outcome_reveal_after": outcome_reveal_after,
            "quote_outcomes": dict(sorted(outcomes.items())),
        }
        atomic_write_json(results_path, results_payload)
        market_sha256 = _sha256_bytes(market_bytes)
        results_sha256 = _sha256_path(results_path)

        governance = {
            "source_identity": source_identity,
            "source_files": source_files,
            "terms_reference": terms,
            "retention_basis": retention,
            "redistribution_policy": policy,
            "acquired_at": acquired,
            "imported_at": imported,
            "coverage": {
                "start_ts": coverage_start,
                "end_ts": coverage_end,
                "source_ids": [_SOURCE_ID],
                "market_types": ["winner"],
            },
            "causality": {
                "strategy_time_field": "observed_ts",
                "outcome_reveal_after": outcome_reveal_after,
            },
        }
        identity_payload = {
            "schema_version": 2,
            "name": name,
            "sport": "table_tennis",
            "market_file": market_path.name,
            "results_file": results_path.name,
            "market_sha256": market_sha256,
            "results_sha256": results_sha256,
            "governance": governance,
        }
        import_identity = _canonical_hash(identity_payload)
        manifest = {
            **identity_payload,
            "dataset_kind": "historical",
            "import_identity": import_identity,
        }
        atomic_write_json(manifest_path, manifest)

        verified = load_dataset(staging)
        if verified.import_identity != import_identity:
            raise ValueError("canonical dataset verification changed Betfair import identity")
        staging.rename(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return BetfairHistoricalImportReport(
        root=str(output),
        import_identity=import_identity,
        market_sha256=market_sha256,
        results_sha256=results_sha256,
        source_identity=source_identity,
        market_event_count=len(events),
        quote_count=len(outcomes),
        settled_market_count=len(observed_market_ids),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-import-betfair-historical",
        description=(
            "Import user-supplied Betfair Historical Data stream files into the canonical "
            "governed table-tennis dataset format. Source files are read locally and are not redistributed."
        ),
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="local Betfair .bz2/.gz/JSON-lines historical files")
    parser.add_argument("--output-dir", type=Path, required=True, help="new canonical dataset directory")
    parser.add_argument("--acquired-at", required=True, help="when these source files were lawfully acquired, ISO-8601")
    parser.add_argument("--terms-reference", required=True, help="terms/licence reference governing the local source files")
    parser.add_argument("--retention-basis", required=True, help="explicit basis permitting local retention/use")
    parser.add_argument(
        "--redistribution-policy",
        choices=sorted(_ALLOWED_REDISTRIBUTION),
        default="prohibited",
        help="raw/source redistribution policy; default is fail-closed prohibited",
    )
    parser.add_argument("--dataset-name", default="betfair-table-tennis-history")
    parser.add_argument(
        "--market-type",
        action="append",
        dest="market_types",
        choices=sorted(_SUPPORTED_MARKET_TYPES),
        help="supported Betfair marketType; repeat as needed (default MATCH_ODDS)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = import_betfair_historical(
            args.inputs,
            args.output_dir,
            acquired_at=args.acquired_at,
            terms_reference=args.terms_reference,
            retention_basis=args.retention_basis,
            redistribution_policy=args.redistribution_policy,
            dataset_name=args.dataset_name,
            allowed_market_types=args.market_types or ("MATCH_ODDS",),
        )
    except (OSError, ValueError) as exc:
        print(f"betfair_historical_import=FAIL_CLOSED error={exc}")
        return 3

    print(
        "betfair_historical_import=IMPORTED "
        f"events={report.market_event_count} quotes={report.quote_count} "
        f"settled_markets={report.settled_market_count}"
    )
    print(f"historical_import_identity={report.import_identity}")
    print(f"market_sha256={report.market_sha256}")
    print(f"sealed_results_sha256={report.results_sha256}")
    print(f"source_identity={report.source_identity}")
    print("licensing_retention_verified=false redistribution_verified=false")
    print("real_money_execution=false human_tested=false nvda_verified=false")
    print(f"dataset={report.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
