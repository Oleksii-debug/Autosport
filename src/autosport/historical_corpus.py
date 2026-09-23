from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlsplit

from .dataset import load_dataset
from .domain import MarketEvent
from .historical_governance import verify_governance_authority_binding
from .integrity import atomic_write_json
from .outcome_lineage import validate_outcome_source_lineage
from .parlay_sport_provider import _canonical_sport_key


_SNAPSHOT_KIND = "parlayapi_point_in_time_historical_snapshot"
_GOVERNANCE_KIND = "historical_corpus_governance_proof"
_OUTCOME_PROVENANCE_KIND = "historical_outcome_provenance"
_ALLOWED_REDISTRIBUTION = {"prohibited", "internal_only", "permitted"}
_REDISTRIBUTION_RANK = {"prohibited": 0, "internal_only": 1, "permitted": 2}
_ALLOWED_OUTCOMES = {"win", "loss", "void"}
_PARLAY_TERMS_REFERENCE = "https://parlay-api.com/terms"
_PARLAY_STANDARD_RETENTION_CEILING = timedelta(days=90)
_TRACKED_ACQUISITION_RESPONSE_HEADERS = (
    "x-api-version",
    "x-api-release-date",
    "deprecation",
    "sunset",
    "link",
    "x-historical-window-hours",
    "x-historical-window-from",
    "x-markets-served",
    "x-markets-unservable",
    "x-markets-served-elsewhere",
    "cache-control",
)
_MAX_ACQUISITION_HEADER_CHARS = 4096
_OUTCOME_LINEAGE_DERIVED_FIELDS = frozenset(
    {
        "source_record_id",
        "source_record_revision_id",
        "source_record_revision",
        "source_record_revision_kind",
        "source_record_recorded_at",
        "source_record_predecessor_sha256",
        "source_record_supersedes_revision_id",
        "source_record_lineage_root_sha256",
        "source_record_lineage_root_revision_id",
        "source_record_lineage_depth",
        "source_record_lineage",
        "source_record_lineage_verified",
    }
)


@dataclass(frozen=True, slots=True)
class HistoricalCorpusBuild:
    root: str
    import_identity: str
    snapshot_count: int
    event_count: int
    market_sha256: str
    results_sha256: str
    coverage_start_ts: str
    coverage_end_ts: str
    source_ids: tuple[str, ...]
    market_types: tuple[str, ...]
    redistribution_policy: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_bytes(path: Path, *, context: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{context} is not readable: {path}") from exc


def _json_object_bytes(
    payload: bytes,
    *,
    path: Path,
    context: str,
) -> dict[str, Any]:
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{context} contains duplicate JSON object key: {key}")
            value[key] = item
        return value

    def _reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"{context} contains non-standard JSON constant: {value}")

    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonstandard_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} is not readable valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a JSON object")
    return raw


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _market_semantic_content_sha256(
    events: Sequence[tuple[MarketEvent, dict[str, Any]]],
) -> str:
    """Hash provider market semantics without product-local acquisition time.

    Historical capture deliberately stamps captured_at into ingest_ts on every
    persisted MarketEvent. That timestamp belongs to acquisition provenance,
    not to provider market content. Keep the exact market artifact SHA bound
    elsewhere, while the CONTENT axis removes only this product-local field
    from the canonical parsed event representation.
    """

    semantic_rows: list[dict[str, Any]] = []
    for event, _raw in events:
        row = event.to_dict()
        row.pop("ingest_ts", None)
        semantic_rows.append(row)
    return _canonical_json_sha256(
        {
            "schema_version": 1,
            "kind": "parlay_historical_market_semantic_content",
            "rows": semantic_rows,
        }
    )


def _timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    return parsed


def _json_object(path: Path, *, context: str) -> dict[str, Any]:
    payload = _read_bytes(path, context=context)
    return _json_object_bytes(payload, path=path, context=context)


def _text(raw: dict[str, Any], key: str, *, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _digest(raw: dict[str, Any], key: str, *, context: str) -> str:
    value = raw.get(key)
    if (
        type(value) is not str
        or value != value.strip()
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(
            f"{context}.{key} must be a canonical lowercase SHA-256 digest"
        )
    return value


def _snapshot_acquisition_provenance(
    evidence: dict[str, Any],
    *,
    sport_key: str,
    requested_at: str,
    response_sha256: str,
) -> dict[str, Any] | None:
    raw = evidence.get("acquisition_provenance")
    claimed_sha = evidence.get("acquisition_sha256")
    if raw is None and claimed_sha is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("snapshot acquisition_provenance must be an object when present")
    acquisition_sha256 = _digest(
        evidence,
        "acquisition_sha256",
        context="snapshot evidence",
    )
    if _canonical_json_sha256(raw) != acquisition_sha256:
        raise ValueError(
            "snapshot evidence acquisition_sha256 does not match acquisition_provenance"
        )

    expected_keys = {
        "schema_version",
        "kind",
        "product_kind",
        "request",
        "http_status",
        "response_payload_sha256",
        "response_headers",
        "canonical_response_payload_bound",
        "raw_response_bytes_bound",
    }
    if set(raw) != expected_keys:
        raise ValueError("snapshot acquisition_provenance fields do not match schema v1")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValueError("snapshot acquisition_provenance schema_version must be exact integer 1")
    if raw.get("kind") != "parlayapi_point_in_time_historical_acquisition":
        raise ValueError("snapshot acquisition_provenance kind is not supported")
    if raw.get("product_kind") != "POINT_IN_TIME_ODDS":
        raise ValueError("snapshot acquisition_provenance product_kind must be POINT_IN_TIME_ODDS")
    if type(raw.get("http_status")) is not int or raw["http_status"] != 200:
        raise ValueError("snapshot acquisition_provenance http_status must be exact HTTP 200")
    if raw.get("canonical_response_payload_bound") is not True:
        raise ValueError(
            "snapshot acquisition_provenance must bind the canonical response payload"
        )
    if raw.get("raw_response_bytes_bound") is not False:
        raise ValueError(
            "snapshot acquisition_provenance cannot claim raw response bytes are bound"
        )
    response_payload_sha256 = _digest(
        raw,
        "response_payload_sha256",
        context="snapshot acquisition_provenance",
    )
    if response_payload_sha256 != response_sha256:
        raise ValueError(
            "snapshot acquisition_provenance response payload digest contradicts snapshot evidence"
        )

    request = raw.get("request")
    if not isinstance(request, dict):
        raise ValueError("snapshot acquisition_provenance.request must be an object")
    expected_request_keys = {
        "method",
        "origin",
        "base_url_sha256",
        "endpoint_path",
        "query",
        "query_string",
        "request_url_sha256",
        "request_url_persisted",
        "request_credentials_persisted",
    }
    if set(request) != expected_request_keys:
        raise ValueError("snapshot acquisition request fields do not match schema v1")
    if request.get("method") != "GET":
        raise ValueError("snapshot acquisition request method must be GET")
    origin = request.get("origin")
    if type(origin) is not str or not origin or origin != origin.strip():
        raise ValueError("snapshot acquisition request origin must be canonical text")
    parsed_origin = urlsplit(origin)
    if (
        parsed_origin.scheme != "https"
        or not parsed_origin.netloc
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
        or parsed_origin.username is not None
        or parsed_origin.password is not None
    ):
        raise ValueError("snapshot acquisition request origin must be a secret-free HTTPS origin")
    _digest(request, "base_url_sha256", context="snapshot acquisition request")
    _digest(request, "request_url_sha256", context="snapshot acquisition request")
    if request.get("request_url_persisted") is not False:
        raise ValueError("snapshot acquisition request_url_persisted must be false")
    if request.get("request_credentials_persisted") is not False:
        raise ValueError("snapshot acquisition request_credentials_persisted must be false")
    expected_endpoint = f"/v1/historical/sports/{sport_key}/odds"
    if request.get("endpoint_path") != expected_endpoint:
        raise ValueError("snapshot acquisition endpoint_path contradicts sport_key")

    query = request.get("query")
    if not isinstance(query, dict):
        raise ValueError("snapshot acquisition request.query must be an object")
    required_query_keys = {"date", "regions", "markets", "oddsFormat", "dateFormat"}
    if set(query) != required_query_keys:
        raise ValueError("snapshot acquisition request.query fields do not match point-in-time odds")
    if query.get("date") != requested_at:
        raise ValueError("snapshot acquisition request date contradicts requested_at")
    if query.get("oddsFormat") != "decimal" or query.get("dateFormat") != "iso":
        raise ValueError("snapshot acquisition request format parameters are not canonical")
    for key in ("regions", "markets"):
        value = query.get(key)
        if type(value) is not str or not value or value != value.strip():
            raise ValueError(f"snapshot acquisition request {key} must be canonical text")
        parts = value.split(",")
        if any(not part or part != part.strip() for part in parts):
            raise ValueError(f"snapshot acquisition request {key} contains an empty/aliased token")

    query_string = request.get("query_string")
    if type(query_string) is not str or not query_string:
        raise ValueError("snapshot acquisition request.query_string must be non-empty text")
    try:
        query_pairs = parse_qsl(
            query_string,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise ValueError("snapshot acquisition request.query_string is invalid") from exc
    if len(query_pairs) != len(required_query_keys):
        raise ValueError("snapshot acquisition request.query_string has duplicate/missing fields")
    query_from_string = dict(query_pairs)
    if len(query_from_string) != len(query_pairs) or query_from_string != query:
        raise ValueError("snapshot acquisition request.query_string contradicts query object")

    response_headers = raw.get("response_headers")
    if not isinstance(response_headers, dict):
        raise ValueError("snapshot acquisition response_headers must be an object")
    if set(response_headers) != set(_TRACKED_ACQUISITION_RESPONSE_HEADERS):
        raise ValueError("snapshot acquisition response_headers fields do not match schema v1")
    for name in _TRACKED_ACQUISITION_RESPONSE_HEADERS:
        value = response_headers[name]
        if value is None:
            continue
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > _MAX_ACQUISITION_HEADER_CHARS
        ):
            raise ValueError(
                f"snapshot acquisition response header {name} is not canonical bounded text"
            )

    return {
        "acquisition_sha256": acquisition_sha256,
        "provenance": raw,
    }


def _governance_proof(path: Path, *, payload: bytes | None = None) -> dict[str, Any]:
    if payload is None:
        payload = _read_bytes(path, context="governance proof")
    raw = _json_object_bytes(payload, path=path, context="governance proof")
    if int(raw.get("schema_version", 0)) != 1:
        raise ValueError("governance proof schema_version must be 1")
    if raw.get("kind") != _GOVERNANCE_KIND:
        raise ValueError(f"governance proof kind must be {_GOVERNANCE_KIND}")
    if raw.get("licensing_or_retention_verified") is not True:
        raise ValueError("governance proof must explicitly set licensing_or_retention_verified=true")

    source_identity = _text(raw, "source_identity", context="governance proof")
    source_ids_raw = raw.get("source_ids")
    if not isinstance(source_ids_raw, list) or not source_ids_raw:
        raise ValueError("governance proof.source_ids must be a non-empty list")
    source_ids = tuple(sorted(str(value).strip() for value in source_ids_raw))
    if any(not value for value in source_ids) or len(set(source_ids)) != len(source_ids):
        raise ValueError("governance proof.source_ids must contain unique non-empty strings")
    terms_reference = _text(raw, "terms_reference", context="governance proof")
    retention_basis = _text(raw, "retention_basis", context="governance proof")
    retention_expires_at = _text(raw, "retention_expires_at", context="governance proof")
    _timestamp(retention_expires_at, field="governance proof.retention_expires_at")
    authorization_valid_through = _text(
        raw,
        "authorization_valid_through",
        context="governance proof",
    )
    _timestamp(
        authorization_valid_through,
        field="governance proof.authorization_valid_through",
    )
    extension_raw = raw.get("retention_extension_authority_reference")
    retention_extension_authority_reference: str | None = None
    if extension_raw is not None:
        if not isinstance(extension_raw, str) or not extension_raw.strip():
            raise ValueError(
                "governance proof.retention_extension_authority_reference must be a non-empty string when present"
            )
        retention_extension_authority_reference = extension_raw.strip()
    authority_reference = _text(raw, "authority_reference", context="governance proof")
    verified_at = _text(raw, "verified_at", context="governance proof")
    _timestamp(verified_at, field="governance proof.verified_at")

    policy = _text(raw, "redistribution_policy", context="governance proof")
    if policy not in _ALLOWED_REDISTRIBUTION:
        raise ValueError(
            "governance proof.redistribution_policy must be prohibited, internal_only, or permitted"
        )
    redistribution_verified = raw.get("redistribution_verified")
    if not isinstance(redistribution_verified, bool):
        raise ValueError("governance proof.redistribution_verified must be boolean")
    if policy == "permitted" and redistribution_verified is not True:
        raise ValueError("redistribution_policy=permitted requires redistribution_verified=true")

    return {
        **raw,
        "source_identity": source_identity,
        "source_ids": source_ids,
        "terms_reference": terms_reference,
        "retention_basis": retention_basis,
        "retention_expires_at": retention_expires_at,
        "authorization_valid_through": authorization_valid_through,
        "retention_extension_authority_reference": retention_extension_authority_reference,
        "authority_reference": authority_reference,
        "verified_at": verified_at,
        "redistribution_policy": policy,
        "redistribution_verified": redistribution_verified,
        "_artifact_sha256": _sha256_bytes(payload),
    }


def _outcome_provenance(
    results: dict[str, Any],
    *,
    source_root: Path,
    reveal_dt: datetime,
    imported_dt: datetime,
) -> dict[str, Any]:
    raw = results.get("outcome_provenance")
    if not isinstance(raw, dict):
        raise ValueError("sealed results outcome_provenance must be an object")
    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise ValueError("sealed results outcome_provenance.schema_version must be exact integer 1 or 2")
    reserved_lineage_fields = sorted(_OUTCOME_LINEAGE_DERIVED_FIELDS.intersection(raw))
    if reserved_lineage_fields:
        raise ValueError(
            "sealed results outcome_provenance must not provide verifier-derived lineage fields: "
            + ", ".join(reserved_lineage_fields)
        )
    if raw.get("kind") != _OUTCOME_PROVENANCE_KIND:
        raise ValueError(
            f"sealed results outcome_provenance.kind must be {_OUTCOME_PROVENANCE_KIND}"
        )
    if raw.get("licensing_or_retention_verified") is not True:
        raise ValueError(
            "sealed results outcome_provenance must explicitly set licensing_or_retention_verified=true"
        )

    source_identity = _text(raw, "source_identity", context="sealed results outcome_provenance")
    source_record_file = _text(
        raw,
        "source_record_file",
        context="sealed results outcome_provenance",
    )
    relative_source_record = Path(source_record_file)
    if (
        relative_source_record.is_absolute()
        or len(relative_source_record.parts) != 1
        or source_record_file in {".", ".."}
    ):
        raise ValueError(
            "sealed results outcome_provenance.source_record_file must name one direct sibling artifact"
        )
    source_record_sha256 = _text(
        raw,
        "source_record_sha256",
        context="sealed results outcome_provenance",
    ).lower()
    if len(source_record_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_record_sha256
    ):
        raise ValueError(
            "sealed results outcome_provenance.source_record_sha256 must be a 64-character SHA-256 hex digest"
        )
    source_record_path = source_root / relative_source_record
    source_record_bytes = _read_bytes(
        source_record_path,
        context="sealed results outcome provenance source record",
    )
    actual_source_record_sha256 = _sha256_bytes(source_record_bytes)
    if actual_source_record_sha256 != source_record_sha256:
        raise ValueError(
            "sealed results outcome_provenance.source_record_sha256 does not match source record artifact"
        )

    source_record = _json_object_bytes(
        source_record_bytes,
        path=source_record_path,
        context="sealed outcome source record",
    )
    source_record_identity = _text(
        source_record,
        "source",
        context="sealed outcome source record",
    )
    if source_record_identity != source_identity:
        raise ValueError(
            "sealed outcome source record.source must match sealed results outcome_provenance.source_identity"
        )

    lineage = None
    if schema_version == 2:
        lineage = validate_outcome_source_lineage(
            source_root=source_root,
            source_record_file=source_record_file,
            source_record_sha256=source_record_sha256,
            source_record=source_record,
            expected_source_identity=source_identity,
        )
        source_record_outcomes = lineage.quote_outcomes
    else:
        source_record_outcomes = source_record.get("quote_outcomes")
        if not isinstance(source_record_outcomes, dict):
            raise ValueError("sealed outcome source record quote_outcomes must be an object")

    sealed_outcomes = results.get("quote_outcomes")
    if not isinstance(sealed_outcomes, dict):
        raise ValueError("sealed results quote_outcomes must be an object")
    if source_record_outcomes != sealed_outcomes:
        raise ValueError(
            "sealed results quote_outcomes do not match hashed source record outcomes"
        )
    quote_outcomes_sha256 = _canonical_json_sha256(sealed_outcomes)

    terms_reference = _text(raw, "terms_reference", context="sealed results outcome_provenance")
    retention_basis = _text(raw, "retention_basis", context="sealed results outcome_provenance")
    authority_reference = _text(raw, "authority_reference", context="sealed results outcome_provenance")

    available_at = _text(raw, "available_at", context="sealed results outcome_provenance")
    acquired_at = _text(raw, "acquired_at", context="sealed results outcome_provenance")
    verified_at = _text(raw, "verified_at", context="sealed results outcome_provenance")
    available_dt = _timestamp(
        available_at,
        field="sealed results outcome_provenance.available_at",
    )
    acquired_dt = _timestamp(
        acquired_at,
        field="sealed results outcome_provenance.acquired_at",
    )
    verified_dt = _timestamp(
        verified_at,
        field="sealed results outcome_provenance.verified_at",
    )
    if lineage is not None:
        source_recorded_dt = _timestamp(
            lineage.recorded_at,
            field="sealed outcome source record.recorded_at",
        )
        if available_dt < source_recorded_dt:
            raise ValueError(
                "sealed results outcome_provenance.available_at must not precede source record recorded_at"
            )
    if reveal_dt < available_dt:
        raise ValueError(
            "outcome_reveal_after must not precede sealed outcome source availability"
        )
    if acquired_dt < available_dt:
        raise ValueError(
            "sealed results outcome_provenance.acquired_at must not precede available_at"
        )
    if verified_dt < acquired_dt:
        raise ValueError(
            "sealed results outcome_provenance.verified_at must not precede acquired_at"
        )
    if imported_dt < acquired_dt:
        raise ValueError("imported_at must not precede sealed outcome acquisition")
    if imported_dt < verified_dt:
        raise ValueError("imported_at must not precede sealed outcome provenance verification")

    policy = _text(raw, "redistribution_policy", context="sealed results outcome_provenance")
    if policy not in _ALLOWED_REDISTRIBUTION:
        raise ValueError(
            "sealed results outcome_provenance.redistribution_policy must be prohibited, internal_only, or permitted"
        )
    redistribution_verified = raw.get("redistribution_verified")
    if not isinstance(redistribution_verified, bool):
        raise ValueError(
            "sealed results outcome_provenance.redistribution_verified must be boolean"
        )
    if policy == "permitted" and redistribution_verified is not True:
        raise ValueError(
            "sealed results outcome_provenance redistribution_policy=permitted requires redistribution_verified=true"
        )

    normalized = {
        **raw,
        "source_identity": source_identity,
        "source_record_file": source_record_file,
        "source_record_sha256": source_record_sha256,
        "quote_outcomes_sha256": quote_outcomes_sha256,
        "terms_reference": terms_reference,
        "retention_basis": retention_basis,
        "authority_reference": authority_reference,
        "available_at": available_at,
        "acquired_at": acquired_at,
        "verified_at": verified_at,
        "redistribution_policy": policy,
        "redistribution_verified": redistribution_verified,
        "licensing_or_retention_verified": True,
    }
    if lineage is not None:
        normalized.update(
            {
                "source_record_id": lineage.record_id,
                "source_record_revision_id": lineage.revision_id,
                "source_record_revision": lineage.revision,
                "source_record_revision_kind": lineage.revision_kind,
                "source_record_recorded_at": lineage.recorded_at,
                "source_record_predecessor_sha256": lineage.predecessor_record_sha256,
                "source_record_supersedes_revision_id": lineage.supersedes_revision_id,
                "source_record_lineage_root_sha256": lineage.lineage_root_sha256,
                "source_record_lineage_root_revision_id": lineage.lineage_root_revision_id,
                "source_record_lineage_depth": lineage.lineage_depth,
                "source_record_lineage": [
                    {
                        "revision_id": revision.revision_id,
                        "revision": revision.revision,
                        "revision_kind": revision.revision_kind,
                        "recorded_at": revision.recorded_at,
                        "record_sha256": revision.record_sha256,
                        "predecessor_record_sha256": revision.predecessor_record_sha256,
                        "supersedes_revision_id": revision.supersedes_revision_id,
                        "correction_reason": revision.correction_reason,
                    }
                    for revision in lineage.revisions
                ],
                "source_record_lineage_verified": True,
            }
        )
    return normalized


def _snapshot(
    market_path: Path,
    evidence_path: Path,
    *,
    expected_terms_reference: str,
) -> tuple[
    list[tuple[MarketEvent, dict[str, Any]]],
    dict[str, Any],
    dict[str, Any] | None,
]:
    evidence_bytes = _read_bytes(evidence_path, context="snapshot evidence")
    evidence = _json_object_bytes(
        evidence_bytes,
        path=evidence_path,
        context="snapshot evidence",
    )
    evidence["_artifact_sha256"] = _sha256_bytes(evidence_bytes)
    if int(evidence.get("schema_version", 0)) != 1:
        raise ValueError("snapshot evidence schema_version must be 1")
    if evidence.get("kind") != _SNAPSHOT_KIND:
        raise ValueError(f"snapshot evidence kind must be {_SNAPSHOT_KIND}")
    raw_sport_key = evidence.get("sport_key")
    try:
        sport_key = _canonical_sport_key(raw_sport_key)
    except ValueError as exc:
        raise ValueError(
            "snapshot evidence sport_key must be one canonical Parlay sport identity"
        ) from exc
    if evidence.get("has_data") is not True:
        raise ValueError("snapshot evidence must prove has_data=true")
    if evidence.get("point_in_time_snapshot_contains_odds") is not True:
        raise ValueError("snapshot evidence must prove point_in_time_snapshot_contains_odds=true")
    if evidence.get("point_in_time_odds_market_coverage_verified") is not False:
        raise ValueError("snapshot evidence must keep point_in_time_odds_market_coverage_verified=false")
    if evidence.get("historical_window_market_coverage_verified") is not False:
        raise ValueError("snapshot evidence must keep historical_window_market_coverage_verified=false")
    if evidence.get("sealed_outcomes_present") is not False:
        raise ValueError("snapshot evidence must keep sealed_outcomes_present=false")
    if evidence.get("replay_corpus_ready") is not False:
        raise ValueError("snapshot evidence must keep replay_corpus_ready=false before assembly")
    if evidence.get("real_money_execution") is not False:
        raise ValueError("snapshot evidence must keep real_money_execution=false")
    if evidence.get("terms_reference") != expected_terms_reference:
        raise ValueError("snapshot evidence terms_reference does not match governance proof")
    provider = _text(evidence, "provider", context="snapshot evidence")
    if provider != "parlayapi":
        raise ValueError("snapshot evidence provider must be parlayapi")
    canonical_source_id = f"parlayapi:{sport_key}"

    expected_sha = _digest(evidence, "market_sha256", context="snapshot evidence")
    response_sha256 = _digest(
        evidence,
        "response_sha256",
        context="snapshot evidence",
    )
    market_bytes = _read_bytes(market_path, context="snapshot market")
    if _sha256_bytes(market_bytes) != expected_sha:
        raise ValueError("snapshot market_sha256 does not match captured market file")

    requested_at = _text(evidence, "requested_at", context="snapshot evidence")
    snapshot_at = _text(evidence, "snapshot_at", context="snapshot evidence")
    captured_at = _text(evidence, "captured_at", context="snapshot evidence")
    requested_dt = _timestamp(requested_at, field="snapshot evidence.requested_at")
    snapshot_dt = _timestamp(snapshot_at, field="snapshot evidence.snapshot_at")
    captured_dt = _timestamp(captured_at, field="snapshot evidence.captured_at")
    if snapshot_dt > requested_dt:
        raise ValueError("snapshot evidence snapshot_at is after requested_at")
    if captured_dt < requested_dt:
        raise ValueError("snapshot evidence captured_at is before requested_at")
    if captured_dt < snapshot_dt:
        raise ValueError("snapshot evidence captured_at is before snapshot_at")

    try:
        market_text = market_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"snapshot market is not readable UTF-8: {market_path}") from exc

    rows: list[tuple[MarketEvent, dict[str, Any]]] = []
    for line_number, line in enumerate(market_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"snapshot market line {line_number} is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"snapshot market line {line_number} must be an object")
        event = MarketEvent.from_dict(raw)
        if event.sport is not None and event.sport != sport_key:
            raise ValueError(
                "snapshot event sport contradicts SHA-bound snapshot evidence sport_key"
            )
        if event.source_id != canonical_source_id:
            raise ValueError("snapshot evidence provider/sport does not match captured market source_id")
        if event.source_ts is None or not event.ingest_ts:
            raise ValueError("historical snapshot rows require explicit source_ts and ingest_ts")
        source_dt = _timestamp(event.source_ts, field="historical snapshot source_ts")
        observed_dt = _timestamp(event.observed_ts, field="historical snapshot observed_ts")
        ingest_dt = _timestamp(event.ingest_ts, field="historical snapshot ingest_ts")
        if source_dt > observed_dt:
            raise ValueError("historical snapshot source_ts is after observed_ts")
        if observed_dt > snapshot_dt:
            raise ValueError("historical snapshot observed_ts is after snapshot boundary")
        if ingest_dt < observed_dt:
            raise ValueError("historical snapshot ingest_ts is before observed_ts")
        rows.append((event, raw))

    quote_count = evidence.get("quote_count")
    if type(quote_count) is not int or quote_count != len(rows):
        raise ValueError("snapshot evidence quote_count does not match captured market rows")
    fallback_count = evidence.get("snapshot_timestamp_fallback_count", 0)
    if (
        type(fallback_count) is not int
        or fallback_count < 0
        or fallback_count > quote_count
    ):
        raise ValueError(
            "snapshot evidence snapshot_timestamp_fallback_count must be an exact bounded integer"
        )
    if not rows:
        raise ValueError("historical snapshot market file is empty")
    acquisition_provenance = _snapshot_acquisition_provenance(
        evidence,
        sport_key=sport_key,
        requested_at=requested_at,
        response_sha256=response_sha256,
    )
    return rows, evidence, acquisition_provenance


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def assemble_historical_corpus(
    snapshot_pairs: Sequence[tuple[str | Path, str | Path]],
    *,
    results_path: str | Path,
    governance_proof_path: str | Path,
    output_dir: str | Path,
    name: str,
    outcome_reveal_after: str,
    imported_at: str,
) -> HistoricalCorpusBuild:
    if not snapshot_pairs:
        raise ValueError("at least one historical snapshot pair is required")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("historical corpus name must be non-empty")

    output = Path(output_dir)
    if output.exists():
        raise ValueError("output_dir already exists; historical corpus assembly never overwrites")
    output.parent.mkdir(parents=True, exist_ok=True)

    proof_path = Path(governance_proof_path)
    binding = verify_governance_authority_binding(proof_path)
    proof = _governance_proof(proof_path, payload=binding.governance_proof_bytes)
    if proof["_artifact_sha256"] != binding.governance_proof_sha256:
        raise ValueError("governance proof parsed bytes do not match verified authority binding")
    imported_dt = _timestamp(imported_at, field="imported_at")
    governance_verified_dt = _timestamp(
        proof["verified_at"],
        field="governance proof.verified_at",
    )
    if imported_dt < governance_verified_dt:
        raise ValueError("imported_at must not precede governance proof verification")
    authorization_valid_through_dt = _timestamp(
        proof["authorization_valid_through"],
        field="governance proof.authorization_valid_through",
    )
    if imported_dt > authorization_valid_through_dt:
        raise ValueError("imported_at exceeds governance proof.authorization_valid_through")
    reveal_dt = _timestamp(outcome_reveal_after, field="outcome_reveal_after")

    events: list[tuple[MarketEvent, dict[str, Any]]] = []
    seen_dedupe: set[str] = set()
    evidence_rows: list[dict[str, Any]] = []
    captures: list[tuple[datetime, str]] = []

    for market_value, evidence_value in snapshot_pairs:
        market_path = Path(market_value)
        evidence_path = Path(evidence_value)
        rows, evidence, acquisition_provenance = _snapshot(
            market_path,
            evidence_path,
            expected_terms_reference=proof["terms_reference"],
        )
        evidence_sha256 = evidence.get("_artifact_sha256")
        if (
            not isinstance(evidence_sha256, str)
            or len(evidence_sha256) != 64
            or any(character not in "0123456789abcdef" for character in evidence_sha256)
        ):
            raise ValueError(
                "snapshot evidence artifact digest was not preserved from its verified byte snapshot"
            )
        for event, raw in rows:
            if event.dedupe_key in seen_dedupe:
                raise ValueError("historical snapshots contain duplicate canonical source event identity")
            seen_dedupe.add(event.dedupe_key)
            events.append((event, raw))
        captured_at = str(evidence["captured_at"])
        captures.append((_timestamp(captured_at, field="snapshot captured_at"), captured_at))
        evidence_rows.append(
            {
                "evidence_sha256": evidence_sha256,
                "market_sha256": str(evidence["market_sha256"]),
                "response_sha256": str(evidence["response_sha256"]),
                "requested_at": str(evidence["requested_at"]),
                "snapshot_at": str(evidence["snapshot_at"]),
                "captured_at": captured_at,
                "sport_key": str(evidence["sport_key"]),
                "source_id": f"parlayapi:{evidence['sport_key']}",
                "product_kind": "POINT_IN_TIME_ODDS",
                "causal_classification": "RETROSPECTIVE_POINT_IN_TIME_PRICE",
                "response_digest_semantics": "canonical_json_payload_not_raw_response_bytes",
                "acquisition_provenance_sha256": (
                    acquisition_provenance["acquisition_sha256"]
                    if acquisition_provenance is not None
                    else None
                ),
                "acquisition_provenance": (
                    acquisition_provenance["provenance"]
                    if acquisition_provenance is not None
                    else None
                ),
                "quote_count": int(evidence["quote_count"]),
                "snapshot_timestamp_fallback_count": int(
                    evidence.get("snapshot_timestamp_fallback_count", 0)
                ),
            }
        )

    governance_proof_sha256 = binding.governance_proof_sha256
    if proof["_artifact_sha256"] != governance_proof_sha256:
        raise ValueError("governance proof artifact digest changed after authority verification")

    earliest_capture_dt, _ = min(captures, key=lambda item: item[0])
    latest_capture_dt, latest_capture = max(captures, key=lambda item: item[0])
    if imported_dt < latest_capture_dt:
        raise ValueError("imported_at must not precede completion of snapshot acquisition")

    retention_expires_dt = _timestamp(
        proof["retention_expires_at"],
        field="governance proof.retention_expires_at",
    )
    if retention_expires_dt < latest_capture_dt:
        raise ValueError("governance proof.retention_expires_at must not precede snapshot capture")
    if imported_dt > retention_expires_dt:
        raise ValueError("imported_at exceeds governance proof.retention_expires_at")
    if proof["terms_reference"].rstrip("/") == _PARLAY_TERMS_REFERENCE:
        standard_ceiling = earliest_capture_dt + _PARLAY_STANDARD_RETENTION_CEILING
        if (
            retention_expires_dt > standard_ceiling
            and proof["retention_extension_authority_reference"] is None
        ):
            raise ValueError(
                "ParlayAPI retention beyond 90 days from earliest capture requires "
                "retention_extension_authority_reference"
            )

    events.sort(
        key=lambda item: (
            item[0].observed_ts,
            item[0].sequence,
            item[0].source_id,
            item[0].event_id,
            item[0].market_id,
            item[0].selection_id,
        )
    )
    coverage_start = min(event.observed_ts for event, _ in events)
    coverage_end = max(event.observed_ts for event, _ in events)
    if reveal_dt < _timestamp(coverage_end, field="coverage end"):
        raise ValueError("outcome_reveal_after must be at or after observed market coverage end")
    source_ids = tuple(sorted({event.source_id for event, _ in events}))
    if source_ids != proof["source_ids"]:
        raise ValueError("governance proof.source_ids do not match historical snapshot source_ids")
    event_sports = {event.sport for event, _ in events}
    snapshot_sports = {str(row["sport_key"]) for row in evidence_rows}
    if event_sports == {None}:
        # Preserve only the pre-existing table-tennis legacy lineage. Once a
        # provider sport other than table_tennis is selected, event-level sport
        # truth is mandatory; otherwise schema-v2 would silently relabel it.
        if snapshot_sports != {"table_tennis"}:
            raise ValueError(
                "non-table-tennis historical snapshots require explicit event sport identity"
            )
        corpus_schema_version = 2
        manifest_sport = "table_tennis"
    elif None not in event_sports and len(event_sports) == 1:
        explicit_sport = next(iter(event_sports))
        assert explicit_sport is not None
        try:
            manifest_sport = _canonical_sport_key(explicit_sport)
        except ValueError as exc:
            raise ValueError(
                "historical snapshot events must use one canonical explicit Parlay sport identity"
            ) from exc
        corpus_schema_version = 3
    else:
        raise ValueError(
            "historical snapshots mix legacy/unproven or multiple explicit sport identities"
        )
    market_types = tuple(sorted({event.market_type.value for event, _ in events}))
    market_content_sha256 = _market_semantic_content_sha256(events)
    upstream_bookmaker_keys = tuple(
        sorted(
            {
                bookmaker
                for event, _ in events
                if (
                    bookmaker := str(event.metadata.get("bookmaker_key") or "").strip()
                )
            }
        )
    )

    results_path_obj = Path(results_path)
    results = _json_object(results_path_obj, context="sealed results")
    results_schema_version = results.get("schema_version")
    if type(results_schema_version) is not int or results_schema_version != 1:
        raise ValueError("sealed results schema_version must be exact integer 1")
    outcomes = results.get("quote_outcomes")
    if not isinstance(outcomes, dict):
        raise ValueError("sealed results quote_outcomes must be an object")
    invalid_outcomes = sorted(
        str(key)
        for key, value in outcomes.items()
        if not isinstance(value, str) or value not in _ALLOWED_OUTCOMES
    )
    if invalid_outcomes:
        raise ValueError("sealed results contain unsupported outcome; allowed values are win, loss, void")
    outcome_provenance = _outcome_provenance(
        results,
        source_root=results_path_obj.parent,
        reveal_dt=reveal_dt,
        imported_dt=imported_dt,
    )
    declared_reveal = results.get("outcome_reveal_after")
    if declared_reveal is not None:
        declared_reveal_dt = _timestamp(
            declared_reveal,
            field="sealed results.outcome_reveal_after",
        )
        if declared_reveal_dt != reveal_dt:
            raise ValueError(
                "sealed results outcome_reveal_after conflicts with requested causal reveal boundary"
            )
    results = {
        **results,
        "outcome_provenance": outcome_provenance,
        "outcome_reveal_after": outcome_reveal_after,
    }
    quote_keys = {event.quote_key for event, _ in events}
    outcome_keys = {str(key) for key in outcomes}
    missing_outcomes = sorted(quote_keys - outcome_keys)
    if missing_outcomes:
        raise ValueError("sealed results are missing quote outcomes for historical market corpus")
    unknown_outcomes = sorted(outcome_keys - quote_keys)
    if unknown_outcomes:
        raise ValueError("sealed results reference quote keys absent from historical market corpus")

    effective_redistribution_policy = min(
        (str(proof["redistribution_policy"]), str(outcome_provenance["redistribution_policy"])),
        key=lambda policy: _REDISTRIBUTION_RANK[policy],
    )

    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.build-", dir=str(output.parent)))
    try:
        market_destination = staging / "market.jsonl"
        results_destination = staging / "results.json"
        manifest_path = staging / "manifest.json"
        governance_proof_destination = staging / "governance-proof.json"
        authority_record_destination = staging / binding.authority_record_file
        if authority_record_destination.name in {
            market_destination.name,
            results_destination.name,
            manifest_path.name,
            governance_proof_destination.name,
        }:
            raise ValueError("governance authority artifact file collides with reserved corpus member")

        governance_proof_destination.write_bytes(binding.governance_proof_bytes)
        authority_record_destination.write_bytes(binding.authority_record_bytes)
        if _sha256(governance_proof_destination) != binding.governance_proof_sha256:
            raise ValueError("persisted governance proof does not match verified digest")
        if _sha256(authority_record_destination) != binding.authority_record_sha256:
            raise ValueError("persisted governance authority record does not match verified digest")

        _write_jsonl(market_destination, (raw for _, raw in events))
        atomic_write_json(results_destination, results)
        market_sha = _sha256(market_destination)
        results_sha = _sha256(results_destination)

        outcome_evidence = {
            "source_identity": outcome_provenance["source_identity"],
            "source_record_file": outcome_provenance["source_record_file"],
            "source_record_sha256": outcome_provenance["source_record_sha256"],
            "source_record_checksum_verified": True,
            "quote_outcomes_bound_to_source_record": True,
            "quote_outcomes_sha256": outcome_provenance["quote_outcomes_sha256"],
            "source_record_redistributed": False,
            "terms_reference": outcome_provenance["terms_reference"],
            "retention_basis": outcome_provenance["retention_basis"],
            "authority_reference": outcome_provenance["authority_reference"],
            "available_at": outcome_provenance["available_at"],
            "acquired_at": outcome_provenance["acquired_at"],
            "verified_at": outcome_provenance["verified_at"],
            "licensing_or_retention_verified": True,
            "redistribution_policy": outcome_provenance["redistribution_policy"],
            "redistribution_verified": outcome_provenance["redistribution_verified"],
        }
        if outcome_provenance["schema_version"] == 2:
            if outcome_provenance.get("source_record_lineage_verified") is not True:
                raise ValueError("validated schema-v2 outcome provenance lost lineage verification")
            outcome_evidence.update(
                {
                    "source_record_id": outcome_provenance["source_record_id"],
                    "source_record_revision_id": outcome_provenance[
                        "source_record_revision_id"
                    ],
                    "source_record_revision": outcome_provenance["source_record_revision"],
                    "source_record_revision_kind": outcome_provenance[
                        "source_record_revision_kind"
                    ],
                    "source_record_recorded_at": outcome_provenance[
                        "source_record_recorded_at"
                    ],
                    "source_record_predecessor_sha256": outcome_provenance[
                        "source_record_predecessor_sha256"
                    ],
                    "source_record_supersedes_revision_id": outcome_provenance[
                        "source_record_supersedes_revision_id"
                    ],
                    "source_record_lineage_root_sha256": outcome_provenance[
                        "source_record_lineage_root_sha256"
                    ],
                    "source_record_lineage_root_revision_id": outcome_provenance[
                        "source_record_lineage_root_revision_id"
                    ],
                    "source_record_lineage_depth": outcome_provenance[
                        "source_record_lineage_depth"
                    ],
                    "source_record_lineage": [
                        dict(revision)
                        for revision in outcome_provenance["source_record_lineage"]
                    ],
                    "source_record_lineage_verified": True,
                }
            )

        outcome_identity = _canonical_json_sha256(
            {
                "schema_version": 1,
                "kind": "parlay_historical_outcome_identity",
                "results_sha256": results_sha,
                "outcome_evidence": outcome_evidence,
            }
        )

        content_identity = _canonical_json_sha256(
            {
                "schema_version": 1,
                "kind": "parlay_historical_content_identity",
                "product_kind": "POINT_IN_TIME_ODDS",
                "sport": manifest_sport,
                "source_ids": list(source_ids),
                "market_content_sha256": market_content_sha256,
                "market_types": list(market_types),
                "upstream_bookmaker_keys": list(upstream_bookmaker_keys),
                "event_count": len(events),
            }
        )
        fully_bound_acquisition_provenance = all(
            row["acquisition_provenance"] is not None
            for row in evidence_rows
        )
        validated_acquisition_provenance_count = sum(
            row["acquisition_provenance"] is not None
            for row in evidence_rows
        )
        canonical_acquisition_rows = sorted(
            evidence_rows,
            key=lambda row: json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        acquisition_identity = _canonical_json_sha256(
            {
                "schema_version": 1,
                "kind": "parlay_historical_acquisition_identity",
                "content_identity": content_identity,
                "scope": "selected_point_in_time_snapshots_only",
                "snapshots": canonical_acquisition_rows,
            }
        )
        governance_identity = _canonical_json_sha256(
            {
                "schema_version": 1,
                "kind": "parlay_historical_governance_identity",
                "governance_proof_sha256": governance_proof_sha256,
                "authority_record_sha256": binding.authority_record_sha256,
                "source_identity": proof["source_identity"],
                "source_ids": list(proof["source_ids"]),
                "terms_reference": proof["terms_reference"],
                "retention_expires_at": proof["retention_expires_at"],
                "authorization_valid_through": proof["authorization_valid_through"],
                "redistribution_policy": proof["redistribution_policy"],
            }
        )
        qualified_corpus_identity = _canonical_json_sha256(
            {
                "schema_version": 1,
                "kind": "parlay_historical_qualified_corpus_identity",
                "content_identity": content_identity,
                "outcome_identity": outcome_identity,
                "acquisition_identity": acquisition_identity,
                "governance_identity": governance_identity,
                "causal_classification": "RETROSPECTIVE_POINT_IN_TIME_PRICE",
                "qualification_scope": "selected_point_in_time_snapshot_corpus_v1",
            }
        )

        governance = {
            "source_identity": proof["source_identity"],
            "terms_reference": proof["terms_reference"],
            "retention_basis": proof["retention_basis"],
            "retention_expires_at": proof["retention_expires_at"],
            "authorization_valid_through": proof["authorization_valid_through"],
            "retention_extension_authority_reference": proof[
                "retention_extension_authority_reference"
            ],
            "redistribution_policy": effective_redistribution_policy,
            "acquired_at": latest_capture,
            "imported_at": imported_at,
            "coverage": {
                "start_ts": coverage_start,
                "end_ts": coverage_end,
                "source_ids": list(source_ids),
                "market_types": list(market_types),
            },
            "causality": {
                "strategy_time_field": "observed_ts",
                "outcome_reveal_after": outcome_reveal_after,
            },
            "acquisition_evidence": {
                "scope": "selected_point_in_time_snapshots_only",
                "snapshot_count": len(evidence_rows),
                "validated_acquisition_provenance_count": validated_acquisition_provenance_count,
                "snapshots": evidence_rows,
                "point_in_time_snapshot_contains_odds": True,
                "historical_window_market_coverage_verified": False,
                "governance_proof_file": governance_proof_destination.name,
                "governance_proof_sha256": governance_proof_sha256,
                "authority_record_file": binding.authority_record_file,
                "authority_record_sha256": binding.authority_record_sha256,
                "licensing_or_retention_verified": True,
                "rights_source_ids": list(proof["source_ids"]),
                "authority_reference": proof["authority_reference"],
                "retention_expires_at": proof["retention_expires_at"],
                "authorization_valid_through": proof["authorization_valid_through"],
                "retention_extension_authority_reference": proof[
                    "retention_extension_authority_reference"
                ],
                "verified_at": proof["verified_at"],
                "redistribution_verified": proof["redistribution_verified"],
                "provenance": {
                    "schema_version": 1,
                    "product_kind": "POINT_IN_TIME_ODDS",
                    "causal_classification": "RETROSPECTIVE_POINT_IN_TIME_PRICE",
                    "qualification_scope": "selected_point_in_time_snapshot_corpus_v1",
                    "content_identity": content_identity,
                    "content_identity_scope": "market_snapshot_semantics_excluding_product_ingest_time",
                    "market_content_sha256": market_content_sha256,
                    "outcome_identity": outcome_identity,
                    "acquisition_identity": acquisition_identity,
                    "governance_identity": governance_identity,
                    "qualified_corpus_identity": qualified_corpus_identity,
                    "upstream_bookmaker_keys": list(upstream_bookmaker_keys),
                    "canonical_response_payload_digest_bound": True,
                    "raw_response_bytes_bound": False,
                    "normalized_request_scope_bound": fully_bound_acquisition_provenance,
                    "provider_response_metadata_bound": fully_bound_acquisition_provenance,
                    "validated_acquisition_provenance_count": validated_acquisition_provenance_count,
                    "prospective_authority": False,
                    "raw_redistribution_authority": proof["redistribution_verified"] is True
                    and proof["redistribution_policy"] == "permitted",
                },
            },
            "outcome_evidence": outcome_evidence,
        }
        manifest = {
            "schema_version": corpus_schema_version,
            "dataset_kind": "historical",
            "name": name.strip(),
            "sport": manifest_sport,
            "market_file": market_destination.name,
            "results_file": results_destination.name,
            "market_sha256": market_sha,
            "results_sha256": results_sha,
            "governance": governance,
        }
        atomic_write_json(manifest_path, manifest)

        staged = load_dataset(staging)
        if staged.import_identity is None:
            raise ValueError("assembled historical corpus did not produce an import identity")
        manifest["import_identity"] = staged.import_identity
        atomic_write_json(manifest_path, manifest)
        load_dataset(staging)

        os.replace(staging, output)
        verified = load_dataset(output)
        if verified.import_identity is None:
            raise ValueError("final historical corpus lost its import identity")
        return HistoricalCorpusBuild(
            root=str(output),
            import_identity=verified.import_identity,
            snapshot_count=len(evidence_rows),
            event_count=len(events),
            market_sha256=verified.market_sha256,
            results_sha256=verified.results_sha256,
            coverage_start_ts=coverage_start,
            coverage_end_ts=coverage_end,
            source_ids=source_ids,
            market_types=market_types,
            redistribution_policy=effective_redistribution_policy,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-build-historical-corpus",
        description=(
            "Assemble selected authenticated Parlay historical snapshots for one exact sport and separate sealed "
            "outcomes into a governed replay corpus; explicit-sport captures use schema-v3 while "
            "legacy captures remain schema-v2 without inventing event-level sport truth."
        ),
    )
    parser.add_argument(
        "--snapshot",
        action="append",
        nargs=2,
        metavar=("MARKET_JSONL", "EVIDENCE_JSON"),
        required=True,
        help="captured market JSONL plus matching machine evidence; repeat for each snapshot",
    )
    parser.add_argument(
        "--results",
        type=Path,
        required=True,
        help=(
            "separate sealed results JSON with outcome provenance naming a sibling source artifact "
            "whose SHA-256 and normalized outcome labels are verified during assembly; provenance "
            "schema 2 additionally requires verified append-only correction lineage"
        ),
    )
    parser.add_argument(
        "--governance-proof",
        type=Path,
        required=True,
        help="external non-secret rights/retention verification JSON",
    )
    parser.add_argument("--output", type=Path, required=True, help="new output dataset directory; never overwritten")
    parser.add_argument("--name", required=True, help="human-readable corpus name")
    parser.add_argument("--outcome-reveal-after", required=True, help="ISO-8601 causal outcome reveal timestamp")
    parser.add_argument("--imported-at", required=True, help="explicit ISO-8601 import timestamp")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = assemble_historical_corpus(
            [(Path(market), Path(evidence)) for market, evidence in args.snapshot],
            results_path=args.results,
            governance_proof_path=args.governance_proof,
            output_dir=args.output,
            name=args.name,
            outcome_reveal_after=args.outcome_reveal_after,
            imported_at=args.imported_at,
        )
    except (OSError, ValueError) as exc:
        print(f"historical_corpus=FAIL_CLOSED error={exc}")
        return 3

    print(
        f"historical_corpus=ASSEMBLED snapshots={result.snapshot_count} events={result.event_count} "
        f"coverage={result.coverage_start_ts}..{result.coverage_end_ts}"
    )
    print(f"historical_import_identity={result.import_identity}")
    print(
        f"market_sha256={result.market_sha256} results_sha256={result.results_sha256} "
        f"redistribution_policy={result.redistribution_policy}"
    )
    print(
        "scope=selected_point_in_time_snapshots_only historical_window_market_coverage_verified=false "
        "licensing_or_retention_verified=true outcome_source_checksum_verified=true "
        "outcome_labels_source_bound=true profitability_claim=false real_money_execution=false"
    )
    print(f"dataset={result.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())