"""Causal provider-sport to canonical-sport mapping authority.

Provider sport identifiers are opaque provider facts. Positive canonical resolution is
issued only from exact source-evidence bytes mechanically verified by this authority.
The product recording chronology is owned by this module and is not caller-injectable.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .integrity import atomic_write_json, durable_path_lock


_SCHEMA = "autosport.provider_sport_mapping"
_VERSION = 2
_EVIDENCE_SCHEMA = "autosport.provider_sport_mapping_evidence"
_EVIDENCE_VERSION = 1
_HEX = frozenset("0123456789abcdef")
_RESERVED_SPORTS = frozenset({"unknown", "mixed"})


class ProviderSportMappingError(ValueError):
    """Raised when provider-to-canonical sport evidence is invalid or ambiguous."""


def _canonical_text(name: str, value: object) -> str:
    if type(value) is not str:
        raise ProviderSportMappingError(f"{name} must be text")
    normalized = unicodedata.normalize("NFKC", value)
    if not normalized or normalized != normalized.strip() or "\x00" in normalized:
        raise ProviderSportMappingError(f"{name} must be non-empty canonical text")
    normalized.encode("utf-8")
    return normalized


def _opaque_provider_id(name: str, value: object) -> str:
    if type(value) is not str:
        raise ProviderSportMappingError(f"{name} must be text")
    if not value or value != value.strip() or "\x00" in value:
        raise ProviderSportMappingError(f"{name} must be non-empty trimmed provider identity")
    value.encode("utf-8")
    return value


def _canonical_sport(value: object) -> str:
    sport = _canonical_text("canonical_sport", value)
    if sport != sport.lower() or sport in _RESERVED_SPORTS or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in sport
    ):
        raise ProviderSportMappingError(
            "canonical_sport must be a lowercase canonical sport identity"
        )
    return sport


def _sha256(name: str, value: object) -> str:
    digest = _canonical_text(name, value)
    if len(digest) != 64 or any(character not in _HEX for character in digest):
        raise ProviderSportMappingError(f"{name} must be lowercase SHA-256 hex")
    return digest


def _instant(name: str, value: object) -> datetime:
    text = _canonical_text(name, value)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderSportMappingError(f"{name} must be ISO-8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ProviderSportMappingError(f"{name} must include a timezone")
    return result.astimezone(timezone.utc)


def _time_text(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_json_object(raw_bytes: bytes) -> dict[str, object]:
    if type(raw_bytes) is not bytes or not raw_bytes:
        raise ProviderSportMappingError("source_snapshot_bytes must be non-empty exact bytes")
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise ProviderSportMappingError("source snapshot must be UTF-8 JSON") from exc

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ProviderSportMappingError(f"source snapshot has duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            raw_text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ProviderSportMappingError(f"source snapshot has invalid JSON constant {value!r}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise ProviderSportMappingError("source snapshot is invalid JSON") from exc
    if type(parsed) is not dict:
        raise ProviderSportMappingError("source snapshot must be a JSON object")
    return parsed


@dataclass(frozen=True, slots=True)
class ProviderSportEvidence:
    provider_namespace: str
    provider_sport_id: str
    canonical_sport: str
    valid_from: str
    valid_until: str | None
    evidence_available_at: str
    source_snapshot_sha256: str

    @classmethod
    def from_exact_bytes(cls, raw_bytes: bytes) -> "ProviderSportEvidence":
        raw = _strict_json_object(raw_bytes)
        expected = {
            "schema",
            "schema_version",
            "provider_namespace",
            "provider_sport_id",
            "canonical_sport",
            "valid_from",
            "valid_until",
            "evidence_available_at",
        }
        if set(raw) != expected:
            raise ProviderSportMappingError("source snapshot evidence shape is invalid")
        if raw["schema"] != _EVIDENCE_SCHEMA or raw["schema_version"] != _EVIDENCE_VERSION:
            raise ProviderSportMappingError("unsupported provider sport mapping evidence schema")
        namespace = _canonical_text("provider_namespace", raw["provider_namespace"])
        opaque_id = _opaque_provider_id("provider_sport_id", raw["provider_sport_id"])
        canonical_sport = _canonical_sport(raw["canonical_sport"])
        valid_from = _time_text("valid_from", raw["valid_from"])
        valid_until_raw = raw["valid_until"]
        if valid_until_raw is not None:
            valid_until = _time_text("valid_until", valid_until_raw)
        else:
            valid_until = None
        available = _time_text("evidence_available_at", raw["evidence_available_at"])
        start = _instant("valid_from", valid_from)
        if valid_until is not None and _instant("valid_until", valid_until) <= start:
            raise ProviderSportMappingError("valid_until must be after valid_from")
        if _instant("evidence_available_at", available) < start:
            raise ProviderSportMappingError(
                "mapping evidence cannot be available before the mapping valid_from boundary"
            )
        return cls(
            provider_namespace=namespace,
            provider_sport_id=opaque_id,
            canonical_sport=canonical_sport,
            valid_from=valid_from,
            valid_until=valid_until,
            evidence_available_at=available,
            source_snapshot_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class ProviderSportBinding:
    provider_namespace: str
    provider_sport_id: str
    canonical_sport: str
    valid_from: str
    valid_until: str | None
    evidence_available_at: str
    source_snapshot_sha256: str
    recorded_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_namespace", _canonical_text("provider_namespace", self.provider_namespace)
        )
        object.__setattr__(
            self, "provider_sport_id", _opaque_provider_id("provider_sport_id", self.provider_sport_id)
        )
        object.__setattr__(self, "canonical_sport", _canonical_sport(self.canonical_sport))
        object.__setattr__(self, "valid_from", _time_text("valid_from", self.valid_from))
        if self.valid_until is not None:
            object.__setattr__(self, "valid_until", _time_text("valid_until", self.valid_until))
        object.__setattr__(
            self, "evidence_available_at", _time_text("evidence_available_at", self.evidence_available_at)
        )
        object.__setattr__(
            self, "source_snapshot_sha256", _sha256("source_snapshot_sha256", self.source_snapshot_sha256)
        )
        object.__setattr__(self, "recorded_at", _time_text("recorded_at", self.recorded_at))
        start = _instant("valid_from", self.valid_from)
        if self.valid_until is not None and _instant("valid_until", self.valid_until) <= start:
            raise ProviderSportMappingError("valid_until must be after valid_from")
        available = _instant("evidence_available_at", self.evidence_available_at)
        if available < start:
            raise ProviderSportMappingError(
                "mapping evidence cannot be available before the mapping valid_from boundary"
            )
        if _instant("recorded_at", self.recorded_at) < available:
            raise ProviderSportMappingError("mapping cannot be recorded before its evidence is available")

    def semantic_payload(self) -> dict[str, str | None]:
        return {
            "provider_namespace": self.provider_namespace,
            "provider_sport_id": self.provider_sport_id,
            "canonical_sport": self.canonical_sport,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "evidence_available_at": self.evidence_available_at,
            "source_snapshot_sha256": self.source_snapshot_sha256,
        }

    @property
    def binding_id(self) -> str:
        return _digest(self.semantic_payload())

    def payload(self) -> dict[str, str | None]:
        return {**self.semantic_payload(), "recorded_at": self.recorded_at, "binding_id": self.binding_id}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ProviderSportBinding":
        expected = {
            "provider_namespace", "provider_sport_id", "canonical_sport", "valid_from",
            "valid_until", "evidence_available_at", "source_snapshot_sha256",
            "recorded_at", "binding_id",
        }
        if set(payload) != expected:
            raise ProviderSportMappingError("provider sport binding payload has unexpected fields")
        binding = cls(
            provider_namespace=payload["provider_namespace"],
            provider_sport_id=payload["provider_sport_id"],
            canonical_sport=payload["canonical_sport"],
            valid_from=payload["valid_from"],
            valid_until=payload["valid_until"],
            evidence_available_at=payload["evidence_available_at"],
            source_snapshot_sha256=payload["source_snapshot_sha256"],
            recorded_at=payload["recorded_at"],
        )
        if payload["binding_id"] != binding.binding_id:
            raise ProviderSportMappingError("provider sport binding_id does not match payload")
        return binding


@dataclass(frozen=True, slots=True)
class CanonicalSportResolution:
    provider_namespace: str
    provider_sport_id: str
    canonical_sport: str
    as_of: str
    binding_id: str
    source_snapshot_sha256: str
    registry_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_namespace", _canonical_text("provider_namespace", self.provider_namespace)
        )
        object.__setattr__(
            self, "provider_sport_id", _opaque_provider_id("provider_sport_id", self.provider_sport_id)
        )
        object.__setattr__(self, "canonical_sport", _canonical_sport(self.canonical_sport))
        object.__setattr__(self, "as_of", _time_text("as_of", self.as_of))
        object.__setattr__(self, "binding_id", _sha256("binding_id", self.binding_id))
        object.__setattr__(
            self, "source_snapshot_sha256", _sha256("source_snapshot_sha256", self.source_snapshot_sha256)
        )
        object.__setattr__(self, "registry_sha256", _sha256("registry_sha256", self.registry_sha256))

    @property
    def resolution_sha256(self) -> str:
        return _digest({
            "provider_namespace": self.provider_namespace,
            "provider_sport_id": self.provider_sport_id,
            "canonical_sport": self.canonical_sport,
            "as_of": self.as_of,
            "binding_id": self.binding_id,
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "registry_sha256": self.registry_sha256,
        })


def _build_register_evidence_method():
    """Seal positive parsing, chronology and durable mutation behind product authority."""

    canonical_registry_type = ProviderSportMappingRegistry
    canonical_clock = _utc_now
    parser_descriptor = vars(ProviderSportEvidence).get("from_exact_bytes")
    if type(parser_descriptor) is not classmethod:
        raise RuntimeError("ProviderSportEvidence.from_exact_bytes must remain a classmethod")
    canonical_parser = parser_descriptor.__func__
    parser_dispatch = {
        "_strict_json_object": _strict_json_object,
        "_canonical_text": _canonical_text,
        "_opaque_provider_id": _opaque_provider_id,
        "_canonical_sport": _canonical_sport,
        "_time_text": _time_text,
        "_instant": _instant,
        "ProviderSportMappingError": ProviderSportMappingError,
        "hashlib": hashlib,
        "json": json,
        "unicodedata": unicodedata,
        "datetime": datetime,
        "timezone": timezone,
        "_RESERVED_SPORTS": _RESERVED_SPORTS,
        "_EVIDENCE_SCHEMA": _EVIDENCE_SCHEMA,
        "_EVIDENCE_VERSION": _EVIDENCE_VERSION,
    }
    canonical_load = vars(canonical_registry_type)["_load"]
    canonical_persist = vars(canonical_registry_type)["_persist"]
    overlap_descriptor = vars(canonical_registry_type)["_assert_no_overlap"]
    if type(overlap_descriptor) is not staticmethod:
        raise RuntimeError("ProviderSportMappingRegistry._assert_no_overlap must remain static")
    canonical_overlap = overlap_descriptor.__func__

    def register_evidence(
        self: "ProviderSportMappingRegistry",
        source_snapshot_bytes: bytes,
    ) -> ProviderSportBinding:
        if type(self) is not canonical_registry_type:
            raise ProviderSportMappingError("canonical provider sport registry type is required")
        if globals().get("_utc_now") is not canonical_clock:
            raise ProviderSportMappingError("product recording clock authority changed")
        if vars(ProviderSportEvidence).get("from_exact_bytes") is not parser_descriptor:
            raise ProviderSportMappingError("canonical evidence parser authority changed")
        for name, expected in parser_dispatch.items():
            if globals().get(name) is not expected:
                raise ProviderSportMappingError("canonical evidence parser authority changed")
        if vars(canonical_registry_type).get("_load") is not canonical_load:
            raise ProviderSportMappingError("canonical registry load authority changed")
        if vars(canonical_registry_type).get("_persist") is not canonical_persist:
            raise ProviderSportMappingError("canonical registry persist authority changed")
        if vars(canonical_registry_type).get("_assert_no_overlap") is not overlap_descriptor:
            raise ProviderSportMappingError("canonical registry overlap authority changed")

        with durable_path_lock(self.path):
            if not self.path.exists():
                raise ProviderSportMappingError(
                    "durable provider sport mapping registry is missing; initialize it first"
                )
            canonical_load(self)
            evidence = canonical_parser(ProviderSportEvidence, source_snapshot_bytes)
            recorded_at = canonical_clock()
            if _instant("recorded_at", recorded_at) < _instant(
                "evidence_available_at", evidence.evidence_available_at
            ):
                raise ProviderSportMappingError(
                    "mapping evidence availability cannot be in the future of product recording time"
                )
            candidate = ProviderSportBinding(
                provider_namespace=evidence.provider_namespace,
                provider_sport_id=evidence.provider_sport_id,
                canonical_sport=evidence.canonical_sport,
                valid_from=evidence.valid_from,
                valid_until=evidence.valid_until,
                evidence_available_at=evidence.evidence_available_at,
                source_snapshot_sha256=evidence.source_snapshot_sha256,
                recorded_at=recorded_at,
            )
            for existing in self._bindings:
                if existing.binding_id == candidate.binding_id:
                    return existing
            canonical_overlap(candidate, self._bindings)
            updated = sorted(
                [*self._bindings, candidate],
                key=lambda value: (
                    value.provider_namespace,
                    value.provider_sport_id,
                    value.valid_from,
                    value.valid_until is None,
                    value.valid_until or "",
                    value.binding_id,
                ),
            )
            canonical_persist(self, updated)
            self._bindings = updated
            return candidate

    register_evidence.__name__ = "register_evidence"
    register_evidence.__qualname__ = "ProviderSportMappingRegistry.register_evidence"
    return register_evidence


class ProviderSportMappingRegistry:
    """Append-only causal registry whose positive entries derive from exact evidence bytes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._bindings: list[ProviderSportBinding] = []
        with durable_path_lock(self.path):
            if self.path.exists():
                self._load()

    @classmethod
    def initialize_pristine(cls, path: str | Path) -> "ProviderSportMappingRegistry":
        target = Path(path)
        registry = cls(target)
        with durable_path_lock(target):
            if target.exists():
                raise ProviderSportMappingError("provider sport mapping registry already exists")
            registry._persist(registry._bindings)
        return registry

    @property
    def bindings(self) -> tuple[ProviderSportBinding, ...]:
        return tuple(self._bindings)

    @property
    def registry_sha256(self) -> str:
        return _digest(self._unsigned_payload(self._bindings))

    def register(self, **_: object) -> ProviderSportBinding:
        raise ProviderSportMappingError(
            "caller-authored mapping fields are not positive authority; use register_evidence(exact_bytes)"
        )

    def resolve(
        self, *, provider_namespace: str, provider_sport_id: str, as_of: str
    ) -> CanonicalSportResolution:
        if type(self) is not ProviderSportMappingRegistry:
            raise ProviderSportMappingError("canonical provider sport registry type is required")
        if "_load" in vars(self):
            raise ProviderSportMappingError("canonical registry load authority changed")
        namespace = _canonical_text("provider_namespace", provider_namespace)
        opaque_id = _opaque_provider_id("provider_sport_id", provider_sport_id)
        instant = _instant("as_of", as_of)
        canonical_as_of = instant.isoformat().replace("+00:00", "Z")
        with durable_path_lock(self.path):
            if not self.path.exists():
                raise ProviderSportMappingError("durable provider sport mapping registry is missing")
            self._load()
        candidates: list[ProviderSportBinding] = []
        for binding in self._bindings:
            if binding.provider_namespace != namespace or binding.provider_sport_id != opaque_id:
                continue
            if instant < _instant("valid_from", binding.valid_from):
                continue
            if binding.valid_until is not None and instant >= _instant("valid_until", binding.valid_until):
                continue
            if instant < _instant("evidence_available_at", binding.evidence_available_at):
                continue
            if instant < _instant("recorded_at", binding.recorded_at):
                continue
            candidates.append(binding)
        if not candidates:
            raise ProviderSportMappingError("provider sport identity has no causal canonical mapping at as_of")
        if len(candidates) != 1:
            raise ProviderSportMappingError("provider sport identity resolves ambiguously at as_of")
        binding = candidates[0]
        return CanonicalSportResolution(
            provider_namespace=binding.provider_namespace,
            provider_sport_id=binding.provider_sport_id,
            canonical_sport=binding.canonical_sport,
            as_of=canonical_as_of,
            binding_id=binding.binding_id,
            source_snapshot_sha256=binding.source_snapshot_sha256,
            registry_sha256=self.registry_sha256,
        )

    @staticmethod
    def _assert_no_overlap(
        candidate: ProviderSportBinding, existing_bindings: list[ProviderSportBinding]
    ) -> None:
        def overlap(left: ProviderSportBinding, right: ProviderSportBinding) -> bool:
            left_start = _instant("valid_from", left.valid_from)
            right_start = _instant("valid_from", right.valid_from)
            left_end = None if left.valid_until is None else _instant("valid_until", left.valid_until)
            right_end = None if right.valid_until is None else _instant("valid_until", right.valid_until)
            return (right_end is None or left_start < right_end) and (
                left_end is None or right_start < left_end
            )

        for existing in existing_bindings:
            if (
                existing.provider_namespace == candidate.provider_namespace
                and existing.provider_sport_id == candidate.provider_sport_id
                and overlap(existing, candidate)
            ):
                raise ProviderSportMappingError("overlapping provider sport mappings are ambiguous")

    @staticmethod
    def _unsigned_payload(bindings: list[ProviderSportBinding]) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _VERSION,
            "bindings": [binding.payload() for binding in bindings],
        }

    def _payload(self, bindings: list[ProviderSportBinding]) -> dict[str, object]:
        unsigned = self._unsigned_payload(bindings)
        return {**unsigned, "registry_sha256": _digest(unsigned)}

    def _persist(self, bindings: list[ProviderSportBinding]) -> None:
        atomic_write_json(self.path, self._payload(bindings))

    def _load(self) -> None:
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ProviderSportMappingError("cannot read provider sport mapping registry") from exc

        def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ProviderSportMappingError(
                        f"provider sport mapping registry has duplicate JSON key {key!r}"
                    )
                result[key] = value
            return result

        try:
            raw = json.loads(
                raw_text,
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ProviderSportMappingError(
                        f"provider sport mapping registry has invalid JSON constant {value!r}"
                    )
                ),
            )
        except json.JSONDecodeError as exc:
            raise ProviderSportMappingError("provider sport mapping registry is invalid JSON") from exc
        if type(raw) is not dict or set(raw) != {
            "schema", "schema_version", "bindings", "registry_sha256"
        }:
            raise ProviderSportMappingError("provider sport mapping registry shape is invalid")
        if raw["schema"] != _SCHEMA or raw["schema_version"] != _VERSION:
            raise ProviderSportMappingError("unsupported provider sport mapping registry schema")
        if type(raw["bindings"]) is not list or type(raw["registry_sha256"]) is not str:
            raise ProviderSportMappingError("provider sport mapping registry payload is invalid")
        expected_digest = _digest({
            "schema": raw["schema"],
            "schema_version": raw["schema_version"],
            "bindings": raw["bindings"],
        })
        if raw["registry_sha256"] != expected_digest:
            raise ProviderSportMappingError("provider sport mapping registry digest mismatch")

        bindings: list[ProviderSportBinding] = []
        seen: set[str] = set()
        for item in raw["bindings"]:
            if type(item) is not dict:
                raise ProviderSportMappingError("provider sport mapping binding must be an object")
            binding = ProviderSportBinding.from_payload(item)
            if binding.binding_id in seen:
                raise ProviderSportMappingError("duplicate provider sport mapping binding_id")
            self._assert_no_overlap(binding, bindings)
            seen.add(binding.binding_id)
            bindings.append(binding)
        canonical = sorted(
            bindings,
            key=lambda value: (
                value.provider_namespace,
                value.provider_sport_id,
                value.valid_from,
                value.valid_until is None,
                value.valid_until or "",
                value.binding_id,
            ),
        )
        if bindings != canonical:
            raise ProviderSportMappingError("provider sport mapping registry is not canonically ordered")
        self._bindings = bindings


ProviderSportMappingRegistry.register_evidence = _build_register_evidence_method()
del _build_register_evidence_method
