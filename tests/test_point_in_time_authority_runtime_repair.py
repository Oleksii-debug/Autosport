from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from autosport import point_in_time_evidence as evidence
from autosport import (
    _dataset_snapshot_lineage_publication_trust_root as lineage_trust_root,
)
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.point_in_time_evidence import (
    EvidenceLedgerCorruptError,
    PointInTimeEvidenceError,
    PointInTimeFeatureAuthority,
)
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


def test_feature_authority_rejects_lineage_subclass_before_authority_dispatch() -> None:
    dispatched = False

    class ForgedLineageAuthority(DatasetSnapshotLineageAuthority):
        def record(self, snapshot_id: str):
            nonlocal dispatched
            dispatched = True
            raise AssertionError("caller-polymorphic record() must never execute")

    forged = object.__new__(ForgedLineageAuthority)

    with pytest.raises(
        PointInTimeEvidenceError,
        match="lineage_authority must be an exact DatasetSnapshotLineageAuthority",
    ):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=forged,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )

    assert dispatched is False


def test_feature_authority_rejects_nested_registry_subclass_before_dispatch() -> None:
    dispatched = False

    class ForgedRegistry(ScientificRegistry):
        def get(self, record_type: str, record_id: str):
            nonlocal dispatched
            dispatched = True
            raise AssertionError("caller-polymorphic registry.get() must never execute")

    lineage = object.__new__(DatasetSnapshotLineageAuthority)
    lineage.registry = object.__new__(ForgedRegistry)

    with pytest.raises(
        PointInTimeEvidenceError,
        match="lineage_authority.registry must be an exact ScientificRegistry",
    ):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=lineage,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )

    assert dispatched is False


def test_feature_authority_rejects_exact_lineage_instance_method_shadow() -> None:
    dispatched = False

    def forged_record(snapshot_id: str):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("caller-shadowed record() must never execute")

    lineage = object.__new__(DatasetSnapshotLineageAuthority)
    lineage.registry = object.__new__(ScientificRegistry)
    lineage.record = forged_record

    with pytest.raises(
        PointInTimeEvidenceError,
        match="lineage_authority shadows trusted concrete authority method: record",
    ):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=lineage,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )

    assert dispatched is False


def test_feature_authority_rejects_exact_registry_instance_method_shadow() -> None:
    dispatched = False

    def forged_get(record_type: str, record_id: str):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("caller-shadowed registry.get() must never execute")

    registry = object.__new__(ScientificRegistry)
    registry.get = forged_get
    lineage = object.__new__(DatasetSnapshotLineageAuthority)
    lineage.registry = registry

    with pytest.raises(
        PointInTimeEvidenceError,
        match=(
            "lineage_authority.registry shadows trusted concrete authority method: get"
        ),
    ):
        PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=lineage,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )

    assert dispatched is False


def _make_lineage_authority(
    workspace: Path,
    *,
    authority_root: Path,
) -> DatasetSnapshotLineageAuthority:
    workspace.mkdir(parents=True, exist_ok=True)
    registry = ScientificRegistry.initialize_pristine(workspace / "scientific-registry.json")
    return DatasetSnapshotLineageAuthority.initialize_pristine(
        workspace / "dataset-snapshot-lineage.json",
        registry,
        authority_root=authority_root,
    )


def test_holdout_lineage_identity_persists_and_rejects_different_reopen(
    tmp_path: Path,
    monkeypatch,
) -> None:
    product_root = (tmp_path / "product-machine-authority").resolve(strict=False)
    monkeypatch.setattr(
        lineage_trust_root,
        "_machine_account_authority_root",
        lambda: product_root,
    )
    workspace = tmp_path / "holdout-workspace"
    lineage = _make_lineage_authority(
        workspace,
        authority_root=product_root,
    )
    holdout_path = workspace / "holdout.json"

    first = evidence.HoldoutConsumptionLedger(
        holdout_path,
        lineage_authority=lineage,
    )
    assert first.records() == ()

    restarted_lineage = DatasetSnapshotLineageAuthority(
        lineage.path,
        ScientificRegistry(lineage.registry.path),
        authority_root=product_root,
    )
    restarted = evidence.HoldoutConsumptionLedger(
        holdout_path,
        lineage_authority=restarted_lineage,
    )
    assert restarted.records() == ()

    alternate = _make_lineage_authority(
        tmp_path / "alternate-workspace",
        authority_root=product_root,
    )
    with pytest.raises(
        PointInTimeEvidenceError,
        match="canonical holdout workspace",
    ):
        evidence.HoldoutConsumptionLedger(
            holdout_path,
            lineage_authority=alternate,
        )


def test_holdout_rejects_initial_lineage_outside_product_composition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    product_root = (tmp_path / "product-machine-authority").resolve(strict=False)
    monkeypatch.setattr(
        lineage_trust_root,
        "_machine_account_authority_root",
        lambda: product_root,
    )
    holdout_workspace = tmp_path / "holdout-workspace"
    holdout_workspace.mkdir()
    alternate = _make_lineage_authority(
        tmp_path / "caller-selected-workspace",
        authority_root=product_root,
    )

    with pytest.raises(
        PointInTimeEvidenceError,
        match="canonical holdout workspace",
    ):
        evidence.HoldoutConsumptionLedger(
            holdout_workspace / "holdout.json",
            lineage_authority=alternate,
        )


def test_holdout_rejects_initial_lineage_path_substitution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    product_root = (tmp_path / "product-machine-authority").resolve(strict=False)
    monkeypatch.setattr(
        lineage_trust_root,
        "_machine_account_authority_root",
        lambda: product_root,
    )
    workspace = tmp_path / "holdout-workspace"
    canonical = _make_lineage_authority(
        workspace,
        authority_root=product_root,
    )
    alternate = object.__new__(DatasetSnapshotLineageAuthority)
    alternate.path = workspace / "caller-selected-lineage.json"
    alternate.registry = canonical.registry
    alternate.monotonic_authority = canonical.monotonic_authority

    with pytest.raises(
        PointInTimeEvidenceError,
        match="canonical product lineage path",
    ):
        evidence.HoldoutConsumptionLedger(
            workspace / "holdout.json",
            lineage_authority=alternate,
        )


def test_holdout_rejects_post_construction_lineage_replacement_before_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    product_root = (tmp_path / "product-machine-authority").resolve(strict=False)
    monkeypatch.setattr(
        lineage_trust_root,
        "_machine_account_authority_root",
        lambda: product_root,
    )
    workspace = tmp_path / "holdout-workspace"
    canonical = _make_lineage_authority(
        workspace,
        authority_root=product_root,
    )
    alternate = _make_lineage_authority(
        tmp_path / "alternate-workspace",
        authority_root=product_root,
    )
    ledger = evidence.HoldoutConsumptionLedger(
        workspace / "holdout.json",
        lineage_authority=canonical,
    )

    dispatched = False

    def forged_record(snapshot_id: str):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("replacement lineage record() must never execute")

    alternate.record = forged_record
    ledger._dataset_lineage_authority = alternate
    snapshot = DatasetSnapshot(
        dataset_snapshot_id="replacement-falsifier",
        manifest_sha256="a" * 64,
        source_identity="provider:replacement-falsifier",
        license_identity="terms:v1",
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:01:00Z",
    )

    with pytest.raises(
        PointInTimeEvidenceError,
        match="canonical dataset lineage authority was replaced after construction",
    ):
        ledger.freshness_id(
            dataset_snapshot=snapshot,
            confirmation_trial_family_id="family-v1",
        )

    assert dispatched is False


def test_point_in_time_module_reload_cannot_restore_legacy_positive_bind() -> None:
    script = r'''
import importlib
import autosport.point_in_time_evidence as evidence
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.scientific_registry import DatasetSnapshot, FeatureSet

reloaded = importlib.reload(evidence)
snapshot = DatasetSnapshot(
    dataset_snapshot_id="reload-snapshot",
    manifest_sha256="a" * 64,
    source_identity="provider:reload-falsifier",
    license_identity="terms:v1",
    causal_cutoff="2026-09-20T10:00:00Z",
    available_at_utc="2026-09-20T10:01:00Z",
)
feature_set = FeatureSet(
    feature_set_id="reload.feature.v1",
    version="v1",
    definition_sha256="b" * 64,
    source_sha256="c" * 64,
    available_at_utc="2026-09-20T10:01:30Z",
)

try:
    reloaded.PointInTimeFeatureAuthority.bind(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_payload_sha256="d" * 64,
        decision_cutoff_utc="2099-01-01T00:00:00Z",
    )
except TypeError as exc:
    assert "lineage_authority" in str(exc)
else:
    raise AssertionError("reload restored the legacy caller-mintable four-argument bind")

class ForgedLineageAuthority(DatasetSnapshotLineageAuthority):
    pass

forged = object.__new__(ForgedLineageAuthority)
try:
    reloaded.PointInTimeFeatureAuthority.bind(
        dataset_snapshot=snapshot,
        feature_set=feature_set,
        feature_provenance=object(),
        lineage_authority=forged,
        decision_cutoff_utc="2099-01-01T00:00:00Z",
    )
except reloaded.PointInTimeEvidenceError as exc:
    assert "lineage_authority must be an exact DatasetSnapshotLineageAuthority" in str(exc)
else:
    raise AssertionError("reload removed the exact lineage capability fence")
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_point_in_time_reload_orders_preserve_exact_capability_fences() -> None:
    script = r'''
import importlib
import autosport.point_in_time_evidence as evidence
import autosport._point_in_time_feature_provenance_guard as provenance_guard
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority
from autosport.scientific_registry import ScientificRegistry


def assert_exact_capability_fences():
    try:
        evidence.PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_payload_sha256="d" * 64,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )
    except TypeError as exc:
        assert "lineage_authority" in str(exc)
    else:
        raise AssertionError("reload order restored legacy four-argument positive bind")

    lineage_dispatched = False

    class ForgedLineageAuthority(DatasetSnapshotLineageAuthority):
        def record(self, snapshot_id: str):
            nonlocal lineage_dispatched
            lineage_dispatched = True
            raise AssertionError("lineage subtype must never dispatch")

    forged_lineage = object.__new__(ForgedLineageAuthority)
    try:
        evidence.PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=forged_lineage,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )
    except evidence.PointInTimeEvidenceError as exc:
        assert "lineage_authority must be an exact DatasetSnapshotLineageAuthority" in str(exc)
    else:
        raise AssertionError("reload order removed exact lineage subtype fence")
    assert lineage_dispatched is False

    registry_dispatched = False

    class ForgedRegistry(ScientificRegistry):
        def get(self, record_type: str, record_id: str):
            nonlocal registry_dispatched
            registry_dispatched = True
            raise AssertionError("registry subtype must never dispatch")

    nested = object.__new__(DatasetSnapshotLineageAuthority)
    nested.registry = object.__new__(ForgedRegistry)
    try:
        evidence.PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=nested,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )
    except evidence.PointInTimeEvidenceError as exc:
        assert "lineage_authority.registry must be an exact ScientificRegistry" in str(exc)
    else:
        raise AssertionError("reload order removed exact nested registry fence")
    assert registry_dispatched is False

    shadow_dispatched = False

    def forged_record(snapshot_id: str):
        nonlocal shadow_dispatched
        shadow_dispatched = True
        raise AssertionError("instance shadow must never dispatch")

    shadowed = object.__new__(DatasetSnapshotLineageAuthority)
    shadowed.registry = object.__new__(ScientificRegistry)
    shadowed.record = forged_record
    try:
        evidence.PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=shadowed,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )
    except evidence.PointInTimeEvidenceError as exc:
        assert "shadows trusted concrete authority method: record" in str(exc)
    else:
        raise AssertionError("reload order removed exact-instance method-shadow fence")
    assert shadow_dispatched is False

    registry_shadow_dispatched = False

    def forged_get(record_type: str, record_id: str):
        nonlocal registry_shadow_dispatched
        registry_shadow_dispatched = True
        raise AssertionError("registry instance shadow must never dispatch")

    registry = object.__new__(ScientificRegistry)
    registry.get = forged_get
    nested_shadow = object.__new__(DatasetSnapshotLineageAuthority)
    nested_shadow.registry = registry
    try:
        evidence.PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=nested_shadow,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )
    except evidence.PointInTimeEvidenceError as exc:
        assert "shadows trusted concrete authority method: get" in str(exc)
    else:
        raise AssertionError("reload order removed registry instance-shadow fence")
    assert registry_shadow_dispatched is False


assert_exact_capability_fences()

provenance_guard = importlib.reload(provenance_guard)
assert_exact_capability_fences()

evidence = importlib.reload(evidence)
provenance_guard = importlib.reload(provenance_guard)
assert_exact_capability_fences()

provenance_guard = importlib.reload(provenance_guard)
evidence = importlib.reload(evidence)
assert_exact_capability_fences()
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_runtime_repair_self_reload_keeps_pristine_delegates_and_one_finder() -> None:
    script = r'''
import importlib
from pathlib import Path
import sys
import tempfile

import autosport._point_in_time_authority_runtime_repair as repair
import autosport._point_in_time_feature_provenance_guard as provenance_guard
import autosport.point_in_time_evidence as evidence
from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    membership_manifest_sha256,
)
from autosport.scientific_registry import DatasetSnapshot, FeatureSet, ScientificRegistry

MARKER = "_autosport_point_in_time_reload_finder_v1"


def finder_count():
    return sum(bool(getattr(finder, MARKER, False)) for finder in sys.meta_path)


def assert_fail_closed_bind_and_holdout_load(root: Path):
    workspace = root / "feature-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    feature_set = FeatureSet(
        feature_set_id="self-reload.feature.v1",
        version="v1",
        definition_sha256="b" * 64,
        source_sha256="c" * 64,
        available_at_utc="2026-09-20T10:01:30Z",
    )
    provisional = DatasetSnapshot(
        dataset_snapshot_id="self-reload-snapshot",
        manifest_sha256="a" * 64,
        source_identity="provider:self-reload",
        license_identity="terms:v1",
        causal_cutoff="2026-09-20T10:00:00Z",
        available_at_utc="2026-09-20T10:01:00Z",
    )
    provenance = evidence.FeatureArtifactProvenance.issue(
        dataset_snapshot=provisional,
        feature_set=feature_set,
        feature_payload=b"self-reload-canonical-payload",
    )
    members = (provenance.provenance_sha256,)
    snapshot = DatasetSnapshot(
        dataset_snapshot_id=provisional.dataset_snapshot_id,
        manifest_sha256=membership_manifest_sha256(members),
        source_identity=provisional.source_identity,
        license_identity=provisional.license_identity,
        causal_cutoff=provisional.causal_cutoff,
        available_at_utc=provisional.available_at_utc,
    )
    registry = ScientificRegistry.initialize_pristine(workspace / "registry.json")
    registry.append(snapshot)
    registry.append(feature_set)
    lineage = DatasetSnapshotLineageAuthority.initialize_pristine(
        workspace / "lineage.json",
        registry,
        authority_root=root / "lineage-authority",
    )
    lineage.register(snapshot_id=snapshot.dataset_snapshot_id, member_sha256=members)
    try:
        evidence.PointInTimeFeatureAuthority.bind(
            dataset_snapshot=snapshot,
            feature_set=feature_set,
            feature_provenance=provenance,
            lineage_authority=lineage,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )
    except evidence.PointInTimeEvidenceError as exc:
        assert "independent source-owned feature artifact authority" in str(exc)
    else:
        raise AssertionError("self-authored feature lineage minted positive evidence")

    holdout_workspace = root / "holdout-workspace"
    holdout_workspace.mkdir(parents=True, exist_ok=True)
    ledger = evidence.HoldoutConsumptionLedger(
        holdout_workspace / "holdout.json",
        authority_root=root / "holdout-authority",
    )
    assert ledger.records() == ()


assert finder_count() == 1
repair = importlib.reload(repair)
assert finder_count() == 1
repair = importlib.reload(repair)
assert finder_count() == 1

with tempfile.TemporaryDirectory() as directory:
    assert_fail_closed_bind_and_holdout_load(Path(directory))

# The sibling reload hooks still have exactly one effective meta-path owner after
# runtime-repair self-reload.  Repeating both orders must not stack a second hook.
evidence = importlib.reload(evidence)
provenance_guard = importlib.reload(provenance_guard)
assert finder_count() == 1
provenance_guard = importlib.reload(provenance_guard)
evidence = importlib.reload(evidence)
assert finder_count() == 1
repair = importlib.reload(repair)
assert finder_count() == 1
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_preimport_marker_spoof_cannot_suppress_canonical_reload_finder() -> None:
    script = r'''
import importlib
import importlib.abc
import inspect
import sys

MARKER = "_autosport_point_in_time_reload_finder_v1"

class SpoofFinder(importlib.abc.MetaPathFinder):
    _autosport_point_in_time_reload_finder_v1 = True

    def find_spec(self, fullname, path=None, target=None):
        return None

spoof = SpoofFinder()
sys.meta_path.insert(0, spoof)

import autosport._point_in_time_authority_runtime_repair as repair
import autosport._point_in_time_feature_provenance_guard as provenance_guard
import autosport.point_in_time_evidence as evidence
from autosport.dataset_snapshot_lineage import DatasetSnapshotLineageAuthority

canonical = repair._CANONICAL_RELOAD_FINDER
assert canonical is not spoof
assert any(finder is canonical for finder in sys.meta_path)
assert sum(finder is canonical for finder in sys.meta_path) == 1
# The forged marker remains visible, proving marker count/attribute is not used as
# possession authority by the installer.
assert sum(bool(getattr(finder, MARKER, False)) for finder in sys.meta_path) >= 2


def assert_guarded():
    parameters = inspect.signature(evidence.PointInTimeFeatureAuthority.bind).parameters
    assert "lineage_authority" in parameters

    class ForgedLineageAuthority(DatasetSnapshotLineageAuthority):
        pass

    forged = object.__new__(ForgedLineageAuthority)
    try:
        evidence.PointInTimeFeatureAuthority.bind(
            dataset_snapshot=object(),
            feature_set=object(),
            feature_provenance=object(),
            lineage_authority=forged,
            decision_cutoff_utc="2099-01-01T00:00:00Z",
        )
    except evidence.PointInTimeEvidenceError as exc:
        assert "lineage_authority must be an exact DatasetSnapshotLineageAuthority" in str(exc)
    else:
        raise AssertionError("marker spoof weakened the exact lineage capability fence")


assert_guarded()
evidence = importlib.reload(evidence)
assert_guarded()
provenance_guard = importlib.reload(provenance_guard)
assert_guarded()
repair = importlib.reload(repair)
assert repair._CANONICAL_RELOAD_FINDER is canonical
assert sum(finder is canonical for finder in sys.meta_path) == 1
evidence = importlib.reload(evidence)
assert_guarded()
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(os.name == "nt", reason="Windows intentionally has no directory fsync contract")
def test_holdout_atomic_publication_fails_when_directory_open_for_sync_is_unavailable(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "holdout.json"
    real_open = os.open

    def fail_only_directory_open(path, flags, *args, **kwargs):
        if Path(path) == tmp_path:
            raise OSError("directory open unavailable")
        return real_open(path, flags, *args, **kwargs)

    with patch(
        "autosport._point_in_time_authority_runtime_repair.os.open",
        side_effect=fail_only_directory_open,
    ):
        with pytest.raises(
            EvidenceLedgerCorruptError,
            match="cannot open holdout ledger directory for durability",
        ):
            evidence._atomic_write_json(state_path, {"schema_version": 1})


@pytest.mark.skipif(os.name == "nt", reason="Windows intentionally has no directory fsync contract")
def test_holdout_atomic_publication_fails_when_directory_fsync_fails(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "holdout.json"
    real_fsync = os.fsync

    def fail_only_directory_fsync(descriptor: int):
        target = Path(f"/proc/self/fd/{descriptor}")
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError):
            return real_fsync(descriptor)
        if resolved == tmp_path:
            raise OSError("directory fsync unavailable")
        return real_fsync(descriptor)

    with patch(
        "autosport._point_in_time_authority_runtime_repair.os.fsync",
        side_effect=fail_only_directory_fsync,
    ):
        with pytest.raises(
            EvidenceLedgerCorruptError,
            match="cannot fsync holdout ledger directory for durability",
        ):
            evidence._atomic_write_json(state_path, {"schema_version": 1})
