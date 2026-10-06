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


def test_boolean_operational_checkpoint_schema_version_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        payload["schema_version"] = True
        error_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

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
            raise AssertionError("boolean operational checkpoint schema_version was accepted")


def test_failure_checkpoint_leaves_canonical_settlement_file_byte_identical() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _LARGE_HISTORY)
        state_path = root / "continuous_session.json"
        before = state_path.read_bytes()

        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")

        assert state_path.read_bytes() == before


def test_latest_failure_checkpoint_wins_across_restart() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="FIRST_FAILURE")
        state.record_failure(code="SECOND_FAILURE")

        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        assert reopened.snapshot().last_error_code == "SECOND_FAILURE"


def test_operational_error_checkpoint_identity_mismatch_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        payload["source_id"] = "provider-b"
        error_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

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
            raise AssertionError("foreign operational error checkpoint was accepted")


def test_conflicting_same_generation_error_authorities_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SIDECAR_FAILURE")
        state_path = root / "continuous_session.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["last_error_code"] = "MAIN_FAILURE"
        state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        reopened = continuous_session._ContinuousSessionState(
            state_path,
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        try:
            reopened.snapshot()
        except continuous_session.ContinuousSessionError:
            pass
        else:
            raise AssertionError("conflicting same-generation error authorities were accepted")


def test_oversized_failure_code_is_rejected_before_operational_write() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        writes: list[tuple[Path, object]] = []
        original_write = continuous_session.atomic_write_json

        def recording_write(path: Path, payload: object) -> None:
            writes.append((Path(path), payload))
            original_write(path, payload)

        with patch.object(continuous_session, "atomic_write_json", recording_write):
            try:
                state.record_failure(
                    code="x" * (state._MAX_ERROR_CODE_CHARS + 1),
                )
            except ValueError as exc:
                assert "resource limit" in str(exc)
            else:
                raise AssertionError("oversized failure code was accepted")

        assert writes == []
        assert not (
            root / "continuous_session.json.operational_error.json"
        ).exists()


def test_oversized_operational_checkpoint_fails_before_json_parse() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        error_path.write_bytes(
            b"x" * (state._MAX_ERROR_CHECKPOINT_BYTES + 1),
        )
        parser_calls = 0

        def forbidden_parser(raw: object) -> object:
            nonlocal parser_calls
            parser_calls += 1
            raise AssertionError("oversized checkpoint reached JSON parser")

        with patch.object(
            continuous_session,
            "strict_json_loads",
            forbidden_parser,
        ):
            try:
                state._read_error_checkpoint()
            except continuous_session.ContinuousSessionError as exc:
                assert "resource limit" in str(exc)
            else:
                raise AssertionError("oversized operational checkpoint was accepted")

        assert parser_calls == 0


def test_operational_checkpoint_serialized_bytes_remain_bounded() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(
            code="e" * state._MAX_ERROR_CODE_CHARS,
        )
        error_path = root / "continuous_session.json.operational_error.json"

        assert error_path.stat().st_size <= state._MAX_ERROR_CHECKPOINT_BYTES
        assert (
            state._read_error_checkpoint()["last_error_code"]
            == "e" * state._MAX_ERROR_CODE_CHARS
        )
