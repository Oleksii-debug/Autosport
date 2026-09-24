from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import autosport.campaign_precommit_manifest as precommit_module
from autosport.campaign_precommit_manifest import (
    RAW_MANIFEST_IS_PROSPECTIVE_AUTHORITY,
    CampaignPrecommitManifest,
    CampaignPrecommitManifestError,
    load_campaign_precommit_manifest,
    publish_campaign_precommit_manifest,
    resolve_campaign_precommit_publication_witness,
    write_campaign_precommit_manifest_once,
)
from autosport.monotonic_workspace_authority import (
    MonotonicAuthorityConflictError,
    MonotonicWorkspaceAuthority,
)


A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64
ZERO = "0" * 64
ONE = "1" * 64


def manifest(**overrides: object) -> CampaignPrecommitManifest:
    values: dict[str, object] = {
        "campaign_id": "paper-forward-2026-09-22",
        "source_id": "betfair:exchange",
        "source_snapshot_sha256": A,
        "committed_at": "2099-12-31T19:00:00Z",
        "observation_not_before": "2100-01-01T06:00:00Z",
        "observation_not_after": "2100-01-08T06:00:00Z",
        "evaluation_universe_sha256": B,
        "strategy_version_id": "strategy-v17",
        "champion_version_id": "model-v42",
        "baseline_version_id": "market-baseline-v3",
        "cost_contract_sha256": C,
        "multiplicity_policy_sha256": D,
        "stopping_policy_sha256": E,
        "restart_policy_sha256": F,
        "causal_evidence_policy_sha256": ZERO,
        "config_sha256": ONE,
    }
    values.update(overrides)
    return CampaignPrecommitManifest(**values)


def test_manifest_digest_is_deterministic_and_sensitive() -> None:
    first = manifest()
    second = manifest()
    assert first.manifest_sha256 == second.manifest_sha256
    assert replace(first, strategy_version_id="strategy-v18").manifest_sha256 != (
        first.manifest_sha256
    )


@pytest.mark.parametrize(
    "field",
    [
        "source_snapshot_sha256",
        "evaluation_universe_sha256",
        "cost_contract_sha256",
        "multiplicity_policy_sha256",
        "stopping_policy_sha256",
        "restart_policy_sha256",
        "causal_evidence_policy_sha256",
        "config_sha256",
    ],
)
def test_sha_fields_require_exact_lowercase_sha256(field: str) -> None:
    with pytest.raises(CampaignPrecommitManifestError):
        manifest(**{field: "A" * 64})
    with pytest.raises(CampaignPrecommitManifestError):
        manifest(**{field: "a" * 63})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("campaign_id", ""),
        ("campaign_id", " campaign"),
        ("source_id", "source "),
        ("strategy_version_id", "strategy\x00v1"),
        ("champion_version_id", 42),
        ("baseline_version_id", True),
    ],
)
def test_text_identity_fields_fail_closed(field: str, value: object) -> None:
    with pytest.raises(CampaignPrecommitManifestError):
        manifest(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("committed_at", "2026-09-21T21:00:00+02:00"),
        ("committed_at", "2026-09-21T19:00:00"),
        ("observation_not_before", "2026-09-22T08:00:00+02:00"),
        ("observation_not_after", "2026-09-29T06:00:00+00:00"),
    ],
)
def test_timestamps_require_canonical_utc_z_form(field: str, value: str) -> None:
    with pytest.raises(CampaignPrecommitManifestError):
        manifest(**{field: value})


def test_precommit_must_precede_first_observation() -> None:
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="before prospective observation",
    ):
        manifest(committed_at="2100-01-01T06:00:00Z")


def test_observation_window_must_be_nonempty() -> None:
    with pytest.raises(CampaignPrecommitManifestError):
        manifest(observation_not_after="2100-01-01T06:00:00Z")


def test_champion_and_baseline_must_be_distinct() -> None:
    with pytest.raises(CampaignPrecommitManifestError):
        manifest(baseline_version_id="model-v42")


def test_round_trip_record_preserves_identity() -> None:
    original = manifest()
    restored = CampaignPrecommitManifest.from_record(original.to_record())
    assert restored == original
    assert restored.manifest_sha256 == original.manifest_sha256


def test_schema_version_bool_alias_fails_closed() -> None:
    record = manifest().to_record()
    record["schema_version"] = True
    with pytest.raises(CampaignPrecommitManifestError):
        CampaignPrecommitManifest.from_record(record)


def test_unknown_field_fails_closed() -> None:
    record = manifest().to_record()
    record["outcome"] = "WIN"
    with pytest.raises(CampaignPrecommitManifestError):
        CampaignPrecommitManifest.from_record(record)


def test_tampered_payload_with_old_digest_fails_closed() -> None:
    record = manifest().to_record()
    record["strategy_version_id"] = "strategy-after-seeing-results"
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="digest mismatch",
    ):
        CampaignPrecommitManifest.from_record(record)


def test_tampered_digest_fails_closed() -> None:
    record = manifest().to_record()
    record["manifest_sha256"] = A
    with pytest.raises(CampaignPrecommitManifestError):
        CampaignPrecommitManifest.from_record(record)


def test_write_once_round_trip_and_identical_retry(tmp_path: Path) -> None:
    original = manifest()
    path = tmp_path / "evidence" / "precommit.json"
    path.parent.mkdir()
    first = write_campaign_precommit_manifest_once(path, original)
    second = write_campaign_precommit_manifest_once(path, original)
    assert first == second == original.manifest_sha256
    assert load_campaign_precommit_manifest(path) == original
    assert not tuple(path.parent.glob(f".{path.name}.*.tmp"))


def test_write_once_ignores_stranded_partial_temp_from_prior_crash(
    tmp_path: Path,
) -> None:
    original = manifest()
    path = tmp_path / "evidence" / "precommit.json"
    path.parent.mkdir()
    stranded = path.parent / f".{path.name}.prior-crash.tmp"
    stranded.write_bytes(b'{"partial":')

    digest = write_campaign_precommit_manifest_once(path, original)

    assert digest == original.manifest_sha256
    assert load_campaign_precommit_manifest(path) == original
    assert stranded.read_bytes() == b'{"partial":'


def test_write_once_refuses_implicit_parent_lineage_creation(tmp_path: Path) -> None:
    path = tmp_path / "new-workspace" / "evidence" / "precommit.json"

    with pytest.raises(
        CampaignPrecommitManifestError,
        match="parent directory must already exist",
    ):
        write_campaign_precommit_manifest_once(path, manifest())

    assert not path.parent.exists()
    assert not path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative parent binding")
def test_write_once_rejects_symlinked_parent_lineage(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_parent = real_root / "evidence"
    real_parent.mkdir(parents=True)
    redirected_root = tmp_path / "redirected-root"
    redirected_root.symlink_to(real_root, target_is_directory=True)
    path = redirected_root / "evidence" / "precommit.json"

    with pytest.raises(
        CampaignPrecommitManifestError,
        match="without symlink redirection",
    ):
        write_campaign_precommit_manifest_once(path, manifest())

    assert not (real_parent / "precommit.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative target binding")
def test_write_once_and_loader_reject_existing_target_symlink(tmp_path: Path) -> None:
    original = manifest()
    authoritative = tmp_path / "authoritative.json"
    write_campaign_precommit_manifest_once(authoritative, original)
    redirected = tmp_path / "redirected.json"
    redirected.symlink_to(authoritative)

    with pytest.raises(
        CampaignPrecommitManifestError,
        match="cannot verify existing campaign precommit manifest",
    ):
        write_campaign_precommit_manifest_once(redirected, original)

    with pytest.raises(
        CampaignPrecommitManifestError,
        match="cannot verify existing campaign precommit manifest",
    ):
        load_campaign_precommit_manifest(redirected)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative parent binding")
def test_write_once_fails_if_parent_identity_changes_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "evidence" / "precommit.json"
    path.parent.mkdir()
    moved_parent = tmp_path / "evidence-moved"
    original_open = precommit_module._open_bound_posix_parent_directory

    def bind_then_swap(parent: Path) -> tuple[int, Path]:
        descriptor, absolute = original_open(parent)
        Path(parent).rename(moved_parent)
        Path(parent).mkdir()
        return descriptor, absolute

    monkeypatch.setattr(
        precommit_module,
        "_open_bound_posix_parent_directory",
        bind_then_swap,
    )

    with pytest.raises(
        CampaignPrecommitManifestError,
        match="parent identity changed during publication",
    ):
        write_campaign_precommit_manifest_once(path, manifest())

    assert not path.exists()
    assert (moved_parent / "precommit.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound parent identity")
def test_windows_write_once_fails_if_parent_identity_changes_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "evidence" / "precommit.json"
    path.parent.mkdir()
    moved_parent = tmp_path / "evidence-moved"
    original_open = precommit_module._open_bound_windows_parent_directory
    swapped = False

    def bind_then_swap(
        parent: Path,
    ) -> tuple[int, Path, tuple[int, int, int]]:
        nonlocal swapped
        handle, absolute, identity = original_open(parent)
        if not swapped:
            Path(parent).rename(moved_parent)
            Path(parent).mkdir()
            swapped = True
        return handle, absolute, identity

    monkeypatch.setattr(
        precommit_module,
        "_open_bound_windows_parent_directory",
        bind_then_swap,
    )

    with pytest.raises(
        CampaignPrecommitManifestError,
        match="parent identity changed during publication",
    ):
        write_campaign_precommit_manifest_once(path, manifest())

    assert not path.exists()
    assert (moved_parent / "precommit.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound restart identity")
def test_windows_loader_rejects_moved_parent_even_with_identical_decoy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "evidence" / "precommit.json"
    path.parent.mkdir()
    write_campaign_precommit_manifest_once(path, manifest())
    expected = path.read_bytes()
    moved_parent = tmp_path / "evidence-moved"
    original_open = precommit_module._open_bound_windows_parent_directory
    swapped = False

    def bind_then_swap(
        parent: Path,
    ) -> tuple[int, Path, tuple[int, int, int]]:
        nonlocal swapped
        handle, absolute, identity = original_open(parent)
        if not swapped:
            Path(parent).rename(moved_parent)
            Path(parent).mkdir()
            path.write_bytes(expected)
            swapped = True
        return handle, absolute, identity

    monkeypatch.setattr(
        precommit_module,
        "_open_bound_windows_parent_directory",
        bind_then_swap,
    )

    with pytest.raises(
        CampaignPrecommitManifestError,
        match="parent identity changed during publication",
    ):
        load_campaign_precommit_manifest(path)

    assert path.read_bytes() == expected
    assert (moved_parent / "precommit.json").read_bytes() == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows junction/reparse contract")
def test_windows_write_once_rejects_junction_parent_lineage(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_parent = real_root / "evidence"
    real_parent.mkdir(parents=True)
    redirected_root = tmp_path / "redirected-root"

    result = subprocess.run(
        [
            "cmd",
            "/c",
            "mklink",
            "/J",
            str(redirected_root),
            str(real_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create Windows test junction: {result.stderr}")

    try:
        path = redirected_root / "evidence" / "precommit.json"
        with pytest.raises(
            CampaignPrecommitManifestError,
            match="reparse",
        ):
            write_campaign_precommit_manifest_once(path, manifest())
        assert not (real_parent / "precommit.json").exists()
    finally:
        if redirected_root.exists():
            os.rmdir(redirected_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows target-reparse contract")
def test_windows_write_once_and_loader_reject_target_symlink(tmp_path: Path) -> None:
    original = manifest()
    authoritative = tmp_path / "authoritative.json"
    write_campaign_precommit_manifest_once(authoritative, original)
    redirected = tmp_path / "redirected.json"
    try:
        redirected.symlink_to(authoritative)
    except OSError as exc:
        pytest.skip(f"cannot create Windows test symlink: {exc}")

    with pytest.raises(
        CampaignPrecommitManifestError,
        match="cannot verify existing campaign precommit manifest",
    ):
        write_campaign_precommit_manifest_once(redirected, original)
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="cannot verify existing campaign precommit manifest",
    ):
        load_campaign_precommit_manifest(redirected)


def test_write_once_refuses_conflicting_successor(tmp_path: Path) -> None:
    path = tmp_path / "precommit.json"
    write_campaign_precommit_manifest_once(path, manifest())
    first_bytes = path.read_bytes()
    changed = manifest(strategy_version_id="strategy-v18")
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="conflicts with precommit",
    ):
        write_campaign_precommit_manifest_once(path, changed)

    assert path.read_bytes() == first_bytes
    assert load_campaign_precommit_manifest(path) == manifest()


def test_manual_byte_tampering_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "precommit.json"
    original = manifest()
    write_campaign_precommit_manifest_once(path, original)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["baseline_version_id"] = "baseline-after-outcome"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CampaignPrecommitManifestError):
        load_campaign_precommit_manifest(path)


def test_loader_rejects_non_json_or_non_object(tmp_path: Path) -> None:
    path = tmp_path / "precommit.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(CampaignPrecommitManifestError):
        load_campaign_precommit_manifest(path)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(CampaignPrecommitManifestError):
        load_campaign_precommit_manifest(path)


def test_write_api_requires_exact_manifest_type(tmp_path: Path) -> None:
    with pytest.raises(CampaignPrecommitManifestError):
        write_campaign_precommit_manifest_once(tmp_path / "x.json", object())  # type: ignore[arg-type]


def test_record_does_not_claim_execution_or_readiness_authority() -> None:
    record = manifest().to_record()
    forbidden = {
        "real_money_execution",
        "human_tested",
        "nvda_verified",
        "whole_product_complete",
        "execution_authorized",
        "promotion_authorized",
        "settlement_authoritative",
    }
    assert forbidden.isdisjoint(record)


def test_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "precommit.json"
    encoded = json.dumps(manifest().to_record(), separators=(",", ":"))
    encoded = encoded.replace(
        '"campaign_id":"paper-forward-2026-09-22"',
        '"campaign_id":"forged","campaign_id":"paper-forward-2026-09-22"',
        1,
    )
    path.write_text(encoded, encoding="utf-8")
    with pytest.raises(CampaignPrecommitManifestError):
        load_campaign_precommit_manifest(path)


def test_loader_rejects_nonstandard_json_constants(tmp_path: Path) -> None:
    path = tmp_path / "precommit.json"
    encoded = json.dumps(manifest().to_record(), separators=(",", ":"))
    encoded = encoded.replace('"schema_version":1', '"schema_version":NaN', 1)
    path.write_text(encoded, encoding="utf-8")
    with pytest.raises(CampaignPrecommitManifestError):
        load_campaign_precommit_manifest(path)


@pytest.mark.parametrize("rewrite", ("whitespace", "key_order", "extra_newline"))
def test_loader_rejects_semantically_equivalent_noncanonical_bytes(
    tmp_path: Path, rewrite: str
) -> None:
    path = tmp_path / "precommit.json"
    original = manifest()
    write_campaign_precommit_manifest_once(path, original)
    record = original.to_record()

    if rewrite == "whitespace":
        rewritten = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    elif rewrite == "key_order":
        reversed_record = dict(reversed(tuple(record.items())))
        rewritten = json.dumps(
            reversed_record,
            ensure_ascii=False,
            separators=(",", ":"),
        ) + "\n"
    else:
        rewritten = path.read_text(encoding="utf-8") + "\n"

    path.write_text(rewritten, encoding="utf-8")
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="bytes are not canonical",
    ):
        load_campaign_precommit_manifest(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-clobber publication")
def test_posix_canonical_leaf_is_absent_until_complete_temp_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = manifest()
    path = tmp_path / "evidence" / "precommit.json"
    path.parent.mkdir()
    expected = precommit_module._canonical_bytes(original.to_record()) + b"\n"
    real_link = precommit_module.os.link
    observed_publish = False

    def inspect_then_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        nonlocal observed_publish
        observed_publish = True
        assert destination == path.name
        assert not path.exists()
        descriptor = os.open(
            source,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=src_dir_fd,
        )
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                assert handle.read() == expected
        finally:
            pass
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(precommit_module.os, "link", inspect_then_link)

    digest = write_campaign_precommit_manifest_once(path, original)

    assert observed_publish is True
    assert digest == original.manifest_sha256
    assert path.read_bytes() == expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-clobber publication")
def test_posix_publish_failure_leaves_canonical_absent_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = manifest()
    path = tmp_path / "evidence" / "precommit.json"
    path.parent.mkdir()
    real_link = precommit_module.os.link

    def fail_publish(*args: object, **kwargs: object) -> None:
        assert not path.exists()
        raise OSError("synthetic link failure")

    monkeypatch.setattr(precommit_module.os, "link", fail_publish)
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="atomically publish",
    ):
        write_campaign_precommit_manifest_once(path, original)

    assert not path.exists()
    assert not tuple(path.parent.glob(f".{path.name}.*.tmp"))

    monkeypatch.setattr(precommit_module.os, "link", real_link)
    digest = write_campaign_precommit_manifest_once(path, original)

    assert digest == original.manifest_sha256
    assert load_campaign_precommit_manifest(path) == original


@pytest.mark.skipif(os.name == "nt", reason="Windows has no directory-fsync contract")
def test_write_once_synchronizes_parent_directory_before_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nested" / "precommit.json"
    path.parent.mkdir()
    calls: list[int] = []

    monkeypatch.setattr(
        precommit_module,
        "_fsync_bound_parent_directory",
        lambda directory_fd: calls.append(directory_fd),
    )

    digest = write_campaign_precommit_manifest_once(path, manifest())

    assert digest == manifest().manifest_sha256
    assert len(calls) == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows has no directory-fsync contract")
def test_parent_directory_sync_failure_fails_closed_and_retry_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "precommit.json"
    original_sync = precommit_module._fsync_bound_parent_directory
    attempts = 0

    def fail_sync(directory_fd: int) -> None:
        nonlocal attempts
        attempts += 1
        raise CampaignPrecommitManifestError("synthetic directory durability failure")

    monkeypatch.setattr(
        precommit_module,
        "_fsync_bound_parent_directory",
        fail_sync,
    )
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="synthetic directory durability failure",
    ):
        write_campaign_precommit_manifest_once(path, manifest())

    assert attempts == 1
    assert path.exists()

    monkeypatch.setattr(
        precommit_module,
        "_fsync_bound_parent_directory",
        original_sync,
    )
    digest = write_campaign_precommit_manifest_once(path, manifest())

    assert digest == manifest().manifest_sha256
    assert load_campaign_precommit_manifest(path) == manifest()


def test_backdated_late_first_publication_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "backdated.json"
    backdated = manifest(
        campaign_id="paper-forward-backdated",
        committed_at="1999-12-31T23:59:59Z",
        observation_not_before="2000-01-01T00:00:00Z",
        observation_not_after="2100-01-01T00:00:00Z",
    )

    with pytest.raises(
        CampaignPrecommitManifestError,
        match="first campaign precommit publication must precede prospective observation",
    ):
        write_campaign_precommit_manifest_once(target, backdated)

    assert not target.exists()


def test_late_exact_retry_remains_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "precommit.json"
    original = manifest()

    monkeypatch.setattr(
        precommit_module,
        "_publication_deadline_reached",
        lambda _deadline: False,
    )
    digest = write_campaign_precommit_manifest_once(target, original)
    first_bytes = target.read_bytes()

    monkeypatch.setattr(
        precommit_module,
        "_publication_deadline_reached",
        lambda _deadline: True,
    )
    assert write_campaign_precommit_manifest_once(target, original) == digest
    assert target.read_bytes() == first_bytes


def test_boundary_crossed_after_temp_flush_never_publishes_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "precommit.json"
    original = manifest()
    checks = iter((False, True))

    monkeypatch.setattr(
        precommit_module,
        "_publication_deadline_reached",
        lambda _deadline: next(checks),
    )
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="first campaign precommit publication must precede prospective observation",
    ):
        write_campaign_precommit_manifest_once(target, original)

    assert not target.exists()
    assert not tuple(tmp_path.glob(f".{target.name}.*.tmp"))

    monkeypatch.setattr(
        precommit_module,
        "_publication_deadline_reached",
        lambda _deadline: False,
    )
    assert (
        write_campaign_precommit_manifest_once(target, original)
        == original.manifest_sha256
    )
    assert load_campaign_precommit_manifest(target) == original



def _publication_workspace(
    tmp_path: Path,
) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    evidence = workspace / "evidence"
    evidence.mkdir(parents=True)
    return workspace, evidence / "precommit.json", tmp_path / "machine-authority"


def test_raw_manifest_bytes_are_not_standalone_prospective_authority() -> None:
    assert RAW_MANIFEST_IS_PROSPECTIVE_AUTHORITY is False


def test_monotonic_publication_witness_round_trip_and_helper_rebind_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, path, authority_root = _publication_workspace(tmp_path)
    original = manifest()

    first = publish_campaign_precommit_manifest(
        path,
        original,
        workspace=workspace,
        authority_root=authority_root,
    )
    resolved = resolve_campaign_precommit_publication_witness(
        path,
        workspace=workspace,
        authority_root=authority_root,
    )

    assert first == resolved
    assert first.manifest_sha256 == original.manifest_sha256
    observed = datetime.fromisoformat(
        first.post_publish_observed_at.replace("Z", "+00:00")
    )
    assert observed < datetime.fromisoformat(
        original.observation_not_before.replace("Z", "+00:00")
    )
    assert first.target_relative_path == "evidence/precommit.json"
    assert first.authority_generation == 2

    # These module-level helpers remain a deterministic seam for the low-level
    # non-authorizing byte writer. They are not positive publication authority.
    monkeypatch.setattr(
        precommit_module,
        "_publication_deadline_reached",
        lambda _deadline: True,
    )
    monkeypatch.setattr(
        precommit_module,
        "_publication_now_text",
        lambda: "2200-01-01T00:00:00.000000Z",
    )
    retry = publish_campaign_precommit_manifest(
        path,
        original,
        workspace=workspace,
        authority_root=authority_root,
    )
    assert retry == first


def test_existing_raw_manifest_cannot_be_retroactively_promoted(
    tmp_path: Path,
) -> None:
    workspace, path, authority_root = _publication_workspace(tmp_path)
    original = manifest()

    write_campaign_precommit_manifest_once(path, original)

    with pytest.raises(
        CampaignPrecommitManifestError,
        match="cannot be retroactively qualified",
    ):
        publish_campaign_precommit_manifest(
            path,
            original,
            workspace=workspace,
            authority_root=authority_root,
        )


def test_module_clock_rebinding_cannot_mint_past_deadline_witness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, path, authority_root = _publication_workspace(tmp_path)
    original = manifest(
        committed_at="2019-12-31T23:00:00Z",
        observation_not_before="2020-01-01T00:00:00Z",
        observation_not_after="2020-01-02T00:00:00Z",
    )
    forged_before = datetime(2019, 12, 31, 23, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(precommit_module, "_publication_now", lambda: forged_before)
    monkeypatch.setattr(
        precommit_module,
        "_publication_deadline_reached",
        lambda _deadline: False,
    )
    monkeypatch.setattr(
        precommit_module,
        "_publication_now_text",
        lambda: "2019-12-31T23:30:00.000000Z",
    )
    monkeypatch.setattr(
        precommit_module,
        "_publication_observed_instant",
        lambda _value: forged_before,
    )
    monkeypatch.setattr(
        precommit_module,
        "_instant",
        lambda _value: datetime(2200, 1, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(
        CampaignPrecommitManifestError,
        match="first campaign precommit publication must precede prospective observation",
    ):
        publish_campaign_precommit_manifest(
            path,
            original,
            workspace=workspace,
            authority_root=authority_root,
        )

    assert not path.exists()


def test_pending_witness_recovers_original_preboundary_observation_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, path, authority_root = _publication_workspace(tmp_path)
    original = manifest(
        committed_at="2099-12-31T23:00:00Z",
        observation_not_before="2100-01-01T00:00:00Z",
        observation_not_after="2100-01-02T00:00:00Z",
    )

    real_commit = MonotonicWorkspaceAuthority.commit

    def interrupt_witness_commit(
        self: MonotonicWorkspaceAuthority,
        **kwargs: object,
    ) -> object:
        tx_id = kwargs.get("tx_id")
        if isinstance(tx_id, str) and tx_id.startswith(
            precommit_module._WITNESS_TX_PREFIX
        ):
            raise MonotonicAuthorityConflictError("simulated crash before witness COMMIT")
        return real_commit(self, **kwargs)

    monkeypatch.setattr(
        precommit_module.MonotonicWorkspaceAuthority,
        "commit",
        interrupt_witness_commit,
    )
    with pytest.raises(
        CampaignPrecommitManifestError,
        match="cannot commit campaign precommit publication witness",
    ):
        publish_campaign_precommit_manifest(
            path,
            original,
            workspace=workspace,
            authority_root=authority_root,
        )

    monkeypatch.setattr(
        precommit_module.MonotonicWorkspaceAuthority,
        "commit",
        real_commit,
    )
    monkeypatch.setattr(
        precommit_module,
        "_publication_now_text",
        lambda: "2200-01-01T00:00:00.000000Z",
    )

    recovered = publish_campaign_precommit_manifest(
        path,
        original,
        workspace=workspace,
        authority_root=authority_root,
    )
    recovered_observed = datetime.fromisoformat(
        recovered.post_publish_observed_at.replace("Z", "+00:00")
    )
    assert recovered_observed < datetime.fromisoformat(
        original.observation_not_before.replace("Z", "+00:00")
    )
    assert recovered.authority_generation == 2
    assert resolve_campaign_precommit_publication_witness(
        path,
        workspace=workspace,
        authority_root=authority_root,
    ) == recovered


def test_committed_witness_rejects_manifest_digest_or_path_drift(
    tmp_path: Path,
) -> None:
    workspace, path, authority_root = _publication_workspace(tmp_path)
    original = manifest()
    witness = publish_campaign_precommit_manifest(
        path,
        original,
        workspace=workspace,
        authority_root=authority_root,
    )

    moved = path.with_name("copied-precommit.json")
    moved.write_bytes(path.read_bytes())
    with pytest.raises(CampaignPrecommitManifestError):
        resolve_campaign_precommit_publication_witness(
            moved,
            workspace=workspace,
            authority_root=authority_root,
        )

    path.unlink()
    changed = replace(original, strategy_version_id="strategy-v18")
    write_campaign_precommit_manifest_once(path, changed)
    with pytest.raises(CampaignPrecommitManifestError):
        resolve_campaign_precommit_publication_witness(
            path,
            workspace=workspace,
            authority_root=authority_root,
        )

    assert witness.manifest_sha256 != changed.manifest_sha256


def test_publication_witness_target_must_remain_inside_workspace(
    tmp_path: Path,
) -> None:
    workspace, _path, authority_root = _publication_workspace(tmp_path)
    outside = tmp_path / "outside.json"

    with pytest.raises(
        CampaignPrecommitManifestError,
        match="inside the protected workspace",
    ):
        publish_campaign_precommit_manifest(
            outside,
            manifest(),
            workspace=workspace,
            authority_root=authority_root,
        )
