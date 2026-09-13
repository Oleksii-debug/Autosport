from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .domain import MarketEvent, MarketType


_FORBIDDEN_HISTORICAL_METADATA_KEYS = frozenset(
    {
        "outcome",
        "result",
        "winner",
        "final_score",
        "final_result",
        "settlement_result",
        "settled_outcome",
        "future_quote",
    }
)
_ALLOWED_HISTORICAL_OUTCOMES = frozenset({"win", "loss", "void"})
_PARLAY_SOURCE_PREFIX = "parlayapi:"
_PARLAY_TERMS_REFERENCE = "https://parlay-api.com/terms"
_PARLAY_STANDARD_RETENTION_CEILING = timedelta(days=90)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_string(raw: dict[str, Any], key: str, *, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _require_digest(raw: dict[str, Any], key: str, *, context: str) -> str:
    value = _require_string(raw, key, context=context)
    if (
        len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{context}.{key} must be a canonical lowercase SHA-256 hex digest")
    return value


def _parse_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    return parsed


def _retention_now() -> datetime:
    """Return the wall-clock access boundary; tests patch this private oracle."""
    return datetime.now(timezone.utc)


def _resolve_member(root: Path, value: str, *, field: str) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"{field} must be relative to the dataset root")
    root_resolved = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"{field} escapes the dataset root") from exc
    if not candidate.is_file():
        raise ValueError(f"{field} does not exist: {value}")
    return candidate


def _resolve_governance_artifact(root: Path, value: str, *, field: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or len(relative.parts) != 1 or value in {".", ".."}:
        raise ValueError(f"{field} must name one direct sibling governance artifact")
    return _resolve_member(root, value, field=field)


def _contains_forbidden_historical_metadata(value: Any) -> bool:
    """Reject future/outcome facts anywhere inside strategy-visible metadata."""
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_HISTORICAL_METADATA_KEYS:
                return True
            if _contains_forbidden_historical_metadata(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_historical_metadata(child) for child in value)
    return False


@dataclass(frozen=True, slots=True)
class DatasetGovernance:
    source_identity: str
    terms_reference: str
    retention_basis: str
    redistribution_policy: str
    acquired_at: str
    imported_at: str
    coverage_start_ts: str
    coverage_end_ts: str
    source_ids: tuple[str, ...]
    market_types: tuple[str, ...]
    outcome_reveal_after: str
    retention_expires_at: str | None = None
    authorization_valid_through: str | None = None
    retention_extension_authority_reference: str | None = None


def _assert_governance_retention_current(
    governance: DatasetGovernance | None,
    retention_as_of: str | None = None,
) -> None:
    if governance is None:
        return
    boundaries: list[tuple[str, datetime]] = []
    if governance.retention_expires_at is not None:
        boundaries.append(
            (
                "retention",
                _parse_timestamp(
                    governance.retention_expires_at,
                    field="governance.retention_expires_at",
                ),
            )
        )
    if governance.authorization_valid_through is not None:
        boundaries.append(
            (
                "authorization",
                _parse_timestamp(
                    governance.authorization_valid_through,
                    field="governance.authorization_valid_through",
                ),
            )
        )
    if not boundaries:
        return
    if retention_as_of is None:
        as_of = _retention_now()
    else:
        as_of = _parse_timestamp(retention_as_of, field="retention_as_of")
    boundary_name, expires = min(boundaries, key=lambda item: item[1])
    if as_of > expires:
        raise ValueError(
            f"historical dataset {boundary_name} window expired; governed market/results access is fail-closed"
        )


@dataclass(frozen=True, slots=True)
class ReplayDataset:
    root: Path
    name: str
    sport: str
    market_path: Path
    results_path: Path
    market_sha256: str
    results_sha256: str
    schema_version: int = 1
    governance: DatasetGovernance | None = None
    import_identity: str | None = None

    def _assert_retention_current(self, retention_as_of: str | None = None) -> None:
        _assert_governance_retention_current(self.governance, retention_as_of)

    def load_market_events(self) -> list[MarketEvent]:
        self._assert_retention_current()
        events: list[MarketEvent] = []
        with self.market_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    events.append(MarketEvent.from_dict(json.loads(line)))
        return events

    def load_results_after_replay(self) -> dict[str, str]:
        self._assert_retention_current()
        raw = json.loads(self.results_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("results payload must be an object")
        if int(raw.get("schema_version", 0)) != 1:
            raise ValueError("unsupported results schema")
        outcomes = raw.get("quote_outcomes")
        if not isinstance(outcomes, dict):
            raise ValueError("quote_outcomes must be an object")
        return {str(key): str(value) for key, value in outcomes.items()}


def _parlay_retention_capture_start(
    governance: dict[str, Any],
    *,
    acquired_dt: datetime,
    retention_expires_at: str,
    authorization_valid_through: str,
    retention_extension_authority_reference: str | None,
) -> datetime:
    acquisition_evidence = governance.get("acquisition_evidence")
    if not isinstance(acquisition_evidence, dict):
        raise ValueError(
            "ParlayAPI historical governance requires acquisition_evidence with snapshot captured_at provenance"
        )

    evidence_expiry = acquisition_evidence.get("retention_expires_at")
    if evidence_expiry != retention_expires_at:
        raise ValueError(
            "governance.acquisition_evidence.retention_expires_at must match governance.retention_expires_at"
        )
    evidence_authorization = acquisition_evidence.get("authorization_valid_through")
    if evidence_authorization != authorization_valid_through:
        raise ValueError(
            "governance.acquisition_evidence.authorization_valid_through must match "
            "governance.authorization_valid_through"
        )
    evidence_extension = acquisition_evidence.get("retention_extension_authority_reference")
    if evidence_extension != retention_extension_authority_reference:
        raise ValueError(
            "governance.acquisition_evidence.retention_extension_authority_reference must match "
            "governance.retention_extension_authority_reference"
        )

    snapshots = acquisition_evidence.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise ValueError(
            "ParlayAPI historical governance requires non-empty acquisition_evidence.snapshots"
        )
    captured: list[datetime] = []
    for index, snapshot in enumerate(snapshots):
        if not isinstance(snapshot, dict):
            raise ValueError(
                f"governance.acquisition_evidence.snapshots[{index}] must be an object"
            )
        captured_at = _require_string(
            snapshot,
            "captured_at",
            context=f"governance.acquisition_evidence.snapshots[{index}]",
        )
        captured_dt = _parse_timestamp(
            captured_at,
            field=f"governance.acquisition_evidence.snapshots[{index}].captured_at",
        )
        if captured_dt > acquired_dt:
            raise ValueError(
                "governance.acquisition_evidence snapshot captured_at must not be after acquired_at"
            )
        captured.append(captured_dt)
    return min(captured)


def _verify_parlay_governance_authority(
    root: Path,
    governance: dict[str, Any],
    *,
    source_identity: str,
    source_ids: tuple[str, ...],
    terms_reference: str,
    retention_basis: str,
    retention_expires_at: str,
    authorization_valid_through: str,
    retention_extension_authority_reference: str | None,
) -> None:
    acquisition_evidence = governance.get("acquisition_evidence")
    if not isinstance(acquisition_evidence, dict):
        raise ValueError("ParlayAPI historical governance requires acquisition_evidence")

    proof_file = _require_string(
        acquisition_evidence,
        "governance_proof_file",
        context="governance.acquisition_evidence",
    )
    expected_proof_sha = _require_digest(
        acquisition_evidence,
        "governance_proof_sha256",
        context="governance.acquisition_evidence",
    )
    authority_file = _require_string(
        acquisition_evidence,
        "authority_record_file",
        context="governance.acquisition_evidence",
    )
    expected_authority_sha = _require_digest(
        acquisition_evidence,
        "authority_record_sha256",
        context="governance.acquisition_evidence",
    )
    proof_path = _resolve_governance_artifact(
        root,
        proof_file,
        field="governance.acquisition_evidence.governance_proof_file",
    )
    _resolve_governance_artifact(
        root,
        authority_file,
        field="governance.acquisition_evidence.authority_record_file",
    )

    from .historical_governance import verify_governance_authority_binding

    binding = verify_governance_authority_binding(proof_path)
    if binding.governance_proof_sha256 != expected_proof_sha:
        raise ValueError(
            "governance.acquisition_evidence.governance_proof_sha256 does not match verified proof artifact"
        )
    if binding.authority_record_file != authority_file:
        raise ValueError(
            "governance.acquisition_evidence.authority_record_file does not match verified proof binding"
        )
    if binding.authority_record_sha256 != expected_authority_sha:
        raise ValueError(
            "governance.acquisition_evidence.authority_record_sha256 does not match verified authority artifact"
        )

    try:
        proof = json.loads(binding.governance_proof_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("verified governance proof bytes are not valid UTF-8 JSON") from exc
    if not isinstance(proof, dict):
        raise ValueError("verified governance proof must be a JSON object")

    exact_claims = {
        "source_identity": source_identity,
        "terms_reference": terms_reference,
        "retention_basis": retention_basis,
        "retention_expires_at": retention_expires_at,
        "authorization_valid_through": authorization_valid_through,
        "retention_extension_authority_reference": retention_extension_authority_reference,
    }
    for field, expected in exact_claims.items():
        actual = proof.get(field)
        if actual != expected:
            raise ValueError(
                f"governance.{field} does not match exact SHA-bound governance authority proof"
            )
    proof_source_ids = proof.get("source_ids")
    if (
        not isinstance(proof_source_ids, list)
        or not proof_source_ids
        or any(
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            for value in proof_source_ids
        )
    ):
        raise ValueError(
            "verified governance proof.source_ids must contain canonical non-empty strings without surrounding whitespace"
        )
    normalized_proof_source_ids = tuple(sorted(proof_source_ids))
    if normalized_proof_source_ids != tuple(sorted(source_ids)):
        raise ValueError(
            "governance.coverage.source_ids do not match exact SHA-bound governance authority proof"
        )
    authority_reference = _require_string(
        acquisition_evidence,
        "authority_reference",
        context="governance.acquisition_evidence",
    )
    if proof.get("authority_reference") != authority_reference:
        raise ValueError(
            "governance.acquisition_evidence.authority_reference does not match exact SHA-bound governance proof"
        )


def _load_governance(raw: dict[str, Any], *, root: Path) -> DatasetGovernance:
    governance = raw.get("governance")
    if not isinstance(governance, dict):
        raise ValueError("schema v2 historical dataset requires governance object")

    source_identity = _require_string(governance, "source_identity", context="governance")
    terms_reference = _require_string(governance, "terms_reference", context="governance")
    if (
        source_identity.startswith(_PARLAY_SOURCE_PREFIX)
        and terms_reference.rstrip("/") != _PARLAY_TERMS_REFERENCE
    ):
        raise ValueError("ParlayAPI historical governance must use canonical terms_reference")
    retention_basis = _require_string(governance, "retention_basis", context="governance")
    redistribution_policy = _require_string(governance, "redistribution_policy", context="governance")
    if redistribution_policy not in {"prohibited", "internal_only", "permitted"}:
        raise ValueError("governance.redistribution_policy must be prohibited, internal_only, or permitted")

    acquired_at = _require_string(governance, "acquired_at", context="governance")
    imported_at = _require_string(governance, "imported_at", context="governance")
    acquired_dt = _parse_timestamp(acquired_at, field="governance.acquired_at")
    imported_dt = _parse_timestamp(imported_at, field="governance.imported_at")
    if imported_dt < acquired_dt:
        raise ValueError("governance.imported_at must not precede acquired_at")

    extension_raw = governance.get("retention_extension_authority_reference")
    retention_extension_authority_reference: str | None = None
    if extension_raw is not None:
        if not isinstance(extension_raw, str) or not extension_raw.strip():
            raise ValueError(
                "governance.retention_extension_authority_reference must be a non-empty string when present"
            )
        retention_extension_authority_reference = extension_raw.strip()

    retention_expires_raw = governance.get("retention_expires_at")
    retention_expires_at: str | None = None
    retention_expires_dt: datetime | None = None
    if retention_expires_raw is not None:
        if not isinstance(retention_expires_raw, str) or not retention_expires_raw.strip():
            raise ValueError("governance.retention_expires_at must be a non-empty ISO-8601 timestamp")
        retention_expires_at = retention_expires_raw.strip()
        retention_expires_dt = _parse_timestamp(
            retention_expires_at,
            field="governance.retention_expires_at",
        )
        if retention_expires_dt < acquired_dt:
            raise ValueError("governance.retention_expires_at must not precede acquired_at")
        if imported_dt > retention_expires_dt:
            raise ValueError("governance.imported_at exceeds retention_expires_at")

    authorization_raw = governance.get("authorization_valid_through")
    authorization_valid_through: str | None = None
    authorization_valid_through_dt: datetime | None = None
    if authorization_raw is not None:
        if not isinstance(authorization_raw, str) or not authorization_raw.strip():
            raise ValueError(
                "governance.authorization_valid_through must be a non-empty ISO-8601 timestamp"
            )
        authorization_valid_through = authorization_raw.strip()
        authorization_valid_through_dt = _parse_timestamp(
            authorization_valid_through,
            field="governance.authorization_valid_through",
        )
        if imported_dt > authorization_valid_through_dt:
            raise ValueError("governance.imported_at exceeds authorization_valid_through")

    coverage = governance.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("governance.coverage must be an object")
    coverage_start_ts = _require_string(coverage, "start_ts", context="governance.coverage")
    coverage_end_ts = _require_string(coverage, "end_ts", context="governance.coverage")
    coverage_start = _parse_timestamp(coverage_start_ts, field="governance.coverage.start_ts")
    coverage_end = _parse_timestamp(coverage_end_ts, field="governance.coverage.end_ts")
    if coverage_end < coverage_start:
        raise ValueError("governance.coverage.end_ts must not precede start_ts")

    source_ids_raw = coverage.get("source_ids")
    if not isinstance(source_ids_raw, list) or not source_ids_raw:
        raise ValueError("governance.coverage.source_ids must be a non-empty list")
    if any(
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        for value in source_ids_raw
    ):
        raise ValueError(
            "governance.coverage.source_ids must contain canonical non-empty strings without surrounding whitespace"
        )
    source_ids = tuple(source_ids_raw)
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("governance.coverage.source_ids must contain unique non-empty strings")
    if (
        any(source_id.startswith(_PARLAY_SOURCE_PREFIX) for source_id in source_ids)
        and terms_reference.rstrip("/") != _PARLAY_TERMS_REFERENCE
    ):
        raise ValueError("ParlayAPI historical governance must use canonical terms_reference")

    is_parlay = (
        source_identity.startswith(_PARLAY_SOURCE_PREFIX)
        or any(source_id.startswith(_PARLAY_SOURCE_PREFIX) for source_id in source_ids)
        or terms_reference.rstrip("/") == _PARLAY_TERMS_REFERENCE
    )
    if is_parlay:
        if retention_expires_at is None or retention_expires_dt is None:
            raise ValueError("ParlayAPI historical governance requires structured retention_expires_at")
        if authorization_valid_through is None or authorization_valid_through_dt is None:
            raise ValueError(
                "ParlayAPI historical governance requires structured authorization_valid_through"
            )
        earliest_capture_dt = _parlay_retention_capture_start(
            governance,
            acquired_dt=acquired_dt,
            retention_expires_at=retention_expires_at,
            authorization_valid_through=authorization_valid_through,
            retention_extension_authority_reference=retention_extension_authority_reference,
        )
        _verify_parlay_governance_authority(
            root,
            governance,
            source_identity=source_identity,
            source_ids=source_ids,
            terms_reference=terms_reference,
            retention_basis=retention_basis,
            retention_expires_at=retention_expires_at,
            authorization_valid_through=authorization_valid_through,
            retention_extension_authority_reference=retention_extension_authority_reference,
        )
        if (
            retention_expires_dt > earliest_capture_dt + _PARLAY_STANDARD_RETENTION_CEILING
            and retention_extension_authority_reference is None
        ):
            raise ValueError(
                "ParlayAPI retention beyond 90 days from earliest governed capture requires "
                "retention_extension_authority_reference"
            )

    market_types_raw = coverage.get("market_types")
    if not isinstance(market_types_raw, list) or not market_types_raw:
        raise ValueError("governance.coverage.market_types must be a non-empty list")
    market_types = tuple(str(value).strip() for value in market_types_raw)
    allowed_market_types = {market_type.value for market_type in MarketType}
    if (
        any(not value for value in market_types)
        or len(set(market_types)) != len(market_types)
        or any(value not in allowed_market_types for value in market_types)
    ):
        raise ValueError("governance.coverage.market_types contains an unsupported or duplicate market type")

    causality = governance.get("causality")
    if not isinstance(causality, dict):
        raise ValueError("governance.causality must be an object")
    strategy_time_field = _require_string(causality, "strategy_time_field", context="governance.causality")
    if strategy_time_field != "observed_ts":
        raise ValueError("governance.causality.strategy_time_field must be observed_ts")
    outcome_reveal_after = _require_string(causality, "outcome_reveal_after", context="governance.causality")
    outcome_reveal_dt = _parse_timestamp(
        outcome_reveal_after,
        field="governance.causality.outcome_reveal_after",
    )
    if outcome_reveal_dt < coverage_end:
        raise ValueError("outcome_reveal_after must be at or after coverage end")

    return DatasetGovernance(
        source_identity=source_identity,
        terms_reference=terms_reference,
        retention_basis=retention_basis,
        redistribution_policy=redistribution_policy,
        acquired_at=acquired_at,
        imported_at=imported_at,
        coverage_start_ts=coverage_start_ts,
        coverage_end_ts=coverage_end_ts,
        source_ids=source_ids,
        market_types=market_types,
        outcome_reveal_after=outcome_reveal_after,
        retention_expires_at=retention_expires_at,
        authorization_valid_through=authorization_valid_through,
        retention_extension_authority_reference=retention_extension_authority_reference,
    )


def _validate_historical_payloads(
    market_path: Path,
    results_path: Path,
    governance: DatasetGovernance,
) -> None:
    coverage_start = _parse_timestamp(governance.coverage_start_ts, field="coverage_start_ts")
    coverage_end = _parse_timestamp(governance.coverage_end_ts, field="coverage_end_ts")
    source_ids = set(governance.source_ids)
    market_types = set(governance.market_types)
    quote_keys: set[str] = set()
    dedupe_keys: set[str] = set()
    event_count = 0

    with market_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            event_count += 1
            try:
                raw_event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"market line {line_number} is not valid JSON") from exc
            if not isinstance(raw_event, dict):
                raise ValueError(f"market line {line_number} must be a JSON object")
            for timestamp_field in ("source_ts", "observed_ts", "ingest_ts"):
                timestamp_value = raw_event.get(timestamp_field)
                if not isinstance(timestamp_value, str) or not timestamp_value.strip():
                    raise ValueError(
                        f"market line {line_number} requires explicit {timestamp_field} for historical governance"
                    )
            event = MarketEvent.from_dict(raw_event)
            if event.source_id not in source_ids:
                raise ValueError(f"market line {line_number} source_id is outside declared coverage")
            if event.market_type.value not in market_types:
                raise ValueError(f"market line {line_number} market_type is outside declared coverage")
            if event.source_ts is None:
                raise ValueError(f"market line {line_number} requires source_ts for historical governance")
            source_ts = _parse_timestamp(event.source_ts, field=f"market line {line_number} source_ts")
            observed_ts = _parse_timestamp(event.observed_ts, field=f"market line {line_number} observed_ts")
            ingest_ts = _parse_timestamp(event.ingest_ts, field=f"market line {line_number} ingest_ts")
            if source_ts > observed_ts:
                raise ValueError(f"market line {line_number} source_ts is after observed_ts")
            if ingest_ts < observed_ts:
                raise ValueError(f"market line {line_number} ingest_ts is before observed_ts")
            if observed_ts < coverage_start or observed_ts > coverage_end:
                raise ValueError(f"market line {line_number} observed_ts is outside declared coverage")
            if _contains_forbidden_historical_metadata(event.metadata):
                raise ValueError(f"market line {line_number} contains future/outcome metadata")
            if event.dedupe_key in dedupe_keys:
                raise ValueError(f"market line {line_number} duplicates canonical source event identity")
            dedupe_keys.add(event.dedupe_key)
            quote_keys.add(event.quote_key)

    if event_count == 0:
        raise ValueError("historical market corpus must contain at least one event")

    results_raw = json.loads(results_path.read_text(encoding="utf-8"))
    if not isinstance(results_raw, dict):
        raise ValueError("results payload must be an object")
    if int(results_raw.get("schema_version", 0)) != 1:
        raise ValueError("unsupported results schema")
    results_reveal_after = _require_string(
        results_raw,
        "outcome_reveal_after",
        context="sealed results",
    )
    results_reveal_dt = _parse_timestamp(
        results_reveal_after,
        field="sealed results.outcome_reveal_after",
    )
    governance_reveal_dt = _parse_timestamp(
        governance.outcome_reveal_after,
        field="governance.causality.outcome_reveal_after",
    )
    if results_reveal_dt != governance_reveal_dt:
        raise ValueError(
            "sealed results outcome_reveal_after must match governance.causality.outcome_reveal_after"
        )
    outcomes = results_raw.get("quote_outcomes")
    if not isinstance(outcomes, dict):
        raise ValueError("quote_outcomes must be an object")
    outcome_keys = {str(key) for key in outcomes}
    missing_outcomes = sorted(quote_keys - outcome_keys)
    if missing_outcomes:
        raise ValueError("sealed results are missing quote outcomes for historical market corpus")
    unknown_outcomes = sorted(outcome_keys - quote_keys)
    if unknown_outcomes:
        raise ValueError("sealed results reference quote keys absent from historical market corpus")
    invalid_outcomes = sorted(
        str(key)
        for key, value in outcomes.items()
        if not isinstance(value, str) or value not in _ALLOWED_HISTORICAL_OUTCOMES
    )
    if invalid_outcomes:
        raise ValueError("sealed results contain unsupported outcome; allowed values are win, loss, void")


def _import_identity(
    *,
    raw: dict[str, Any],
    market_sha256: str,
    results_sha256: str,
) -> str:
    payload = {
        "schema_version": 2,
        "name": str(raw.get("name", "")),
        "sport": str(raw.get("sport", "")),
        "market_file": str(raw["market_file"]),
        "results_file": str(raw["results_file"]),
        "market_sha256": market_sha256,
        "results_sha256": results_sha256,
        "governance": raw["governance"],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_dataset(root: str | Path) -> ReplayDataset:
    root = Path(root)
    manifest_path = root / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("dataset manifest must be an object")
    schema_version = int(raw.get("schema_version", 0))
    if schema_version not in {1, 2}:
        raise ValueError("unsupported dataset schema")

    governance: DatasetGovernance | None = None
    if schema_version == 2:
        if str(raw.get("dataset_kind", "")) != "historical":
            raise ValueError("schema v2 requires dataset_kind=historical")
        governance = _load_governance(raw, root=root)
        # Retention and authorization are byte-access boundaries, not merely replay API boundaries.
        # Governance authority is verified first, then an expired governed corpus is rejected
        # before market/results members are resolved, hashed or parsed.
        _assert_governance_retention_current(governance)

    market_path = _resolve_member(root, str(raw["market_file"]), field="market_file")
    results_path = _resolve_member(root, str(raw["results_file"]), field="results_file")
    if market_path == results_path:
        raise ValueError("market and results files must remain physically separate")

    expected_market = str(raw["market_sha256"])
    expected_results = str(raw["results_sha256"])
    actual_market = _sha256(market_path)
    actual_results = _sha256(results_path)
    if actual_market != expected_market:
        raise ValueError("market dataset hash mismatch")
    if actual_results != expected_results:
        raise ValueError("sealed results hash mismatch")

    import_identity: str | None = None
    if schema_version == 2:
        assert governance is not None
        _validate_historical_payloads(market_path, results_path, governance)
        import_identity = _import_identity(
            raw=raw,
            market_sha256=actual_market,
            results_sha256=actual_results,
        )
        declared_identity = raw.get("import_identity")
        if declared_identity is not None and str(declared_identity) != import_identity:
            raise ValueError("historical import identity mismatch")

    return ReplayDataset(
        root=root,
        name=str(raw.get("name", root.name)),
        sport=str(raw.get("sport", "unknown")),
        market_path=market_path,
        results_path=results_path,
        market_sha256=actual_market,
        results_sha256=actual_results,
        schema_version=schema_version,
        governance=governance,
        import_identity=import_identity,
    )
