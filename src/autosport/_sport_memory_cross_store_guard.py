from __future__ import annotations

"""Fail-closed durable cross-store publication fencing for sport memory.

Four independent durability hazards are closed here without creating a second
business authority:

* ``OpponentIntelligenceStore`` is a whole-file read/modify/write store. Every
  live instance retains the exact durable root it loaded/committed and compares
  that root while holding the same durable path lock immediately before its next
  publication. A stale instance therefore cannot erase an intervening commit.
* ``SportMemoryRuntime`` is also a whole-file store. Product mutations take the
  runtime-path lock for the complete reload/check/apply/publish transaction, so
  independently opened bound runtimes converge on the latest canonical image
  instead of merely detecting a stale writer after useful work has been done.
* bound sport-memory materialization locks the runtime and both canonical upstream
  roots in one globally stable path order across refresh, snapshot/artifact
  publication and post-publication verification. Identity or opponent authority
  cannot drift inside that causal transaction.
* bound decision-memory reads/consumption use the same ordered transaction. An
  already-open runtime therefore re-resolves both its durable checkpoint image and
  immutable authority generation before it can select or publish decision memory.

The module is imported by ``autosport.__init__`` after the public materialization
authority guard so these fences compose with, rather than replace, that guard.
"""

from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path

from .integrity import durable_path_lock
from .opponent_intelligence import OpponentIntelligenceError, OpponentIntelligenceStore
from .sport_memory_checkpoint import (
    BoundSportMemoryRuntime,
    SportMemoryCheckpointError,
    _verify_runtime_snapshot_bindings,
)
from .sport_memory_runtime import SportMemoryError, SportMemoryRuntime


_OPPONENT_ROOT_ATTR = "_autosport_persisted_root_sha256"
_RUNTIME_ROOT_ATTR = "_autosport_runtime_persisted_root_sha256"
_ORIGINAL_OPPONENT_INIT = OpponentIntelligenceStore.__init__
_ORIGINAL_OPPONENT_PERSIST_STATE = OpponentIntelligenceStore._persist_state
_ORIGINAL_RUNTIME_INIT = SportMemoryRuntime.__init__
_ORIGINAL_RUNTIME_PERSIST = SportMemoryRuntime._persist


def _resolved_key(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _file_root(path: Path, *, kind: str, error_type: type[ValueError]) -> str | None:
    if not path.exists():
        return None
    try:
        if not path.is_file():
            raise error_type(f"{kind} durable path is not a regular file")
        return sha256(path.read_bytes()).hexdigest()
    except ValueError:
        raise
    except OSError as exc:
        raise error_type(f"cannot read {kind} durable root") from exc


def _opponent_durable_root(path: Path) -> str | None:
    return _file_root(
        path,
        kind="opponent intelligence",
        error_type=OpponentIntelligenceError,
    )


def _runtime_durable_root(path: Path) -> str | None:
    return _file_root(
        path,
        kind="sport memory",
        error_type=SportMemoryError,
    )


def _guarded_opponent_init(self, path, identity_registry) -> None:
    destination = Path(path)
    # Read the state and bind its exact byte root under one durable fence. Hashing
    # after releasing the lock could bind an old in-memory parse to a newer root.
    with durable_path_lock(destination):
        _ORIGINAL_OPPONENT_INIT(self, destination, identity_registry)
        setattr(self, _OPPONENT_ROOT_ATTR, _opponent_durable_root(destination))


def _guarded_opponent_persist_state(self, *args, **kwargs) -> None:
    destination = Path(self.path)
    with durable_path_lock(destination):
        expected = getattr(self, _OPPONENT_ROOT_ATTR, None)
        current = _opponent_durable_root(destination)
        if current != expected:
            raise OpponentIntelligenceError(
                "opponent intelligence durable root changed since this store was loaded"
            )
        _ORIGINAL_OPPONENT_PERSIST_STATE(self, *args, **kwargs)
        committed = _opponent_durable_root(destination)
        if committed is None:
            raise OpponentIntelligenceError(
                "opponent intelligence publication did not create durable state"
            )
        setattr(self, _OPPONENT_ROOT_ATTR, committed)


def _guarded_runtime_init(
    self,
    path,
    opponent_authority,
    *,
    authority_generation_sha256,
) -> None:
    destination = Path(path)
    # Bind the exact runtime image parsed by __init__. A later writer must compare
    # against this byte root before replacing the whole-file state.
    with durable_path_lock(destination):
        _ORIGINAL_RUNTIME_INIT(
            self,
            destination,
            opponent_authority,
            authority_generation_sha256=authority_generation_sha256,
        )
        setattr(self, _RUNTIME_ROOT_ATTR, _runtime_durable_root(destination))


def _guarded_runtime_persist(self) -> None:
    destination = Path(self.path)
    with durable_path_lock(destination):
        expected = getattr(self, _RUNTIME_ROOT_ATTR, None)
        current = _runtime_durable_root(destination)
        if current != expected:
            raise SportMemoryError(
                "sport memory durable root changed since this runtime was loaded"
            )
        _ORIGINAL_RUNTIME_PERSIST(self)
        committed = _runtime_durable_root(destination)
        if committed is None:
            raise SportMemoryError(
                "sport memory publication did not create durable state"
            )
        setattr(self, _RUNTIME_ROOT_ATTR, committed)


def _transaction_paths(runtime: BoundSportMemoryRuntime) -> tuple[Path, ...]:
    paths = {
        _resolved_key(Path(runtime.path)): Path(runtime.path),
        _resolved_key(Path(runtime._bound_identity_selector.path)): Path(
            runtime._bound_identity_selector.path
        ),
        _resolved_key(Path(runtime._bound_opponent_selector.path)): Path(
            runtime._bound_opponent_selector.path
        ),
    }
    if len(paths) != 3:
        # Product composition already requires distinct paths. Keep this local
        # fence independently fail-closed if a malformed instance bypasses init.
        raise OpponentIntelligenceError(
            "bound sport-memory runtime and upstream authorities must use distinct durable paths"
        )
    return tuple(paths[key] for key in sorted(paths))


def _reload_runtime_under_transaction(self: BoundSportMemoryRuntime) -> None:
    """Replace stale mutable state with the exact locked durable image.

    A bound runtime seals its authority-bearing bindings after construction.
    Re-running the base initializer on that sealed instance would route the
    initializer's normal path/opponent-authority writes through the caller-facing
    rebinding fence. Reload into an exact temporary base runtime instead and copy
    back only mutable checkpoint projections.
    """

    destination = Path(self.path)
    if not destination.exists():
        raise SportMemoryError("sport memory durable checkpoint disappeared")

    reloaded = object.__new__(SportMemoryRuntime)
    _ORIGINAL_RUNTIME_INIT(
        reloaded,
        destination,
        self.opponent_authority,
        authority_generation_sha256=self.authority_generation_sha256,
    )
    committed = _runtime_durable_root(destination)
    if committed is None:
        raise SportMemoryError("sport memory durable checkpoint disappeared")

    # The temporary base runtime parsed and validated the exact locked durable
    # image. Transfer only mutable state; never replay binding assignments onto
    # the sealed product runtime.
    object.__setattr__(self, "_artifacts", reloaded._artifacts)
    object.__setattr__(self, "_consumptions", reloaded._consumptions)
    object.__setattr__(
        self,
        "_decision_consumptions",
        reloaded._decision_consumptions,
    )
    object.__setattr__(self, _RUNTIME_ROOT_ATTR, committed)
    self._assert_binding_seal()


def _enter_transaction(runtime: BoundSportMemoryRuntime) -> ExitStack:
    stack = ExitStack()
    try:
        for path in _transaction_paths(runtime):
            stack.enter_context(durable_path_lock(path))
    except BaseException:
        stack.close()
        raise
    return stack


def _transactional_bound_matchup_as_of(self, *args, **kwargs):
    # Reads used as positive decision evidence must not remain pinned to a stale
    # per-instance runtime image after another bound process/object publishes a
    # newer memory snapshot. Re-resolve the runtime and upstream roots atomically.
    with _enter_transaction(self):
        _reload_runtime_under_transaction(self)
        verified_opponent = self._refresh_bound_authority()
        _verify_runtime_snapshot_bindings(self, verified_opponent)
        evidence = super(BoundSportMemoryRuntime, self).matchup_as_of(*args, **kwargs)
        verified_opponent = self._refresh_bound_authority()
        _verify_runtime_snapshot_bindings(self, verified_opponent)
        return evidence


def _transactional_bound_materialize(self, **kwargs):
    # Runtime + both upstream roots use one deterministic global path order. This
    # removes the former source->runtime versus runtime->source lock inversion and
    # lets a stale bound instance adopt an intervening checkpoint publication
    # before validating/applying its own mutation.
    with _enter_transaction(self):
        _reload_runtime_under_transaction(self)
        self._refresh_bound_authority()
        artifact = super(BoundSportMemoryRuntime, self).materialize(**kwargs)
        verified_opponent = self._refresh_bound_authority()
        _verify_runtime_snapshot_bindings(self, verified_opponent)
        return artifact


def _transactional_bound_record_consumption(self, **kwargs):
    with _enter_transaction(self):
        _reload_runtime_under_transaction(self)
        verified_opponent = self._refresh_bound_authority()
        _verify_runtime_snapshot_bindings(self, verified_opponent)
        artifact = self.get(kwargs.get("memory_id"))
        if artifact.authority_generation_sha256 != self.authority_generation_sha256:
            raise SportMemoryCheckpointError(
                "sport-memory artifact authority generation changed"
            )
        record = super(BoundSportMemoryRuntime, self).record_consumption(**kwargs)
        verified_opponent = self._refresh_bound_authority()
        _verify_runtime_snapshot_bindings(self, verified_opponent)
        return record


def _transactional_bound_record_matchup_consumption(self, **kwargs):
    # The base implementation stages both matchup members and publishes once. The
    # outer transaction supplies the missing multi-instance guarantee: first adopt
    # the latest checkpoint image, then validate/stage/publish the pair while no
    # peer writer can replace that image. Nested matchup reads and atomic writes
    # re-enter the same ordered locks safely.
    with _enter_transaction(self):
        _reload_runtime_under_transaction(self)
        verified_opponent = self._refresh_bound_authority()
        _verify_runtime_snapshot_bindings(self, verified_opponent)
        records = super(BoundSportMemoryRuntime, self).record_matchup_consumption(
            **kwargs
        )
        verified_opponent = self._refresh_bound_authority()
        _verify_runtime_snapshot_bindings(self, verified_opponent)
        return records


OpponentIntelligenceStore.__init__ = _guarded_opponent_init
OpponentIntelligenceStore._persist_state = _guarded_opponent_persist_state
SportMemoryRuntime.__init__ = _guarded_runtime_init
SportMemoryRuntime._persist = _guarded_runtime_persist
BoundSportMemoryRuntime.matchup_as_of = _transactional_bound_matchup_as_of
BoundSportMemoryRuntime.materialize = _transactional_bound_materialize
BoundSportMemoryRuntime.record_consumption = _transactional_bound_record_consumption
BoundSportMemoryRuntime.record_matchup_consumption = (
    _transactional_bound_record_matchup_consumption
)
