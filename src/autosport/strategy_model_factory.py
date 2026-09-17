from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Sequence

from . import _strategy_model_factory_impl as _impl
from ._strategy_model_factory_impl import *  # noqa: F401,F403
from .integrity import atomic_write_json
from .scientific_registry import ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock


class _StagedFactoryArtifactStore:
    """Stage factory evidence until one registry transaction is proven valid."""

    def __init__(self, real_store: _impl.FactoryArtifactStore, root: Path) -> None:
        self._real = real_store
        self._staged = _impl.FactoryArtifactStore(root)
        self._writes: dict[tuple[str, str], dict[str, object]] = {}

    def exists(self, kind: str, identity: str) -> bool:
        return self._staged.exists(kind, identity) or self._real.exists(kind, identity)

    def write(self, kind: str, identity: str, payload: dict[str, object]) -> str:
        if self._real.exists(kind, identity):
            existing = self._real.read(kind, identity)
            if existing != payload:
                raise ValueError(
                    f"conflicting immutable factory artifact: {kind}:{identity}"
                )
            return self._real.sha256(kind, identity)
        digest = self._staged.write(kind, identity, payload)
        self._writes[(kind, identity)] = payload
        return digest

    def read(
        self,
        kind: str,
        identity: str,
        *,
        expected_sha256: str | None = None,
    ) -> dict[str, object]:
        if self._staged.exists(kind, identity):
            return self._staged.read(
                kind,
                identity,
                expected_sha256=expected_sha256,
            )
        return self._real.read(
            kind,
            identity,
            expected_sha256=expected_sha256,
        )

    def sha256(self, kind: str, identity: str) -> str:
        if self._staged.exists(kind, identity):
            return self._staged.sha256(kind, identity)
        return self._real.sha256(kind, identity)

    def path_for_testing(self, kind: str, identity: str) -> Path:
        if self._staged.exists(kind, identity):
            return self._staged.path_for_testing(kind, identity)
        return self._real.path_for_testing(kind, identity)

    def _rollback(self, created: Sequence[tuple[str, str]], primary: BaseException) -> None:
        for kind, identity in reversed(tuple(created)):
            try:
                self._real.path_for_testing(kind, identity).unlink()
            except FileNotFoundError:
                continue
            except BaseException as cleanup_error:
                try:
                    primary.add_note(
                        "factory transaction artifact rollback also failed for "
                        f"{kind}:{identity}: {type(cleanup_error).__name__}: {cleanup_error}"
                    )
                except BaseException:
                    pass

    def publish(self) -> tuple[tuple[str, str], ...]:
        created: list[tuple[str, str]] = []
        try:
            for (kind, identity), payload in self._writes.items():
                if self._real.exists(kind, identity):
                    existing = self._real.read(kind, identity)
                    if existing != payload:
                        raise ValueError(
                            f"conflicting immutable factory artifact: {kind}:{identity}"
                        )
                    continue
                try:
                    self._real.write(kind, identity, payload)
                except BaseException:
                    path = self._real.path_for_testing(kind, identity)
                    if path.exists():
                        try:
                            path.unlink()
                        except BaseException:
                            pass
                    raise
                created.append((kind, identity))
        except BaseException as publish_error:
            self._rollback(created, publish_error)
            raise
        return tuple(created)


class ExperimentRunner(_impl.ExperimentRunner):
    """Factory runner with one fail-closed workspace transaction per candidate.

    The legacy implementation remains the scientific/evaluation authority. This
    facade changes only the durable commit boundary: all candidate registry rows and
    artifacts are first validated against one lock-protected snapshot, staged in an
    isolated workspace, and published only after the complete promotion lineage is
    accepted. A concurrent writer therefore either owns the workspace lock or sees no
    partial candidate lineage.
    """

    def run_baseline_candidate(
        self,
        spec: _impl.FactoryCandidateSpec,
        points: Sequence[_impl.TrainingPoint],
        *,
        rule: _impl.PromotionRule,
        minimum_train_size: int | None = None,
    ) -> _impl.FactoryRunResult:
        real_registry = self.registry
        real_store = self.artifact_store

        with WorkspaceEconomicLock(real_registry.path.parent):
            original_state = real_registry._read()
            with tempfile.TemporaryDirectory(
                prefix="autosport-factory-transaction-"
            ) as temporary_directory:
                temporary_root = Path(temporary_directory)
                staged_registry_path = temporary_root / "scientific_registry.json"
                atomic_write_json(staged_registry_path, original_state)
                staged_registry = ScientificRegistry(staged_registry_path)
                staged_store = _StagedFactoryArtifactStore(
                    real_store,
                    temporary_root / "factory-artifacts",
                )
                staged_runner = _impl.ExperimentRunner(
                    staged_registry,
                    staged_store,
                    baseline_model_factory=self.baseline_model_factory,
                )
                result = staged_runner.run_baseline_candidate(
                    spec,
                    points,
                    rule=rule,
                    minimum_train_size=minimum_train_size,
                )
                final_state = staged_registry._read()
                created_artifacts = staged_store.publish()
                try:
                    atomic_write_json(real_registry.path, final_state)
                    real_registry._read()
                except BaseException as publish_error:
                    try:
                        atomic_write_json(real_registry.path, original_state)
                        real_registry._read()
                    except BaseException as restore_error:
                        try:
                            publish_error.add_note(
                                "factory transaction registry rollback also failed: "
                                f"{type(restore_error).__name__}: {restore_error}"
                            )
                        except BaseException:
                            pass
                    staged_store._rollback(created_artifacts, publish_error)
                    raise
                return result
