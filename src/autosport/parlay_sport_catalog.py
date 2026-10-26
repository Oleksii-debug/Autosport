"""Observational ParlayAPI active-sport catalog evidence.

This module models only the bytes supplied as a purported ``GET /v1/sports``
response.  It deliberately does not perform network access, authenticate the
provider origin, infer odds/live/historical coverage, authorize provider writes,
or widen real-money execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from hashlib import sha256
import json
import re
from typing import Any


_SCHEMA_VERSION = 1
_SPORT_KEY_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_EXPECTED_ROW_FIELDS = frozenset(
    {"key", "group", "title", "description", "active", "has_outrights"}
)


class ParlaySportCatalogError(ValueError):
    """Raised when catalog evidence is malformed, ambiguous, or non-canonical."""


class ParlaySportCatalogStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN_MISSING = "unknown_missing"
    UNKNOWN_STALE = "unknown_stale"


class ParlaySportCapabilityState(str, Enum):
    UNKNOWN = "unknown"


def _text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ParlaySportCatalogError(f"{field} must be a string")
    if "\x00" in value:
        raise ParlaySportCatalogError(f"{field} must not contain NUL")
    if value != value.strip():
        raise ParlaySportCatalogError(f"{field} must be trimmed")
    if not allow_empty and not value:
        raise ParlaySportCatalogError(f"{field} must be non-empty")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ParlaySportCatalogError(f"{field} must be valid UTF-8 text") from exc
    return value


def _sport_key(value: object, field: str = "sport key") -> str:
    text = _text(value, field)
    if not _SPORT_KEY_RE.fullmatch(text):
        raise ParlaySportCatalogError(
            f"{field} must be lowercase ASCII alphanumeric segments separated by underscores"
        )
    return text


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ParlaySportCatalogError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ParlaySportCatalogError(f"{field} must include a timezone offset")
    return parsed


def _sha256_hex(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ParlaySportCatalogError(
            f"{field} must be lowercase 64-character SHA-256 hex"
        )
    return text


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ParlaySportCatalogError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ParlaySportCatalogError("catalog value is not canonical JSON") from exc


@dataclass(frozen=True, slots=True, order=True)
class ParlaySportCatalogEntry:
    key: str
    group: str
    title: str
    description: str
    active: bool
    has_outrights: bool

    def __post_init__(self) -> None:
        _sport_key(self.key)
        _text(self.group, "group")
        _text(self.title, "title")
        _text(self.description, "description", allow_empty=True)
        if type(self.active) is not bool:
            raise ParlaySportCatalogError("active must be an exact bool")
        if type(self.has_outrights) is not bool:
            raise ParlaySportCatalogError("has_outrights must be an exact bool")

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "description": self.description,
            "group": self.group,
            "has_outrights": self.has_outrights,
            "key": self.key,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class ParlaySportCatalogLookup:
    sport_key: str
    status: ParlaySportCatalogStatus
    evidence_id: str
    catalog_id: str
    entry: ParlaySportCatalogEntry | None
    provider_origin_verified: bool = False
    odds_supported: ParlaySportCapabilityState = ParlaySportCapabilityState.UNKNOWN
    live_supported: ParlaySportCapabilityState = ParlaySportCapabilityState.UNKNOWN
    historical_supported: ParlaySportCapabilityState = ParlaySportCapabilityState.UNKNOWN
    provider_write_authorized: bool = False
    real_money_execution_authorized: bool = False

    def __post_init__(self) -> None:
        _sport_key(self.sport_key)
        if type(self.status) is not ParlaySportCatalogStatus:
            raise ParlaySportCatalogError("status must be ParlaySportCatalogStatus")
        _sha256_hex(self.evidence_id, "evidence_id")
        _sha256_hex(self.catalog_id, "catalog_id")
        if self.entry is not None and type(self.entry) is not ParlaySportCatalogEntry:
            raise ParlaySportCatalogError("entry must be ParlaySportCatalogEntry or None")
        if self.entry is not None and self.entry.key != self.sport_key:
            raise ParlaySportCatalogError("lookup entry does not match sport_key")
        if self.status in {
            ParlaySportCatalogStatus.ACTIVE,
            ParlaySportCatalogStatus.INACTIVE,
        } and self.entry is None:
            raise ParlaySportCatalogError("known catalog status requires matching entry")
        if self.status in {
            ParlaySportCatalogStatus.UNKNOWN_MISSING,
            ParlaySportCatalogStatus.UNKNOWN_STALE,
        } and self.entry is not None:
            raise ParlaySportCatalogError("unknown catalog status must not expose an entry")
        if self.provider_origin_verified is not False:
            raise ParlaySportCatalogError("catalog bytes do not verify provider origin")
        if self.odds_supported is not ParlaySportCapabilityState.UNKNOWN:
            raise ParlaySportCatalogError("catalog cannot prove odds support")
        if self.live_supported is not ParlaySportCapabilityState.UNKNOWN:
            raise ParlaySportCatalogError("catalog cannot prove live support")
        if self.historical_supported is not ParlaySportCapabilityState.UNKNOWN:
            raise ParlaySportCatalogError("catalog cannot prove historical support")
        if self.provider_write_authorized is not False:
            raise ParlaySportCatalogError("catalog cannot authorize provider writes")
        if self.real_money_execution_authorized is not False:
            raise ParlaySportCatalogError("catalog cannot authorize real-money execution")

    @property
    def is_observed_active(self) -> bool:
        return self.status is ParlaySportCatalogStatus.ACTIVE

    @property
    def eligible_for_product_admission(self) -> bool:
        """Catalog observation alone is never sufficient product admission evidence."""

        return False


@dataclass(frozen=True, slots=True)
class ParlayActiveSportCatalogEvidence:
    observed_at: str
    source_ref: str
    raw_payload_sha256: str
    canonical_payload_sha256: str
    entries: tuple[ParlaySportCatalogEntry, ...]
    provider_origin_verified: bool = False
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        _timestamp(self.observed_at, "observed_at")
        _text(self.source_ref, "source_ref")
        _sha256_hex(self.raw_payload_sha256, "raw_payload_sha256")
        _sha256_hex(self.canonical_payload_sha256, "canonical_payload_sha256")
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise ParlaySportCatalogError("schema_version must be exactly 1")
        if self.provider_origin_verified is not False:
            raise ParlaySportCatalogError(
                "parser cannot assert provider_origin_verified from supplied bytes"
            )
        if type(self.entries) is not tuple:
            raise ParlaySportCatalogError("entries must be a tuple")
        if any(type(entry) is not ParlaySportCatalogEntry for entry in self.entries):
            raise ParlaySportCatalogError(
                "entries must contain only ParlaySportCatalogEntry values"
            )
        keys = [entry.key for entry in self.entries]
        if keys != sorted(keys):
            raise ParlaySportCatalogError("entries must be sorted by exact sport key")
        if len(set(keys)) != len(keys):
            raise ParlaySportCatalogError("duplicate sport key")
        expected_canonical = sha256(
            _canonical_json_bytes([entry.to_canonical_dict() for entry in self.entries])
        ).hexdigest()
        if expected_canonical != self.canonical_payload_sha256:
            raise ParlaySportCatalogError(
                "canonical_payload_sha256 does not match canonical catalog semantics"
            )

    @property
    def catalog_id(self) -> str:
        """Order-independent semantic identity of the observed catalog rows."""

        return self.canonical_payload_sha256

    @property
    def catalog_revision(self) -> str:
        return self.canonical_payload_sha256

    @property
    def evidence_id(self) -> str:
        payload = {
            "canonical_payload_sha256": self.canonical_payload_sha256,
            "observed_at": self.observed_at,
            "provider_origin_verified": self.provider_origin_verified,
            "raw_payload_sha256": self.raw_payload_sha256,
            "schema_version": self.schema_version,
            "source_ref": self.source_ref,
        }
        return sha256(_canonical_json_bytes(payload)).hexdigest()

    def lookup(
        self,
        sport_key: str,
        *,
        as_of: str,
        max_age: timedelta,
    ) -> ParlaySportCatalogLookup:
        key = _sport_key(sport_key)
        as_of_dt = _timestamp(as_of, "as_of")
        observed_dt = _timestamp(self.observed_at, "observed_at")
        if not isinstance(max_age, timedelta) or max_age < timedelta(0):
            raise ParlaySportCatalogError("max_age must be a non-negative timedelta")
        if observed_dt > as_of_dt or as_of_dt - observed_dt > max_age:
            return ParlaySportCatalogLookup(
                sport_key=key,
                status=ParlaySportCatalogStatus.UNKNOWN_STALE,
                evidence_id=self.evidence_id,
                catalog_id=self.catalog_id,
                entry=None,
            )
        for entry in self.entries:
            if entry.key == key:
                return ParlaySportCatalogLookup(
                    sport_key=key,
                    status=(
                        ParlaySportCatalogStatus.ACTIVE
                        if entry.active
                        else ParlaySportCatalogStatus.INACTIVE
                    ),
                    evidence_id=self.evidence_id,
                    catalog_id=self.catalog_id,
                    entry=entry,
                )
        return ParlaySportCatalogLookup(
            sport_key=key,
            status=ParlaySportCatalogStatus.UNKNOWN_MISSING,
            evidence_id=self.evidence_id,
            catalog_id=self.catalog_id,
            entry=None,
        )


def parse_parlay_sport_catalog(
    payload: bytes,
    *,
    observed_at: str,
    source_ref: str,
) -> ParlayActiveSportCatalogEvidence:
    """Parse supplied catalog bytes into immutable observational evidence.

    ``source_ref`` is descriptive caller metadata only.  This function does not perform
    acquisition and therefore cannot prove that ``payload`` came from ParlayAPI.
    """

    if type(payload) is not bytes:
        raise ParlaySportCatalogError("payload must be exact bytes")
    _timestamp(observed_at, "observed_at")
    _text(source_ref, "source_ref")
    raw_sha = sha256(payload).hexdigest()
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ParlaySportCatalogError("payload must be valid UTF-8 JSON") from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ParlaySportCatalogError(f"non-finite JSON number {value!r}")
            ),
        )
    except ParlaySportCatalogError:
        raise
    except json.JSONDecodeError as exc:
        raise ParlaySportCatalogError("payload must be valid JSON") from exc
    if type(raw) is not list:
        raise ParlaySportCatalogError("catalog payload must be a JSON array")

    parsed: list[ParlaySportCatalogEntry] = []
    seen: set[str] = set()
    for index, row in enumerate(raw):
        if type(row) is not dict or frozenset(row) != _EXPECTED_ROW_FIELDS:
            raise ParlaySportCatalogError(
                f"catalog row {index} must contain exactly the schema-v1 fields"
            )
        entry = ParlaySportCatalogEntry(
            key=_sport_key(row["key"], f"catalog row {index} key"),
            group=_text(row["group"], f"catalog row {index} group"),
            title=_text(row["title"], f"catalog row {index} title"),
            description=_text(
                row["description"], f"catalog row {index} description", allow_empty=True
            ),
            active=row["active"],
            has_outrights=row["has_outrights"],
        )
        if entry.key in seen:
            raise ParlaySportCatalogError(f"duplicate sport key {entry.key!r}")
        seen.add(entry.key)
        parsed.append(entry)

    entries = tuple(sorted(parsed, key=lambda item: item.key))
    canonical_sha = sha256(
        _canonical_json_bytes([entry.to_canonical_dict() for entry in entries])
    ).hexdigest()
    return ParlayActiveSportCatalogEvidence(
        observed_at=observed_at,
        source_ref=source_ref,
        raw_payload_sha256=raw_sha,
        canonical_payload_sha256=canonical_sha,
        entries=entries,
    )
