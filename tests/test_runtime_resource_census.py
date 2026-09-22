from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from autosport.runtime_resource_census import (
    OwnedThreadResource,
    RuntimeResourceCensus,
    capture_runtime_resource_census,
    owned_thread_keys,
    probe_disposable_workspace_move_delete,
    probe_workspace_move_round_trip,
    residual_owned_threads,
    stable_evidence_sha256,
)


def test_census_tracks_only_live_autosport_owned_thread_names() -> None:
    baseline = capture_runtime_resource_census()
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        entered.set()
        release.wait()

    thread = threading.Thread(
        target=hold,
        name="foreign-resource-sentinel",
        daemon=False,
    )
    thread.start()
    assert entered.wait(5.0)
    try:
        during = capture_runtime_resource_census()
        assert residual_owned_threads(baseline, during) == ()
    finally:
        release.set()
        thread.join(5.0)
    assert not thread.is_alive()


def test_seeded_owned_thread_is_detected_and_restores_baseline() -> None:
    baseline = capture_runtime_resource_census()
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        entered.set()
        release.wait()

    thread = threading.Thread(
        target=hold,
        name="autosport-resource-test-leak",
        daemon=False,
    )
    thread.start()
    assert entered.wait(5.0)
    try:
        during = capture_runtime_resource_census()
        residual = residual_owned_threads(baseline, during)
        assert any(item.name == "autosport-resource-test-leak" for item in residual)
    finally:
        release.set()
        thread.join(5.0)

    assert not thread.is_alive()
    assert residual_owned_threads(baseline, capture_runtime_resource_census()) == ()


def test_residual_census_is_multiplicity_aware() -> None:
    baseline = RuntimeResourceCensus(
        (
            OwnedThreadResource(
                name="autosport-worker",
                daemon=False,
                ident=1,
            ),
        )
    )
    current = RuntimeResourceCensus(
        (
            OwnedThreadResource(
                name="autosport-worker",
                daemon=False,
                ident=2,
            ),
            OwnedThreadResource(
                name="autosport-worker",
                daemon=False,
                ident=3,
            ),
        )
    )

    residual = residual_owned_threads(baseline, current)

    assert len(residual) == 1
    assert owned_thread_keys(residual) == (("autosport-worker", False),)


def test_workspace_round_trip_and_disposable_delete_are_behavioral_probes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        shared = root / "shared"
        shared.mkdir()
        (shared / "state.txt").write_text("durable\n", encoding="utf-8")

        round_trip = probe_workspace_move_round_trip(shared)

        assert round_trip.status == "PASS"
        assert shared.is_dir()
        assert (shared / "state.txt").read_text(encoding="utf-8") == "durable\n"

        disposable = root / "disposable"
        disposable.mkdir()
        (disposable / "market.db").write_bytes(b"qualification")

        deleted = probe_disposable_workspace_move_delete(disposable)

        assert deleted.status == "PASS"
        assert not disposable.exists()
        assert not (root / ".disposable.autosport-resource-delete-probe").exists()


def test_workspace_probe_refuses_preexisting_probe_target() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "shared"
        workspace.mkdir()
        target = root / ".shared.autosport-resource-probe"
        target.mkdir()

        result = probe_workspace_move_round_trip(workspace)

        assert result.status == "FAIL"
        assert result.error_type == "FileExistsError"
        assert workspace.is_dir()
        assert target.is_dir()


def test_evidence_digest_is_canonical_across_mapping_order() -> None:
    first = {"z": 1, "a": {"two": 2, "one": 1}}
    second = {"a": {"one": 1, "two": 2}, "z": 1}

    assert stable_evidence_sha256(first) == stable_evidence_sha256(second)
