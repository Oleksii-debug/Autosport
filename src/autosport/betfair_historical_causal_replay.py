from __future__ import annotations

import bz2
import gzip
import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable


_SCOPE = "RESEARCH_BACKTEST_ONLY"


class BetfairHistoricalReplayError(ValueError):
    pass


class HistoricalFieldUnavailableError(BetfairHistoricalReplayError):
    pass


class HistoricalPackageTier(str, Enum):
    BASIC = "BASIC"
    ADVANCED = "ADVANCED"
    PRO = "PRO"


class HistoricalRepresentation(str, Enum):
    MARKET = "M"
    EVENT = "E"


class HistoricalCompression(str, Enum):
    PLAIN = "PLAIN"
    BZ2 = "BZ2"
    GZIP = "GZIP"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_obj(value: Any) -> str:
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BetfairHistoricalReplayError(f"{field} must be a non-empty string")
    return value.strip()


def _digest(value: str, field: str) -> str:
    value = _text(value, field)
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise BetfairHistoricalReplayError(f"{field} must be lowercase SHA-256")
    return value


def _enum(value: Any, cls: type[Enum], field: str) -> Enum:
    try:
        return value if isinstance(value, cls) else cls(value)
    except (TypeError, ValueError) as exc:
        raise BetfairHistoricalReplayError(f"invalid {field}") from exc


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise BetfairHistoricalReplayError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _constant(value: str) -> None:
    raise BetfairHistoricalReplayError(f"non-finite JSON number: {value}")


def _require_finite_json_numbers(value: Any) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BetfairHistoricalReplayError("non-finite JSON number")
        return
    if isinstance(value, dict):
        for child in value.values():
            _require_finite_json_numbers(child)
        return
    if isinstance(value, list):
        for child in value:
            _require_finite_json_numbers(child)


def _payload(line: bytes, line_number: int) -> dict[str, Any]:
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BetfairHistoricalReplayError(f"line {line_number} is not UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except json.JSONDecodeError as exc:
        raise BetfairHistoricalReplayError(f"line {line_number} is not JSON") from exc
    if not isinstance(value, dict):
        raise BetfairHistoricalReplayError(f"line {line_number} must be a JSON object")
    _require_finite_json_numbers(value)
    return value


def _decoded(raw: bytes, compression: HistoricalCompression) -> bytes:
    try:
        if compression is HistoricalCompression.BZ2:
            return bz2.decompress(raw)
        if compression is HistoricalCompression.GZIP:
            return gzip.decompress(raw)
        return raw
    except (OSError, EOFError) as exc:
        raise BetfairHistoricalReplayError("historical source decompression failed") from exc


def _lines(raw: bytes) -> Iterable[tuple[int, bytes]]:
    for number, line in enumerate(raw.split(b"\n"), 1):
        if line.endswith(b"\r"):
            line = line[:-1]
        if b"\r" in line:
            raise BetfairHistoricalReplayError(f"line {number} contains stray carriage return")
        if line.strip():
            yield number, line


@dataclass(frozen=True, slots=True)
class BetfairHistoricalReplaySource:
    provider_file_identity: str
    raw_file_sha256: str
    entitlement_snapshot_sha256: str
    package_tier: HistoricalPackageTier
    representation: HistoricalRepresentation
    parser_revision: str
    compression: HistoricalCompression = HistoricalCompression.PLAIN

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_file_identity", _text(self.provider_file_identity, "provider_file_identity"))
        object.__setattr__(self, "raw_file_sha256", _digest(self.raw_file_sha256, "raw_file_sha256"))
        object.__setattr__(self, "entitlement_snapshot_sha256", _digest(self.entitlement_snapshot_sha256, "entitlement_snapshot_sha256"))
        object.__setattr__(self, "package_tier", _enum(self.package_tier, HistoricalPackageTier, "package_tier"))
        object.__setattr__(self, "representation", _enum(self.representation, HistoricalRepresentation, "representation"))
        object.__setattr__(self, "compression", _enum(self.compression, HistoricalCompression, "compression"))
        object.__setattr__(self, "parser_revision", _text(self.parser_revision, "parser_revision"))

    @property
    def source_identity(self) -> str:
        return _hash_obj({
            "domain": "autosport.betfair-historical-source.v1",
            "provider_file_identity": self.provider_file_identity,
            "raw_file_sha256": self.raw_file_sha256,
            "entitlement_snapshot_sha256": self.entitlement_snapshot_sha256,
            "package_tier": self.package_tier.value,
            "representation": self.representation.value,
            "parser_revision": self.parser_revision,
            "compression": self.compression.value,
        })

    @property
    def evidence_scope(self) -> str:
        return _SCOPE

    @property
    def lawful_provider_evidence_proven(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class BetfairHistoricalReplayRecord:
    source_identity: str
    source_ordinal: int
    provider_pt_ms: int
    line_sha256: str
    payload_sha256: str
    payload_json: str
    package_tier: HistoricalPackageTier
    representation: HistoricalRepresentation

    @property
    def evidence_scope(self) -> str:
        return _SCOPE

    @property
    def live_quote_proven(self) -> bool:
        return False

    @property
    def actual_execution_proven(self) -> bool:
        return False

    def payload(self) -> dict[str, Any]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise BetfairHistoricalReplayError("record payload is not an object")
        return value


@dataclass(frozen=True, slots=True)
class BetfairHistoricalReplayCursor:
    source_identity: str
    next_source_ordinal: int
    last_provider_pt_ms: int | None
    replay_state_sha256: str
    cursor_sha256: str

    def verify_self(self) -> None:
        _digest(self.source_identity, "cursor.source_identity")
        _digest(self.replay_state_sha256, "cursor.replay_state_sha256")
        _digest(self.cursor_sha256, "cursor.cursor_sha256")
        if isinstance(self.next_source_ordinal, bool) or self.next_source_ordinal < 1:
            raise BetfairHistoricalReplayError("cursor.next_source_ordinal must be >= 1")
        if self.last_provider_pt_ms is not None and (isinstance(self.last_provider_pt_ms, bool) or self.last_provider_pt_ms < 0):
            raise BetfairHistoricalReplayError("invalid cursor.last_provider_pt_ms")
        if self.cursor_sha256 != _cursor_hash(self.source_identity, self.next_source_ordinal, self.last_provider_pt_ms, self.replay_state_sha256):
            raise BetfairHistoricalReplayError("cursor self-digest mismatch")


@dataclass(frozen=True, slots=True)
class BetfairHistoricalReplayWindow:
    source_identity: str
    cutoff_pt_ms: int
    records: tuple[BetfairHistoricalReplayRecord, ...]
    cursor: BetfairHistoricalReplayCursor

    @property
    def evidence_scope(self) -> str:
        return _SCOPE


def _cursor_hash(source_identity: str, ordinal: int, last_pt: int | None, state: str) -> str:
    return _hash_obj({"source_identity": source_identity, "next_source_ordinal": ordinal, "last_provider_pt_ms": last_pt, "replay_state_sha256": state})


def _cursor(source_identity: str, ordinal: int, last_pt: int | None, state: str) -> BetfairHistoricalReplayCursor:
    return BetfairHistoricalReplayCursor(source_identity, ordinal, last_pt, state, _cursor_hash(source_identity, ordinal, last_pt, state))


def _initial(source_identity: str) -> str:
    return _hash_obj({"domain": "autosport.betfair-historical-causal-replay.v1", "source_identity": source_identity})


def _advance(state: str, ordinal: int, pt: int, line_sha: str, payload_sha: str) -> str:
    return _hash_obj({"previous": state, "source_ordinal": ordinal, "provider_pt_ms": pt, "line_sha256": line_sha, "payload_sha256": payload_sha})


def replay_betfair_historical_until(
    raw_bytes: bytes,
    source: BetfairHistoricalReplaySource,
    *,
    cutoff_pt_ms: int,
    resume_from: BetfairHistoricalReplayCursor | None = None,
) -> BetfairHistoricalReplayWindow:
    if not isinstance(raw_bytes, bytes):
        raise BetfairHistoricalReplayError("raw_bytes must be bytes")
    if type(source) is not BetfairHistoricalReplaySource:
        raise BetfairHistoricalReplayError("source must be exact BetfairHistoricalReplaySource")
    if isinstance(cutoff_pt_ms, bool) or not isinstance(cutoff_pt_ms, int) or cutoff_pt_ms < 0:
        raise BetfairHistoricalReplayError("cutoff_pt_ms must be non-negative integer")
    if _sha(raw_bytes) != source.raw_file_sha256:
        raise BetfairHistoricalReplayError("raw historical file SHA-256 mismatch")

    source_id = source.source_identity
    state = _initial(source_id)
    ordinal = 0
    last_pt: int | None = None
    emitted: list[BetfairHistoricalReplayRecord] = []
    resume_verified = resume_from is None
    if resume_from is not None:
        if type(resume_from) is not BetfairHistoricalReplayCursor:
            raise BetfairHistoricalReplayError("resume_from must be exact cursor")
        resume_from.verify_self()
        if resume_from.source_identity != source_id:
            raise BetfairHistoricalReplayError("cursor source identity mismatch")
        if resume_from.last_provider_pt_ms is not None and cutoff_pt_ms < resume_from.last_provider_pt_ms:
            raise BetfairHistoricalReplayError("cutoff precedes cursor time")

    for line_number, line in _lines(_decoded(raw_bytes, source.compression)):
        value = _payload(line, line_number)
        if value.get("op") != "mcm":
            continue
        candidate = ordinal + 1
        pt = value.get("pt")
        if isinstance(pt, bool) or not isinstance(pt, int) or pt < 0:
            raise BetfairHistoricalReplayError(f"line {line_number} mcm.pt must be non-negative integer")
        if last_pt is not None and pt < last_pt:
            raise BetfairHistoricalReplayError(f"provider publish time regressed at source ordinal {candidate}")
        if resume_from is not None and candidate == resume_from.next_source_ordinal:
            if state != resume_from.replay_state_sha256 or last_pt != resume_from.last_provider_pt_ms:
                raise BetfairHistoricalReplayError("cursor does not match exact replay prefix")
            resume_verified = True
        if pt > cutoff_pt_ms:
            break

        ordinal = candidate
        try:
            canonical = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except ValueError as exc:
            raise BetfairHistoricalReplayError(
                f"non-finite JSON number at source ordinal {candidate}"
            ) from exc
        line_sha = _sha(line)
        payload_sha = _sha(canonical.encode())
        state = _advance(state, ordinal, pt, line_sha, payload_sha)
        last_pt = pt
        if resume_from is None or ordinal >= resume_from.next_source_ordinal:
            emitted.append(BetfairHistoricalReplayRecord(source_id, ordinal, pt, line_sha, payload_sha, canonical, source.package_tier, source.representation))

    if resume_from is not None and not resume_verified:
        if ordinal != resume_from.next_source_ordinal - 1:
            raise BetfairHistoricalReplayError("cursor points beyond available replay records")
        if state != resume_from.replay_state_sha256 or last_pt != resume_from.last_provider_pt_ms:
            raise BetfairHistoricalReplayError("cursor does not match exact replay prefix")

    cursor = _cursor(source_id, ordinal + 1, last_pt, state)
    return BetfairHistoricalReplayWindow(source_id, cutoff_pt_ms, tuple(emitted), cursor)


def require_observed_field(record: BetfairHistoricalReplayRecord, *path: str) -> Any:
    if type(record) is not BetfairHistoricalReplayRecord or not path or any(not isinstance(p, str) or not p for p in path):
        raise BetfairHistoricalReplayError("invalid record or field path")
    value: Any = record.payload()
    for part in path:
        if not isinstance(value, dict) or part not in value:
            raise HistoricalFieldUnavailableError(f"historical field {'.'.join(path)} was not observed")
        value = value[part]
    return value


def validate_replay_campaign_sources(sources: Iterable[BetfairHistoricalReplaySource]) -> tuple[str, ...]:
    items = tuple(sources)
    if not items or any(type(item) is not BetfairHistoricalReplaySource for item in items):
        raise BetfairHistoricalReplayError("campaign requires exact replay sources")
    if len({item.representation for item in items}) != 1:
        raise BetfairHistoricalReplayError("one replay campaign cannot mix M and E representations")
    hashes = [item.raw_file_sha256 for item in items]
    if len(hashes) != len(set(hashes)):
        raise BetfairHistoricalReplayError("duplicate historical source bytes are not allowed")
    provider_ids = [item.provider_file_identity for item in items]
    if len(provider_ids) != len(set(provider_ids)):
        raise BetfairHistoricalReplayError("duplicate provider file identity is not allowed")
    return tuple(item.source_identity for item in items)


__all__ = [
    "BetfairHistoricalReplayCursor", "BetfairHistoricalReplayError", "BetfairHistoricalReplayRecord",
    "BetfairHistoricalReplaySource", "BetfairHistoricalReplayWindow", "HistoricalCompression",
    "HistoricalFieldUnavailableError", "HistoricalPackageTier", "HistoricalRepresentation",
    "replay_betfair_historical_until", "require_observed_field", "validate_replay_campaign_sources",
]
