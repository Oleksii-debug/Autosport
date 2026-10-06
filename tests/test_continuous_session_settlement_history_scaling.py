from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import autosport.continuous_session as continuous_session


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


def test_failure_checkpoint_survives_restart_without_touching_settlement_history() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")

        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        assert reopened.snapshot().last_error_code == "SYNTHETIC_PROVIDER_FAILURE"


def test_success_generation_supersedes_stale_failure_checkpoint_after_restart() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        stale_error = error_path.read_text(encoding="utf-8")

        state.record_success(
            at="2026-09-22T06:21:00+00:00",
            full_refresh=False,
            settlement_evidence=(),
        )
        error_path.write_text(stale_error, encoding="utf-8")

        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        assert reopened.snapshot().last_error_code is None


def test_corrupt_operational_error_checkpoint_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        error_path.write_text('{"schema":"wrong"}', encoding="utf-8")

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
            raise AssertionError("corrupt operational error checkpoint was accepted")
