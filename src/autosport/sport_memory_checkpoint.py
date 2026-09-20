"""Cross-store authority generation for durable sport memory.

This module does not create a second identity, opponent, or snapshot authority.
It binds the exact validated persisted roots of the canonical
ParticipantIdentityRegistry and OpponentIntelligenceStore into one immutable
content-addressed generation before SportMemoryRuntime can be opened.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from .integrity import atomic_write_json
from .opponent_intelligence import OpponentIntelligenceStore
from .participant_identity import ParticipantIdentityRegistry
from .sport_memory_runtime import SportMemoryRuntime


_SCHEMA = "autosport.sport_memory_authority_checkpoint"
_VERSION = 1


class SportMemoryCheckpointError(ValueError):
    """Raised when canonical sport-memory authority roots cannot be verified."""


def _sha256_text(name: str, value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SportMemoryCheckpointError(
            f"{name} must be a lowercase sha256 hex digest"
        )
    return value


def _canonical_digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def _file_root(name: str, path: Path) -> str:
    try:
        if not path.is_file():
            raise SportMemoryCheckpointError(
                f"{name} canonical store file does not exist"
            )
        return sha256(path.read_bytes()).hexdigest()
    except SportMemoryCheckpointError:
        raise
    except OSError as exc:
        raise SportMemoryCheckpointError(
            f"cannot read {name} canonical store"
        ) from exc


def _resolved(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise SportMemoryCheckpointError("cannot resolve authority store path") from exc


def _require_distinct_paths(*paths: Path) -> None:
    resolved = tuple(_resolved(Path(path)) for path in paths)
    if len(set(resolved)) != len(resolved):
        raise SportMemoryCheckpointError(
            "sport-memory checkpoint, runtime, identity, and opponent paths must be distinct"
        )


def _validated_authorities(
    identity_registry: ParticipantIdentityRegistry,
    opponent_store: OpponentIntelligenceStore,
) -> tuple[
    ParticipantIdentityRegistry,
    OpponentIntelligenceStore,
    str,
    str,
]:
    """Re-open and return the exact canonical authorities whose roots are hashed.

    Caller-owned in-memory objects are accepted only as path/binding selectors.
    Runtime reads must use the freshly re-opened authority objects returned here,
    otherwise a stale caller object could be paired with newer persisted roots.
    """

    if not isinstance(identity_registry, ParticipantIdentityRegistry):
        raise TypeError("identity_registry must be ParticipantIdentityRegistry")
    if not isinstance(opponent_store, OpponentIntelligenceStore):
        raise TypeError("opponent_store must be OpponentIntelligenceStore")
    if opponent_store.identity_registry is not identity_registry:
        raise SportMemoryCheckpointError(
            "opponent store must use the exact supplied identity registry"
        )

    identity_path = Path(identity_registry.path)
    opponent_path = Path(opponent_store.path)
    _require_distinct_paths(identity_path, opponent_path)

    identity_before = _file_root("identity", identity_path)
    opponent_before = _file_root("opponent", opponent_path)

    # Re-open the canonical files so locally persisted bytes are not accepted as
    # roots merely because an older in-memory object once parsed successfully.
    # These constructors are the existing authorities' own validation boundary.
    try:
        verified_identity = ParticipantIdentityRegistry(identity_path)
        verified_opponent = OpponentIntelligenceStore(
            opponent_path, verified_identity
        )
    except Exception as exc:
        raise SportMemoryCheckpointError(
            "canonical identity/opponent stores failed source validation"
        ) from exc

    identity_after = _file_root("identity", identity_path)
    opponent_after = _file_root("opponent", opponent_path)
    if (identity_before, opponent_before) != (identity_after, opponent_after):
        raise SportMemoryCheckpointError(
            "canonical authority roots changed during checkpoint validation"
        )
    return (
        verified_identity,
        verified_opponent,
        identity_after,
        opponent_after,
    )


@dataclass(frozen=True, slots=True)
class SportMemoryAuthorityCheckpoint:
    """Immutable binding of canonical identity and opponent persisted roots."""

    identity_root_sha256: str
    opponent_root_sha256: str
    generation_sha256: str

    def __post_init__(self) -> None:
        _sha256_text("identity_root_sha256", self.identity_root_sha256)
        _sha256_text("opponent_root_sha256", self.opponent_root_sha256)
        _sha256_text("generation_sha256", self.generation_sha256)
        if self.generation_sha256 != self.expected_generation_sha256:
            raise SportMemoryCheckpointError(
                "sport-memory authority generation digest mismatch"
            )

    @property
    def expected_generation_sha256(self) -> str:
        return _canonical_digest(
            {
                "schema": _SCHEMA,
                "version": _VERSION,
                "identity_root_sha256": self.identity_root_sha256,
                "opponent_root_sha256": self.opponent_root_sha256,
            }
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "version": _VERSION,
            "identity_root_sha256": self.identity_root_sha256,
            "opponent_root_sha256": self.opponent_root_sha256,
            "generation_sha256": self.generation_sha256,
        }

    @classmethod
    def capture(
        cls,
        identity_registry: ParticipantIdentityRegistry,
        opponent_store: OpponentIntelligenceStore,
    ) -> "SportMemoryAuthorityCheckpoint":
        _, _, identity_root, opponent_root = _validated_authorities(
            identity_registry, opponent_store
        )
        generation = _canonical_digest(
            {
                "schema": _SCHEMA,
                "version": _VERSION,
                "identity_root_sha256": identity_root,
                "opponent_root_sha256": opponent_root,
            }
        )
        return cls(
            identity_root_sha256=identity_root,
            opponent_root_sha256=opponent_root,
            generation_sha256=generation,
        )

    def verify_current_roots(
        self,
        identity_registry: ParticipantIdentityRegistry,
        opponent_store: OpponentIntelligenceStore,
    ) -> None:
        _verify_checkpoint_against_authorities(
            self, identity_registry, opponent_store
        )


def _checkpoint_from_raw(raw: object) -> SportMemoryAuthorityCheckpoint:
    if type(raw) is not dict:
        raise SportMemoryCheckpointError("invalid sport-memory authority checkpoint")
    expected_keys = {
        "schema",
        "version",
        "identity_root_sha256",
        "opponent_root_sha256",
        "generation_sha256",
    }
    if set(raw) != expected_keys:
        raise SportMemoryCheckpointError(
            "sport-memory authority checkpoint has unexpected fields"
        )
    if raw["schema"] != _SCHEMA or type(raw["schema"]) is not str:
        raise SportMemoryCheckpointError("unsupported sport-memory checkpoint schema")
    if type(raw["version"]) is not int or raw["version"] != _VERSION:
        raise SportMemoryCheckpointError("unsupported sport-memory checkpoint version")
    return SportMemoryAuthorityCheckpoint(
        identity_root_sha256=_sha256_text(
            "identity_root_sha256", raw["identity_root_sha256"]
        ),
        opponent_root_sha256=_sha256_text(
            "opponent_root_sha256", raw["opponent_root_sha256"]
        ),
        generation_sha256=_sha256_text(
            "generation_sha256", raw["generation_sha256"]
        ),
    )


def _read_checkpoint(path: Path) -> SportMemoryAuthorityCheckpoint:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SportMemoryCheckpointError(
            "cannot load sport-memory authority checkpoint"
        ) from exc
    return _checkpoint_from_raw(raw)


def _verify_checkpoint_against_authorities(
    checkpoint: SportMemoryAuthorityCheckpoint,
    identity_registry: ParticipantIdentityRegistry,
    opponent_store: OpponentIntelligenceStore,
) -> OpponentIntelligenceStore:
    _, verified_opponent, identity_root, opponent_root = _validated_authorities(
        identity_registry, opponent_store
    )
    if identity_root != checkpoint.identity_root_sha256:
        raise SportMemoryCheckpointError(
            "participant identity root drift/rollback detected"
        )
    if opponent_root != checkpoint.opponent_root_sha256:
        raise SportMemoryCheckpointError(
            "opponent intelligence root drift/rollback detected"
        )
    return verified_opponent


def _load_verified_checkpoint_and_opponent(
    path: Path,
    identity_registry: ParticipantIdentityRegistry,
    opponent_store: OpponentIntelligenceStore,
) -> tuple[SportMemoryAuthorityCheckpoint, OpponentIntelligenceStore]:
    checkpoint_path = Path(path)
    _require_distinct_paths(
        checkpoint_path,
        Path(identity_registry.path),
        Path(opponent_store.path),
    )
    checkpoint = _read_checkpoint(checkpoint_path)
    verified_opponent = _verify_checkpoint_against_authorities(
        checkpoint, identity_registry, opponent_store
    )
    return checkpoint, verified_opponent


def _initialize_checkpoint_and_opponent(
    path: Path,
    identity_registry: ParticipantIdentityRegistry,
    opponent_store: OpponentIntelligenceStore,
) -> tuple[SportMemoryAuthorityCheckpoint, OpponentIntelligenceStore]:
    checkpoint_path = Path(path)
    _require_distinct_paths(
        checkpoint_path,
        Path(identity_registry.path),
        Path(opponent_store.path),
    )
    if checkpoint_path.exists():
        raise SportMemoryCheckpointError(
            "sport-memory authority checkpoint already exists"
        )

    checkpoint = SportMemoryAuthorityCheckpoint.capture(
        identity_registry, opponent_store
    )
    try:
        atomic_write_json(checkpoint_path, checkpoint.payload())
    except OSError as exc:
        raise SportMemoryCheckpointError(
            "cannot persist sport-memory authority checkpoint"
        ) from exc

    # Re-read both the durable checkpoint and the canonical upstream stores after
    # publication. This closes the checkpoint-publication window and returns the
    # exact fresh opponent object whose bytes match the committed generation.
    persisted, verified_opponent = _load_verified_checkpoint_and_opponent(
        checkpoint_path, identity_registry, opponent_store
    )
    if persisted != checkpoint:
        raise SportMemoryCheckpointError(
            "persisted sport-memory authority checkpoint changed during initialization"
        )
    return persisted, verified_opponent


def initialize_sport_memory_authority_checkpoint(
    path: Path,
    identity_registry: ParticipantIdentityRegistry,
    opponent_store: OpponentIntelligenceStore,
) -> SportMemoryAuthorityCheckpoint:
    checkpoint, _ = _initialize_checkpoint_and_opponent(
        path, identity_registry, opponent_store
    )
    return checkpoint


def load_verified_sport_memory_authority_checkpoint(
    path: Path,
    identity_registry: ParticipantIdentityRegistry,
    opponent_store: OpponentIntelligenceStore,
) -> SportMemoryAuthorityCheckpoint:
    checkpoint, _ = _load_verified_checkpoint_and_opponent(
        path, identity_registry, opponent_store
    )
    return checkpoint


def open_bound_sport_memory_runtime(
    runtime_path: Path,
    checkpoint_path: Path,
    identity_registry: ParticipantIdentityRegistry,
    opponent_store: OpponentIntelligenceStore,
) -> SportMemoryRuntime:
    """Open sport memory only after both canonical upstream roots verify."""

    runtime = Path(runtime_path)
    checkpoint = Path(checkpoint_path)
    _require_distinct_paths(
        runtime,
        checkpoint,
        Path(identity_registry.path),
        Path(opponent_store.path),
    )
    if not runtime.exists():
        raise SportMemoryCheckpointError("sport-memory runtime does not exist")
    authority, verified_opponent = _load_verified_checkpoint_and_opponent(
        checkpoint, identity_registry, opponent_store
    )
    return SportMemoryRuntime(
        runtime,
        verified_opponent,
        authority_generation_sha256=authority.generation_sha256,
    )


def initialize_or_open_bound_sport_memory_runtime(
    runtime_path: Path,
    checkpoint_path: Path,
    identity_registry: ParticipantIdentityRegistry,
    opponent_store: OpponentIntelligenceStore,
) -> SportMemoryRuntime:
    """Crash-safe bounded composition of checkpoint then sport-memory runtime.

    The checkpoint is committed first. If runtime creation then fails, a retry
    verifies the exact same upstream roots and can finish runtime creation. A
    runtime without its checkpoint is never adopted because that would let a
    caller-provided generation self-attest canonical upstream state.
    """

    runtime = Path(runtime_path)
    checkpoint = Path(checkpoint_path)
    _require_distinct_paths(
        runtime,
        checkpoint,
        Path(identity_registry.path),
        Path(opponent_store.path),
    )

    if checkpoint.exists():
        authority, verified_opponent = _load_verified_checkpoint_and_opponent(
            checkpoint, identity_registry, opponent_store
        )
    else:
        if runtime.exists():
            raise SportMemoryCheckpointError(
                "sport-memory runtime exists without canonical authority checkpoint"
            )
        authority, verified_opponent = _initialize_checkpoint_and_opponent(
            checkpoint, identity_registry, opponent_store
        )

    if runtime.exists():
        return SportMemoryRuntime(
            runtime,
            verified_opponent,
            authority_generation_sha256=authority.generation_sha256,
        )
    return SportMemoryRuntime.initialize_pristine(
        runtime,
        verified_opponent,
        authority_generation_sha256=authority.generation_sha256,
    )
