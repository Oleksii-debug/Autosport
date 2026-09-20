from __future__ import annotations

"""Fail-closed durable cross-store publication fencing for sport memory.

Two independent durability hazards are closed here without creating a second
business authority:

* ``OpponentIntelligenceStore`` is a whole-file read/modify/write store.  Every
  live instance retains the exact durable root it loaded/committed and compares
  that root while holding the same durable path lock immediately before its next
  publication.  A stale instance therefore cannot erase an intervening commit.
* bound sport-memory publication locks both canonical upstream roots, in a stable
  path order, across refresh, snapshot/artifact publication and post-publication
  verification.  Identity or opponent authority cannot drift inside that causal
  transaction.

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
    _verify_runtime_snapshot_bindings,
)


_ROOT_ATTR = "_autosport_persisted_root_sha256"
_ORIGINAL_OPPONENT_INIT = OpponentIntelligenceStore.__init__
_ORIGINAL_OPPONENT_PERSIST_STATE = OpponentIntelligenceStore._persist_state


def _resolved_key(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _durable_root(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        if not path.is_file():
            raise OpponentIntelligenceError(
                "opponent intelligence durable path is not a regular file"
            )
        return sha256(path.read_bytes()).hexdigest()
    except OpponentIntelligenceError:
        raise
    except OSError as exc:
        raise OpponentIntelligenceError(
            "cannot read opponent intelligence durable root"
        ) from exc


def _guarded_opponent_init(self, path, identity_registry) -> None:
    destination = Path(path)
    # Read the state and bind its exact byte root under one durable fence.  Hashing
    # after releasing the lock could bind an old in-memory parse to a newer root.
    with durable_path_lock(destination):
        _ORIGINAL_OPPONENT_INIT(self, destination, identity_registry)
        setattr(self, _ROOT_ATTR, _durable_root(destination))


def _guarded_opponent_persist_state(self, *args, **kwargs) -> None:
    destination = Path(self.path)
    with durable_path_lock(destination):
        expected = getattr(self, _ROOT_ATTR, None)
        current = _durable_root(destination)
        if current != expected:
            raise OpponentIntelligenceError(
                "opponent intelligence durable root changed since this store was loaded"
            )
        _ORIGINAL_OPPONENT_PERSIST_STATE(self, *args, **kwargs)
        committed = _durable_root(destination)
        if committed is None:
            raise OpponentIntelligenceError(
                "opponent intelligence publication did not create durable state"
            )
        setattr(self, _ROOT_ATTR, committed)


def _transaction_paths(runtime: BoundSportMemoryRuntime) -> tuple[Path, ...]:
    paths = {
        _resolved_key(Path(runtime._bound_identity_selector.path)): Path(
            runtime._bound_identity_selector.path
        ),
        _resolved_key(Path(runtime._bound_opponent_selector.path)): Path(
            runtime._bound_opponent_selector.path
        ),
    }
    if len(paths) != 2:
        # Product composition already requires distinct paths. Keep this local
        # fence independently fail-closed if a malformed instance bypasses init.
        raise OpponentIntelligenceError(
            "bound sport-memory upstream authorities must use distinct durable paths"
        )
    return tuple(paths[key] for key in sorted(paths))


def _transactional_bound_materialize(self, **kwargs):
    # Acquire both upstream roots in one deterministic order before *any* refresh.
    # Nested atomic writes reuse these locks re-entrantly.  No identity/opponent
    # writer can therefore commit between generation verification and the final
    # canonical projection check.
    with ExitStack() as stack:
        for path in _transaction_paths(self):
            stack.enter_context(durable_path_lock(path))
        self._refresh_bound_authority()
        artifact = super(BoundSportMemoryRuntime, self).materialize(**kwargs)
        verified_opponent = self._refresh_bound_authority()
        _verify_runtime_snapshot_bindings(self, verified_opponent)
        return artifact


OpponentIntelligenceStore.__init__ = _guarded_opponent_init
OpponentIntelligenceStore._persist_state = _guarded_opponent_persist_state
BoundSportMemoryRuntime.materialize = _transactional_bound_materialize
