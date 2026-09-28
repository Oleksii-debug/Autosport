"""Cross-store authority generation for durable sport memory.

This module does not create a second identity, opponent, or snapshot authority.
It binds the exact validated persisted root of the canonical
ParticipantIdentityRegistry and the validated source-authority projection of the
OpponentIntelligenceStore into one immutable content-addressed generation before
SportMemoryRuntime can be opened. Derived opponent rating/feature snapshots are
outputs of sport-memory materialization and therefore cannot redefine the
upstream authority generation that authorized that materialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads
from .opponent_intelligence import OpponentIntelligenceStore
from .participant_identity import ParticipantIdentityRegistry
from .sport_memory_runtime import SportMemoryRuntime


_SCHEMA = "autosport.sport_memory_authority_checkpoint"
_VERSION = 1
_OPPONENT_STORE_SCHEMA = "autosport.opponent_intelligence"
_OPPONENT_STORE_VERSION = 1
_OPPONENT_SOURCE_SCHEMA = "autosport.sport_memory.opponent_source_authority"
_OPPONENT_SOURCE_VERSION = 1
_OPPONENT_STORE_KEYS = {
    "schema",
    "version",
    "performances",
    "rating_snapshots",
    "feature_snapshots",
    "invalidations",
}


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


def _opponent_source_root(path: Path) -> str:
    """Hash canonical opponent source evidence, not derived snapshot caches.

    Rating/feature snapshots are deterministic derived outputs written by
    ``build_snapshots``. Including them in the upstream authority root makes a
    successful sport-memory materialization invalidate the very generation that
    authorized it. Performance observations and invalidations are the canonical
    source evidence that can change the causal inputs, so those remain bound.
    """

    try:
        raw: Any = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SportMemoryCheckpointError(
            "cannot read opponent canonical store"
        ) from exc
    if type(raw) is not dict or set(raw) != _OPPONENT_STORE_KEYS:
        raise SportMemoryCheckpointError(
            "opponent canonical store has unexpected fields"
        )
    if (
        raw["schema"] != _OPPONENT_STORE_SCHEMA
        or type(raw["schema"]) is not str
        or type(raw["version"]) is not int
        or raw["version"] != _OPPONENT_STORE_VERSION
    ):
        raise SportMemoryCheckpointError(
            "unsupported opponent canonical store schema"
        )
    if type(raw["performances"]) is not list or type(raw["invalidations"]) is not list:
        raise SportMemoryCheckpointError(
            "invalid opponent canonical source-authority state"
        )
    return _canonical_digest(
        {
            "schema": _OPPONENT_SOURCE_SCHEMA,
            "version": _OPPONENT_SOURCE_VERSION,
            "opponent_store_schema": raw["schema"],
            "opponent_store_version": raw["version"],
            "performances": raw["performances"],
            "invalidations": raw["invalidations"],
        }
    )


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
    """Re-open exact canonical authorities and return their bound source roots.

    Caller-owned in-memory objects are accepted only as path/binding selectors.
    Runtime reads must use the freshly re-opened authority objects returned here,
    otherwise a stale caller object could be paired with newer persisted roots.

    The whole opponent file is still byte-fenced across validation to reject a
    concurrent mutation. The durable checkpoint binds only the validated source
    projection so deterministic rating/feature cache publication cannot make a
    normal materialization self-invalidate its upstream generation.
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
    opponent_bytes_before = _file_root("opponent", opponent_path)
    opponent_source_before = _opponent_source_root(opponent_path)

    # Re-open the canonical files so locally persisted bytes are not accepted as
    # authority merely because an older in-memory object once parsed successfully.
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
    opponent_bytes_after = _file_root("opponent", opponent_path)
    opponent_source_after = _opponent_source_root(opponent_path)
    if identity_before != identity_after:
        raise SportMemoryCheckpointError(
            "canonical authority roots changed during checkpoint validation"
        )
    if opponent_bytes_before != opponent_bytes_after:
        raise SportMemoryCheckpointError(
            "canonical authority roots changed during checkpoint validation"
        )
    if opponent_source_before != opponent_source_after:
        raise SportMemoryCheckpointError(
            "canonical opponent source authority changed during checkpoint validation"
        )
    return (
        verified_identity,
        verified_opponent,
        identity_after,
        opponent_source_after,
    )


@dataclass(frozen=True, slots=True)
class SportMemoryAuthorityCheckpoint:
    """Immutable binding of canonical identity and opponent source authority."""

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
        raw: Any = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
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


def _verify_runtime_snapshot_bindings(
    runtime: SportMemoryRuntime,
    opponent_store: OpponentIntelligenceStore,
) -> SportMemoryRuntime:
    """Require every durable artifact field to match canonical derived evidence."""

    for artifact in runtime._artifacts.values():
        rating = opponent_store._ratings.get(artifact.rating_snapshot_id)
        feature = opponent_store._features.get(artifact.feature_snapshot_id)
        if rating is None or feature is None:
            raise SportMemoryCheckpointError(
                "sport-memory artifact references missing canonical opponent snapshot"
            )
        if (
            rating.snapshot_id != artifact.rating_snapshot_id
            or feature.snapshot_id != artifact.feature_snapshot_id
            or feature.rating_snapshot_id != rating.snapshot_id
            or rating.participant_entity_id != artifact.participant_entity_id
            or feature.participant_entity_id != artifact.participant_entity_id
            or rating.sport_id != artifact.scope.sport_id
            or feature.sport_id != artifact.scope.sport_id
            or rating.league_id != artifact.scope.league_entity_id
            or feature.league_id != artifact.scope.league_entity_id
            or rating.market_context_id != artifact.scope.market_context_id
            or feature.market_context_id != artifact.scope.market_context_id
            or rating.view is not artifact.identity_view
            or feature.view is not artifact.identity_view
            or rating.causal_cutoff != artifact.causal_cutoff
            or feature.causal_cutoff != artifact.causal_cutoff
            or rating.published_at != artifact.published_at
            or feature.published_at != artifact.published_at
            or tuple(rating.input_performance_ids) != artifact.input_performance_ids
            or rating.input_digest != artifact.input_digest
            or feature.input_digest != artifact.input_digest
            or rating.support != artifact.support
            or feature.support != artifact.support
            or rating.effective_sample != artifact.effective_sample
            or feature.effective_sample != artifact.effective_sample
            or rating.opponent_count != artifact.opponent_count
            or feature.opponent_count != artifact.opponent_count
            or rating.rating != artifact.rating
            or rating.uncertainty != artifact.uncertainty
            or rating.state.value != artifact.state
            or feature.state.value != artifact.state
            or feature.last_observed_at != artifact.last_observed_at
            or feature.age_seconds != artifact.age_seconds
        ):
            raise SportMemoryCheckpointError(
                "sport-memory artifact canonical opponent snapshot binding mismatch"
            )
    return runtime


@dataclass(frozen=True, slots=True)
class _BoundSportMemoryBindingSeal:
    """Immutable construction-time identity for a bound sport-memory runtime."""

    runtime_path: Path
    checkpoint_path: Path
    identity_selector: ParticipantIdentityRegistry
    identity_path: Path
    opponent_selector: OpponentIntelligenceStore
    opponent_path: Path
    authority_generation_sha256: str


class BoundSportMemoryRuntime(SportMemoryRuntime):
    """Product-owned runtime that refreshes canonical source authority per write."""

    __slots__ = ("_binding_seal",)

    _PROTECTED_BINDING_FIELDS = frozenset(
        {
            "_bound_checkpoint_path",
            "_bound_identity_selector",
            "_bound_opponent_selector",
            "path",
            "opponent_authority",
            "authority_generation_sha256",
        }
    )

    def __setattr__(self, name: str, value: object) -> None:
        # Positive authority is dispatched through this exact concrete runtime.
        # After construction, authority-binding state is immutable to callers.
        # The only legitimate opponent-authority refresh bypasses this method
        # after re-verifying the frozen selectors against durable roots.
        try:
            object.__getattribute__(self, "_binding_seal")
        except AttributeError:
            sealed = False
        else:
            sealed = True
        if sealed and name in self._PROTECTED_BINDING_FIELDS:
            raise SportMemoryCheckpointError(
                f"bound sport-memory runtime binding is immutable: {name}"
            )

        # Never allow an instance attribute to shadow a class/inherited member:
        # doing so could replace verification/write methods while preserving the
        # exact BoundSportMemoryRuntime type checked by product binders.
        for authority_class in type(self).__mro__:
            if name in authority_class.__dict__:
                raise SportMemoryCheckpointError(
                    f"bound sport-memory runtime forbids instance authority shadow: {name}"
                )
        object.__setattr__(self, name, value)

    def __getattribute__(self, name: str):
        # Also fail closed on direct __dict__ injection, which bypasses
        # __setattr__. Special-method dispatch resolves this guard on the class,
        # so an instance shadow cannot bypass the check itself.
        instance_state = object.__getattribute__(self, "__dict__")
        if name in instance_state:
            for authority_class in type(self).__mro__:
                if name in authority_class.__dict__:
                    raise SportMemoryCheckpointError(
                        f"bound sport-memory runtime detected instance authority shadow: {name}"
                    )
        return object.__getattribute__(self, name)

    def __init__(
        self,
        path: Path,
        opponent_authority: OpponentIntelligenceStore,
        *,
        authority_generation_sha256: str,
        checkpoint_path: Path,
        identity_registry: ParticipantIdentityRegistry,
        opponent_store: OpponentIntelligenceStore,
    ) -> None:
        if type(opponent_authority) is not OpponentIntelligenceStore:
            raise SportMemoryCheckpointError(
                "bound sport memory requires exact canonical opponent store"
            )
        self._bound_checkpoint_path = Path(checkpoint_path)
        self._bound_identity_selector = identity_registry
        self._bound_opponent_selector = opponent_store
        super().__init__(
            path,
            opponent_authority,
            authority_generation_sha256=authority_generation_sha256,
        )
        seal = _BoundSportMemoryBindingSeal(
            runtime_path=Path(object.__getattribute__(self, "path")),
            checkpoint_path=Path(checkpoint_path),
            identity_selector=identity_registry,
            identity_path=Path(identity_registry.path),
            opponent_selector=opponent_store,
            opponent_path=Path(opponent_store.path),
            authority_generation_sha256=object.__getattribute__(
                self, "authority_generation_sha256"
            ),
        )
        object.__setattr__(self, "_binding_seal", seal)
        self._assert_binding_seal()

    def _assert_binding_seal(self) -> _BoundSportMemoryBindingSeal:
        try:
            seal = object.__getattribute__(self, "_binding_seal")
        except AttributeError as exc:
            raise SportMemoryCheckpointError(
                "bound sport-memory runtime binding seal is missing"
            ) from exc
        state = object.__getattribute__(self, "__dict__")
        if "_binding_seal" in state:
            raise SportMemoryCheckpointError(
                "bound sport-memory runtime binding seal shadow detected"
            )

        if (
            state.get("_bound_checkpoint_path") != seal.checkpoint_path
            or state.get("_bound_identity_selector") is not seal.identity_selector
            or state.get("_bound_opponent_selector") is not seal.opponent_selector
            or state.get("authority_generation_sha256")
            != seal.authority_generation_sha256
        ):
            raise SportMemoryCheckpointError(
                "bound sport-memory runtime binding seal mismatch"
            )
        try:
            runtime_path = Path(state["path"])
            identity_path = Path(seal.identity_selector.path)
            opponent_path = Path(seal.opponent_selector.path)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise SportMemoryCheckpointError(
                "bound sport-memory runtime binding path drift"
            ) from exc
        if (
            runtime_path != seal.runtime_path
            or identity_path != seal.identity_path
            or opponent_path != seal.opponent_path
            or seal.opponent_selector.identity_registry is not seal.identity_selector
        ):
            raise SportMemoryCheckpointError(
                "bound sport-memory runtime binding path drift"
            )

        current_opponent = state.get("opponent_authority")
        if type(current_opponent) is not OpponentIntelligenceStore:
            raise SportMemoryCheckpointError(
                "bound sport-memory runtime opponent authority drift"
            )
        try:
            current_opponent_path = Path(current_opponent.path)
            current_identity_path = Path(current_opponent.identity_registry.path)
        except (TypeError, ValueError, OSError, AttributeError) as exc:
            raise SportMemoryCheckpointError(
                "bound sport-memory runtime opponent authority drift"
            ) from exc
        if (
            current_opponent_path != seal.opponent_path
            or current_identity_path != seal.identity_path
        ):
            raise SportMemoryCheckpointError(
                "bound sport-memory runtime opponent authority drift"
            )
        return seal

    def _require_durable_positive_authority(self) -> None:
        # Every inherited positive-authority entry point must verify the frozen
        # binding even if a caller explicitly dispatches through the base class.
        self._assert_binding_seal()
        super()._require_durable_positive_authority()

    def _refresh_bound_authority(self) -> OpponentIntelligenceStore:
        seal = self._assert_binding_seal()
        authority, verified_opponent = _load_verified_checkpoint_and_opponent(
            seal.checkpoint_path,
            seal.identity_selector,
            seal.opponent_selector,
        )
        if authority.generation_sha256 != seal.authority_generation_sha256:
            raise SportMemoryCheckpointError(
                "bound sport-memory authority generation changed"
            )
        # This is the sole mutable binding field. It is replaced only with the
        # freshly verified canonical object whose durable paths match the seal.
        object.__setattr__(self, "opponent_authority", verified_opponent)
        self._assert_binding_seal()
        return verified_opponent

    def matchup_as_of(self, *args, **kwargs):
        """Read decision-time memory only under the current canonical roots."""
        seal = self._assert_binding_seal()
        first_path, second_path = sorted(
            (seal.identity_path, seal.opponent_path),
            key=lambda path: str(_resolved(path)),
        )
        with durable_path_lock(first_path):
            with durable_path_lock(second_path):
                verified_opponent = self._refresh_bound_authority()
                _verify_runtime_snapshot_bindings(self, verified_opponent)
                evidence = super().matchup_as_of(*args, **kwargs)
                verified_opponent = self._refresh_bound_authority()
                _verify_runtime_snapshot_bindings(self, verified_opponent)
                return evidence

    def materialize(self, **kwargs):
        # Identity and opponent source evidence jointly define the authority
        # generation. Fence both canonical stores for the complete verified
        # publication transaction. Sorting resolved paths gives every caller the
        # same lock order, while atomic_write_json can safely re-enter either lock.
        seal = self._assert_binding_seal()
        first_path, second_path = sorted(
            (seal.identity_path, seal.opponent_path),
            key=lambda path: str(_resolved(path)),
        )
        with durable_path_lock(first_path):
            with durable_path_lock(second_path):
                self._refresh_bound_authority()
                artifact = super().materialize(**kwargs)
                verified_opponent = self._refresh_bound_authority()
                _verify_runtime_snapshot_bindings(self, verified_opponent)
                return artifact


def _new_bound_runtime(
    runtime_path: Path,
    checkpoint_path: Path,
    identity_registry: ParticipantIdentityRegistry,
    opponent_store: OpponentIntelligenceStore,
    authority: SportMemoryAuthorityCheckpoint,
    verified_opponent: OpponentIntelligenceStore,
) -> BoundSportMemoryRuntime:
    runtime = Path(runtime_path)
    if runtime.exists():
        raise SportMemoryCheckpointError("sport-memory runtime already exists")
    bound = BoundSportMemoryRuntime(
        runtime,
        verified_opponent,
        authority_generation_sha256=authority.generation_sha256,
        checkpoint_path=checkpoint_path,
        identity_registry=identity_registry,
        opponent_store=opponent_store,
    )
    bound._persist()
    return bound


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
    # exact fresh opponent object whose source authority matches the committed
    # generation.
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
    bound = BoundSportMemoryRuntime(
        runtime,
        verified_opponent,
        authority_generation_sha256=authority.generation_sha256,
        checkpoint_path=checkpoint,
        identity_registry=identity_registry,
        opponent_store=opponent_store,
    )
    return _verify_runtime_snapshot_bindings(bound, verified_opponent)


def initialize_or_open_bound_sport_memory_runtime(
    runtime_path: Path,
    checkpoint_path: Path,
    identity_registry: ParticipantIdentityRegistry,
    opponent_store: OpponentIntelligenceStore,
) -> SportMemoryRuntime:
    """Crash-safe bounded composition of checkpoint then sport-memory runtime.

    The checkpoint is committed first. If runtime creation then fails, a retry
    verifies the exact same upstream source roots and can finish runtime creation.
    A runtime without its checkpoint is never adopted because that would let a
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
        bound = BoundSportMemoryRuntime(
            runtime,
            verified_opponent,
            authority_generation_sha256=authority.generation_sha256,
            checkpoint_path=checkpoint,
            identity_registry=identity_registry,
            opponent_store=opponent_store,
        )
        return _verify_runtime_snapshot_bindings(bound, verified_opponent)
    return _new_bound_runtime(
        runtime,
        checkpoint,
        identity_registry,
        opponent_store,
        authority,
        verified_opponent,
    )
