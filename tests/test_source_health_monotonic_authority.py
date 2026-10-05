from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import autosport.ingestion_health as health_module
from autosport.continuous_observation import (
    ContinuousObservationConfig,
    run_continuous_observation,
)
from autosport.ingestion_health import SourceHealthState, SourceHealthStore
from autosport.monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicAuthorityRecoveryRequiredError,
    MonotonicAuthorityRollbackError,
)


T0 = "2026-10-04T00:00:00Z"
T1 = "2026-10-04T00:01:00Z"
T2 = "2026-10-04T00:02:00Z"


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "workspace") -> SourceHealthStore:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path / "machine-authority").resolve()),
    )
    return SourceHealthStore(tmp_path / name / "source-health.json")


def _record_success(store: SourceHealthStore) -> None:
    state = store.record_success(
        "provider-a",
        now=T0,
        received=1,
        accepted=1,
        rejected=0,
        cursor="cursor-1",
        latest_source_ts=T0,
        quality_flags=(),
    )
    assert state.status == "healthy"


def _record_provider_failure(store: SourceHealthStore) -> None:
    state = store.record_failure(
        "provider-a",
        now=T1,
        error=RuntimeError("provider unavailable"),
        failure_kind="provider_unavailable",
    )
    assert state.status == "failed"
    assert state.last_failure_kind == "provider_unavailable"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_success_cas_rejects_subclass_equality_forgery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "cas-subclass")
    _record_success(store)
    current = store.get("provider-a")

    class ForgedExpected(SourceHealthState):
        def validate(self) -> None:
            return None

        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

    forged = ForgedExpected(source_id="provider-a")
    assert current == forged

    with pytest.raises(
        TypeError,
        match="expected_before must be exact SourceHealthState",
    ):
        store.record_success_if_current(
            forged,
            now=T2,
            received=1,
            accepted=1,
            rejected=0,
            cursor="cursor-2",
            latest_source_ts=T2,
            quality_flags=(),
        )

    assert store.get("provider-a") == current


def test_success_cas_rejects_subclassed_ambiguous_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "cas-ambiguous-subclass")
    _record_success(store)
    current = store.get("provider-a")

    class ForgedAmbiguous(SourceHealthState):
        def validate(self) -> None:
            return None

        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

    forged = ForgedAmbiguous(source_id="provider-a")
    assert current == forged

    with pytest.raises(
        TypeError,
        match="ambiguous_after must be exact SourceHealthState or null",
    ):
        store.record_success_if_current(
            current,
            ambiguous_after=forged,
            now=T2,
            received=1,
            accepted=1,
            rejected=0,
            cursor="cursor-2",
            latest_source_ts=T2,
            quality_flags=(),
        )

    assert store.get("provider-a") == current


def test_success_cas_rejects_string_subclass_field_equality_forgery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "cas-string-field-subclass")
    _record_success(store)
    current = store.get("provider-a")
    forged = store.get("provider-a")

    class AlwaysEqualText(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

    forged.last_cursor = AlwaysEqualText("not-the-durable-cursor")
    # This is the exact DTO class; only one primitive field carries hostile
    # equality. Before the boundary repair dataclass equality could therefore
    # make a stale caller assertion compare equal to the durable state.
    assert type(forged) is SourceHealthState
    assert current == forged

    with pytest.raises(
        ValueError,
        match="last_cursor must be an exact string or null",
    ):
        store.record_success_if_current(
            forged,
            now=T2,
            received=1,
            accepted=1,
            rejected=0,
            cursor="cursor-2",
            latest_source_ts=T2,
            quality_flags=(),
        )

    assert store.get("provider-a") == current


def test_success_cas_rejects_integer_subclass_field_equality_forgery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "cas-integer-field-subclass")
    _record_success(store)
    current = store.get("provider-a")
    forged = store.get("provider-a")

    class AlwaysEqualInt(int):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

    forged.poll_count = AlwaysEqualInt(999)
    assert type(forged) is SourceHealthState
    assert current == forged

    with pytest.raises(
        ValueError,
        match="poll_count must be a non-negative integer",
    ):
        store.record_success_if_current(
            forged,
            now=T2,
            received=1,
            accepted=1,
            rejected=0,
            cursor="cursor-2",
            latest_source_ts=T2,
            quality_flags=(),
        )

    assert store.get("provider-a") == current


def test_replay_cutoff_rejects_datetime_subclass_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "as-of-datetime-subclass")
    _record_success(store)
    _record_provider_failure(store)

    class ForgedAsOf(datetime):
        def astimezone(self, tz=None):
            return datetime.max.replace(tzinfo=timezone.utc)

    forged_cutoff = ForgedAsOf(
        2026,
        10,
        4,
        0,
        0,
        30,
        tzinfo=timezone.utc,
    )
    with pytest.raises(TypeError, match="as_of must be an exact datetime"):
        store.get_as_of("provider-a", as_of=forged_cutoff)


def test_success_cas_snapshots_mutable_expected_state_before_writer_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "cas-entry-snapshot")
    _record_success(store)
    stale_expected = store.get("provider-a")
    _record_provider_failure(store)
    current = store.get("provider-a")
    assert stale_expected != current

    canonical_guard = store._writer_guard

    class MutatingGuard:
        def __enter__(self):
            self._inner = canonical_guard()
            self._inner.__enter__()
            for name in SourceHealthState.__dataclass_fields__:
                setattr(stale_expected, name, getattr(current, name))
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self._inner.__exit__(exc_type, exc_value, traceback)

    monkeypatch.setattr(store, "_writer_guard", lambda: MutatingGuard())

    with pytest.raises(
        RuntimeError,
        match="changed since the committed ingestion outcome",
    ):
        store.record_success_if_current(
            stale_expected,
            now=T2,
            received=1,
            accepted=1,
            rejected=0,
            cursor="cursor-2",
            latest_source_ts=T2,
            quality_flags=(),
        )

    # The caller object was changed after method entry, proving the race was
    # exercised, but the durable CAS decision stayed bound to the entry snapshot.
    assert stale_expected == current
    assert store.get("provider-a") == current


def test_source_health_authority_key_matches_windows_filename_identity() -> None:
    lower = Path("source_health.json")
    upper = Path("SOURCE_HEALTH.JSON")

    assert health_module._source_health_authority_key(
        lower,
        windows=True,
    ) == health_module._source_health_authority_key(
        upper,
        windows=True,
    )
    assert health_module._source_health_authority_key(
        lower,
        windows=False,
    ) != health_module._source_health_authority_key(
        upper,
        windows=False,
    )


def test_valid_old_source_health_image_is_rejected_after_newer_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch)
    _record_success(store)
    valid_old = store.path.read_bytes()

    _record_provider_failure(store)
    current = store.path.read_bytes()
    assert current != valid_old

    # Both the projection and causal history are internally valid at T0. Restoring
    # those bytes must not erase the newer provider-unavailable/backoff evidence.
    store.path.write_bytes(valid_old)

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="rolled back|unproven|authority|match",
    ):
        SourceHealthStore(store.path)

    assert store.path.read_bytes() == valid_old


def test_deleting_source_health_after_committed_history_cannot_rebootstrap_pristine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch)
    _record_success(store)
    _record_provider_failure(store)

    store.path.unlink()

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="rolled back|unproven|authority|match",
    ):
        SourceHealthStore(store.path)

    assert not store.path.exists()
    assert not store.path.with_suffix(store.path.suffix + ".tmp").exists()


def test_prepare_without_local_publish_is_aborted_on_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch)
    _record_success(store)

    observed = _sha256(store.path.read_bytes())
    intended = "f" * 64
    authority = store._monotonic_authority()
    binding = store._authority_binding(observed, intended, kind="PUBLISH")
    authority.prepare(
        tx_id="test-pre-publish-crash",
        observed_state_sha256=observed,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )

    reopened = SourceHealthStore(store.path)
    assert reopened.get("provider-a").status == "healthy"
    history = reopened._monotonic_authority().read_history()
    assert history[-1].phase is AuthorityPhase.ABORT
    assert history[-1].previous_committed_state_sha256 == observed


def test_published_bytes_without_commit_are_recovered_on_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "target")
    _record_success(store)
    observed_bytes = store.path.read_bytes()
    observed = _sha256(observed_bytes)

    # Generate a second, independently valid SourceHealthStore image with the exact
    # same prefix and one newer typed provider-unavailable transition. Authority
    # metadata is external and therefore does not alter the store bytes.
    future = _store(tmp_path, monkeypatch, "future")
    _record_success(future)
    _record_provider_failure(future)
    intended_bytes = future.path.read_bytes()
    intended = _sha256(intended_bytes)
    assert intended_bytes != observed_bytes

    authority = store._monotonic_authority()
    binding = store._authority_binding(observed, intended, kind="PUBLISH")
    authority.prepare(
        tx_id="test-post-publish-crash",
        observed_state_sha256=observed,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    store.path.write_bytes(intended_bytes)

    reopened = SourceHealthStore(store.path)
    state = reopened.get("provider-a")
    assert state.status == "failed"
    assert state.last_failure_kind == "provider_unavailable"

    history = reopened._monotonic_authority().read_history()
    assert history[-1].phase is AuthorityPhase.COMMIT
    assert history[-1].intended_state_sha256 == intended


def test_validated_legacy_image_establishes_one_baseline_then_rejects_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path / "machine-authority").resolve()),
    )
    path = tmp_path / "legacy-workspace" / "source-health.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{\n  "history": {},\n  "schema_version": 4,\n  "sources": {}\n}\n',
        encoding="utf-8",
    )
    legacy_bytes = path.read_bytes()

    store = SourceHealthStore(path)
    history = store._monotonic_authority().read_history()
    assert history[-1].phase is AuthorityPhase.COMMIT
    assert history[-1].intended_state_sha256 == _sha256(legacy_bytes)

    _record_success(store)
    path.write_bytes(legacy_bytes)

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="rolled back|unproven|authority|match",
    ):
        SourceHealthStore(path)


def test_long_lived_writer_recovers_published_prefix_before_next_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "target-live")
    _record_success(store)
    observed = _sha256(store.path.read_bytes())

    future = _store(tmp_path, monkeypatch, "future-live")
    _record_success(future)
    _record_provider_failure(future)
    intended_bytes = future.path.read_bytes()
    intended = _sha256(intended_bytes)

    authority = store._monotonic_authority()
    binding = store._authority_binding(observed, intended, kind="PUBLISH")
    authority.prepare(
        tx_id="test-live-post-publish-crash",
        observed_state_sha256=observed,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    store.path.write_bytes(intended_bytes)

    # An ordinary reader does not mutate independent authority to finish someone
    # else's in-flight publication; it fails closed while COMMIT is absent.
    with pytest.raises(
        MonotonicAuthorityRecoveryRequiredError,
        match="recovery|required|commit",
    ):
        store.get("provider-a")

    # A later writer holds the canonical SourceHealthStore lock, so it may recover
    # the exact prepared image before deriving and publishing its successor.
    recovered = store.record_success(
        "provider-a",
        now=T2,
        received=1,
        accepted=1,
        rejected=0,
        cursor="cursor-2",
        latest_source_ts=T2,
        quality_flags=(),
    )
    assert recovered.status == "healthy"
    assert recovered.last_failure_kind is None

    history = store._monotonic_authority().read_history()
    assert history[-1].phase is AuthorityPhase.COMMIT
    assert history[-1].intended_state_sha256 == _sha256(store.path.read_bytes())


def test_post_baseline_valid_v3_downgrade_is_rejected_as_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "downgrade")
    _record_success(store)

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["schema_version"] = 3
    for payload in raw["sources"].values():
        payload.pop("last_failure_kind")
        payload.pop("consecutive_failure_kind_count")
    for entries in raw["history"].values():
        for entry in entries:
            entry["state"].pop("last_failure_kind")
            entry["state"].pop("consecutive_failure_kind_count")
    store.path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # The image is internally valid schema-v3, but this exact workspace already
    # committed newer authority-bound bytes. Schema compatibility cannot be used
    # to bypass the monotonic high-water mark.
    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="rolled back|unproven|authority|match",
    ):
        SourceHealthStore(store.path)


def test_recovery_does_not_commit_published_prefix_until_reflush_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "reflush-target")
    _record_success(store)
    observed = _sha256(store.path.read_bytes())

    future = _store(tmp_path, monkeypatch, "reflush-future")
    _record_success(future)
    _record_provider_failure(future)
    intended_bytes = future.path.read_bytes()
    intended = _sha256(intended_bytes)

    authority = store._monotonic_authority()
    binding = store._authority_binding(observed, intended, kind="PUBLISH")
    authority.prepare(
        tx_id="test-reflush-required",
        observed_state_sha256=observed,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    store.path.write_bytes(intended_bytes)

    canonical_sync = health_module._sync_existing_file

    def fail_sync(_path: Path) -> None:
        raise OSError("simulated durability barrier failure")

    monkeypatch.setattr(health_module, "_sync_existing_file", fail_sync)
    with pytest.raises(OSError, match="durability barrier failure"):
        SourceHealthStore(store.path)

    # Failed durability proof must leave the independent authority at PREPARE.
    history = authority.read_history()
    assert history[-1].phase is AuthorityPhase.PREPARE

    monkeypatch.setattr(health_module, "_sync_existing_file", canonical_sync)
    reopened = SourceHealthStore(store.path)
    assert reopened.get("provider-a").status == "failed"
    history = authority.read_history()
    assert history[-1].phase is AuthorityPhase.COMMIT
    assert history[-1].intended_state_sha256 == intended


def test_constructor_revalidates_after_recovery_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "constructor-race")
    _record_success(store)
    valid_old = store.path.read_bytes()
    _record_provider_failure(store)
    current = store.path.read_bytes()
    assert current != valid_old

    canonical_recover = SourceHealthStore._recover_or_bootstrap_authority
    injected = False

    def rollback_after_recovery(
        self: SourceHealthStore,
        observed: str | None,
    ):
        nonlocal injected
        authority = canonical_recover(self, observed)
        if self.path == store.path and not injected:
            injected = True
            self.path.write_bytes(valid_old)
        return authority

    monkeypatch.setattr(
        SourceHealthStore,
        "_recover_or_bootstrap_authority",
        rollback_after_recovery,
    )

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="rolled back|unproven|authority|match",
    ):
        SourceHealthStore(store.path)

    assert injected is True
    assert store.path.read_bytes() == valid_old


def test_bootstrap_digest_is_from_the_exact_validated_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path / "machine-authority").resolve()),
    )

    initial_fixture = SourceHealthStore(
        tmp_path / "validated-fixture" / "source-health.json"
    )
    initial_bytes = initial_fixture.path.read_bytes()

    replacement_fixture = SourceHealthStore(
        tmp_path / "replacement-fixture" / "source-health.json"
    )
    _record_success(replacement_fixture)
    replacement_bytes = replacement_fixture.path.read_bytes()
    assert replacement_bytes != initial_bytes

    path = tmp_path / "snapshot-race" / "source-health.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(initial_bytes)

    canonical_snapshot = SourceHealthStore._read_snapshot
    swapped = False

    def swap_after_validated_snapshot(
        self: SourceHealthStore,
        *,
        verify_authority: bool = True,
    ) -> tuple[dict, str]:
        nonlocal swapped
        result = canonical_snapshot(self, verify_authority=verify_authority)
        if self.path == path and not verify_authority and not swapped:
            swapped = True
            path.write_bytes(replacement_bytes)
        return result

    monkeypatch.setattr(
        SourceHealthStore,
        "_read_snapshot",
        swap_after_validated_snapshot,
    )

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="changed during authority bootstrap",
    ):
        SourceHealthStore(path)

    assert swapped is True
    probe = object.__new__(SourceHealthStore)
    probe.path = path
    probe._lock_path = path.with_name(path.name + ".lock")
    assert probe._monotonic_authority().read_history() == ()


def test_bootstrap_rejects_bytes_changed_during_durability_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path / "machine-authority").resolve()),
    )

    initial_fixture = SourceHealthStore(
        tmp_path / "fixture-initial" / "source-health.json"
    )
    initial_bytes = initial_fixture.path.read_bytes()

    changed_fixture = SourceHealthStore(
        tmp_path / "fixture-changed" / "source-health.json"
    )
    _record_success(changed_fixture)
    changed_bytes = changed_fixture.path.read_bytes()
    assert changed_bytes != initial_bytes

    path = tmp_path / "bootstrap-race" / "source-health.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(initial_bytes)

    canonical_sync = health_module._sync_existing_file

    def replace_after_sync(sync_path: Path) -> None:
        canonical_sync(sync_path)
        if sync_path == path:
            path.write_bytes(changed_bytes)

    monkeypatch.setattr(health_module, "_sync_existing_file", replace_after_sync)

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="changed during authority bootstrap",
    ):
        SourceHealthStore(path)

    # The raced bytes must not acquire a COMMIT from the digest that was validated
    # before the durability barrier.
    probe = object.__new__(SourceHealthStore)
    probe.path = path
    probe._lock_path = path.with_name(path.name + ".lock")
    assert probe._monotonic_authority().read_history() == ()


def test_recovery_rehashes_after_barrier_before_committing_prepared_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "recovery-race-target")
    _record_success(store)
    observed_bytes = store.path.read_bytes()
    observed = _sha256(observed_bytes)

    future = _store(tmp_path, monkeypatch, "recovery-race-future")
    _record_success(future)
    _record_provider_failure(future)
    intended_bytes = future.path.read_bytes()
    intended = _sha256(intended_bytes)

    authority = store._monotonic_authority()
    binding = store._authority_binding(observed, intended, kind="PUBLISH")
    authority.prepare(
        tx_id="test-recovery-barrier-race",
        observed_state_sha256=observed,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    store.path.write_bytes(intended_bytes)

    canonical_sync = health_module._sync_existing_file

    def restore_previous_after_sync(sync_path: Path) -> None:
        canonical_sync(sync_path)
        if sync_path == store.path:
            store.path.write_bytes(observed_bytes)

    monkeypatch.setattr(
        health_module,
        "_sync_existing_file",
        restore_previous_after_sync,
    )

    reopened = SourceHealthStore(store.path)
    assert reopened.get("provider-a").status == "healthy"

    history = authority.read_history()
    assert history[-1].phase is AuthorityPhase.ABORT
    assert history[-1].previous_committed_state_sha256 == observed
    assert all(
        not (
            record.phase is AuthorityPhase.COMMIT
            and record.intended_state_sha256 == intended
        )
        for record in history
    )


def test_continuous_observation_rejects_health_rollback_before_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path / "machine-authority").resolve()),
    )
    workspace = tmp_path / "live-workspace"
    health = SourceHealthStore(workspace / "source_health.json")
    _record_success(health)
    valid_old = health.path.read_bytes()
    _record_provider_failure(health)
    health.path.write_bytes(valid_old)

    class _NoIoProvider:
        source_id = "provider-a"

        def __init__(self) -> None:
            self.calls = 0

        def read_batch(self, max_items: int = 1000):
            self.calls += 1
            raise AssertionError("provider I/O must not run after source-health rollback")

    provider = _NoIoProvider()
    config = ContinuousObservationConfig(
        workspace=workspace,
        max_cycles=1,
        max_runtime_seconds=30,
        interval_seconds=1,
        max_backoff_seconds=8,
        max_items=10,
    )

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="rolled back|unproven|authority|match",
    ):
        run_continuous_observation(
            provider,
            config,
            monotonic=lambda: 0.0,
            wall_clock=lambda: T2,
            waiter=lambda _seconds: False,
            reporter=None,
            run_id="rollback-must-stop-before-provider-io",
        )

    assert provider.calls == 0


def test_same_transition_can_retry_after_pre_publish_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path, monkeypatch, "abort-retry-target")
    _record_success(store)
    observed = _sha256(store.path.read_bytes())

    future = _store(tmp_path, monkeypatch, "abort-retry-future")
    _record_success(future)
    _record_provider_failure(future)
    intended = _sha256(future.path.read_bytes())

    authority = store._monotonic_authority()
    binding = store._authority_binding(observed, intended, kind="PUBLISH")
    first_tx_id = store._next_authority_tx_id(
        authority,
        observed,
        intended,
        binding,
    )
    authority.prepare(
        tx_id=first_tx_id,
        observed_state_sha256=observed,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )

    # Crash before local replace: reopening/repair observes the previous committed
    # image and therefore terminates the prepared generation with ABORT.
    recovery = authority.recover(observed_state_sha256=observed)
    assert recovery.record is not None
    assert recovery.record.phase is AuthorityPhase.ABORT

    state = store.record_failure(
        "provider-a",
        now=T1,
        error=RuntimeError("provider unavailable"),
        failure_kind="provider_unavailable",
    )
    assert state.status == "failed"

    history = authority.read_history()
    assert history[-1].phase is AuthorityPhase.COMMIT
    assert history[-1].intended_state_sha256 == intended
    assert history[-1].tx_id != first_tx_id


def test_first_legacy_baseline_can_retry_after_aborted_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path / "machine-authority").resolve()),
    )

    fixture = SourceHealthStore(tmp_path / "fixture-bootstrap" / "source-health.json")
    legacy_bytes = fixture.path.read_bytes()

    path = tmp_path / "legacy-bootstrap-retry" / "source-health.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(legacy_bytes)

    # Build the same authority identity without running SourceHealthStore.__init__
    # so the test can stop the first-ever baseline at PREPARE.
    probe = object.__new__(SourceHealthStore)
    probe.path = path
    probe._lock_path = path.with_name(path.name + ".lock")
    observed = _sha256(legacy_bytes)
    authority = probe._monotonic_authority()
    binding = probe._authority_binding(None, observed, kind="BOOTSTRAP")
    first_tx_id = probe._next_authority_tx_id(
        authority,
        None,
        observed,
        binding,
    )
    authority.prepare(
        tx_id=first_tx_id,
        observed_state_sha256=None,
        intended_state_sha256=observed,
        semantic_binding_sha256=binding,
    )

    # Simulate loss before the first baseline could be committed. With no prior
    # COMMIT, recovery legitimately ABORTs back to pristine authority state.
    path.unlink()
    recovery = authority.recover(observed_state_sha256=None)
    assert recovery.record is not None
    assert recovery.record.phase is AuthorityPhase.ABORT

    path.write_bytes(legacy_bytes)
    reopened = SourceHealthStore(path)
    assert reopened.get("provider-a").status == "unknown"

    history = authority.read_history()
    assert history[-1].phase is AuthorityPhase.COMMIT
    assert history[-1].intended_state_sha256 == observed
    assert history[-1].tx_id != first_tx_id
