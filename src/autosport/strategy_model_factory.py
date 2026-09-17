from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Sequence

from . import _strategy_model_factory_impl as _impl
from ._strategy_model_factory_impl import *  # noqa: F401,F403
from .integrity import atomic_write_json
from .run_transaction import RunTransaction, RunTransactionError
from .scientific_registry import ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock


_FACTORY_PUBLISH_TRANSACTION_FILENAME = ".factory-publish-transaction-v1.json"


class FactoryArtifactStore(_impl.FactoryArtifactStore):
    """Immutable factory evidence read from one stable regular filesystem object."""

    def _stable_snapshot(self, kind: str, identity: str):
        path = self._path(kind, identity)
        try:
            return RunTransaction._read_canonical_file_snapshot(
                path,
                f"factory artifact {kind}:{identity}",
            )
        except RunTransactionError as exc:
            raise ValueError(
                f"factory artifact path is not a stable regular object: {kind}:{identity}"
            ) from exc

    @staticmethod
    def _decode_snapshot(snapshot, kind: str, identity: str) -> dict[str, object]:
        try:
            payload = json.loads(snapshot.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("factory artifact is invalid JSON") from exc
        if type(payload) is not dict:
            raise ValueError("factory artifact root must be an object")
        return payload

    def write(self, kind: str, identity: str, payload: dict[str, object]) -> str:
        path = self._path(kind, identity)
        if path.exists():
            snapshot = self._stable_snapshot(kind, identity)
            existing = self._decode_snapshot(snapshot, kind, identity)
            if existing != payload:
                raise ValueError(f"conflicting immutable factory artifact: {kind}:{identity}")
            return snapshot.sha256
        return super().write(kind, identity, payload)

    def read(
        self,
        kind: str,
        identity: str,
        *,
        expected_sha256: str | None = None,
    ) -> dict[str, object]:
        snapshot = self._stable_snapshot(kind, identity)
        if expected_sha256 is not None and snapshot.sha256 != _impl._sha256(
            expected_sha256,
            "expected_sha256",
        ):
            raise ValueError(f"factory artifact hash mismatch: {kind}:{identity}")
        return self._decode_snapshot(snapshot, kind, identity)

    def sha256(self, kind: str, identity: str) -> str:
        return self._stable_snapshot(kind, identity).sha256


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

    def transaction_artifacts(self) -> tuple[dict[str, str], ...]:
        """Hash-bind exactly the new immutable objects this transaction may publish."""
        return tuple(
            {
                "kind": kind,
                "identity": identity,
                "sha256": self._staged.sha256(kind, identity),
            }
            for kind, identity in sorted(self._writes)
        )

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


class _CausalFinalFitFactory:
    """Fail-close the final model-adapter boundary to the frozen causal population."""

    def __init__(
        self,
        delegate: _impl.BaselineModelFactory,
        *,
        final_model_id: str,
        minimum_causal_train_size: int,
    ) -> None:
        self._delegate = delegate
        self._final_model_id = final_model_id
        self._minimum_causal_train_size = minimum_causal_train_size
        self.model_family = delegate.model_family

    def fit(
        self,
        model_id: str,
        points: Sequence[_impl.TrainingPoint],
        *,
        training_cutoff: str,
    ) -> _impl.BaselineModel:
        if model_id != self._final_model_id:
            return self._delegate.fit(
                model_id,
                points,
                training_cutoff=training_cutoff,
            )
        cutoff = _impl._instant(training_cutoff, "training_cutoff")
        causal_points = tuple(
            point
            for point in _impl._ordered_training_points(points)
            if _impl._instant(point.observed_at, "observed_at") <= cutoff
            and _impl._instant(point.target_reveal_at, "target_available_at") <= cutoff
        )
        if len(causal_points) < self._minimum_causal_train_size:
            raise ValueError(
                "final model lacks the frozen minimum causally revealed training population"
            )
        return self._delegate.fit(
            model_id,
            causal_points,
            training_cutoff=training_cutoff,
        )


def _publish_transaction_path(registry: ScientificRegistry) -> Path:
    return registry.path.parent / _FACTORY_PUBLISH_TRANSACTION_FILENAME


def _registry_state_sha256(state: dict[str, object]) -> str:
    return _impl._canonical_digest(state)


def _unlink_transaction_manifest(path: Path, primary: BaseException | None = None) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except BaseException as cleanup_error:
        if primary is None:
            raise
        try:
            primary.add_note(
                "factory publish transaction manifest cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        except BaseException:
            pass


def _read_publish_transaction(path: Path) -> dict[str, object]:
    try:
        snapshot = RunTransaction._read_canonical_file_snapshot(
            path,
            "factory publish transaction",
        )
    except RunTransactionError as exc:
        raise ValueError("factory publish transaction is not a stable regular object") from exc
    try:
        payload = json.loads(snapshot.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("factory publish transaction is invalid JSON") from exc
    if type(payload) is not dict or payload.get("schema_version") != 1:
        raise ValueError("factory publish transaction schema is invalid")
    if payload.get("phase") != "prepared":
        raise ValueError("factory publish transaction phase is invalid")
    _impl._sha256(payload.get("original_registry_sha256"), "original_registry_sha256")
    _impl._sha256(payload.get("final_registry_sha256"), "final_registry_sha256")
    artifacts = payload.get("artifacts")
    if type(artifacts) is not list:
        raise ValueError("factory publish transaction artifacts are invalid")
    seen: set[tuple[str, str]] = set()
    for artifact in artifacts:
        if type(artifact) is not dict or set(artifact) != {"kind", "identity", "sha256"}:
            raise ValueError("factory publish transaction artifact entry is invalid")
        kind = _impl._text(artifact.get("kind"), "artifact kind")
        identity = _impl._text(artifact.get("identity"), "artifact identity")
        _impl._sha256(artifact.get("sha256"), "artifact sha256")
        key = (kind, identity)
        if key in seen:
            raise ValueError("factory publish transaction artifact identities must be unique")
        seen.add(key)
    return payload


def _recover_interrupted_factory_publish(
    registry: ScientificRegistry,
    store: FactoryArtifactStore,
) -> None:
    """Resolve the only two safe post-crash states, otherwise fail closed."""
    path = _publish_transaction_path(registry)
    if not path.exists():
        return
    transaction = _read_publish_transaction(path)
    current_sha256 = _registry_state_sha256(registry._read())
    original_sha256 = transaction["original_registry_sha256"]
    final_sha256 = transaction["final_registry_sha256"]
    artifacts = transaction["artifacts"]

    if current_sha256 == original_sha256:
        for artifact in artifacts:
            kind = artifact["kind"]
            identity = artifact["identity"]
            expected_sha256 = artifact["sha256"]
            artifact_path = store.path_for_testing(kind, identity)
            if not artifact_path.exists():
                continue
            if store.sha256(kind, identity) != expected_sha256:
                raise ValueError(
                    "interrupted factory publish artifact changed before recovery: "
                    f"{kind}:{identity}"
                )
            artifact_path.unlink()
        _unlink_transaction_manifest(path)
        return

    if current_sha256 == final_sha256:
        for artifact in artifacts:
            kind = artifact["kind"]
            identity = artifact["identity"]
            expected_sha256 = artifact["sha256"]
            if not store.path_for_testing(kind, identity).exists():
                raise ValueError(
                    "committed factory registry is missing a transaction artifact: "
                    f"{kind}:{identity}"
                )
            if store.sha256(kind, identity) != expected_sha256:
                raise ValueError(
                    "committed factory transaction artifact hash mismatch: "
                    f"{kind}:{identity}"
                )
        _unlink_transaction_manifest(path)
        return

    raise ValueError(
        "factory publish transaction registry state is neither precommit nor committed"
    )


class ExperimentRunner(_impl.ExperimentRunner):
    """Factory runner with one fail-closed workspace transaction per candidate.

    The existing implementation remains the scientific/evaluation authority. This
    facade changes only the durable commit boundary: all candidate registry rows and
    artifacts are first validated against one lock-protected snapshot, staged in an
    isolated workspace, and published only after the complete promotion lineage is
    accepted. Interrupted publication is recovered from a hash-bound transaction
    manifest before any later candidate may reuse an immutable identity.
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
            _recover_interrupted_factory_publish(real_registry, real_store)
            original_state = real_registry._read()
            protocol = real_registry.get("ResearchProtocol", spec.research_protocol_id)
            if protocol is None:
                raise ValueError("factory research protocol is missing")
            binding = protocol.payload.get("binding")
            if type(binding) is not dict:
                raise ValueError("factory research protocol lacks frozen binding")
            evaluation_config = _impl.WalkForwardEvaluationConfig.from_frozen_text(
                binding.get("evaluation_design")
            )
            causal_factory = _CausalFinalFitFactory(
                self.baseline_model_factory,
                final_model_id=spec.model_version_id,
                minimum_causal_train_size=evaluation_config.minimum_causal_train_size,
            )
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
                    baseline_model_factory=causal_factory,
                )
                result = staged_runner.run_baseline_candidate(
                    spec,
                    points,
                    rule=rule,
                    minimum_train_size=minimum_train_size,
                )
                final_state = staged_registry._read()
                transaction_path = _publish_transaction_path(real_registry)
                transaction = {
                    "schema_version": 1,
                    "phase": "prepared",
                    "original_registry_sha256": _registry_state_sha256(original_state),
                    "final_registry_sha256": _registry_state_sha256(final_state),
                    "artifacts": list(staged_store.transaction_artifacts()),
                }
                atomic_write_json(transaction_path, transaction)
                try:
                    created_artifacts = staged_store.publish()
                except Exception as publish_error:
                    _unlink_transaction_manifest(transaction_path, publish_error)
                    raise
                try:
                    atomic_write_json(real_registry.path, final_state)
                    real_registry._read()
                except Exception as publish_error:
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
                    _unlink_transaction_manifest(transaction_path, publish_error)
                    raise
                _unlink_transaction_manifest(transaction_path)
                return result


# The implementation split is internal-only. Preserve the original public module
# identity for definitions whose public object was not replaced by a facade subclass.
for _public_name, _public_value in vars(_impl).items():
    if (
        not _public_name.startswith("_")
        and _public_name not in {"ExperimentRunner", "FactoryArtifactStore"}
        and getattr(_public_value, "__module__", None) == _impl.__name__
    ):
        try:
            _public_value.__module__ = __name__
        except (AttributeError, TypeError):
            pass
del _public_name, _public_value
