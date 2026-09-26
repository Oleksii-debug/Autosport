"""Durable causal procedural memory for Autosport research skills.

The registry stores immutable, provenance-bound procedure knowledge. It is a
read-only knowledge primitive: resolving a memory record never grants
promotion, provider-write, risk expansion, economic-goal expansion, or
real-money execution authority.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping

from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock


SCHEMA: Final = "autosport.procedural_skill_memory"
SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class ProceduralSkillMemoryError(RuntimeError):
    pass


class ConflictingProceduralSkillVersionError(ProceduralSkillMemoryError):
    pass


class ProceduralSkillAuthorityError(ProceduralSkillMemoryError):
    pass


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ProceduralSkillMemoryError(f"{name} must be a non-empty canonical string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProceduralSkillMemoryError(f"{name} must be valid UTF-8") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or text != text.lower() or any(ch not in _HEX for ch in text):
        raise ProceduralSkillMemoryError(f"{name} must be canonical lowercase SHA-256 hex")
    return text


def _instant(value: object, name: str) -> str:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProceduralSkillMemoryError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProceduralSkillMemoryError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant_value(value: object, name: str) -> datetime:
    return datetime.fromisoformat(_instant(value, name).replace("Z", "+00:00"))


def _sorted_unique_texts(value: object, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise ProceduralSkillMemoryError(f"{name} must be a tuple")
    result = tuple(_text(item, f"{name} item") for item in value)
    if not allow_empty and not result:
        raise ProceduralSkillMemoryError(f"{name} must not be empty")
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise ProceduralSkillMemoryError(f"{name} must be sorted and unique")
    return result


def _provenance(value: object) -> tuple[tuple[str, str], ...]:
    if type(value) is not tuple:
        raise ProceduralSkillMemoryError("provenance must be a tuple")
    result: list[tuple[str, str]] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise ProceduralSkillMemoryError("provenance members must be (key, sha256)")
        result.append((_text(item[0], "provenance key"), _sha256(item[1], "provenance sha256")))
    canonical = tuple(result)
    if not canonical:
        raise ProceduralSkillMemoryError("provenance must not be empty")
    if canonical != tuple(sorted(canonical)) or len({key for key, _ in canonical}) != len(canonical):
        raise ProceduralSkillMemoryError("provenance must be sorted with unique keys")
    return canonical


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProceduralSkillMemoryError("payload must be canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProceduralSkillMemoryError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class ProceduralSkillMemoryVersion:
    skill_key: str
    skill_version: str
    procedure_sha256: str
    contract_sha256: str
    available_at: str
    validity_domain: tuple[str, ...]
    learned_from: tuple[str, ...]
    provenance: tuple[tuple[str, str], ...]
    owner_authority_sha256: str
    risk_authority_sha256: str
    economic_goal_sha256: str
    synthetic: bool = False
    predecessor_id: str | None = None
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        _text(self.skill_key, "skill_key")
        _text(self.skill_version, "skill_version")
        _sha256(self.procedure_sha256, "procedure_sha256")
        _sha256(self.contract_sha256, "contract_sha256")
        object.__setattr__(
            self,
            "available_at",
            _instant(self.available_at, "available_at"),
        )
        _sorted_unique_texts(self.validity_domain, "validity_domain")
        _sorted_unique_texts(self.learned_from, "learned_from")
        _provenance(self.provenance)
        _sha256(self.owner_authority_sha256, "owner_authority_sha256")
        _sha256(self.risk_authority_sha256, "risk_authority_sha256")
        _sha256(self.economic_goal_sha256, "economic_goal_sha256")
        if type(self.synthetic) is not bool:
            raise ProceduralSkillMemoryError("synthetic must be boolean")
        if self.predecessor_id is not None:
            _sha256(self.predecessor_id, "predecessor_id")
        if type(self.execution_authorized) is not bool or self.execution_authorized:
            raise ProceduralSkillAuthorityError("procedural memory can never grant execution authority")

    def payload(self, *, include_id: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "skill_key": self.skill_key,
            "skill_version": self.skill_version,
            "procedure_sha256": self.procedure_sha256,
            "contract_sha256": self.contract_sha256,
            "available_at": _instant(self.available_at, "available_at"),
            "validity_domain": list(self.validity_domain),
            "learned_from": list(self.learned_from),
            "provenance": [list(item) for item in self.provenance],
            "owner_authority_sha256": self.owner_authority_sha256,
            "risk_authority_sha256": self.risk_authority_sha256,
            "economic_goal_sha256": self.economic_goal_sha256,
            "synthetic": self.synthetic,
            "predecessor_id": self.predecessor_id,
            "execution_authorized": False,
        }
        if include_id:
            payload["version_id"] = self.version_id
        return payload

    @property
    def version_id(self) -> str:
        return _digest(
            {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "kind": "ProceduralSkillMemoryVersion",
                **self.payload(include_id=False),
            }
        )


class ProceduralSkillMemory:
    """Persistent immutable procedural-memory registry with causal retrieval."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self._read()
        except FileNotFoundError as exc:
            raise ProceduralSkillMemoryError("procedural skill memory state is missing") from exc

    @classmethod
    def initialize(cls, path: str | Path) -> "ProceduralSkillMemory":
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(target.parent):
            if not target.exists():
                cls._write(
                    target,
                    {
                        "schema": SCHEMA,
                        "schema_version": SCHEMA_VERSION,
                        "versions": [],
                    },
                )
        return cls(target)

    @staticmethod
    def _without_digest(state: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in state.items() if key != "state_sha256"}

    @classmethod
    def _write(cls, path: Path, state: Mapping[str, Any]) -> None:
        payload = dict(state)
        payload["state_sha256"] = _digest(cls._without_digest(payload))
        atomic_write_json(path, payload)

    @staticmethod
    def _decode_version(raw: object) -> ProceduralSkillMemoryVersion:
        if type(raw) is not dict:
            raise ProceduralSkillMemoryError("version entry must be an object")
        expected = {
            "version_id",
            "skill_key",
            "skill_version",
            "procedure_sha256",
            "contract_sha256",
            "available_at",
            "validity_domain",
            "learned_from",
            "provenance",
            "owner_authority_sha256",
            "risk_authority_sha256",
            "economic_goal_sha256",
            "synthetic",
            "predecessor_id",
            "execution_authorized",
        }
        if set(raw) != expected:
            raise ProceduralSkillMemoryError("version entry schema mismatch")
        if type(raw["validity_domain"]) is not list or type(raw["learned_from"]) is not list or type(raw["provenance"]) is not list:
            raise ProceduralSkillMemoryError("version collection fields must be arrays")
        provenance: list[tuple[str, str]] = []
        for item in raw["provenance"]:
            if type(item) is not list or len(item) != 2:
                raise ProceduralSkillMemoryError("persisted provenance member must be [key, sha256]")
            provenance.append((item[0], item[1]))
        version = ProceduralSkillMemoryVersion(
            skill_key=raw["skill_key"],
            skill_version=raw["skill_version"],
            procedure_sha256=raw["procedure_sha256"],
            contract_sha256=raw["contract_sha256"],
            available_at=raw["available_at"],
            validity_domain=tuple(raw["validity_domain"]),
            learned_from=tuple(raw["learned_from"]),
            provenance=tuple(provenance),
            owner_authority_sha256=raw["owner_authority_sha256"],
            risk_authority_sha256=raw["risk_authority_sha256"],
            economic_goal_sha256=raw["economic_goal_sha256"],
            synthetic=raw["synthetic"],
            predecessor_id=raw["predecessor_id"],
            execution_authorized=raw["execution_authorized"],
        )
        if raw["version_id"] != version.version_id:
            raise ProceduralSkillMemoryError("persisted version identity mismatch")
        return version

    @classmethod
    def _validate_lineages(cls, versions: list[ProceduralSkillMemoryVersion]) -> None:
        by_id: dict[str, ProceduralSkillMemoryVersion] = {}
        keys: set[tuple[str, str]] = set()
        latest_by_skill: dict[str, ProceduralSkillMemoryVersion] = {}
        for version in versions:
            if version.version_id in by_id:
                raise ProceduralSkillMemoryError("duplicate procedural memory version identity")
            version_key = (version.skill_key, version.skill_version)
            if version_key in keys:
                raise ProceduralSkillMemoryError("duplicate procedural memory skill version")
            predecessor: ProceduralSkillMemoryVersion | None = None
            if version.predecessor_id is not None:
                predecessor = by_id.get(version.predecessor_id)
                if predecessor is None:
                    raise ProceduralSkillMemoryError("procedural memory predecessor is missing or forward-referenced")
                if predecessor.skill_key != version.skill_key:
                    raise ProceduralSkillMemoryError("procedural memory predecessor belongs to another skill")
                if latest_by_skill.get(version.skill_key) != predecessor:
                    raise ProceduralSkillMemoryError("procedural memory successor must extend the latest version")
                if _instant_value(version.available_at, "available_at") <= _instant_value(predecessor.available_at, "predecessor available_at"):
                    raise ProceduralSkillMemoryError("procedural memory successor availability must advance")
                if not set(version.validity_domain).issubset(predecessor.validity_domain):
                    raise ProceduralSkillMemoryError("procedural memory validity domain cannot expand")
            elif version.skill_key in latest_by_skill:
                raise ProceduralSkillMemoryError("additional skill version requires predecessor linkage")
            by_id[version.version_id] = version
            keys.add(version_key)
            latest_by_skill[version.skill_key] = version

    def _read(self) -> dict[str, Any]:
        try:
            state = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ProceduralSkillMemoryError(f"non-finite JSON constant: {value}")
                ),
            )
        except json.JSONDecodeError as exc:
            raise ProceduralSkillMemoryError("procedural skill memory state must be valid JSON") from exc
        if type(state) is not dict or set(state) != {
            "schema",
            "schema_version",
            "versions",
            "state_sha256",
        }:
            raise ProceduralSkillMemoryError("procedural skill memory state keys invalid")
        if state["schema"] != SCHEMA:
            raise ProceduralSkillMemoryError("procedural skill memory schema mismatch")
        if type(state["schema_version"]) is not int or state["schema_version"] != SCHEMA_VERSION:
            raise ProceduralSkillMemoryError("procedural skill memory schema version mismatch")
        if type(state["versions"]) is not list:
            raise ProceduralSkillMemoryError("procedural skill memory versions must be an array")
        if state["state_sha256"] != _digest(self._without_digest(state)):
            raise ProceduralSkillMemoryError("procedural skill memory state digest mismatch")
        versions = [self._decode_version(item) for item in state["versions"]]
        self._validate_lineages(versions)
        return state

    def publish(self, version: ProceduralSkillMemoryVersion) -> str:
        if not isinstance(version, ProceduralSkillMemoryVersion):
            raise TypeError("version must be ProceduralSkillMemoryVersion")
        entry = version.payload()
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            existing = [self._decode_version(item) for item in state["versions"]]
            same_id = next((item for item in existing if item.version_id == version.version_id), None)
            if same_id is not None:
                return same_id.version_id
            same_version = next(
                (
                    item
                    for item in existing
                    if item.skill_key == version.skill_key
                    and item.skill_version == version.skill_version
                ),
                None,
            )
            if same_version is not None:
                raise ConflictingProceduralSkillVersionError(
                    f"conflicting procedural memory for {version.skill_key}@{version.skill_version}"
                )
            latest = next(
                (item for item in reversed(existing) if item.skill_key == version.skill_key),
                None,
            )
            if latest is None:
                if version.predecessor_id is not None:
                    raise ProceduralSkillMemoryError("first procedural memory version cannot name predecessor")
            else:
                if version.predecessor_id != latest.version_id:
                    raise ProceduralSkillMemoryError("successor must name the latest procedural memory version")
                if _instant_value(version.available_at, "available_at") <= _instant_value(latest.available_at, "latest available_at"):
                    raise ProceduralSkillMemoryError("procedural memory successor availability must advance")
                if not set(version.validity_domain).issubset(latest.validity_domain):
                    raise ProceduralSkillMemoryError("procedural memory validity domain cannot expand")
            state["versions"].append(entry)
            self._write(self.path, self._without_digest(state))
        return version.version_id

    def all_versions(self, *, skill_key: str | None = None) -> tuple[ProceduralSkillMemoryVersion, ...]:
        if skill_key is not None:
            skill_key = _text(skill_key, "skill_key")
        result = tuple(self._decode_version(item) for item in self._read()["versions"])
        if skill_key is None:
            return result
        return tuple(item for item in result if item.skill_key == skill_key)

    def as_of(self, *, skill_key: str, as_of: str) -> ProceduralSkillMemoryVersion | None:
        skill = _text(skill_key, "skill_key")
        cutoff = _instant_value(as_of, "as_of")
        eligible = [
            item
            for item in self.all_versions(skill_key=skill)
            if _instant_value(item.available_at, "available_at") <= cutoff
        ]
        if not eligible:
            return None
        return max(
            eligible,
            key=lambda item: (
                _instant_value(item.available_at, "available_at"),
                item.version_id,
            ),
        )

    def resolve_exact(
        self,
        *,
        skill_key: str,
        skill_version: str,
        version_id: str,
        as_of: str,
        required_domain: tuple[str, ...],
        owner_authority_sha256: str,
        risk_authority_sha256: str,
        economic_goal_sha256: str,
    ) -> ProceduralSkillMemoryVersion:
        skill = _text(skill_key, "skill_key")
        requested_version = _text(skill_version, "skill_version")
        identity = _sha256(version_id, "version_id")
        cutoff = _instant_value(as_of, "as_of")
        domain = _sorted_unique_texts(required_domain, "required_domain")
        owner = _sha256(owner_authority_sha256, "owner_authority_sha256")
        risk = _sha256(risk_authority_sha256, "risk_authority_sha256")
        economic = _sha256(economic_goal_sha256, "economic_goal_sha256")
        found = [
            item
            for item in self.all_versions(skill_key=skill)
            if item.version_id == identity
        ]
        if len(found) != 1:
            raise ProceduralSkillMemoryError("exact procedural memory version is not registered")
        item = found[0]
        if item.skill_version != requested_version:
            raise ProceduralSkillMemoryError("procedural memory version label does not match exact identity")
        if _instant_value(item.available_at, "available_at") > cutoff:
            raise ProceduralSkillMemoryError("procedural memory was not available at requested causal cutoff")
        if not set(domain).issubset(item.validity_domain):
            raise ProceduralSkillMemoryError("procedural memory is outside requested validity domain")
        if (
            item.owner_authority_sha256 != owner
            or item.risk_authority_sha256 != risk
            or item.economic_goal_sha256 != economic
        ):
            raise ProceduralSkillAuthorityError("procedural memory authority fingerprints do not match")
        return item

    @property
    def state_sha256(self) -> str:
        state = self._read()
        return _sha256(state["state_sha256"], "state_sha256")
