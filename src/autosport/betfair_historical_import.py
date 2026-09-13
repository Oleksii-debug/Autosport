from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Iterable

from . import _betfair_historical_import_base as _base
from .dataset import load_dataset
from .integrity import atomic_write_json


BetfairHistoricalImportReport = _base.BetfairHistoricalImportReport
_PRICE_SEMANTICS = "last_traded_price"


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _replay_visible_price_timestamps(
    source_paths: tuple[Path, ...],
    allowed_market_types: set[str],
) -> dict[str, set[Path]]:
    """Return source files contributing strategy-visible LTP rows at each provider timestamp."""
    definitions: dict[str, dict[str, Any]] = {}
    timestamp_sources: dict[str, set[Path]] = {}

    for path, _line_number, message in _base._iter_messages(source_paths):
        if message.get("op") != "mcm":
            continue
        observed = _base._timestamp_from_epoch_ms(message.get("pt"))
        changes = message.get("mc")
        if not isinstance(changes, list):
            # Canonical base importer owns the detailed shape error.
            continue

        for change in changes:
            if not isinstance(change, dict):
                continue
            market_id = str(change.get("id") or "").strip()
            if not market_id:
                continue
            market_definition = change.get("marketDefinition")
            if isinstance(market_definition, dict):
                definitions[market_id] = {
                    **definitions.get(market_id, {}),
                    **market_definition,
                }
            definition = definitions.get(market_id)
            if not definition:
                continue
            if _base._market_type(definition.get("marketType"), allowed_market_types) is None:
                continue
            if str(definition.get("eventTypeId") or "").strip() != _base._TABLE_TENNIS_EVENT_TYPE_ID:
                continue
            if str(definition.get("status") or "").upper() != "OPEN":
                continue
            runner_changes = change.get("rc")
            if not isinstance(runner_changes, list):
                continue
            if not any(
                isinstance(runner_change, dict) and runner_change.get("ltp") is not None
                for runner_change in runner_changes
            ):
                continue
            timestamp_sources.setdefault(observed, set()).add(path.resolve())

    return timestamp_sources


def _reject_ambiguous_cross_file_ties(
    source_paths: tuple[Path, ...],
    allowed_market_types: set[str],
) -> None:
    timestamp_sources = _replay_visible_price_timestamps(source_paths, allowed_market_types)
    ambiguous = sorted(
        timestamp
        for timestamp, sources in timestamp_sources.items()
        if len(sources) > 1
    )
    if ambiguous:
        raise ValueError(
            "Betfair historical inputs contain equal replay-visible publish time across source files; "
            "cross-file source order is ambiguous: " + ",".join(ambiguous)
        )


def _bind_price_truth(root: Path) -> tuple[str, str, str]:
    """Bind non-execution LTP semantics into market rows and immutable import identity."""
    market_path = root / "market.jsonl"
    results_path = root / "results.json"
    manifest_path = root / "manifest.json"

    rewritten: list[dict[str, Any]] = []
    with market_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            raw = json.loads(line)
            metadata = dict(raw.get("metadata", {}))
            metadata.update(
                {
                    "price_semantics": _PRICE_SEMANTICS,
                    "execution_quote_verified": False,
                    "market_availability_history_complete_verified": False,
                }
            )
            raw["metadata"] = metadata
            rewritten.append(raw)

    market_bytes = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for item in rewritten
    ).encode("utf-8")
    market_path.write_bytes(market_bytes)
    market_sha256 = _sha256_bytes(market_bytes)
    results_sha256 = hashlib.sha256(results_path.read_bytes()).hexdigest()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    governance = dict(manifest["governance"])
    governance["price_evidence"] = {
        "price_semantics": _PRICE_SEMANTICS,
        "execution_quote_verified": False,
        "market_availability_history_complete_verified": False,
    }
    identity_payload = {
        "schema_version": int(manifest["schema_version"]),
        "name": str(manifest["name"]),
        "sport": str(manifest["sport"]),
        "market_file": str(manifest["market_file"]),
        "results_file": str(manifest["results_file"]),
        "market_sha256": market_sha256,
        "results_sha256": results_sha256,
        "governance": governance,
    }
    import_identity = _canonical_hash(identity_payload)
    bound_manifest = {
        **identity_payload,
        "dataset_kind": str(manifest.get("dataset_kind", "historical")),
        "import_identity": import_identity,
    }
    atomic_write_json(manifest_path, bound_manifest)

    verified = load_dataset(root)
    if verified.import_identity != import_identity:
        raise ValueError("canonical dataset verification changed Betfair price-truth import identity")
    loaded_events = verified.load_market_events()
    if not loaded_events:
        raise ValueError("Betfair price-truth binding produced no market events")
    for event in loaded_events:
        if event.metadata.get("price_semantics") != _PRICE_SEMANTICS:
            raise ValueError("Betfair LTP price semantics were not preserved by canonical serialization")
        if event.metadata.get("execution_quote_verified") is not False:
            raise ValueError("Betfair LTP must remain explicitly non-verified as an executable quote")
        if event.metadata.get("market_availability_history_complete_verified") is not False:
            raise ValueError("Betfair skipped market states must not imply complete availability history")

    return import_identity, market_sha256, results_sha256


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
    """Canonical Betfair importer with fail-closed causal and LTP evidence boundaries."""
    source_paths = tuple(Path(item) for item in inputs)
    market_type_values = tuple(str(value).strip().upper() for value in allowed_market_types if str(value).strip())
    allowed = set(market_type_values)
    if source_paths and allowed:
        _reject_ambiguous_cross_file_ties(source_paths, allowed)

    output = Path(output_dir)
    if output.exists():
        raise ValueError("output_dir already exists; historical imports never overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)
    wrapped_output = output.with_name(f".{output.name}.price-truth-{uuid.uuid4().hex}")

    try:
        base_report = _base.import_betfair_historical(
            source_paths,
            wrapped_output,
            acquired_at=acquired_at,
            terms_reference=terms_reference,
            retention_basis=retention_basis,
            redistribution_policy=redistribution_policy,
            dataset_name=dataset_name,
            allowed_market_types=market_type_values,
            imported_at=imported_at,
        )
        import_identity, market_sha256, results_sha256 = _bind_price_truth(wrapped_output)
        wrapped_output.rename(output)
    except Exception:
        shutil.rmtree(wrapped_output, ignore_errors=True)
        raise

    return BetfairHistoricalImportReport(
        root=str(output),
        import_identity=import_identity,
        market_sha256=market_sha256,
        results_sha256=results_sha256,
        source_identity=base_report.source_identity,
        market_event_count=base_report.market_event_count,
        quote_count=base_report.quote_count,
        settled_market_count=base_report.settled_market_count,
    )


def build_parser():
    return _base.build_parser()


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
    print(
        "price_semantics=last_traded_price execution_quote_verified=false "
        "market_availability_history_complete_verified=false"
    )
    print("licensing_retention_verified=false redistribution_verified=false")
    print("real_money_execution=false human_tested=false nvda_verified=false")
    print(f"dataset={report.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
