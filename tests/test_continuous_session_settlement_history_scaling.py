from __future__ import annotations

import json
import tempfile
from pathlib import Path
from threading import RLock
from unittest.mock import patch

import autosport.continuous_session as continuous_session
import autosport.product_runtime as product_runtime


_AT = "2026-09-22T06:20:00+00:00"
_SMALL_HISTORY = 4
_LARGE_HISTORY = 128
_CONSTANT_SLACK = 8


def _checkpoint_payload(history_size: int) -> dict[str, object]:
    receipts: list[dict[str, object]] = []
    for index in range(history_size):
        digest_seed = f"{index:064x}"[-64:]
        receipts.append(
            {
                "event_identity": f"provider-a:event-{index}",
                "settlement_ref": f"provider-result:{index}",
                "evidence_id": f"receipt-{index:06d}",
                "evidence_sha256": digest_seed,
                "available_at": _AT,
            }
        )
    return {
        "schema": "autosport.continuous_session",
        "schema_version": 2,
        "session_id": "session-history-scaling",
        "source_id": "provider-a",
        "state": "RUNNING",
        "started_at": _AT,
        "cycles_completed": history_size,
        "last_success_at": _AT,
        "last_error_code": None,
        "last_full_refresh_at": None,
        "settlement_evidence": receipts,
        "source_gap_state": None,
        "source_sync_state": None,
        "source_state_delta_id": None,
        "source_unresolved_gap_delta_ids": [],
        "source_projection_stream_epoch": None,
        "source_state_projection_backlog": False,
    }


def _state_with_history(root: Path, history_size: int):
    path = root / "continuous_session.json"
    path.write_text(
        json.dumps(_checkpoint_payload(history_size), sort_keys=True),
        encoding="utf-8",
    )
    return continuous_session._ContinuousSessionState(
        path,
        session_id="session-history-scaling",
        source_id="provider-a",
        clock=lambda: _AT,
    )


def _settlement_digest_checks_for_unrelated_checkpoint(history_size: int) -> int:
    with tempfile.TemporaryDirectory() as directory:
        state = _state_with_history(Path(directory), history_size)
        original_sha256 = continuous_session._sha256
        settlement_digest_checks = 0

        def counting_sha256(value: object, field: str) -> str:
            nonlocal settlement_digest_checks
            if field.startswith("settlement_evidence "):
                settlement_digest_checks += 1
            return original_sha256(value, field)

        with patch.object(continuous_session, "_sha256", counting_sha256):
            state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")
        return settlement_digest_checks


def _historical_receipts_rewritten_by_unrelated_checkpoint(history_size: int) -> int:
    with tempfile.TemporaryDirectory() as directory:
        state = _state_with_history(Path(directory), history_size)
        original_write = continuous_session.atomic_write_json
        rewritten_history_sizes: list[int] = []

        def recording_write(path: Path, payload: object) -> None:
            if isinstance(payload, dict):
                history = payload.get("settlement_evidence")
                if isinstance(history, list):
                    rewritten_history_sizes.append(len(history))
                else:
                    rewritten_history_sizes.append(0)
            original_write(path, payload)

        with patch.object(continuous_session, "atomic_write_json", recording_write):
            state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")

        assert rewritten_history_sizes, "operational checkpoint write was not observed"
        return max(rewritten_history_sizes)


def test_unrelated_checkpoint_validation_is_bounded_by_active_state_not_history() -> None:
    small = _settlement_digest_checks_for_unrelated_checkpoint(_SMALL_HISTORY)
    large = _settlement_digest_checks_for_unrelated_checkpoint(_LARGE_HISTORY)

    assert large <= small + _CONSTANT_SLACK, (
        "a no-new-settlement operational checkpoint revalidated work proportional "
        f"to historical settlement receipts: small={small}, large={large}"
    )


def test_unrelated_checkpoint_does_not_rewrite_full_settlement_history() -> None:
    small = _historical_receipts_rewritten_by_unrelated_checkpoint(_SMALL_HISTORY)
    large = _historical_receipts_rewritten_by_unrelated_checkpoint(_LARGE_HISTORY)

    assert large <= small + _CONSTANT_SLACK, (
        "a no-new-settlement operational checkpoint rewrote historical receipt "
        f"population instead of bounded active state: small={small}, large={large}"
    )

def _journal_snapshot(root: Path) -> dict[str, bytes]:
    journal = root / "continuous_session.settlement-evidence"
    if not journal.exists():
        return {}
    return {
        path.name: path.read_bytes()
        for path in journal.iterdir()
        if not path.name.startswith(".") and path.suffix == ".json"
    }


def test_unrelated_failure_checkpoint_preserves_settlement_journal_bytes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _LARGE_HISTORY)
        before = _journal_snapshot(root)

        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")

        assert _journal_snapshot(root) == before
        checkpoint = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert checkpoint["schema_version"] == 4
        assert "settlement_evidence" not in checkpoint
        assert checkpoint["last_error_code"] == "SYNTHETIC_PROVIDER_FAILURE"
        assert len(state.snapshot().settlement_evidence) == _LARGE_HISTORY


def test_operational_failure_state_survives_restart_without_history_rewrite() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        before = _journal_snapshot(root)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")

        restarted = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        assert _journal_snapshot(root) == before
        assert (
            restarted.operational_snapshot().last_error_code
            == "SYNTHETIC_PROVIDER_FAILURE"
        )
        assert len(restarted.snapshot().settlement_evidence) == _SMALL_HISTORY


def test_success_without_settlement_evidence_is_bounded_operational_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _LARGE_HISTORY)
        before = _journal_snapshot(root)

        state.record_success(
            at=_AT,
            full_refresh=False,
            settlement_evidence=(),
        )

        assert _journal_snapshot(root) == before
        snapshot = state.operational_snapshot()
        assert snapshot.cycles_completed == _LARGE_HISTORY + 1
        assert snapshot.last_success_at == "2026-09-22T06:20:00+00:00"
        assert snapshot.last_error_code is None


def test_empty_checkpoint_rejects_orphan_settlement_journal() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, 0)
        checkpoint_path = root / "continuous_session.json"
        before = checkpoint_path.read_bytes()
        journal = root / "continuous_session.settlement-evidence"
        assert not journal.exists()
        journal.mkdir()

        try:
            state.record_failure(code="SHOULD_NOT_COMMIT")
        except continuous_session.ContinuousSessionError:
            pass
        else:
            raise AssertionError(
                "bounded mutation accepted an orphan settlement-evidence journal"
            )

        assert checkpoint_path.read_bytes() == before


def test_new_session_rejects_preexisting_orphan_journal_before_checkpoint_publish() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        checkpoint_path = root / "continuous_session.json"
        journal = root / "continuous_session.settlement-evidence"
        journal.mkdir()

        try:
            continuous_session._ContinuousSessionState(
                checkpoint_path,
                session_id="session-history-scaling",
                source_id="provider-a",
                clock=lambda: _AT,
            )
        except continuous_session.ContinuousSessionError:
            pass
        else:
            raise AssertionError(
                "new session published state over a preexisting orphan journal"
            )

        assert not checkpoint_path.exists()
        assert journal.exists()


def test_empty_legacy_migration_rejects_orphan_journal_without_checkpoint_rewrite() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        checkpoint_path = root / "continuous_session.json"
        checkpoint_path.write_text(
            json.dumps(_checkpoint_payload(0), sort_keys=True),
            encoding="utf-8",
        )
        before = checkpoint_path.read_bytes()
        journal = root / "continuous_session.settlement-evidence"
        journal.mkdir()

        try:
            continuous_session._ContinuousSessionState(
                checkpoint_path,
                session_id="session-history-scaling",
                source_id="provider-a",
                clock=lambda: _AT,
            )
        except continuous_session.ContinuousSessionError:
            pass
        else:
            raise AssertionError(
                "empty legacy migration published schema v4 over an orphan journal"
            )

        assert checkpoint_path.read_bytes() == before
        assert json.loads(checkpoint_path.read_text(encoding="utf-8"))[
            "schema_version"
        ] == 2
        assert journal.exists()


def test_runtime_tip_deletion_blocks_bounded_operational_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        checkpoint = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        tip_path = (
            root
            / "continuous_session.settlement-evidence"
            / f"{checkpoint['settlement_evidence_tip_key_sha256']}.json"
        )
        tip_path.unlink()

        try:
            state.record_failure(code="SHOULD_NOT_COMMIT")
        except continuous_session.ContinuousSessionError:
            pass
        else:
            raise AssertionError(
                "operational mutation accepted a missing settlement-history tip"
            )


def test_restart_detects_non_tip_history_deletion() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _state_with_history(root, _SMALL_HISTORY)
        journal = root / "continuous_session.settlement-evidence"
        first = sorted(
            path
            for path in journal.iterdir()
            if not path.name.startswith(".") and path.suffix == ".json"
        )[0]
        first.unlink()

        try:
            continuous_session._ContinuousSessionState(
                root / "continuous_session.json",
                session_id="session-history-scaling",
                source_id="provider-a",
                clock=lambda: _AT,
            )
        except continuous_session.ContinuousSessionError:
            pass
        else:
            raise AssertionError(
                "restart accepted an incomplete settlement-evidence journal"
            )


def test_operational_status_exposes_unmaterialized_history_count_truth() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        full = state.snapshot()
        assert full.settlement_evidence_materialized is True
        assert full.settlement_evidence_count == _SMALL_HISTORY
        assert len(full.settlement_evidence) == _SMALL_HISTORY

        operational = state.operational_snapshot()
        assert operational.settlement_evidence_materialized is False
        assert operational.settlement_evidence_count == _SMALL_HISTORY
        assert operational.settlement_evidence == ()


def test_empty_settlement_validation_does_not_scan_history() -> None:
    with tempfile.TemporaryDirectory() as directory:
        state = _state_with_history(Path(directory), _LARGE_HISTORY)

        with patch.object(
            state,
            "_load_evidence_history",
            side_effect=AssertionError("history scan is forbidden for empty input"),
        ):
            state.validate_settlement_evidence(settlement_evidence=())


def test_empty_settlement_validation_verifies_committed_tip_without_history_scan() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        checkpoint = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        tip_path = (
            root
            / "continuous_session.settlement-evidence"
            / f"{checkpoint['settlement_evidence_tip_key_sha256']}.json"
        )
        tip_path.unlink()

        with patch.object(
            state,
            "_load_evidence_history",
            side_effect=AssertionError(
                "bounded empty validation must not scan settlement history"
            ),
        ):
            try:
                state.validate_settlement_evidence(settlement_evidence=())
            except continuous_session.ContinuousSessionError:
                pass
            else:
                raise AssertionError(
                    "empty settlement validation accepted a missing committed tip"
                )


def test_pending_settlement_journal_recovers_after_write_crash() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        new_resolution = continuous_session.SettlementResolution(
            event_identity="provider-a:event-new",
            settlement_ref="provider-result:new",
            quote_outcomes={"provider-a:event-new:winner:home": "win"},
            evidence_id="receipt-new",
            evidence_sha256="f" * 64,
            available_at=_AT,
        )

        with patch.object(
            state,
            "_write_evidence_record",
            side_effect=OSError("synthetic evidence write crash"),
        ):
            try:
                state.record_success(
                    at=_AT,
                    full_refresh=False,
                    settlement_evidence=(new_resolution,),
                )
            except OSError:
                pass
            else:
                raise AssertionError("synthetic write crash did not interrupt commit")

        interrupted = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert interrupted["settlement_evidence_pending"] is not None
        assert interrupted["cycles_completed"] == _SMALL_HISTORY

        restarted = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        recovered = restarted.snapshot()
        assert recovered.cycles_completed == _SMALL_HISTORY + 1
        assert recovered.last_success_at == "2026-09-22T06:20:00+00:00"
        assert {item["evidence_id"] for item in recovered.settlement_evidence} == {
            *(f"receipt-{index:06d}" for index in range(_SMALL_HISTORY)),
            "receipt-new",
        }
        final_checkpoint = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert final_checkpoint["settlement_evidence_pending"] is None
        assert final_checkpoint["settlement_evidence_count"] == _SMALL_HISTORY + 1


def test_wrong_session_id_cannot_recover_pending_transaction() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "continuous_session.json"
        state = _state_with_history(root, _SMALL_HISTORY)
        resolution = continuous_session.SettlementResolution(
            event_identity="provider-a:event-wrong-session",
            settlement_ref="provider-result:wrong-session",
            quote_outcomes={
                "provider-a:event-wrong-session:winner:home": "win"
            },
            evidence_id="receipt-wrong-session",
            evidence_sha256="9" * 64,
            available_at=_AT,
        )

        with patch.object(
            state,
            "_write_evidence_record",
            side_effect=OSError("synthetic evidence write crash"),
        ):
            try:
                state.record_success(
                    at=_AT,
                    full_refresh=False,
                    settlement_evidence=(resolution,),
                )
            except OSError:
                pass
            else:
                raise AssertionError("synthetic write crash did not interrupt commit")

        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        pending = checkpoint["settlement_evidence_pending"]
        assert pending is not None
        pending_path = (
            root
            / "continuous_session.settlement-evidence"
            / f"{pending['records'][0]['evidence_key_sha256']}.json"
        )
        checkpoint_before = path.read_bytes()
        journal_before = _journal_snapshot(root)
        assert not pending_path.exists()

        try:
            continuous_session._ContinuousSessionState(
                path,
                session_id="wrong-session-id",
                source_id="provider-a",
                clock=lambda: _AT,
            )
        except continuous_session.ContinuousSessionError:
            pass
        else:
            raise AssertionError(
                "wrong session identity was allowed to recover durable state"
            )

        assert path.read_bytes() == checkpoint_before
        assert _journal_snapshot(root) == journal_before
        assert not pending_path.exists()


def test_pending_recovery_accepts_exact_already_written_pending_prefix() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        resolutions = (
            continuous_session.SettlementResolution(
                event_identity="provider-a:event-pending-prefix-a",
                settlement_ref="provider-result:pending-prefix-a",
                quote_outcomes={
                    "provider-a:event-pending-prefix-a:winner:home": "win"
                },
                evidence_id="receipt-pending-prefix-a",
                evidence_sha256="a" * 64,
                available_at=_AT,
            ),
            continuous_session.SettlementResolution(
                event_identity="provider-a:event-pending-prefix-b",
                settlement_ref="provider-result:pending-prefix-b",
                quote_outcomes={
                    "provider-a:event-pending-prefix-b:winner:home": "loss"
                },
                evidence_id="receipt-pending-prefix-b",
                evidence_sha256="b" * 64,
                available_at=_AT,
            ),
        )
        original_write = state._write_evidence_record
        calls = 0

        def crash_after_first_pending_record(record):
            nonlocal calls
            calls += 1
            original_write(record)
            if calls == 1:
                raise OSError("synthetic crash after first pending record")

        with patch.object(
            state,
            "_write_evidence_record",
            crash_after_first_pending_record,
        ):
            try:
                state.record_success(
                    at=_AT,
                    full_refresh=False,
                    settlement_evidence=resolutions,
                )
            except OSError:
                pass
            else:
                raise AssertionError(
                    "synthetic partial pending write crash did not interrupt commit"
                )

        checkpoint = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        pending = checkpoint["settlement_evidence_pending"]
        assert pending is not None
        assert len(pending["records"]) == 2
        first_pending_path = (
            root
            / "continuous_session.settlement-evidence"
            / f"{pending['records'][0]['evidence_key_sha256']}.json"
        )
        second_pending_path = (
            root
            / "continuous_session.settlement-evidence"
            / f"{pending['records'][1]['evidence_key_sha256']}.json"
        )
        assert first_pending_path.exists()
        assert not second_pending_path.exists()

        restarted = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        recovered = restarted.snapshot()
        assert recovered.cycles_completed == _SMALL_HISTORY + 1
        assert {
            "receipt-pending-prefix-a",
            "receipt-pending-prefix-b",
        }.issubset(
            {item["evidence_id"] for item in recovered.settlement_evidence}
        )
        assert first_pending_path.exists()
        assert second_pending_path.exists()
        final_checkpoint = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert final_checkpoint["settlement_evidence_pending"] is None
        assert final_checkpoint["settlement_evidence_count"] == _SMALL_HISTORY + 2


def test_pending_recovery_rejects_corrupt_base_before_advancing_checkpoint() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "continuous_session.json"
        state = _state_with_history(root, _SMALL_HISTORY)
        new_resolution = continuous_session.SettlementResolution(
            event_identity="provider-a:event-pending-corrupt-base",
            settlement_ref="provider-result:pending-corrupt-base",
            quote_outcomes={
                "provider-a:event-pending-corrupt-base:winner:home": "win"
            },
            evidence_id="receipt-pending-corrupt-base",
            evidence_sha256="c" * 64,
            available_at=_AT,
        )

        with patch.object(
            state,
            "_write_evidence_record",
            side_effect=OSError("synthetic evidence write crash"),
        ):
            try:
                state.record_success(
                    at=_AT,
                    full_refresh=False,
                    settlement_evidence=(new_resolution,),
                )
            except OSError:
                pass
            else:
                raise AssertionError("synthetic write crash did not interrupt commit")

        interrupted = json.loads(path.read_text(encoding="utf-8"))
        pending = interrupted["settlement_evidence_pending"]
        assert pending is not None
        assert interrupted["settlement_evidence_count"] == _SMALL_HISTORY
        assert interrupted["cycles_completed"] == _SMALL_HISTORY

        pending_path = (
            root
            / "continuous_session.settlement-evidence"
            / f"{pending['records'][0]['evidence_key_sha256']}.json"
        )
        assert not pending_path.exists()

        journal = root / "continuous_session.settlement-evidence"
        tip_name = (
            f"{interrupted['settlement_evidence_tip_key_sha256']}.json"
        )
        non_tip = next(
            item
            for item in journal.iterdir()
            if item.suffix == ".json"
            and not item.name.startswith(".")
            and item.name != tip_name
        )
        non_tip.unlink()

        checkpoint_before_recovery = path.read_bytes()
        journal_before_recovery = _journal_snapshot(root)

        try:
            continuous_session._ContinuousSessionState(
                path,
                session_id="session-history-scaling",
                source_id="provider-a",
                clock=lambda: _AT,
            )
        except continuous_session.ContinuousSessionError:
            pass
        else:
            raise AssertionError(
                "pending recovery advanced across a corrupt committed base journal"
            )

        assert path.read_bytes() == checkpoint_before_recovery
        assert _journal_snapshot(root) == journal_before_recovery
        assert not pending_path.exists()
        unchanged = json.loads(path.read_text(encoding="utf-8"))
        assert unchanged["settlement_evidence_pending"] == pending
        assert unchanged["settlement_evidence_count"] == _SMALL_HISTORY
        assert unchanged["cycles_completed"] == _SMALL_HISTORY


def test_wrong_session_id_cannot_migrate_legacy_checkpoint() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "continuous_session.json"
        legacy = json.dumps(
            _checkpoint_payload(_SMALL_HISTORY),
            sort_keys=True,
        )
        path.write_text(legacy, encoding="utf-8")
        before = path.read_bytes()
        journal = root / "continuous_session.settlement-evidence"
        assert not journal.exists()

        try:
            continuous_session._ContinuousSessionState(
                path,
                session_id="wrong-session-id",
                source_id="provider-a",
                clock=lambda: _AT,
            )
        except continuous_session.ContinuousSessionError:
            pass
        else:
            raise AssertionError(
                "wrong session identity was allowed to migrate durable state"
            )

        assert path.read_bytes() == before
        assert not journal.exists()


def test_wave_m_schema_v3_is_not_misread_as_bounded_journal_schema() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "continuous_session.json"
        foreign = _checkpoint_payload(1)
        foreign["schema_version"] = 3
        for item in foreign["settlement_evidence"]:
            item["quote_outcomes_sha256"] = "a" * 64
        path.write_text(
            json.dumps(foreign, sort_keys=True),
            encoding="utf-8",
        )
        before = path.read_bytes()

        try:
            continuous_session._ContinuousSessionState(
                path,
                session_id="session-history-scaling",
                source_id="provider-a",
                clock=lambda: _AT,
            )
        except continuous_session.ContinuousSessionError:
            pass
        else:
            raise AssertionError(
                "Wave M schema v3 was misread as the bounded journal checkpoint schema"
            )

        assert path.read_bytes() == before
        assert not (root / "continuous_session.settlement-evidence").exists()


def test_legacy_v2_migration_preserves_session_truth_and_evidence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        checkpoint = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert checkpoint["schema_version"] == 4
        assert checkpoint["cycles_completed"] == _SMALL_HISTORY
        assert checkpoint["settlement_evidence_count"] == _SMALL_HISTORY
        assert "settlement_evidence" not in checkpoint
        snapshot = state.snapshot()
        assert len(snapshot.settlement_evidence) == _SMALL_HISTORY
        assert snapshot.cycles_completed == _SMALL_HISTORY

def test_empty_success_does_not_scan_settlement_history() -> None:
    with tempfile.TemporaryDirectory() as directory:
        state = _state_with_history(Path(directory), _LARGE_HISTORY)

        with patch.object(
            state,
            "_load_evidence_history",
            side_effect=AssertionError(
                "history scan is forbidden for no-settlement success"
            ),
        ):
            state.record_success(
                at=_AT,
                full_refresh=False,
                settlement_evidence=(),
            )


def test_legacy_migration_recovers_from_partial_journal_publication() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "continuous_session.json"
        path.write_text(
            json.dumps(_checkpoint_payload(_SMALL_HISTORY), sort_keys=True),
            encoding="utf-8",
        )
        state_type = continuous_session._ContinuousSessionState
        original_write = state_type._write_evidence_record
        calls = 0

        def crash_during_migration(self, record):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("synthetic migration crash")
            return original_write(self, record)

        with patch.object(
            state_type,
            "_write_evidence_record",
            crash_during_migration,
        ):
            try:
                state_type(
                    path,
                    session_id="session-history-scaling",
                    source_id="provider-a",
                    clock=lambda: _AT,
                )
            except OSError:
                pass
            else:
                raise AssertionError("synthetic migration crash did not interrupt")

        interrupted = json.loads(path.read_text(encoding="utf-8"))
        assert interrupted["schema_version"] == 2
        assert 0 < len(_journal_snapshot(root)) < _SMALL_HISTORY

        restarted = state_type(
            path,
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        assert checkpoint["schema_version"] == 4
        assert checkpoint["settlement_evidence_count"] == _SMALL_HISTORY
        assert len(restarted.snapshot().settlement_evidence) == _SMALL_HISTORY


def test_legacy_rollback_cannot_discard_newer_settlement_journal_tail() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "continuous_session.json"
        legacy = json.dumps(
            _checkpoint_payload(_SMALL_HISTORY),
            sort_keys=True,
        )
        path.write_text(legacy, encoding="utf-8")
        state = continuous_session._ContinuousSessionState(
            path,
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        state.record_success(
            at=_AT,
            full_refresh=False,
            settlement_evidence=(
                continuous_session.SettlementResolution(
                    event_identity="provider-a:event-new",
                    settlement_ref="provider-result:new",
                    quote_outcomes={
                        "provider-a:event-new:winner:home": "win"
                    },
                    evidence_id="receipt-newer-than-legacy",
                    evidence_sha256="e" * 64,
                    available_at=_AT,
                ),
            ),
        )
        assert (
            state.operational_snapshot().cycles_completed
            == _SMALL_HISTORY + 1
        )

        path.write_text(legacy, encoding="utf-8")
        try:
            continuous_session._ContinuousSessionState(
                path,
                session_id="session-history-scaling",
                source_id="provider-a",
                clock=lambda: _AT,
            )
        except continuous_session.ContinuousSessionError:
            pass
        else:
            raise AssertionError(
                "legacy rollback discarded a newer committed settlement journal tail"
            )

def test_product_tick_uses_bounded_coherent_status_path() -> None:
    status = continuous_session.ContinuousSessionStatus(
        session_id="bounded-product-tick",
        source_id="provider-a",
        state=continuous_session.SessionState.RUNNING,
        cycles_completed=0,
        last_success_at=None,
        last_error_code=None,
        last_full_refresh_at=None,
        settlement_evidence=(),
    )

    class Coordinator:
        def status(self):
            raise AssertionError(
                "product tick must not request full settlement-history status"
            )

        def operational_status(self):
            return status

        def tick(self):
            return "bounded-tick-result"

    class Collector:
        def status(self):
            return {
                "stopped_at": None,
                "stop_reason": None,
            }

    class Lease:
        authority_active = True

    class StartStore:
        @staticmethod
        def pending():
            return None

    runtime = object.__new__(product_runtime.AutonomousProductRuntime)
    runtime.coordinator = Coordinator()
    runtime.collector = Collector()
    runtime._runtime_lease = Lease()
    runtime._start_transition_store = StartStore()
    runtime._closed = False
    runtime._operation_fence = RLock()

    assert runtime.tick() == "bounded-tick-result"

def test_public_settlement_evidence_order_matches_v2_contract_after_append() -> None:
    with tempfile.TemporaryDirectory() as directory:
        state = _state_with_history(Path(directory), _SMALL_HISTORY)
        state.record_success(
            at=_AT,
            full_refresh=False,
            settlement_evidence=(
                continuous_session.SettlementResolution(
                    event_identity="provider-a:event-lexical-first",
                    settlement_ref="provider-result:lexical-first",
                    quote_outcomes={
                        "provider-a:event-lexical-first:winner:home": "win"
                    },
                    evidence_id="aaa-receipt-new",
                    evidence_sha256="d" * 64,
                    available_at=_AT,
                ),
            ),
        )

        evidence_ids = tuple(
            item["evidence_id"]
            for item in state.snapshot().settlement_evidence
        )
        assert evidence_ids == tuple(sorted(evidence_ids))
        assert evidence_ids[0] == "aaa-receipt-new"

