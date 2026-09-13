from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import json
import math
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, TextIO

from .betfair_available_back import BetfairAvailableBackBook, AvailableBackQuote
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
_CLASSIC_PRICE_BANDS = (
    (Decimal("1.01"), Decimal("2"), Decimal("0.01")),
    (Decimal("2"), Decimal("3"), Decimal("0.02")),
    (Decimal("3"), Decimal("4"), Decimal("0.05")),
    (Decimal("4"), Decimal("6"), Decimal("0.1")),
    (Decimal("6"), Decimal("10"), Decimal("0.2")),
    (Decimal("10"), Decimal("20"), Decimal("0.5")),
    (Decimal("20"), Decimal("30"), Decimal("1")),
    (Decimal("30"), Decimal("50"), Decimal("2")),
    (Decimal("50"), Decimal("100"), Decimal("5")),
    (Decimal("100"), Decimal("1000"), Decimal("10")),
)


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
    if not math.isfinite(float(value)):
        raise ValueError("Betfair publish time pt must be finite epoch milliseconds")
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


def _validated_ltp(value: Any, *, path: Path, line_number: int) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 1.0
    ):
        raise ValueError(f"{path}: line {line_number} ltp must be finite decimal odds > 1")
    return float(value)


def _bet_delay_seconds(definition: dict[str, Any], *, path: Path, line_number: int) -> int | None:
    raw = definition.get("betDelay")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
        raise ValueError(f"{path}: line {line_number} marketDefinition betDelay must be a finite non-negative integer")
    value = float(raw)
    if value < 0 or not value.is_integer():
        raise ValueError(f"{path}: line {line_number} marketDefinition betDelay must be a finite non-negative integer")
    return int(value)


def _execution_price_ladder_type(
    definition: dict[str, Any], *, path: Path, line_number: int
) -> str:
    raw = definition.get("priceLadderDefinition")
    if isinstance(raw, dict):
        raw = raw.get("type")
    ladder_type = str(raw or "").strip().upper()
    if not ladder_type:
        raise ValueError(
            f"{path}: line {line_number} available-back execution evidence requires explicit marketDefinition priceLadderDefinition"
        )
    if ladder_type != "CLASSIC":
        raise ValueError(
            f"{path}: line {line_number} unsupported Betfair execution price ladder {ladder_type}; only CLASSIC is verified"
        )
    return ladder_type


def _finite_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _is_classic_price(price: Decimal) -> bool:
    for lower, upper, increment in _CLASSIC_PRICE_BANDS:
        if lower <= price <= upper and (price - lower) % increment == 0:
            return True
    return False


def _validate_execution_ladder_contract(
    runner_change: dict[str, Any],
    definition: dict[str, Any],
    *,
    path: Path,
    line_number: int,
) -> str | None:
    has_atb = "atb" in runner_change
    has_batb = "batb" in runner_change
    if not has_atb and not has_batb:
        return None

    ladder_type = _execution_price_ladder_type(definition, path=path, line_number=line_number)
    if has_atb and has_batb:
        return ladder_type  # Stateful decoder emits the structural error.

    field = "atb" if has_atb else "batb"
    rows = runner_change[field]
    if not isinstance(rows, list):
        return ladder_type  # Stateful decoder emits the structural error.

    expected_length = 2 if field == "atb" else 3
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != expected_length:
            continue  # Stateful decoder emits the structural error.
        if field == "batb":
            size = _finite_decimal(row[2])
            if size == 0:
                # Betfair level removals may carry a zero/sentinel price.
                continue
            price_raw = row[1]
        else:
            price_raw = row[0]
        price = _finite_decimal(price_raw)
        if price is None or price <= 1:
            continue  # Stateful decoder emits the numeric/odds error.
        if not _is_classic_price(price):
            raise ValueError(
                f"{path}: line {line_number} {field}[{index}].price={price} is outside the declared CLASSIC Betfair price ladder"
            )
    return ladder_type


def _base_metadata(
    definition: dict[str, Any],
    names: dict[str, str],
    selection_id: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "provider": "betfair_exchange_historical",
        "betfair_market_type": str(definition.get("marketType") or ""),
    }
    event_name = str(definition.get("eventName") or "").strip()
    if event_name:
        metadata["event_name"] = event_name
    selection_name = names.get(selection_id)
    if selection_name:
        metadata["selection_name"] = selection_name
    if isinstance(definition.get("inPlay"), bool):
        metadata["betfair_in_play"] = bool(definition["inPlay"])
    return metadata


def _available_back_metadata(
    quote: AvailableBackQuote,
    *,
    definition: dict[str, Any],
    names: dict[str, str],
    selection_id: str,
    path: Path,
    line_number: int,
    last_traded_price: float | None,
    price_ladder_type: str,
) -> dict[str, Any]:
    metadata = _base_metadata(definition, names, selection_id)
    bet_delay = _bet_delay_seconds(definition, path=path, line_number=line_number)
    cache_verified = bool(quote.cache_verified)
    if not cache_verified:
        eligibility_reason = "available-to-back ladder cache was not initialized by a provider image"
    elif bet_delay is None:
        eligibility_reason = "Betfair betDelay is absent; zero-delay paper fill cannot be proven"
    elif bet_delay > 0:
        eligibility_reason = "positive Betfair betDelay is not simulated by this paper fill model"
    else:
        eligibility_reason = (
            "observed Betfair available size unit is not canonically bound to the paper stake unit"
        )

    metadata.update(
        {
            "price_semantics": "betfair_available_to_back",
            "provider_price_field": quote.provider_price_field,
            "betfair_ladder_kind": quote.ladder_kind,
            "betfair_price_ladder_type": price_ladder_type,
            "execution_price_ladder_verified": True,
            "execution_quote_verified": cache_verified,
            "actual_fill_verified": False,
            "paper_fill_eligible": False,
            "paper_fill_eligibility_reason": eligibility_reason,
            "paper_fill_capacity_verified": False,
            "paper_fill_capacity_unit_bound": False,
            "paper_fill_available_size": str(quote.available_size),
            "paper_fill_size_unit": "betfair_historical_stream_size_unit",
            "betfair_bet_delay_seconds": bet_delay,
        }
    )
    if last_traded_price is not None:
        metadata["betfair_last_traded_price"] = str(last_traded_price)
        metadata["last_traded_price_execution_quote_verified"] = False
    return metadata


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
    BASIC last-traded prices remain observational only. ADVANCED/PRO available-to-back
    ladders preserve observed executable quote and size provenance only when the source
    explicitly declares a supported price-ladder contract and runner roster. Source ladder
    size does not authorize paper economics until Autosport has a canonical, provenance-bound
    paper stake unit. No historical quote is treated as proof that a real order filled.
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
    source_file_ordinals = {path: ordinal for ordinal, path in enumerate(source_paths, start=1)}
    source_files = [
        {"ordinal": ordinal, "sha256": digest, "byte_size": path.stat().st_size}
        for ordinal, (path, digest) in enumerate(zip(source_paths, source_hashes), start=1)
    ]
    source_identity = f"betfair-historical-files:{_canonical_hash(source_files)}"

    definitions: dict[str, dict[str, Any]] = {}
    runner_names: dict[str, dict[str, str]] = {}
    declared_runner_ids: dict[str, set[str]] = {}
    final_statuses: dict[str, dict[str, str]] = {}
    settlement_ts: dict[str, str] = {}
    last_publish_ts: dict[str, str] = {}
    market_source_path: dict[str, Path] = {}
    available_books: dict[tuple[str, str], BetfairAvailableBackBook] = {}
    last_visible_price: dict[tuple[str, str], str] = {}
    last_visible_metadata: dict[tuple[str, str], dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    source_event_ordinal = 0
    max_source_publish_ts: str | None = None
    ltp_emitted = False
    available_back_emitted = False
    available_back_fields: set[str] = set()

    def append_event(
        *,
        path: Path,
        event_id: str,
        market_id: str,
        selection_id: str,
        odds: Any,
        observed: str,
        canonical_market_type: str,
        status: str,
        metadata: dict[str, Any],
    ) -> None:
        nonlocal source_event_ordinal
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
                "status": status,
                "source_ts": observed,
                "ingest_ts": imported,
                "score_state": None,
                "metadata": metadata,
                "_source_event_ordinal": source_event_ordinal,
                "_source_file_ordinal": source_file_ordinals[path],
            }
        )
        key = (market_id, selection_id)
        last_visible_price[key] = str(odds)
        last_visible_metadata[key] = dict(metadata)

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

            image = change.get("img") is True
            if image:
                for (book_market_id, _selection_id), book in available_books.items():
                    if book_market_id == market_id:
                        book.reset()

            prior_definition: dict[str, Any] | None = None
            market_definition = change.get("marketDefinition")
            if market_definition is not None:
                if not isinstance(market_definition, dict):
                    raise ValueError(f"{path}: line {line_number} marketDefinition must be an object")
                prior = definitions.get(market_id, {})
                if prior:
                    prior_definition = dict(prior)
                for field in ("eventId", "eventTypeId", "marketType"):
                    if field not in market_definition or field not in prior:
                        continue
                    previous_value = str(prior.get(field) or "").strip()
                    declared_value = str(market_definition.get(field) or "").strip()
                    if previous_value and declared_value != previous_value:
                        raise ValueError(
                            f"{path}: line {line_number} marketDefinition {field} changed for Betfair market {market_id}"
                        )
                merged = {**prior, **market_definition}
                definitions[market_id] = merged
                names = runner_names.setdefault(market_id, {})
                runners = market_definition.get("runners")
                if "runners" in market_definition:
                    if not isinstance(runners, list):
                        raise ValueError(
                            f"{path}: line {line_number} marketDefinition.runners must be a list when declared"
                        )
                    roster: set[str] = set()
                    for index, runner in enumerate(runners):
                        if not isinstance(runner, dict) or runner.get("id") is None:
                            raise ValueError(
                                f"{path}: line {line_number} marketDefinition.runners[{index}] requires id"
                            )
                        selection_id = str(runner["id"]).strip()
                        if not selection_id:
                            raise ValueError(
                                f"{path}: line {line_number} marketDefinition.runners[{index}].id must be non-empty"
                            )
                        if selection_id in roster:
                            raise ValueError(
                                f"{path}: line {line_number} marketDefinition.runners contains duplicate id {selection_id}"
                            )
                        roster.add(selection_id)
                        if runner.get("name") is not None:
                            names[selection_id] = str(runner["name"])
                    declared_runner_ids[market_id] = roster

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
                        previous_statuses = final_statuses.get(market_id)
                        if previous_statuses is not None and previous_statuses != statuses:
                            raise ValueError(
                                f"{path}: line {line_number} final settlement changed for Betfair market {market_id}"
                            )
                        if previous_statuses is None:
                            final_statuses[market_id] = statuses
                            settlement_ts[market_id] = observed

            definition = definitions.get(market_id)
            if definition is None:
                if change.get("rc"):
                    raise ValueError(f"{path}: line {line_number} runner changes precede market definition")
                continue
            canonical_market_type = _market_type(definition.get("marketType"), allowed)
            if canonical_market_type is None:
                continue
            event_type_id = str(definition.get("eventTypeId") or "").strip()
            if event_type_id != _TABLE_TENNIS_EVENT_TYPE_ID:
                raise ValueError(
                    f"{path}: line {line_number} supported market is not Betfair Table Tennis eventTypeId={_TABLE_TENNIS_EVENT_TYPE_ID}"
                )

            market_status = str(definition.get("status") or "").upper()
            current_bet_delay = _bet_delay_seconds(definition, path=path, line_number=line_number)
            if prior_definition is not None:
                prior_status = str(prior_definition.get("status") or "").upper()
                prior_bet_delay = _bet_delay_seconds(
                    prior_definition,
                    path=path,
                    line_number=line_number,
                )
                definition_state_changed = (
                    prior_status != market_status or prior_bet_delay != current_bet_delay
                )
                if definition_state_changed and market_status != "CLOSED":
                    names = runner_names.get(market_id, {})
                    event_id = str(definition.get("eventId") or "").strip()
                    if not event_id:
                        raise ValueError(
                            f"{path}: line {line_number} supported Betfair market requires source eventId"
                        )
                    visible_keys = sorted(
                        key for key in last_visible_price if key[0] == market_id
                    )
                    for key in visible_keys:
                        _market_id, selection_id = key
                        previous_metadata = last_visible_metadata.get(key, {})
                        provider_field = str(
                            previous_metadata.get("provider_price_field") or "marketDefinition"
                        )
                        metadata = _base_metadata(definition, names, selection_id)
                        metadata.update(
                            {
                                "price_semantics": "betfair_market_definition_state_transition",
                                "provider_price_field": provider_field,
                                "execution_quote_verified": False,
                                "actual_fill_verified": False,
                                "paper_fill_eligible": False,
                                "paper_fill_eligibility_reason": (
                                    "marketDefinition status/betDelay changed; prior quote is invalid until a fresh runner price update"
                                ),
                                "paper_fill_capacity_verified": False,
                                "paper_fill_capacity_unit_bound": False,
                                "betfair_market_status": market_status or "UNKNOWN",
                                "betfair_bet_delay_seconds": current_bet_delay,
                                "market_definition_transition": True,
                                "prior_betfair_market_status": prior_status or "UNKNOWN",
                                "prior_betfair_bet_delay_seconds": prior_bet_delay,
                            }
                        )
                        append_event(
                            path=path,
                            event_id=event_id,
                            market_id=market_id,
                            selection_id=selection_id,
                            odds=last_visible_price[key],
                            observed=observed,
                            canonical_market_type=canonical_market_type,
                            status=(market_status.lower() if market_status else "unknown"),
                            metadata=metadata,
                        )

            if market_status == "CLOSED":
                # Settlement facts stay outside strategy-visible market rows.
                continue
            if market_status != "OPEN":
                # Explicit pre-settlement marketDefinition state transitions invalidate
                # prior quotes above; no non-open runner update is rewritten as tradable.
                continue

            runner_changes = change.get("rc")
            if runner_changes is None:
                continue
            if not isinstance(runner_changes, list):
                raise ValueError(f"{path}: line {line_number} rc must be a list")
            names = runner_names.get(market_id, {})
            roster = declared_runner_ids.get(market_id, set())
            if runner_changes and not roster:
                raise ValueError(
                    f"{path}: line {line_number} runner changes require an authoritative marketDefinition.runners roster"
                )
            event_id = str(definition.get("eventId") or "").strip()
            if not event_id:
                raise ValueError(f"{path}: line {line_number} supported Betfair market requires source eventId")

            for runner_change in runner_changes:
                if not isinstance(runner_change, dict) or runner_change.get("id") is None:
                    raise ValueError(f"{path}: line {line_number} runner change requires id")
                selection_id = str(runner_change["id"])
                if selection_id not in roster:
                    raise ValueError(
                        f"{path}: line {line_number} runner change selection {selection_id} is not declared by marketDefinition.runners for market {market_id}"
                    )
                ltp: float | None = None
                if runner_change.get("ltp") is not None:
                    ltp = _validated_ltp(runner_change["ltp"], path=path, line_number=line_number)

                ladder_type = _validate_execution_ladder_contract(
                    runner_change,
                    definition,
                    path=path,
                    line_number=line_number,
                )
                key = (market_id, selection_id)
                book = available_books.setdefault(key, BetfairAvailableBackBook())
                try:
                    update = book.apply(runner_change, image=image)
                except ValueError as exc:
                    raise ValueError(f"{path}: line {line_number} invalid Betfair available-back ladder: {exc}") from exc

                if update.touched:
                    quote = update.quote
                    if quote is not None:
                        quote_ladder_type = ladder_type or _execution_price_ladder_type(
                            definition, path=path, line_number=line_number
                        )
                        metadata = _available_back_metadata(
                            quote,
                            definition=definition,
                            names=names,
                            selection_id=selection_id,
                            path=path,
                            line_number=line_number,
                            last_traded_price=ltp,
                            price_ladder_type=quote_ladder_type,
                        )
                        append_event(
                            path=path,
                            event_id=event_id,
                            market_id=market_id,
                            selection_id=selection_id,
                            odds=quote.decimal_odds,
                            observed=observed,
                            canonical_market_type=canonical_market_type,
                            status="open",
                            metadata=metadata,
                        )
                        available_back_emitted = True
                        available_back_fields.add(quote.provider_price_field)
                        continue

                    previous_price = last_visible_price.get(key)
                    if previous_price is not None:
                        quote_ladder_type = ladder_type or _execution_price_ladder_type(
                            definition, path=path, line_number=line_number
                        )
                        metadata = _base_metadata(definition, names, selection_id)
                        provider_field = "rc[].atb" if book.mode == "atb" else "rc[].batb"
                        metadata.update(
                            {
                                "price_semantics": "betfair_available_to_back_unavailable",
                                "provider_price_field": provider_field,
                                "betfair_price_ladder_type": quote_ladder_type,
                                "execution_price_ladder_verified": True,
                                "execution_quote_verified": False,
                                "actual_fill_verified": False,
                                "paper_fill_eligible": False,
                                "paper_fill_eligibility_reason": "available-to-back ladder has no verified best quote",
                                "paper_fill_capacity_verified": False,
                                "paper_fill_capacity_unit_bound": False,
                            }
                        )
                        append_event(
                            path=path,
                            event_id=event_id,
                            market_id=market_id,
                            selection_id=selection_id,
                            odds=previous_price,
                            observed=observed,
                            canonical_market_type=canonical_market_type,
                            status="open",
                            metadata=metadata,
                        )
                        available_back_emitted = True
                        available_back_fields.add(provider_field)
                    continue

                if ltp is None:
                    continue
                metadata = _base_metadata(definition, names, selection_id)
                metadata.update(
                    {
                        "price_semantics": "betfair_last_traded_price",
                        "provider_price_field": "rc[].ltp",
                        "execution_quote_verified": False,
                    }
                )
                append_event(
                    path=path,
                    event_id=event_id,
                    market_id=market_id,
                    selection_id=selection_id,
                    odds=ltp,
                    observed=observed,
                    canonical_market_type=canonical_market_type,
                    status="open",
                    metadata=metadata,
                )
                ltp_emitted = True

    if not events:
        raise ValueError("Betfair inputs produced no supported historical market quotes")
    if max_source_publish_ts is None:
        raise ValueError("Betfair inputs contain no MarketChangeMessage publish timestamps")
    if _as_datetime(acquired) < _as_datetime(max_source_publish_ts):
        raise ValueError(
            "acquired_at must not precede the latest source publish time present in the supplied files"
        )

    semantic_keys: set[tuple[str, ...]] = set()
    sources_by_observed_ts: dict[str, set[int]] = {}
    for event in events:
        metadata = event.get("metadata", {})
        semantic_key = (
            str(event["market_id"]),
            str(event["selection_id"]),
            str(event["observed_ts"]),
            str(event["decimal_odds"]),
            str(event["status"]),
            str(metadata.get("price_semantics", "")),
            str(metadata.get("paper_fill_available_size", "")),
        )
        if semantic_key in semantic_keys:
            raise ValueError("Betfair historical inputs contain duplicate market quote changes")
        semantic_keys.add(semantic_key)
        sources_by_observed_ts.setdefault(str(event["observed_ts"]), set()).add(
            int(event["_source_file_ordinal"])
        )

    ambiguous_cross_file_timestamps = sorted(
        (
            observed_ts
            for observed_ts, source_ordinals in sources_by_observed_ts.items()
            if len(source_ordinals) > 1
        ),
        key=_as_datetime,
    )
    if ambiguous_cross_file_timestamps:
        raise ValueError(
            "Betfair replay-visible events share publish time across input files; cross-file source order is ambiguous: "
            + ",".join(ambiguous_cross_file_timestamps)
        )

    # Replay orders by observed_ts then sequence. Within one source file, sequence preserves
    # provider source order for ties. Cross-file ties fail closed above because CLI file order
    # is not provider chronology and must never fabricate a causal replay order.
    events.sort(key=lambda item: (item["observed_ts"], item["_source_event_ordinal"]))
    for sequence, event in enumerate(events, start=1):
        event["sequence"] = sequence
        event.pop("_source_event_ordinal", None)
        event.pop("_source_file_ordinal", None)

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

        if available_back_emitted:
            price_semantics: dict[str, Any] = {
                "decimal_odds": "per_event_provider_price_semantics",
                "provider_fields": sorted(available_back_fields | ({"rc[].ltp"} if ltp_emitted else set())),
                "available_to_back_quote_verified_per_event": True,
                "available_to_back_cache_requires_provider_image": True,
                "execution_price_ladder_contract_verified": True,
                "supported_execution_price_ladder_types": ["CLASSIC"],
                "runner_roster_membership_verified": True,
                "paper_fill_capacity_enforced": False,
                "paper_fill_capacity_unit_bound": False,
                "paper_fill_capacity_authorizes_economics": False,
                "positive_or_unknown_bet_delay_paper_fill_allowed": False,
                "actual_fill_verified": False,
                "last_traded_price_execution_quote_verified": False,
            }
        else:
            # Keep the schema-v2 BASIC/LTP price-semantics contract byte-for-byte compatible
            # with the pre-order-book importer. Availability semantics remain independent:
            # explicit marketDefinition state transitions are replay-visible for BASIC too.
            price_semantics = {
                "decimal_odds": "betfair_last_traded_price",
                "provider_field": "rc[].ltp",
                "execution_quote_verified": False,
            }

        availability_semantics: dict[str, Any] = {
            "strategy_visible_market_status": "OPEN_QUOTES_PLUS_EXPLICIT_SOURCE_STATE_TRANSITIONS",
            "definition_state_transitions_preserved": True,
            "suspended_or_non_open_intervals_preserved": False,
            "complete_availability_history_verified": False,
        }

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
            "price_semantics": price_semantics,
            "availability_semantics": availability_semantics,
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
    print("price_truth=available_back_observed quote_verified_requires_provider_image price_ladder=CLASSIC_verified runner_roster_verified=true paper_capacity_unit_bound=false actual_fill_verified=false")
    print("licensing_retention_verified=false redistribution_verified=false")
    print("real_money_execution=false human_tested=false nvda_verified=false")
    print(f"dataset={report.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())