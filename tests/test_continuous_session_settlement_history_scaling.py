from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import replace
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


def _settlement_digest_checks_for_repeated_failure(history_size: int) -> int:
    with tempfile.TemporaryDirectory() as directory:
        state = _state_with_history(Path(directory), history_size)
        state.record_failure(code="FIRST_FAILURE")
        original_sha256 = continuous_session._sha256
        settlement_digest_checks = 0

        def counting_sha256(value: object, field: str) -> str:
            nonlocal settlement_digest_checks
            if field.startswith("settlement_evidence "):
                settlement_digest_checks += 1
            return original_sha256(value, field)

        with patch.object(continuous_session, "_sha256", counting_sha256):
            state.record_failure(code="SECOND_FAILURE")
        return settlement_digest_checks


def _historical_receipts_rewritten_by_unrelated_checkpoint(history_size: int) -> int:
    with tempfile.TemporaryDirectory() as directory:
        state = _state_with_history(Path(directory), history_size)
        before = state.path.read_bytes()

        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")

        after = state.path.read_bytes()
        return history_size if after != before else 0


def test_unrelated_checkpoint_validation_is_bounded_by_active_state_not_history() -> None:
    small = _settlement_digest_checks_for_unrelated_checkpoint(_SMALL_HISTORY)
    large = _settlement_digest_checks_for_unrelated_checkpoint(_LARGE_HISTORY)

    assert large <= small + _CONSTANT_SLACK, (
        "a no-new-settlement operational checkpoint revalidated work proportional "
        f"to historical settlement receipts: small={small}, large={large}"
    )


def test_repeated_failure_validation_stays_bounded_by_active_state() -> None:
    small = _settlement_digest_checks_for_repeated_failure(_SMALL_HISTORY)
    large = _settlement_digest_checks_for_repeated_failure(_LARGE_HISTORY)

    assert large <= small + _CONSTANT_SLACK, (
        "a repeated operational failure revalidated work proportional to "
        f"historical settlement receipts: small={small}, large={large}"
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

        try:
            continuous_session._ContinuousSessionState(
                state_path,
                session_id="session-history-scaling",
                source_id="provider-a",
                clock=lambda: _AT,
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "error authorities conflict at bootstrap" in str(exc)
        else:
            raise AssertionError(
                "conflicting same-generation error authorities survived bootstrap"
            )


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


def test_pathological_operational_checkpoint_nesting_is_normalized() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        error_path = root / "continuous_session.json.operational_error.json"
        nested = (
            '{"schema":"autosport.continuous_session.operational_error",'
            '"schema_version":1,"session_id":"session-history-scaling",'
            '"source_id":"provider-a","observed_cycles_completed":4,'
            '"observed_last_success_at":"' + _AT + '",'
            '"last_error_code":' + ("[" * 1500) + '"x"' + ("]" * 1500) + "}"
        )
        assert len(nested.encode("utf-8")) < state._MAX_ERROR_CHECKPOINT_BYTES
        error_path.write_text(nested, encoding="utf-8")

        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "cannot verify" in str(exc)
        else:
            raise AssertionError("pathological nested sidecar was accepted")


def test_continuous_session_schema_ignores_rebound_class_constants(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_SCHEMA",
            "attacker.schema",
        )
        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_VERSION",
            999,
        )
        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_FIELDS",
            {"attacker"},
        )

        reopened = _state_with_history(root, _SMALL_HISTORY)
        assert reopened.snapshot().session_id == "session-history-scaling"


def test_operational_checkpoint_ignores_rebound_class_constants(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_ERROR_SCHEMA",
            "attacker.schema",
        )
        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_ERROR_VERSION",
            999,
        )
        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_ERROR_FIELDS",
            {"attacker"},
        )
        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_MAX_ERROR_CHECKPOINT_BYTES",
            1,
        )
        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_MAX_ERROR_CODE_CHARS",
            1,
        )

        reopened = _state_with_history(root, _SMALL_HISTORY)
        assert reopened._read_error_checkpoint()["last_error_code"] == (
            "SYNTHETIC_PROVIDER_FAILURE"
        )

def test_operational_checkpoint_symlink_is_rejected_before_read() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        target_path = root / "foreign-error-checkpoint.json"
        target_path.write_bytes(error_path.read_bytes())
        error_path.unlink()
        try:
            os.symlink(target_path, error_path)
        except (OSError, NotImplementedError):
            return

        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "regular file" in str(exc) or "cannot verify" in str(exc)
        else:
            raise AssertionError("symlink operational checkpoint was accepted")


def test_operational_checkpoint_hard_link_is_rejected_before_read() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        alias_path = root / "operational-error-alias.json"
        try:
            os.link(error_path, alias_path)
        except (OSError, NotImplementedError):
            return

        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "hard links" in str(exc) or "not trustworthy" in str(exc)
        else:
            raise AssertionError("multiply-linked operational checkpoint was accepted")


def test_operational_checkpoint_path_replacement_during_open_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        replacement = root / "replacement.json"
        replacement.write_bytes(error_path.read_bytes())
        original_open = continuous_session.os.open
        replaced = False

        def replacing_open(path: object, flags: int, *args: object) -> int:
            nonlocal replaced
            if not replaced and Path(path) == error_path:
                replacement.replace(error_path)
                replaced = True
            return original_open(path, flags, *args)

        with patch.object(continuous_session.os, "open", replacing_open):
            try:
                state._read_error_checkpoint()
            except continuous_session.ContinuousSessionError as exc:
                assert (
                    "replaced" in str(exc)
                    or "changed" in str(exc)
                    or "filesystem authority" in str(exc)
                )
            else:
                raise AssertionError(
                    "path-replaced operational checkpoint was accepted"
                )


def test_dangling_operational_checkpoint_symlink_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        error_path = root / "continuous_session.json.operational_error.json"
        missing_target = root / "missing-operational-error.json"
        try:
            os.symlink(missing_target, error_path)
        except (OSError, NotImplementedError):
            return

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
            raise AssertionError("dangling operational checkpoint symlink was ignored")


def test_in_place_operational_checkpoint_mutation_during_read_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="FIRST_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        original_read = state._bounded_descriptor_read
        calls = 0

        def mutating_read(descriptor: int, limit: int) -> bytes:
            nonlocal calls
            data = original_read(descriptor, limit)
            calls += 1
            if calls == 1:
                payload = json.loads(error_path.read_text(encoding="utf-8"))
                payload["last_error_code"] = "OTHER_FAILURE"
                replacement = (
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                        allow_nan=False,
                    )
                    + "\n"
                ).encode("utf-8")
                assert len(replacement) == len(error_path.read_bytes())
                with error_path.open("r+b") as handle:
                    handle.seek(0)
                    handle.write(replacement)
                    handle.flush()
                    os.fsync(handle.fileno())
            return data

        with patch.object(
            state,
            "_bounded_descriptor_read",
            mutating_read,
        ):
            try:
                state._read_error_checkpoint()
            except continuous_session.ContinuousSessionError as exc:
                assert "changed during bounded read" in str(exc)
            else:
                raise AssertionError("same-inode checkpoint mutation was accepted")

def test_checkpoint_authority_ignores_rebound_module_constants(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        monkeypatch.setattr(
            continuous_session,
            "_CONTINUOUS_SESSION_SCHEMA",
            "attacker.session",
        )
        monkeypatch.setattr(continuous_session, "_CONTINUOUS_SESSION_VERSION", 999)
        monkeypatch.setattr(
            continuous_session,
            "_CONTINUOUS_SESSION_FIELDS",
            frozenset({"attacker"}),
        )
        monkeypatch.setattr(
            continuous_session,
            "_CONTINUOUS_SESSION_ERROR_SCHEMA",
            "attacker.error",
        )
        monkeypatch.setattr(
            continuous_session,
            "_CONTINUOUS_SESSION_ERROR_VERSION",
            999,
        )
        monkeypatch.setattr(
            continuous_session,
            "_CONTINUOUS_SESSION_ERROR_FIELDS",
            frozenset({"attacker"}),
        )
        monkeypatch.setattr(
            continuous_session,
            "_CONTINUOUS_SESSION_ERROR_MAX_BYTES",
            1,
        )
        monkeypatch.setattr(
            continuous_session,
            "_CONTINUOUS_SESSION_ERROR_MAX_CODE_CHARS",
            1,
        )

        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")
        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        assert reopened.snapshot().last_error_code == "SYNTHETIC_PROVIDER_FAILURE"

        error_path = root / "continuous_session.json.operational_error.json"
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        assert payload["schema"] == "autosport.continuous_session.operational_error"
        assert payload["schema_version"] == 3

        payload["schema"] = "attacker.error"
        payload["schema_version"] = 999
        error_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        try:
            reopened._read_error_checkpoint()
        except continuous_session.ContinuousSessionError:
            pass
        else:
            raise AssertionError(
                "module-constant rebinding changed operational checkpoint authority"
            )

        fresh_path = root / "fresh_session.json"
        fresh = continuous_session._ContinuousSessionState(
            fresh_path,
            session_id="fresh-session",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        fresh_payload = json.loads(fresh_path.read_text(encoding="utf-8"))
        assert fresh_payload["schema"] == "autosport.continuous_session"
        assert fresh_payload["schema_version"] == 3
        assert fresh.snapshot().session_id == "fresh-session"



def test_state_reason_commit_supersedes_stale_failure_after_cleanup_crash() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="PROVIDER_FAILURE")

        original_write_error = state._write_error_checkpoint

        def crash_during_cleanup(code: str | None) -> None:
            if code is None:
                raise RuntimeError("simulated crash after canonical state commit")
            original_write_error(code)

        with patch.object(state, "_write_error_checkpoint", crash_during_cleanup):
            try:
                state.set_state(
                    continuous_session.SessionState.PAUSED,
                    reason="OPERATOR_PAUSE",
                )
            except RuntimeError as exc:
                assert "simulated crash" in str(exc)
            else:
                raise AssertionError("cleanup crash was not simulated")

        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        snapshot = reopened.snapshot()
        assert snapshot.state is continuous_session.SessionState.PAUSED
        assert snapshot.last_error_code == "OPERATOR_PAUSE"


def test_operational_checkpoint_state_marker_fails_closed_when_invalid() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        payload["observed_state"] = "ATTACKER_STATE"
        error_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "observed_state" in str(exc)
        else:
            raise AssertionError("invalid sidecar observed_state was accepted")


def test_operational_checkpoint_open_is_nonblocking_before_type_verification() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="SYNTHETIC_PROVIDER_FAILURE")

        original_open = continuous_session.os.open
        observed_flags: list[int] = []

        def observing_open(path: object, flags: int, *args: object) -> int:
            observed_flags.append(flags)
            return original_open(path, flags, *args)

        with patch.object(continuous_session.os, "open", observing_open):
            checkpoint = state._read_error_checkpoint()

        assert checkpoint["last_error_code"] == "SYNTHETIC_PROVIDER_FAILURE"
        assert observed_flags
        nonblock = getattr(continuous_session.os, "O_NONBLOCK", 0)
        if nonblock:
            assert observed_flags[0] & nonblock


def test_sidecar_parser_authority_ignores_runtime_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        def attacker_parser(_text: str) -> object:
            return {
                "schema": "attacker",
                "schema_version": 999,
            }

        monkeypatch.setattr(
            continuous_session,
            "strict_json_loads",
            attacker_parser,
        )

        checkpoint = state._read_error_checkpoint()
        assert checkpoint["last_error_code"] == "CANONICAL_FAILURE"
        assert checkpoint["schema"] == "autosport.continuous_session.operational_error"


def test_sidecar_serializer_and_publisher_ignore_runtime_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        def attacker_dumps(*_args: object, **_kwargs: object) -> str:
            raise AssertionError("runtime-rebound json.dumps gained sidecar authority")

        def attacker_write(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("runtime-rebound atomic_write_json gained sidecar authority")

        monkeypatch.setattr(continuous_session.json, "dumps", attacker_dumps)
        monkeypatch.setattr(
            continuous_session,
            "atomic_write_json",
            attacker_write,
        )

        state.record_failure(code="CANONICAL_FAILURE")

        error_path = root / "continuous_session.json.operational_error.json"
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        assert payload["schema"] == "autosport.continuous_session.operational_error"
        assert payload["schema_version"] == 3
        assert payload["last_error_code"] == "CANONICAL_FAILURE"


def test_sidecar_atomic_serializer_rebinding_fails_before_publication(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        error_path = root / "continuous_session.json.operational_error.json"

        def attacker_dump(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("runtime-rebound json.dump executed")

        monkeypatch.setattr(continuous_session.json, "dump", attacker_dump)

        try:
            state.record_failure(code="CANONICAL_FAILURE")
        except continuous_session.ContinuousSessionError as exc:
            assert "writer code identity" in str(exc)
        else:
            raise AssertionError("runtime-rebound json.dump was accepted")

        assert not error_path.exists()


def test_sidecar_verified_read_ignores_runtime_method_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        def attacker_read(*_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("runtime-rebound sidecar byte reader executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_read_error_checkpoint_bytes",
            attacker_read,
        )

        checkpoint = state._read_error_checkpoint()
        assert checkpoint["last_error_code"] == "CANONICAL_FAILURE"


def test_invalid_sidecar_success_timestamp_is_normalized_to_domain_error() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        payload["observed_last_success_at"] = "not-an-instant"
        error_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "invalid field" in str(exc)
        else:
            raise AssertionError("invalid observed_last_success_at escaped validation")


def test_invalid_sidecar_error_code_is_normalized_to_domain_error() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        payload["last_error_code"] = " untrimmed "
        error_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "invalid field" in str(exc)
        else:
            raise AssertionError("invalid last_error_code escaped validation")


def test_durable_session_writer_ignores_runtime_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        def attacker_write(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("runtime-rebound atomic_write_json gained session authority")

        monkeypatch.setattr(continuous_session, "atomic_write_json", attacker_write)

        state.record_success(
            at=_AT,
            full_refresh=False,
            settlement_evidence=(),
        )

        snapshot = state.snapshot()
        assert snapshot.cycles_completed == _SMALL_HISTORY + 1
        assert snapshot.last_success_at == _AT


def test_durable_session_parser_ignores_runtime_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        def attacker_parser(_text: object) -> object:
            raise AssertionError("runtime-rebound strict_json_loads gained session authority")

        monkeypatch.setattr(continuous_session, "strict_json_loads", attacker_parser)

        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        assert reopened.snapshot().session_id == "session-history-scaling"


def test_durable_session_path_reader_ignores_runtime_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        def attacker_read_text(*_args: object, **_kwargs: object) -> str:
            raise AssertionError("runtime-rebound Path.read_text gained session authority")

        monkeypatch.setattr(Path, "read_text", attacker_read_text)

        assert state.snapshot().cycles_completed == _SMALL_HISTORY


def test_state_round_trip_preserves_failure_until_resume() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="STALE_PROVIDER_FAILURE")
        assert state.snapshot().last_error_code == "STALE_PROVIDER_FAILURE"

        state.set_state(continuous_session.SessionState.PAUSED)
        assert state.snapshot().last_error_code == "STALE_PROVIDER_FAILURE"

        state.set_state(continuous_session.SessionState.RUNNING)
        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        snapshot = reopened.snapshot()
        assert snapshot.state is continuous_session.SessionState.RUNNING
        assert snapshot.last_error_code is None


def test_durable_session_parser_and_path_reader_code_identity_are_immutable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        original_parser = continuous_session.strict_json_loads
        original_parser_code = original_parser.__code__
        original_read_text = Path.read_text
        original_read_text_code = original_read_text.__code__

        def attacker_parser(_text: object) -> object:
            raise AssertionError("mutated session parser executed")

        def attacker_read_text(_self: Path, **_kwargs: object) -> str:
            raise AssertionError("mutated Path.read_text executed")

        try:
            original_parser.__code__ = attacker_parser.__code__
            original_read_text.__code__ = attacker_read_text.__code__

            for operation in (
                lambda: state.snapshot(),
                lambda: continuous_session._ContinuousSessionState(
                    root / "reopened.json",
                    session_id="reopened-session",
                    source_id="provider-a",
                    clock=lambda: _AT,
                ),
            ):
                try:
                    operation()
                except continuous_session.ContinuousSessionError as exc:
                    assert "code identity" in str(exc)
                else:
                    raise AssertionError(
                        "mutated durable session reader was accepted"
                    )
        finally:
            original_parser.__code__ = original_parser_code
            original_read_text.__code__ = original_read_text_code


def test_durable_session_writer_code_identity_is_immutable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        original_writer = continuous_session.atomic_write_json
        original_writer_code = original_writer.__code__

        def attacker_writer(
            _path: object,
            _payload: object,
        ) -> None:
            raise AssertionError("mutated session writer executed")

        try:
            original_writer.__code__ = attacker_writer.__code__
            try:
                state.record_success(
                    at=_AT,
                    full_refresh=False,
                    settlement_evidence=(),
                )
            except continuous_session.ContinuousSessionError as exc:
                assert "writer code identity" in str(exc)
            else:
                raise AssertionError("mutated durable session writer was accepted")
        finally:
            original_writer.__code__ = original_writer_code


def test_operational_checkpoint_writer_code_identity_is_immutable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        original_dumps = continuous_session.json.dumps
        original_dumps_code = original_dumps.__code__
        original_writer = continuous_session.atomic_write_json
        original_writer_code = original_writer.__code__

        def attacker_dumps(*_args: object, **_kwargs: object) -> str:
            raise AssertionError("mutated sidecar serializer executed")

        def attacker_writer(
            _path: object,
            _payload: object,
        ) -> None:
            raise AssertionError("mutated sidecar writer executed")

        try:
            original_dumps.__code__ = attacker_dumps.__code__
            original_writer.__code__ = attacker_writer.__code__
            try:
                state.record_failure(code="CANONICAL_FAILURE")
            except continuous_session.ContinuousSessionError as exc:
                assert "writer code identity" in str(exc)
            else:
                raise AssertionError(
                    "mutated operational-checkpoint writer was accepted"
                )
        finally:
            original_dumps.__code__ = original_dumps_code
            original_writer.__code__ = original_writer_code


def test_pathological_main_checkpoint_nesting_is_normalized() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state_path = root / "continuous_session.json"
        state_path.write_text(
            ("[" * 1500) + "0" + ("]" * 1500),
            encoding="utf-8",
        )

        try:
            state._read()
        except continuous_session.ContinuousSessionError as exc:
            assert "cannot verify continuous session state" in str(exc)
        else:
            raise AssertionError("pathological main checkpoint escaped domain validation")


def test_sidecar_descriptor_close_failure_is_normalized(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        original_close = continuous_session.os.close

        def failing_close(descriptor: int) -> None:
            original_close(descriptor)
            raise OSError("simulated close failure")

        monkeypatch.setattr(continuous_session.os, "close", failing_close)

        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "cannot close" in str(exc)
        else:
            raise AssertionError("descriptor close failure escaped domain normalization")


def test_settlement_evidence_validator_ignores_runtime_class_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        def attacker_validator(_raw: object) -> tuple[dict[str, str], ...]:
            raise AssertionError("runtime-rebound settlement validator executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_validate_settlement_evidence",
            staticmethod(attacker_validator),
        )

        assert state.snapshot().cycles_completed == _SMALL_HISTORY


def test_settlement_evidence_validator_code_identity_is_immutable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        descriptor = continuous_session._ContinuousSessionState.__dict__[
            "_validate_settlement_evidence"
        ]
        original_validator = descriptor.__func__
        original_code = original_validator.__code__

        def attacker_validator(_raw: object) -> tuple[dict[str, str], ...]:
            raise AssertionError("mutated settlement validator executed")

        try:
            original_validator.__code__ = attacker_validator.__code__
            try:
                state.snapshot()
            except continuous_session.ContinuousSessionError as exc:
                assert "code identity" in str(exc)
            else:
                raise AssertionError("mutated settlement validator was accepted")
        finally:
            original_validator.__code__ = original_code


def test_settlement_evidence_normalizer_ignores_runtime_class_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        evidence = continuous_session.SettlementResolution(
            event_identity="provider-a:event-normalizer",
            settlement_ref="provider-result:normalizer",
            quote_outcomes={"quote-normalizer": "win"},
            evidence_id="receipt-normalizer",
            evidence_sha256="a" * 64,
            available_at=_AT,
        )

        def attacker_normalizer(
            _evidence: continuous_session.SettlementResolution,
        ) -> dict[str, str]:
            raise AssertionError("runtime-rebound settlement normalizer executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_normalized_settlement_evidence",
            staticmethod(attacker_normalizer),
        )

        state.validate_settlement_evidence(settlement_evidence=(evidence,))


def test_settlement_evidence_normalizer_code_identity_is_immutable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        evidence = continuous_session.SettlementResolution(
            event_identity="provider-a:event-normalizer-code",
            settlement_ref="provider-result:normalizer-code",
            quote_outcomes={"quote-normalizer-code": "win"},
            evidence_id="receipt-normalizer-code",
            evidence_sha256="b" * 64,
            available_at=_AT,
        )

        descriptor = continuous_session._ContinuousSessionState.__dict__[
            "_normalized_settlement_evidence"
        ]
        original_normalizer = descriptor.__func__
        original_code = original_normalizer.__code__

        def attacker_normalizer(
            _evidence: continuous_session.SettlementResolution,
        ) -> dict[str, str]:
            raise AssertionError("mutated settlement normalizer executed")

        try:
            original_normalizer.__code__ = attacker_normalizer.__code__
            try:
                state.validate_settlement_evidence(settlement_evidence=(evidence,))
            except continuous_session.ContinuousSessionError as exc:
                assert "normalizer code identity" in str(exc)
            else:
                raise AssertionError("mutated settlement normalizer was accepted")
        finally:
            original_normalizer.__code__ = original_code


def test_operational_checkpoint_filesystem_function_rebinding_fails_closed(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        def attacker_fstat(_descriptor: object) -> object:
            raise AssertionError("runtime-rebound os.fstat executed")

        monkeypatch.setattr(continuous_session.os, "fstat", attacker_fstat)

        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "filesystem authority" in str(exc)
        else:
            raise AssertionError("runtime-rebound os.fstat was accepted")


def test_operational_checkpoint_reader_function_rebinding_fails_closed(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        for name, replacement in (
            ("lseek", lambda *_args: 0),
            ("read", lambda *_args: b""),
            ("close", lambda *_args: None),
        ):
            original = getattr(continuous_session.os, name)
            monkeypatch.setattr(continuous_session.os, name, replacement)
            try:
                state._read_error_checkpoint()
            except continuous_session.ContinuousSessionError as exc:
                assert "filesystem authority" in str(exc)
            else:
                raise AssertionError(
                    f"runtime-rebound os.{name} was accepted"
                )
            finally:
                monkeypatch.setattr(continuous_session.os, name, original)


def test_operational_checkpoint_path_lstat_rebinding_fails_closed(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        original_lstat = Path.lstat

        def attacker_lstat(_self: Path) -> object:
            raise AssertionError("runtime-rebound Path.lstat executed")

        monkeypatch.setattr(Path, "lstat", attacker_lstat)
        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "filesystem authority" in str(exc)
        else:
            raise AssertionError("runtime-rebound Path.lstat was accepted")

        monkeypatch.setattr(Path, "lstat", original_lstat)


def test_snapshot_cannot_hide_operational_checkpoint_via_path_lstat_rebinding(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        original_lstat = Path.lstat

        def attacker_lstat(_self: Path) -> object:
            raise FileNotFoundError("hide operational checkpoint")

        monkeypatch.setattr(Path, "lstat", attacker_lstat)
        try:
            state.snapshot()
        except continuous_session.ContinuousSessionError as exc:
            assert "presence authority" in str(exc)
        else:
            raise AssertionError(
                "runtime-rebound Path.lstat hid a durable operational checkpoint"
            )
        finally:
            monkeypatch.setattr(Path, "lstat", original_lstat)


def test_operational_checkpoint_presence_probe_code_identity_is_immutable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        original_lstat = Path.lstat
        original_code = original_lstat.__code__

        def attacker_lstat(_self: Path) -> object:
            raise FileNotFoundError("hide operational checkpoint")

        try:
            original_lstat.__code__ = attacker_lstat.__code__
            try:
                state.snapshot()
            except continuous_session.ContinuousSessionError as exc:
                assert "presence authority" in str(exc)
            else:
                raise AssertionError(
                    "mutated Path.lstat code hid a durable operational checkpoint"
                )
        finally:
            original_lstat.__code__ = original_code


def test_snapshot_ignores_rebound_sidecar_dispatch_helpers(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_error_checkpoint_present",
            lambda _self: False,
        )

        def attacker_read(_self: object) -> dict[str, object]:
            raise AssertionError("runtime-rebound sidecar reader executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_read_error_checkpoint",
            attacker_read,
        )

        assert state.snapshot().last_error_code == "CANONICAL_FAILURE"


def test_snapshot_sidecar_dispatch_code_identity_is_immutable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        descriptor = continuous_session._ContinuousSessionState.__dict__[
            "_error_checkpoint_present"
        ]
        original_probe = descriptor
        original_code = original_probe.__code__

        def attacker_probe(_self: object) -> bool:
            return False

        try:
            original_probe.__code__ = attacker_probe.__code__
            try:
                state.snapshot()
            except continuous_session.ContinuousSessionError as exc:
                assert "snapshot authority" in str(exc)
            else:
                raise AssertionError(
                    "mutated sidecar presence helper bypassed snapshot authority"
                )
        finally:
            original_probe.__code__ = original_code


def test_operational_checkpoint_helper_rebinding_cannot_redirect_verified_read(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        def attacker_read(*_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("runtime-rebound bounded reader executed")

        def attacker_identity(_info: object) -> tuple[int, int]:
            raise AssertionError("runtime-rebound file identity helper executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_bounded_descriptor_read",
            staticmethod(attacker_read),
        )
        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_file_identity",
            staticmethod(attacker_identity),
        )

        checkpoint = state._read_error_checkpoint()
        assert checkpoint["last_error_code"] == "CANONICAL_FAILURE"


def test_operational_checkpoint_helper_code_identity_is_immutable() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        descriptor = continuous_session._ContinuousSessionState.__dict__[
            "_bounded_descriptor_read"
        ]
        original_reader = descriptor.__func__
        original_code = original_reader.__code__

        def attacker_reader(*_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("mutated bounded reader executed")

        try:
            original_reader.__code__ = attacker_reader.__code__
            try:
                state._read_error_checkpoint()
            except continuous_session.ContinuousSessionError as exc:
                assert "filesystem authority" in str(exc)
            else:
                raise AssertionError("mutated bounded reader was accepted")
        finally:
            original_reader.__code__ = original_code


def test_operational_checkpoint_stat_and_seek_authority_rebinding_fails_closed(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        original_isreg = continuous_session.stat.S_ISREG
        monkeypatch.setattr(continuous_session.stat, "S_ISREG", lambda _mode: True)
        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "filesystem authority" in str(exc)
        else:
            raise AssertionError("runtime-rebound stat.S_ISREG was accepted")
        finally:
            monkeypatch.setattr(continuous_session.stat, "S_ISREG", original_isreg)

        original_seek_set = continuous_session.os.SEEK_SET
        monkeypatch.setattr(continuous_session.os, "SEEK_SET", original_seek_set + 1)
        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "filesystem authority" in str(exc)
        else:
            raise AssertionError("runtime-rebound os.SEEK_SET was accepted")


def test_legacy_v2_session_checkpoint_promotes_generation_on_first_write() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state_path = root / "continuous_session.json"
        legacy = _checkpoint_payload(_SMALL_HISTORY)
        assert legacy["schema_version"] == 2
        assert "generation" not in legacy
        state_path.write_text(
            json.dumps(legacy, sort_keys=True),
            encoding="utf-8",
        )

        state = continuous_session._ContinuousSessionState(
            state_path,
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        assert state.snapshot().cycles_completed == _SMALL_HISTORY

        state.set_state(continuous_session.SessionState.PAUSED)

        promoted = json.loads(state_path.read_text(encoding="utf-8"))
        assert promoted["schema_version"] == 3
        assert promoted["generation"] == 1
        assert promoted["state"] == "PAUSED"


def test_state_cycle_cleanup_crashes_cannot_resurrect_stale_failure() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="ORIGINAL_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        stale_error = error_path.read_bytes()

        def crash_on_cleanup(_code: str | None) -> None:
            raise RuntimeError("simulated crash before sidecar cleanup")

        with patch.object(state, "_write_error_checkpoint", crash_on_cleanup):
            try:
                state.set_state(continuous_session.SessionState.PAUSED)
            except RuntimeError as exc:
                assert "simulated crash" in str(exc)
            else:
                raise AssertionError("first cleanup crash was not simulated")

        paused = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        assert paused.snapshot().state is continuous_session.SessionState.PAUSED
        assert paused.snapshot().last_error_code == "ORIGINAL_FAILURE"
        assert error_path.read_bytes() == stale_error

        with patch.object(paused, "_write_error_checkpoint", crash_on_cleanup):
            try:
                paused.set_state(continuous_session.SessionState.RUNNING)
            except RuntimeError as exc:
                assert "simulated crash" in str(exc)
            else:
                raise AssertionError("second cleanup crash was not simulated")

        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        snapshot = reopened.snapshot()
        assert snapshot.state is continuous_session.SessionState.RUNNING
        assert snapshot.last_error_code is None

        canonical = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        stale = json.loads(error_path.read_text(encoding="utf-8"))
        assert canonical["generation"] == 2
        assert stale["observed_generation"] == 0


def test_operational_checkpoint_generation_marker_rejects_boolean() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")
        error_path = root / "continuous_session.json.operational_error.json"
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        payload["observed_generation"] = True
        error_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "observed_generation" in str(exc)
        else:
            raise AssertionError("boolean observed_generation was accepted")


def test_session_update_blocks_behind_canonical_durable_path_lock() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        entered_mutation = threading.Event()
        completed = threading.Event()

        def mutate(raw: dict[str, object]) -> None:
            entered_mutation.set()
            raw["source_state_projection_backlog"] = True

        def worker() -> None:
            state._update(mutate)
            completed.set()

        with continuous_session.durable_path_lock(state.path):
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            assert not entered_mutation.wait(0.15)
            assert not completed.is_set()

        assert entered_mutation.wait(1.0)
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert completed.is_set()
        assert state.snapshot().source_state_projection_backlog is True


def test_session_update_rejects_runtime_rmw_lock_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        def attacker_lock(_path: object):
            raise AssertionError("runtime-rebound durable path lock executed")

        monkeypatch.setattr(
            continuous_session,
            "durable_path_lock",
            attacker_lock,
        )

        try:
            state.record_source_projection(deltas=(), backlog=False)
        except continuous_session.ContinuousSessionError as exc:
            assert "read-modify-write authority" in str(exc)
        else:
            raise AssertionError("runtime-rebound durable path lock was accepted")


def test_state_transition_blocks_late_failure_after_pause_wins() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="OLDER_FAILURE")

        original_write = state._write_error_checkpoint
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        failure_started = threading.Event()
        failure_completed = threading.Event()
        failure_errors: list[BaseException] = []

        def gated_write(code: str | None) -> None:
            if code is None:
                cleanup_entered.set()
                assert release_cleanup.wait(2.0)
            original_write(code)

        state._write_error_checkpoint = gated_write  # type: ignore[method-assign]

        transition = threading.Thread(
            target=lambda: state.set_state(continuous_session.SessionState.PAUSED),
            daemon=True,
        )
        transition.start()
        assert cleanup_entered.wait(1.0)

        def publish_new_failure() -> None:
            failure_started.set()
            try:
                state.record_failure(code="NEWER_FAILURE")
            except BaseException as exc:
                failure_errors.append(exc)
            finally:
                failure_completed.set()

        failure = threading.Thread(target=publish_new_failure, daemon=True)
        failure.start()
        assert failure_started.wait(1.0)
        assert not failure_completed.wait(0.15)

        release_cleanup.set()
        transition.join(timeout=2.0)
        failure.join(timeout=2.0)
        assert not transition.is_alive()
        assert not failure.is_alive()
        assert failure_completed.is_set()

        snapshot = state.snapshot()
        assert snapshot.state is continuous_session.SessionState.PAUSED
        assert snapshot.last_error_code == "OLDER_FAILURE"
        assert len(failure_errors) == 1
        assert isinstance(failure_errors[0], continuous_session.SessionPausedError)


def test_record_failure_rejects_runtime_session_lock_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        def attacker_lock(_path: object):
            raise AssertionError("runtime-rebound failure lock executed")

        monkeypatch.setattr(
            continuous_session,
            "durable_path_lock",
            attacker_lock,
        )
        try:
            state.record_failure(code="FAIL")
        except continuous_session.ContinuousSessionError as exc:
            assert "failure publication lock authority" in str(exc)
        else:
            raise AssertionError("runtime-rebound failure lock was accepted")


def test_record_failure_rejects_runtime_text_validator_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        def attacker_text(_value: object, _field: str) -> str:
            raise AssertionError("runtime-rebound failure text validator executed")

        monkeypatch.setattr(continuous_session, "_text", attacker_text)
        try:
            state.record_failure(code="FAIL")
        except continuous_session.ContinuousSessionError as exc:
            assert "failure publication lock authority" in str(exc)
        else:
            raise AssertionError("runtime-rebound failure validator was accepted")


def test_record_failure_ignores_instance_writer_rebinding() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        def attacker_writer(_code: str | None) -> None:
            raise AssertionError("instance-rebound checkpoint writer executed")

        with patch.object(state, "_write_error_checkpoint", attacker_writer):
            publication = state.record_failure(code="CANONICAL_FAILURE")

        assert publication.last_error_code == "CANONICAL_FAILURE"
        payload = json.loads(
            (root / "continuous_session.json.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        assert payload["last_error_code"] == "CANONICAL_FAILURE"


def test_record_failure_rejects_class_writer_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        def attacker_writer(
            _self: continuous_session._ContinuousSessionState,
            _code: str | None,
        ) -> None:
            raise AssertionError("class-rebound checkpoint writer executed")

        monkeypatch.setattr(
            continuous_session._ContinuousSessionState,
            "_write_error_checkpoint",
            attacker_writer,
        )
        try:
            state.record_failure(code="FAIL")
        except continuous_session.ContinuousSessionError as exc:
            assert "failure publication lock authority" in str(exc)
        else:
            raise AssertionError("class-rebound failure writer was accepted")


def test_stale_failure_writer_cannot_clobber_newer_failure_sidecar() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        stale = _state_with_history(root, _SMALL_HISTORY)
        current = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        current.record_success(
            at="2026-10-06T04:53:00+00:00",
            full_refresh=False,
            settlement_evidence=(),
        )
        current.record_failure(code="NEWER_FAILURE")

        try:
            stale.record_failure(code="STALE_FAILURE")
        except continuous_session.ContinuousSessionError as exc:
            assert "cannot overwrite newer operational error checkpoint" in str(exc)
        else:
            raise AssertionError("stale failure writer clobbered a newer checkpoint")

        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        assert reopened.snapshot().last_error_code == "NEWER_FAILURE"

        sidecar = json.loads(
            (
                root / "continuous_session.json.operational_error.json"
            ).read_text(encoding="utf-8")
        )
        canonical = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert sidecar["observed_generation"] == canonical["generation"]


def test_same_generation_failure_marker_conflict_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")
        sidecar_path = root / "continuous_session.json.operational_error.json"
        original = json.loads(sidecar_path.read_text(encoding="utf-8"))

        mutations = (
            ("observed_cycles_completed", original["observed_cycles_completed"] + 1),
            (
                "observed_last_success_at",
                (
                    None
                    if original["observed_last_success_at"] is not None
                    else "2026-10-06T04:53:00+00:00"
                ),
            ),
            (
                "observed_state",
                (
                    "PAUSED"
                    if original["observed_state"] != "PAUSED"
                    else "STOPPED"
                ),
            ),
        )
        for field, value in mutations:
            corrupted = dict(original)
            corrupted[field] = value
            sidecar_path.write_text(
                json.dumps(corrupted, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            before = sidecar_path.read_bytes()

            try:
                state.record_failure(code="SECOND_FAILURE")
            except continuous_session.ContinuousSessionError as exc:
                assert "same-generation operational error checkpoint markers" in str(exc)
            else:
                raise AssertionError(
                    f"same-generation {field} conflict was overwritten"
                )

            assert sidecar_path.read_bytes() == before


def test_stale_instance_failure_cannot_overwrite_newer_canonical_generation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        stale = _state_with_history(root, _SMALL_HISTORY)
        current = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        current.record_success(
            at="2026-10-06T04:53:00+00:00",
            full_refresh=False,
            settlement_evidence=(),
        )
        assert current.snapshot().cycles_completed == 5

        try:
            stale.record_failure(code="POST_SUCCESS_FAILURE")
        except continuous_session.ContinuousSessionError as exc:
            assert "canonical checkpoint changed" in str(exc)
        else:
            raise AssertionError("stale failure crossed a newer success generation")

        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        snapshot = reopened.snapshot()
        assert snapshot.cycles_completed == 5
        assert snapshot.last_error_code is None

        payload = json.loads(
            (root / "continuous_session.json.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        canonical = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert payload["observed_generation"] == canonical["generation"]
        assert payload["observed_cycles_completed"] == canonical["cycles_completed"]
        assert payload["observed_last_success_at"] == canonical["last_success_at"]
        assert payload["last_error_code"] is None
        assert canonical["last_success_at"] == "2026-10-06T04:53:00+00:00"


def test_restart_rejects_same_generation_failure_marker_conflicts() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")
        sidecar_path = root / "continuous_session.json.operational_error.json"
        original = json.loads(sidecar_path.read_text(encoding="utf-8"))

        mutations = (
            ("observed_cycles_completed", original["observed_cycles_completed"] + 1),
            (
                "observed_last_success_at",
                (
                    None
                    if original["observed_last_success_at"] is not None
                    else "2026-10-06T04:53:00+00:00"
                ),
            ),
            (
                "observed_state",
                (
                    "PAUSED"
                    if original["observed_state"] != "PAUSED"
                    else "STOPPED"
                ),
            ),
        )
        for field, value in mutations:
            corrupted = dict(original)
            corrupted[field] = value
            sidecar_path.write_text(
                json.dumps(corrupted, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            try:
                continuous_session._ContinuousSessionState(
                    root / "continuous_session.json",
                    session_id="session-history-scaling",
                    source_id="provider-a",
                    clock=lambda: _AT,
                )
            except continuous_session.ContinuousSessionError as exc:
                assert "same-generation operational error checkpoint markers" in str(exc)
            else:
                raise AssertionError(
                    f"same-generation {field} conflict survived restart"
                )


def test_restart_rejects_future_generation_failure_checkpoint() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")
        sidecar_path = root / "continuous_session.json.operational_error.json"
        corrupted = json.loads(sidecar_path.read_text(encoding="utf-8"))
        corrupted["observed_generation"] += 1
        sidecar_path.write_text(
            json.dumps(corrupted, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        try:
            continuous_session._ContinuousSessionState(
                root / "continuous_session.json",
                session_id="session-history-scaling",
                source_id="provider-a",
                clock=lambda: _AT,
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "generation is ahead of canonical session bootstrap state" in str(exc)
        else:
            raise AssertionError("future-generation failure checkpoint survived restart")


def test_snapshot_rejects_future_generation_failure_checkpoint() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")
        sidecar_path = root / "continuous_session.json.operational_error.json"
        corrupted = json.loads(sidecar_path.read_text(encoding="utf-8"))
        corrupted["observed_generation"] += 1
        sidecar_path.write_text(
            json.dumps(corrupted, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        try:
            state.snapshot()
        except continuous_session.ContinuousSessionError as exc:
            assert "generation is ahead of canonical session state" in str(exc)
        else:
            raise AssertionError("future-generation failure checkpoint was ignored")


def test_snapshot_rejects_same_generation_failure_marker_conflicts() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")
        sidecar_path = root / "continuous_session.json.operational_error.json"
        original = json.loads(sidecar_path.read_text(encoding="utf-8"))

        mutations = (
            ("observed_cycles_completed", original["observed_cycles_completed"] + 1),
            (
                "observed_last_success_at",
                (
                    None
                    if original["observed_last_success_at"] is not None
                    else "2026-10-06T04:53:00+00:00"
                ),
            ),
            (
                "observed_state",
                (
                    "PAUSED"
                    if original["observed_state"] != "PAUSED"
                    else "STOPPED"
                ),
            ),
        )
        for field, value in mutations:
            corrupted = dict(original)
            corrupted[field] = value
            sidecar_path.write_text(
                json.dumps(corrupted, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            try:
                state.snapshot()
            except continuous_session.ContinuousSessionError as exc:
                assert "same-generation operational error checkpoint markers" in str(exc)
            else:
                raise AssertionError(
                    f"same-generation {field} conflict was hidden by snapshot"
                )


def test_snapshot_serializes_with_concurrent_failure_publication() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        original_write = state._write_error_checkpoint
        writer_entered = threading.Event()
        release_writer = threading.Event()
        snapshot_started = threading.Event()
        snapshot_completed = threading.Event()
        snapshot_result: list[continuous_session.ContinuousSessionStatus] = []

        def gated_write(code: str | None) -> None:
            if code == "CONCURRENT_FAILURE":
                writer_entered.set()
                assert release_writer.wait(2.0)
            original_write(code)

        state._write_error_checkpoint = gated_write  # type: ignore[method-assign]

        publisher = threading.Thread(
            target=lambda: state.record_failure(code="CONCURRENT_FAILURE"),
            daemon=True,
        )
        publisher.start()
        assert writer_entered.wait(1.0)

        def take_snapshot() -> None:
            snapshot_started.set()
            snapshot_result.append(state.snapshot())
            snapshot_completed.set()

        reader = threading.Thread(target=take_snapshot, daemon=True)
        reader.start()
        assert snapshot_started.wait(1.0)
        assert not snapshot_completed.wait(0.15)

        release_writer.set()
        publisher.join(timeout=2.0)
        reader.join(timeout=2.0)
        assert not publisher.is_alive()
        assert not reader.is_alive()
        assert snapshot_completed.is_set()
        assert snapshot_result[0].last_error_code == "CONCURRENT_FAILURE"


def test_snapshot_rejects_runtime_session_lock_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        def attacker_lock(_path: object):
            raise AssertionError("runtime-rebound snapshot lock executed")

        monkeypatch.setattr(
            continuous_session,
            "durable_path_lock",
            attacker_lock,
        )
        try:
            state.snapshot()
        except continuous_session.ContinuousSessionError as exc:
            assert "snapshot authority changed" in str(exc)
        else:
            raise AssertionError("runtime-rebound snapshot lock was accepted")


def test_concurrent_bootstrap_has_one_identity_winner_and_conflict_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state_path = root / "continuous_session.json"
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, str]] = []
        outcomes_lock = threading.Lock()

        def bootstrap(session_id: str) -> None:
            barrier.wait()
            try:
                state = continuous_session._ContinuousSessionState(
                    state_path,
                    session_id=session_id,
                    source_id="provider-a",
                    clock=lambda: _AT,
                )
                outcome = ("success", state.session_id)
            except continuous_session.ContinuousSessionError as exc:
                outcome = ("error", str(exc))
            with outcomes_lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=bootstrap, args=("session-a",), daemon=True),
            threading.Thread(target=bootstrap, args=("session-b",), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)
            assert not thread.is_alive()

        successes = [value for kind, value in outcomes if kind == "success"]
        errors = [value for kind, value in outcomes if kind == "error"]
        assert len(successes) == 1
        assert len(errors) == 1
        assert "durable session_id does not match configured session" in errors[0]

        payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert payload["session_id"] == successes[0]
        assert payload["generation"] == 0


def test_bootstrap_rejects_runtime_session_lock_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        def attacker_lock(_path: object):
            raise AssertionError("runtime-rebound bootstrap lock executed")

        monkeypatch.setattr(
            continuous_session,
            "durable_path_lock",
            attacker_lock,
        )
        try:
            continuous_session._ContinuousSessionState(
                root / "continuous_session.json",
                session_id="session-a",
                source_id="provider-a",
                clock=lambda: _AT,
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "bootstrap authority changed" in str(exc)
        else:
            raise AssertionError("runtime-rebound bootstrap lock was accepted")


def test_repeated_pause_is_generation_neutral_and_preserves_failure() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="PAUSED_FAILURE")
        state.set_state(continuous_session.SessionState.PAUSED)

        before = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        state.set_state(continuous_session.SessionState.PAUSED)
        after = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )

        assert after["generation"] == before["generation"]
        assert state.snapshot().last_error_code == "PAUSED_FAILURE"


def test_repeated_stop_same_reason_is_generation_neutral() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.set_state(
            continuous_session.SessionState.STOPPED,
            reason="OPERATOR_STOP",
        )
        before = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )

        state.set_state(
            continuous_session.SessionState.STOPPED,
            reason="OPERATOR_STOP",
        )
        after = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert after["generation"] == before["generation"]
        assert after["last_error_code"] == "OPERATOR_STOP"


def test_resume_clears_predecessor_stop_reason_immediately() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.set_state(
            continuous_session.SessionState.STOPPED,
            reason="OPERATOR_STOP",
        )
        stopped = state.snapshot()
        assert stopped.state is continuous_session.SessionState.STOPPED
        assert stopped.last_error_code == "OPERATOR_STOP"

        state.set_state(continuous_session.SessionState.RUNNING)
        resumed = state.snapshot()
        assert resumed.state is continuous_session.SessionState.RUNNING
        assert resumed.last_error_code is None


def test_resume_from_paused_reason_clears_predecessor_reason() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.set_state(
            continuous_session.SessionState.PAUSED,
            reason="OPERATOR_PAUSE",
        )
        assert state.snapshot().last_error_code == "OPERATOR_PAUSE"

        state.set_state(continuous_session.SessionState.RUNNING)
        resumed = state.snapshot()
        assert resumed.state is continuous_session.SessionState.RUNNING
        assert resumed.last_error_code is None


def test_invalid_main_session_id_is_normalized_to_domain_error() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state_path = root / "continuous_session.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["session_id"] = " bad-session "
        state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        try:
            state._read()
        except continuous_session.ContinuousSessionError as exc:
            assert "identity/timestamp" in str(exc)
        else:
            raise AssertionError("invalid main session_id escaped domain normalization")


def test_invalid_main_success_timestamp_is_normalized_to_domain_error() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state_path = root / "continuous_session.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["last_success_at"] = "not-an-instant"
        state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        try:
            state._read()
        except continuous_session.ContinuousSessionError as exc:
            assert "invalid durable field" in str(exc)
        else:
            raise AssertionError("invalid main success timestamp escaped validation")


def test_causally_impossible_main_session_time_combinations_fail_closed() -> None:
    cases = (
        ("zero-cycles-with-success", {"cycles_completed": 0}),
        ("positive-cycles-without-success", {"last_success_at": None}),
        (
            "success-before-start",
            {
                "started_at": "2026-09-22T06:21:00+00:00",
                "last_success_at": "2026-09-22T06:20:00+00:00",
            },
        ),
        (
            "full-refresh-after-success",
            {"last_full_refresh_at": "2026-09-22T06:21:00+00:00"},
        ),
    )
    for label, changes in cases:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "continuous_session.json"
            payload = _checkpoint_payload(_SMALL_HISTORY)
            payload.update(changes)
            state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

            try:
                continuous_session._ContinuousSessionState(
                    state_path,
                    session_id="session-history-scaling",
                    source_id="provider-a",
                    clock=lambda: _AT,
                )
            except continuous_session.ContinuousSessionError:
                pass
            else:
                raise AssertionError(f"{label} durable checkpoint was accepted")


def test_invalid_settlement_evidence_digest_is_normalized_to_domain_error() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state_path = root / "continuous_session.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["settlement_evidence"][0]["evidence_sha256"] = "not-a-digest"
        state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        try:
            state._read()
        except continuous_session.ContinuousSessionError as exc:
            assert "invalid durable field" in str(exc)
        else:
            raise AssertionError("invalid settlement evidence digest escaped validation")


def test_duplicate_settlement_evidence_id_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state_path = root / "continuous_session.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        duplicate = dict(payload["settlement_evidence"][0])
        payload["settlement_evidence"].append(duplicate)
        state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        try:
            state._read()
        except continuous_session.ContinuousSessionError as exc:
            assert "evidence_id values must be unique" in str(exc)
        else:
            raise AssertionError("duplicate settlement evidence id was accepted")


def test_conflicting_duplicate_settlement_evidence_id_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state_path = root / "continuous_session.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        conflicting = dict(payload["settlement_evidence"][0])
        conflicting["settlement_ref"] = "conflicting-ref"
        payload["settlement_evidence"].append(conflicting)
        state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        try:
            state._read()
        except continuous_session.ContinuousSessionError as exc:
            assert "evidence_id values must be unique" in str(exc)
        else:
            raise AssertionError("conflicting duplicate evidence id was accepted")


def test_concurrent_successes_return_distinct_committed_cycle_indexes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first = _state_with_history(root, _SMALL_HISTORY)
        second = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        barrier = threading.Barrier(2)
        results: list[int] = []
        results_lock = threading.Lock()

        def succeed(state: continuous_session._ContinuousSessionState) -> None:
            barrier.wait()
            cycle_index = state.record_success(
                at="2026-10-06T04:59:00+00:00",
                full_refresh=False,
                settlement_evidence=(),
            )
            with results_lock:
                results.append(cycle_index)

        threads = [
            threading.Thread(target=succeed, args=(first,), daemon=True),
            threading.Thread(target=succeed, args=(second,), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)
            assert not thread.is_alive()

        assert sorted(results) == [5, 6]
        assert first.snapshot().cycles_completed == 6


def test_success_timestamp_cannot_precede_session_start() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        before = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )

        try:
            state.record_success(
                at="2026-09-22T06:19:59+00:00",
                full_refresh=False,
                settlement_evidence=(),
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "precedes session start" in str(exc)
        else:
            raise AssertionError("pre-start success timestamp was accepted")

        after = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert after == before


def test_success_timestamp_cannot_roll_back_last_success() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        committed = state.record_success(
            at="2026-09-22T06:21:00+00:00",
            full_refresh=False,
            settlement_evidence=(),
        )
        assert committed == 5

        before = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        try:
            state.record_success(
                at="2026-09-22T06:20:30+00:00",
                full_refresh=False,
                settlement_evidence=(),
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "roll back durable session time" in str(exc)
        else:
            raise AssertionError("success-time rollback was accepted")

        after = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert after == before


def test_record_success_requires_exact_boolean_full_refresh() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        try:
            state.record_success(
                at="2026-09-22T06:21:00+00:00",
                full_refresh=1,  # type: ignore[arg-type]
                settlement_evidence=(),
            )
        except TypeError as exc:
            assert "full_refresh must be boolean" in str(exc)
        else:
            raise AssertionError("non-boolean full_refresh was accepted")


def test_record_success_rejects_future_settlement_evidence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        evidence = continuous_session.SettlementResolution(
            event_identity="provider-a:event-future",
            settlement_ref="provider-result:future",
            quote_outcomes={"quote-future": "win"},
            evidence_id="receipt-future",
            evidence_sha256="c" * 64,
            available_at="2026-09-22T06:22:00+00:00",
        )
        before = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )

        try:
            state.record_success(
                at="2026-09-22T06:21:00+00:00",
                full_refresh=False,
                settlement_evidence=(evidence,),
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "invalid for success cutoff" in str(exc)
        else:
            raise AssertionError("future settlement evidence was accepted")

        after = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert after == before


def test_record_success_rejects_non_tuple_settlement_evidence() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        try:
            state.record_success(
                at="2026-09-22T06:21:00+00:00",
                full_refresh=False,
                settlement_evidence=[],  # type: ignore[arg-type]
            )
        except TypeError as exc:
            assert "exact tuple" in str(exc)
        else:
            raise AssertionError("non-tuple settlement evidence was accepted")


def test_record_success_rejects_noncanonical_resolution_type() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        class DerivedResolution(continuous_session.SettlementResolution):
            pass

        evidence = DerivedResolution(
            event_identity="provider-a:event-derived",
            settlement_ref="provider-result:derived",
            quote_outcomes={"quote-derived": "win"},
            evidence_id="receipt-derived",
            evidence_sha256="d" * 64,
            available_at=_AT,
        )
        try:
            state.record_success(
                at="2026-09-22T06:21:00+00:00",
                full_refresh=False,
                settlement_evidence=(evidence,),
            )
        except TypeError as exc:
            assert "exact SettlementResolution" in str(exc)
        else:
            raise AssertionError("derived settlement resolution was accepted")


def _projection_delta(
    index: int,
    *,
    delta_id: str | None = None,
    epoch: str = "epoch-a",
    position: int | None = None,
    revision_of: str | None = None,
    revision_number: int = 0,
    gap_state: continuous_session.GapState = continuous_session.GapState.NONE,
    sync_state: continuous_session.SyncState = continuous_session.SyncState.READY,
    gap_from_cursor: str | None = None,
    gap_to_cursor: str | None = None,
) -> continuous_session.CollectorDelta:
    resolved_position = index if position is None else position
    return continuous_session.CollectorDelta(
        schema_version=1,
        delta_id=delta_id or f"delta-{index}",
        source_id="provider-a",
        lawful_terms_ref="lawful:provider-a",
        retention_ref="retention:default",
        stream_epoch=epoch,
        source_cursor=f"cursor-{resolved_position}",
        cursor_position=resolved_position,
        event_dedupe_key=f"event-dedupe-{resolved_position}",
        event_id=f"event-{resolved_position}",
        source_payload_digest=f"{index + 1:064x}"[-64:],
        canonical_event_digest=f"{index + 101:064x}"[-64:],
        source_observed_at=_AT,
        collector_received_at=_AT,
        collector_committed_at=_AT,
        desktop_available_at=_AT,
        revision_of=revision_of,
        revision_number=revision_number,
        gap_state=gap_state,
        sync_state=sync_state,
        gap_from_cursor=gap_from_cursor,
        gap_to_cursor=gap_to_cursor,
    )


def test_source_projection_change_is_session_generation_neutral() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        before = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )

        state.record_source_projection(
            deltas=(_projection_delta(1),),
            backlog=False,
        )

        after = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert after["generation"] == before.get("generation", 0)
        assert after["source_state_delta_id"] == "delta-1"
        assert after["source_projection_stream_epoch"] == "epoch-a"


def test_empty_source_projection_noop_is_generation_neutral() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        before = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )

        state.record_source_projection(deltas=(), backlog=False)

        after = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert after.get("generation", 0) == before.get("generation", 0)


def test_projection_change_preserves_active_failure_overlay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="PRE_PROJECTION_FAILURE")
        assert state.snapshot().last_error_code == "PRE_PROJECTION_FAILURE"

        state.record_source_projection(
            deltas=(_projection_delta(1),),
            backlog=False,
        )

        # Source projection updates observation truth but is not a successful
        # session generation. It must not erase the active operational failure.
        assert state.snapshot().last_error_code == "PRE_PROJECTION_FAILURE"


def test_failure_after_projection_binds_to_current_session_generation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_source_projection(
            deltas=(_projection_delta(1),),
            backlog=False,
        )
        state.record_failure(code="POST_PROJECTION_FAILURE")

        snapshot = state.snapshot()
        assert snapshot.last_error_code == "POST_PROJECTION_FAILURE"
        main = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        sidecar = json.loads(
            (root / "continuous_session.json.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        assert sidecar["observed_generation"] == main["generation"]


def test_projection_noop_preserves_current_failure_overlay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CURRENT_FAILURE")

        state.record_source_projection(deltas=(), backlog=False)

        assert state.snapshot().last_error_code == "CURRENT_FAILURE"


def test_source_projection_rejects_duplicate_delta_ids_without_state_change() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        before = (root / "continuous_session.json").read_bytes()

        try:
            state.record_source_projection(
                deltas=(
                    _projection_delta(1, delta_id="same-delta"),
                    _projection_delta(2, delta_id="same-delta"),
                ),
                backlog=False,
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "delta ids must be unique" in str(exc)
        else:
            raise AssertionError("duplicate projection delta ids were accepted")

        assert (root / "continuous_session.json").read_bytes() == before


def test_source_projection_rejects_regressive_positions_within_epoch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        try:
            state.record_source_projection(
                deltas=(
                    _projection_delta(1, position=2),
                    _projection_delta(2, position=1),
                ),
                backlog=False,
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "position moved backwards" in str(exc)
        else:
            raise AssertionError("regressive projection positions were accepted")


def test_source_projection_rejects_equal_positions_within_epoch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        try:
            state.record_source_projection(
                deltas=(
                    _projection_delta(1, position=2),
                    _projection_delta(2, position=2),
                ),
                backlog=False,
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "requires a revision" in str(exc)
        else:
            raise AssertionError("equal non-revision projection positions were accepted")


def test_source_projection_allows_position_reset_on_epoch_change() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        state.record_source_projection(
            deltas=(
                _projection_delta(1, epoch="epoch-a", position=10),
                _projection_delta(2, epoch="epoch-b", position=0),
            ),
            backlog=False,
        )

        snapshot = state.snapshot()
        assert snapshot.source_state_delta_id == "delta-2"
        assert snapshot.source_projection_stream_epoch == "epoch-b"


def test_source_projection_backlog_requires_a_delta() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        try:
            state.record_source_projection(deltas=(), backlog=True)
        except continuous_session.ContinuousSessionError as exc:
            assert "backlog requires at least one delta" in str(exc)
        else:
            raise AssertionError("backlog without projection delta was accepted")


def test_source_projection_requires_exact_tuple() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        try:
            state.record_source_projection(
                deltas=[_projection_delta(1)],  # type: ignore[arg-type]
                backlog=False,
            )
        except TypeError as exc:
            assert "exact tuple" in str(exc)
        else:
            raise AssertionError("non-tuple projection batch was accepted")


def test_source_projection_allows_equal_position_explicit_revision() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        state.record_source_projection(
            deltas=(
                _projection_delta(1, delta_id="base-delta", position=5),
                _projection_delta(
                    2,
                    delta_id="revision-delta",
                    position=5,
                    revision_of="base-delta",
                    revision_number=1,
                ),
            ),
            backlog=False,
        )

        snapshot = state.snapshot()
        assert snapshot.source_state_delta_id == "revision-delta"


def test_source_projection_allows_late_revision_of_older_position_in_same_batch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        state.record_source_projection(
            deltas=(
                _projection_delta(1, delta_id="base-delta", position=5),
                _projection_delta(2, delta_id="newer-delta", position=6),
                _projection_delta(
                    3,
                    delta_id="late-revision",
                    position=5,
                    revision_of="base-delta",
                    revision_number=1,
                ),
            ),
            backlog=False,
        )

        assert state.snapshot().source_state_delta_id == "late-revision"


def test_source_projection_rejects_equal_position_revision_number_jump() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        before = state.path.read_bytes()

        try:
            state.record_source_projection(
                deltas=(
                    _projection_delta(1, delta_id="base-delta", position=5),
                    _projection_delta(
                        2,
                        delta_id="revision-delta",
                        position=5,
                        revision_of="base-delta",
                        revision_number=2,
                    ),
                ),
                backlog=False,
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "advance exactly one step" in str(exc)
        else:
            raise AssertionError("equal-position revision number jump was accepted")

        assert state.path.read_bytes() == before


def test_source_projection_late_gap_recovery_clears_original_gap() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        state.record_source_projection(
            deltas=(
                _projection_delta(
                    1,
                    delta_id="gap-delta",
                    position=5,
                    gap_state=continuous_session.GapState.DETECTED,
                    sync_state=continuous_session.SyncState.GAP_DETECTED,
                    gap_from_cursor="cursor-4",
                    gap_to_cursor="cursor-5",
                ),
                _projection_delta(2, delta_id="newer-delta", position=6),
                _projection_delta(
                    3,
                    delta_id="gap-recovery",
                    position=5,
                    revision_of="gap-delta",
                    revision_number=1,
                    gap_state=continuous_session.GapState.RECOVERED,
                    sync_state=continuous_session.SyncState.RECOVERED,
                    gap_from_cursor="cursor-4",
                    gap_to_cursor="cursor-5",
                ),
            ),
            backlog=False,
        )

        snapshot = state.snapshot()
        assert snapshot.source_state_delta_id == "gap-recovery"
        assert snapshot.source_unresolved_gap_delta_ids == ()
        assert snapshot.source_gap_state == continuous_session.GapState.RECOVERED.value
        assert snapshot.source_sync_state == continuous_session.SyncState.RECOVERED.value


def test_source_projection_rejects_same_batch_revision_identity_mismatch() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        before = state.path.read_bytes()
        base = _projection_delta(1, delta_id="base-delta", position=5)
        revision = replace(
            _projection_delta(
                2,
                delta_id="revision-delta",
                position=5,
                revision_of="base-delta",
                revision_number=1,
            ),
            event_id="different-event",
        )

        try:
            state.record_source_projection(
                deltas=(base, revision),
                backlog=False,
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "revision event does not match predecessor" in str(exc)
        else:
            raise AssertionError("same-batch revision identity mismatch was accepted")

        assert state.path.read_bytes() == before


def test_source_projection_allows_cross_batch_revision_of_older_position() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_source_projection(
            deltas=(
                _projection_delta(1, delta_id="base-delta", position=5),
                _projection_delta(2, delta_id="newer-delta", position=6),
            ),
            backlog=False,
        )

        state.record_source_projection(
            deltas=(
                _projection_delta(
                    3,
                    delta_id="late-revision",
                    position=5,
                    revision_of="base-delta",
                    revision_number=1,
                ),
            ),
            backlog=False,
            expected_after_delta_id="newer-delta",
        )

        assert state.snapshot().source_state_delta_id == "late-revision"


def test_source_projection_allows_first_revision_of_expected_predecessor() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_source_projection(
            deltas=(
                _projection_delta(1, delta_id="base-delta", position=5),
            ),
            backlog=False,
        )

        state.record_source_projection(
            deltas=(
                _projection_delta(
                    2,
                    delta_id="revision-delta",
                    position=5,
                    revision_of="base-delta",
                    revision_number=1,
                ),
            ),
            backlog=False,
            expected_after_delta_id="base-delta",
        )

        assert state.snapshot().source_state_delta_id == "revision-delta"



def test_record_failure_returns_bounded_publication_without_full_history_read() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _LARGE_HISTORY)

        def forbidden_reader():
            raise AssertionError(
                "failure publication must not re-enter the full settlement-history reader"
            )

        with patch.object(state, "_read", forbidden_reader):
            publication = state.record_failure(code="BOUNDED_FAILURE")

        assert publication.session_id == "session-history-scaling"
        assert publication.state is continuous_session.SessionState.RUNNING
        assert publication.generation == 0
        assert publication.cycles_completed == _LARGE_HISTORY
        assert publication.last_success_at == _AT
        assert publication.last_error_code == "BOUNDED_FAILURE"



def test_provider_unavailable_tick_uses_bounded_failure_publication() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _LARGE_HISTORY)
        state.record_source_projection(
            deltas=(_projection_delta(1),),
            backlog=False,
        )

        class ProviderUnavailableCycle:
            provider_unavailable = True
            source_id = "provider-a"
            committed_delta_ids = ()

        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = state
        coordinator.clock = lambda: _AT
        coordinator.collector = type(
            "CollectorStub",
            (),
            {"run_cycle": lambda _self: ProviderUnavailableCycle()},
        )()
        coordinator.invalidation_buffer = type(
            "InvalidationBufferStub",
            (),
            {"pending_count": 0, "full_refresh_required": False},
        )()

        def forbidden_reader():
            raise AssertionError(
                "provider-unavailable tick must not read the full settlement history"
            )

        def forbidden_projection():
            raise AssertionError(
                "provider-unavailable tick must not refresh the full session projection"
            )

        coordinator._refresh_source_state_projection = forbidden_projection

        with patch.object(state, "_read", forbidden_reader):
            result = coordinator.tick()

        assert result.session_id == "session-history-scaling"
        assert result.cycle_index == _LARGE_HISTORY
        assert result.source_id == "provider-a"
        assert result.source_provider_unavailable is True
        assert result.source_gap_states == ("NONE",)
        assert result.source_sync_states == ("READY",)
        assert result.last_success_at == _AT
        error_path = root / "continuous_session.json.operational_error.json"
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        assert payload["last_error_code"] == "ProviderUnavailableError"


def test_record_failure_does_not_enter_full_history_reader() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _LARGE_HISTORY)
        before = state.path.read_bytes()

        def forbidden_reader():
            raise AssertionError("bounded operational failure must not read full settlement history")

        with patch.object(state, "_read", forbidden_reader):
            state.record_failure(code="BOUNDED_FAILURE")

        assert state.path.read_bytes() == before
        error_path = root / "continuous_session.json.operational_error.json"
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        assert payload["last_error_code"] == "BOUNDED_FAILURE"



def test_record_failure_with_existing_sidecar_still_avoids_full_history_reader() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _LARGE_HISTORY)
        state.record_failure(code="FIRST_FAILURE")

        main_before = state.path.read_bytes()
        error_path = root / "continuous_session.json.operational_error.json"

        def forbidden_reader():
            raise AssertionError(
                "bounded operational failure with an existing sidecar must not read full settlement history"
            )

        with patch.object(state, "_read", forbidden_reader):
            state.record_failure(code="SECOND_FAILURE")

        assert state.path.read_bytes() == main_before
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        assert payload["last_error_code"] == "SECOND_FAILURE"


def test_record_failure_rejects_corrupt_existing_sidecar_without_full_history_read() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _LARGE_HISTORY)
        state.record_failure(code="FIRST_FAILURE")

        main_before = state.path.read_bytes()
        error_path = root / "continuous_session.json.operational_error.json"
        error_path.write_bytes(b'{"schema": "autosport.continuous_session.operational_error"')

        sidecar_before = error_path.read_bytes()

        def forbidden_reader():
            raise AssertionError(
                "corrupt operational sidecar must not redirect failure handling into full settlement-history read"
            )

        with patch.object(state, "_read", forbidden_reader):
            try:
                state.record_failure(code="SECOND_FAILURE")
            except continuous_session.ContinuousSessionError as exc:
                assert "cannot verify continuous session operational error checkpoint" in str(exc)
            else:
                raise AssertionError("corrupt operational sidecar was overwritten")

        assert state.path.read_bytes() == main_before
        assert error_path.read_bytes() == sidecar_before


def test_stale_instance_failure_overlay_is_ignored_after_newer_generation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first = _state_with_history(root, _SMALL_HISTORY)
        second = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        second.record_success(
            at="2026-09-22T06:21:00+00:00",
            full_refresh=False,
            settlement_evidence=(),
        )
        assert second.snapshot().cycles_completed == _SMALL_HISTORY + 1

        try:
            first.record_failure(code="STALE_INSTANCE_FAILURE")
        except continuous_session.ContinuousSessionError as exc:
            assert "canonical checkpoint changed" in str(exc)
        else:
            raise AssertionError("stale failure crossed a newer success generation")

        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        assert reopened.snapshot().last_error_code is None
        assert reopened.snapshot().cycles_completed == _SMALL_HISTORY + 1


def test_fresh_failure_checkpoint_remains_visible_when_generation_is_unchanged() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first = _state_with_history(root, _SMALL_HISTORY)
        second = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        first.record_failure(code="FIRST_FAILURE")
        second.record_failure(code="SECOND_FAILURE")

        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        assert reopened.snapshot().last_error_code == "SECOND_FAILURE"



def test_bootstrap_rejects_runtime_path_exists_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        def attacker_exists(_path: Path) -> bool:
            raise AssertionError("runtime-rebound Path.exists executed")

        monkeypatch.setattr(Path, "exists", attacker_exists)
        try:
            continuous_session._ContinuousSessionState(
                root / "continuous_session.json",
                session_id="session-a",
                source_id="provider-a",
                clock=lambda: _AT,
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "bootstrap authority changed" in str(exc)
        else:
            raise AssertionError("runtime-rebound Path.exists was accepted")


def test_source_projection_accepts_exact_expected_predecessor() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)

        state.record_source_projection(
            deltas=(_projection_delta(1),),
            backlog=False,
            expected_after_delta_id=None,
        )
        assert state.snapshot().source_state_delta_id == "delta-1"

        state.record_source_projection(
            deltas=(_projection_delta(2),),
            backlog=False,
            expected_after_delta_id="delta-1",
        )
        assert state.snapshot().source_state_delta_id == "delta-2"


def test_source_projection_rejects_stale_expected_predecessor_without_write() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first = _state_with_history(root, _SMALL_HISTORY)
        second = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        first.record_source_projection(
            deltas=(_projection_delta(1),),
            backlog=False,
            expected_after_delta_id=None,
        )
        before = (root / "continuous_session.json").read_bytes()

        try:
            second.record_source_projection(
                deltas=(_projection_delta(2),),
                backlog=False,
                expected_after_delta_id=None,
            )
        except continuous_session.ContinuousSessionError as exc:
            assert "predecessor changed" in str(exc)
        else:
            raise AssertionError("stale source projection predecessor was accepted")

        assert (root / "continuous_session.json").read_bytes() == before
        assert first.snapshot().source_state_delta_id == "delta-1"


def test_source_projection_rejects_invalid_expected_predecessor_identity() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        before = (root / "continuous_session.json").read_bytes()

        try:
            state.record_source_projection(
                deltas=(_projection_delta(1),),
                backlog=False,
                expected_after_delta_id=" bad-id ",
            )
        except ValueError as exc:
            assert "expected_after_delta_id" in str(exc)
        else:
            raise AssertionError("invalid projection predecessor identity was accepted")

        assert (root / "continuous_session.json").read_bytes() == before


class _ResolutionRecord:
    def __init__(self, identity: str, settlement_ref: str) -> None:
        self.phase = continuous_session.EventPhase.COMPLETED
        self.settlement_ref = settlement_ref
        self.identity = identity


class _ResolutionLifecycle:
    def __init__(self, records: tuple[object, ...]) -> None:
        self._records = records

    def records(self) -> tuple[object, ...]:
        return self._records


class _ResolutionAuthority:
    def __init__(
        self,
        values: tuple[continuous_session.SettlementResolution | None, ...],
    ) -> None:
        self._values = list(values)

    def resolve(
        self,
        _record: object,
        *,
        as_of: str,
    ) -> continuous_session.SettlementResolution | None:
        assert as_of
        return self._values.pop(0)


def _resolution_coordinator(
    records: tuple[object, ...],
    values: tuple[continuous_session.SettlementResolution | None, ...],
) -> continuous_session.ContinuousSessionCoordinator:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    coordinator.lifecycle = _ResolutionLifecycle(records)
    coordinator.outcome_authority = _ResolutionAuthority(values)
    return coordinator


def _resolution(
    *,
    evidence_id: str = "evidence-1",
    outcome: str = "win",
    digest_char: str = "e",
    available_at: str = _AT,
) -> continuous_session.SettlementResolution:
    return continuous_session.SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="settlement-1",
        quote_outcomes={"quote-1": outcome},
        evidence_id=evidence_id,
        evidence_sha256=digest_char * 64,
        available_at=available_at,
    )


def test_settlement_resolution_collection_deduplicates_identical_evidence() -> None:
    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    resolution = _resolution()
    coordinator = _resolution_coordinator(
        (record, record),
        (resolution, resolution),
    )

    collected = coordinator._settlement_resolutions(as_of=_AT)

    assert collected == (resolution,)


def test_settlement_resolution_collection_rejects_conflicting_duplicate_evidence() -> None:
    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    coordinator = _resolution_coordinator(
        (record, record),
        (
            _resolution(outcome="win", digest_char="e"),
            _resolution(outcome="loss", digest_char="f"),
        ),
    )

    try:
        coordinator._settlement_resolutions(as_of=_AT)
    except continuous_session.ContinuousSessionError as exc:
        assert "conflicting duplicate evidence_id" in str(exc)
    else:
        raise AssertionError("conflicting duplicate settlement evidence was accepted")


def test_settlement_resolution_collection_rejects_conflicting_outcomes_across_evidence_ids() -> None:
    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    coordinator = _resolution_coordinator(
        (record, record),
        (
            _resolution(
                evidence_id="evidence-1",
                outcome="win",
                digest_char="e",
            ),
            _resolution(
                evidence_id="evidence-2",
                outcome="loss",
                digest_char="f",
            ),
        ),
    )

    try:
        coordinator._settlement_resolutions(as_of=_AT)
    except continuous_session.ContinuousSessionError as exc:
        assert "conflicting settlement outcomes" in str(exc)
    else:
        raise AssertionError("order-dependent settlement outcome conflict was accepted")


def test_settlement_resolution_collection_keeps_corroborating_evidence_ids() -> None:
    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    coordinator = _resolution_coordinator(
        (record, record),
        (
            _resolution(
                evidence_id="evidence-1",
                outcome="win",
                digest_char="e",
            ),
            _resolution(
                evidence_id="evidence-2",
                outcome="win",
                digest_char="f",
            ),
        ),
    )

    collected = coordinator._settlement_resolutions(as_of=_AT)

    assert tuple(item.evidence_id for item in collected) == (
        "evidence-1",
        "evidence-2",
    )


def test_settlement_resolution_collection_rejects_noncanonical_outcome_mapping_before_copy() -> None:
    class ExplodingDict(dict[str, str]):
        def copy(self):
            raise AssertionError("noncanonical mapping copy executed")

        def items(self):
            raise AssertionError("noncanonical mapping items executed")

        def __iter__(self):
            raise AssertionError("noncanonical mapping iteration executed")

    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    resolution = continuous_session.SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="settlement-1",
        quote_outcomes=ExplodingDict({"quote-1": "win"}),
        evidence_id="evidence-1",
        evidence_sha256="a" * 64,
        available_at=_AT,
    )
    coordinator = _resolution_coordinator((record,), (resolution,))

    try:
        coordinator._settlement_resolutions(as_of=_AT)
    except continuous_session.ContinuousSessionError as exc:
        assert "quote_outcomes must be an exact dict" in str(exc)
    else:
        raise AssertionError("noncanonical settlement outcome mapping was accepted")


def test_settlement_resolution_collection_rejects_derived_resolution_type() -> None:
    class DerivedResolution(continuous_session.SettlementResolution):
        pass

    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    derived = DerivedResolution(
        event_identity="provider-a:event-1",
        settlement_ref="settlement-1",
        quote_outcomes={"quote-1": "win"},
        evidence_id="evidence-derived",
        evidence_sha256="a" * 64,
        available_at=_AT,
    )
    coordinator = _resolution_coordinator((record,), (derived,))

    try:
        coordinator._settlement_resolutions(as_of=_AT)
    except continuous_session.ContinuousSessionError as exc:
        assert "exact SettlementResolution" in str(exc)
    else:
        raise AssertionError("derived settlement resolution was accepted")


def test_settlement_resolution_collection_normalizes_future_evidence_error() -> None:
    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    coordinator = _resolution_coordinator(
        (record,),
        (_resolution(available_at="2026-09-22T06:21:00+00:00"),),
    )

    try:
        coordinator._settlement_resolutions(as_of=_AT)
    except continuous_session.ContinuousSessionError as exc:
        assert "invalid settlement resolution" in str(exc)
    else:
        raise AssertionError("future settlement resolution was accepted")


def test_settlement_resolution_collection_rejects_callback_identity_mutation() -> None:
    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    coordinator = _resolution_coordinator((record,), (_resolution(),))

    class MutatingAuthority:
        def resolve(
            self,
            live_record: object,
            *,
            as_of: str,
        ) -> continuous_session.SettlementResolution:
            assert as_of == _AT
            live_record.identity = "provider-a:event-attacker"
            live_record.settlement_ref = "settlement-attacker"
            return continuous_session.SettlementResolution(
                event_identity="provider-a:event-attacker",
                settlement_ref="settlement-attacker",
                quote_outcomes={"quote-1": "win"},
                evidence_id="evidence-attacker",
                evidence_sha256="a" * 64,
                available_at=_AT,
            )

    coordinator.outcome_authority = MutatingAuthority()

    try:
        coordinator._settlement_resolutions(as_of=_AT)
    except continuous_session.ContinuousSessionError as exc:
        assert "mutated lifecycle settlement identity" in str(exc)
    else:
        raise AssertionError("callback lifecycle identity mutation was accepted")


def test_settlement_resolution_collection_rejects_callback_reference_mutation_on_none() -> None:
    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    coordinator = _resolution_coordinator((record,), (_resolution(),))

    class MutatingAuthority:
        def resolve(self, live_record: object, *, as_of: str) -> None:
            assert as_of == _AT
            live_record.settlement_ref = "settlement-attacker"
            return None

    coordinator.outcome_authority = MutatingAuthority()

    try:
        coordinator._settlement_resolutions(as_of=_AT)
    except continuous_session.ContinuousSessionError as exc:
        assert "mutated lifecycle settlement identity" in str(exc)
    else:
        raise AssertionError("callback mutation returning None was accepted")


def test_settlement_resolution_collection_rejects_callback_phase_mutation() -> None:
    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    coordinator = _resolution_coordinator((record,), (_resolution(),))

    class MutatingAuthority:
        def resolve(self, live_record: object, *, as_of: str) -> None:
            assert as_of == _AT
            live_record.phase = continuous_session.EventPhase.ACTIVE
            return None

    coordinator.outcome_authority = MutatingAuthority()

    try:
        coordinator._settlement_resolutions(as_of=_AT)
    except continuous_session.ContinuousSessionError as exc:
        assert "mutated lifecycle settlement identity" in str(exc)
    else:
        raise AssertionError("callback lifecycle phase mutation was accepted")


def test_settlement_resolution_detaches_authority_mapping_before_validation(
    monkeypatch,
) -> None:
    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    original = _resolution(outcome="win")
    coordinator = _resolution_coordinator((record,), (original,))
    canonical_text = continuous_session._text

    def text_then_mutate_authority(value: object, field: str) -> str:
        normalized = canonical_text(value, field)
        if field == "quote_outcomes quote_key":
            # SettlementResolution.validate() already captured this item's
            # outcome value for the current loop iteration.  Mutating the
            # authority-owned mapping here models the old validate-then-copy
            # TOCTOU window deterministically without replacing the validator.
            original.quote_outcomes["quote-1"] = "attacker-invalid"
        return normalized

    monkeypatch.setattr(continuous_session, "_text", text_then_mutate_authority)

    collected = coordinator._settlement_resolutions(as_of=_AT)

    assert original.quote_outcomes == {"quote-1": "attacker-invalid"}
    assert collected[0].quote_outcomes == {"quote-1": "win"}


def test_settlement_resolution_collection_rejects_runtime_validator_rebinding(
    monkeypatch,
) -> None:
    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    coordinator = _resolution_coordinator((record,), (_resolution(),))

    def attacker_validate(
        self: continuous_session.SettlementResolution,
        *,
        as_of: str,
    ) -> None:
        del self, as_of

    monkeypatch.setattr(
        continuous_session.SettlementResolution,
        "validate",
        attacker_validate,
    )

    try:
        coordinator._settlement_resolutions(as_of=_AT)
    except continuous_session.ContinuousSessionError as exc:
        assert "validator authority changed" in str(exc)
    else:
        raise AssertionError("runtime-rebound settlement validator was accepted")


def test_collected_settlement_resolution_is_detached_from_authority_mutation() -> None:
    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    original = _resolution(outcome="win")
    coordinator = _resolution_coordinator((record,), (original,))

    collected = coordinator._settlement_resolutions(as_of=_AT)
    original.quote_outcomes["quote-1"] = "loss"

    assert collected[0].quote_outcomes == {"quote-1": "win"}


def test_learning_resolution_detach_does_not_mutate_canonical_tuple() -> None:
    canonical = (_resolution(outcome="win"),)

    detached = continuous_session.ContinuousSessionCoordinator._detached_settlement_resolutions(
        canonical
    )
    detached[0].quote_outcomes["quote-1"] = "loss"

    assert canonical[0].quote_outcomes == {"quote-1": "win"}
    assert detached[0].quote_outcomes == {"quote-1": "loss"}


def test_each_learning_resolution_detach_has_independent_quote_mapping() -> None:
    canonical = (_resolution(outcome="win"),)

    first = continuous_session.ContinuousSessionCoordinator._detached_settlement_resolutions(
        canonical
    )
    second = continuous_session.ContinuousSessionCoordinator._detached_settlement_resolutions(
        canonical
    )
    first[0].quote_outcomes["quote-1"] = "loss"

    assert second[0].quote_outcomes == {"quote-1": "win"}
    assert canonical[0].quote_outcomes == {"quote-1": "win"}


def test_settlement_consumer_rejects_conflicting_duplicate_evidence_before_io() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    first = _resolution(outcome="win", digest_char="a")
    conflicting = _resolution(outcome="loss", digest_char="b")

    try:
        coordinator._settle(resolutions=(first, conflicting))
    except continuous_session.ContinuousSessionError as exc:
        assert "conflicting duplicate evidence_id" in str(exc)
    else:
        raise AssertionError("conflicting duplicate evidence reached settlement consumer")


def test_settlement_consumer_rejects_conflicting_outcomes_across_evidence_ids() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    first = _resolution(
        evidence_id="evidence-1",
        outcome="win",
        digest_char="a",
    )
    conflicting = _resolution(
        evidence_id="evidence-2",
        outcome="loss",
        digest_char="b",
    )

    try:
        coordinator._settle(resolutions=(first, conflicting))
    except continuous_session.ContinuousSessionError as exc:
        assert "conflicting settlement outcomes" in str(exc)
    else:
        raise AssertionError("conflicting settlement outcomes reached book I/O")


def test_settlement_consumer_rejects_invalid_resolution_before_workspace_io() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    invalid = continuous_session.SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="settlement-1",
        quote_outcomes={"quote-1": "attacker-invalid"},
        evidence_id="evidence-1",
        evidence_sha256="a" * 64,
        available_at=_AT,
    )

    try:
        coordinator._settle(resolutions=(invalid,))
    except continuous_session.ContinuousSessionError as exc:
        assert "invalid settlement resolution" in str(exc)
    else:
        raise AssertionError("invalid settlement resolution reached economic I/O")


def test_settlement_consumer_rejects_runtime_validator_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    def attacker_validate(
        self: continuous_session.SettlementResolution,
        *,
        as_of: str,
    ) -> None:
        del self, as_of

    monkeypatch.setattr(
        continuous_session.SettlementResolution,
        "validate",
        attacker_validate,
    )

    try:
        coordinator._settle(resolutions=(_resolution(),))
    except continuous_session.ContinuousSessionError as exc:
        assert "consumer validator authority changed" in str(exc)
    else:
        raise AssertionError("runtime-rebound consumer validator was accepted")


def test_settlement_consumer_rejects_noncanonical_outcome_mapping_before_io() -> None:
    class ExplodingDict(dict[str, str]):
        def copy(self):
            raise AssertionError("noncanonical consumer mapping copy executed")

        def items(self):
            raise AssertionError("noncanonical consumer mapping items executed")

        def __iter__(self):
            raise AssertionError("noncanonical consumer mapping iteration executed")

    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
    resolution = continuous_session.SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="settlement-1",
        quote_outcomes=ExplodingDict({"quote-1": "win"}),
        evidence_id="evidence-1",
        evidence_sha256="a" * 64,
        available_at=_AT,
    )

    try:
        coordinator._settle(resolutions=(resolution,))
    except continuous_session.ContinuousSessionError as exc:
        assert "consumer quote_outcomes must be an exact dict" in str(exc)
    else:
        raise AssertionError("noncanonical consumer outcome mapping was accepted")


def test_settlement_consumer_requires_exact_resolution_tuple() -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    try:
        coordinator._settle(resolutions=[_resolution()])  # type: ignore[arg-type]
    except TypeError as exc:
        assert "exact tuple" in str(exc)
    else:
        raise AssertionError("non-tuple settlement resolution batch was accepted")



def test_stale_instance_failure_cannot_override_newer_state_transition_generation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        stale = _state_with_history(root, _SMALL_HISTORY)
        current = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        current.set_state(
            continuous_session.SessionState.PAUSED,
            reason="OPERATOR_PAUSE",
        )
        assert current.snapshot().last_error_code == "OPERATOR_PAUSE"

        # This instance intentionally retains the pre-transition generation.
        # The transition cleanup now publishes a bounded same-generation tombstone,
        # so a stale process must be rejected before it can return a misleading
        # publication receipt for a generation that canonical state superseded.
        try:
            stale.record_failure(code="STALE_PROVIDER_FAILURE")
        except continuous_session.ContinuousSessionError as exc:
            assert "stale continuous session instance" in str(exc)
        else:
            raise AssertionError("stale instance returned a failure publication")

        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        snapshot = reopened.snapshot()
        assert snapshot.state is continuous_session.SessionState.PAUSED
        assert snapshot.last_error_code == "OPERATOR_PAUSE"

        canonical = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        sidecar = json.loads(
            (root / "continuous_session.json.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        assert sidecar["observed_generation"] == canonical["generation"]
        assert sidecar["observed_state"] == "PAUSED"
        assert sidecar["last_error_code"] is None
        assert canonical["state"] == "PAUSED"
        assert canonical["last_error_code"] == "OPERATOR_PAUSE"


def test_settlement_resolution_collection_rejects_non_string_identity_before_equality_dispatch() -> None:
    class ExplodingIdentity:
        def __eq__(self, _other: object) -> bool:
            raise AssertionError("authority-owned identity equality executed")

        def __ne__(self, _other: object) -> bool:
            raise AssertionError("authority-owned identity inequality executed")

    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    resolution = continuous_session.SettlementResolution(
        event_identity=ExplodingIdentity(),  # type: ignore[arg-type]
        settlement_ref="settlement-1",
        quote_outcomes={"quote-1": "win"},
        evidence_id="evidence-malformed-identity",
        evidence_sha256="a" * 64,
        available_at=_AT,
    )
    coordinator = _resolution_coordinator((record,), (resolution,))

    try:
        coordinator._settlement_resolutions(as_of=_AT)
    except continuous_session.ContinuousSessionError as exc:
        assert "malformed settlement resolution fields" in str(exc)
    else:
        raise AssertionError("non-string identity reached equality dispatch")


def test_settlement_resolution_collection_rejects_non_string_reference_before_equality_dispatch() -> None:
    class ExplodingReference:
        def __eq__(self, _other: object) -> bool:
            raise AssertionError("authority-owned reference equality executed")

        def __ne__(self, _other: object) -> bool:
            raise AssertionError("authority-owned reference inequality executed")

    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    resolution = continuous_session.SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref=ExplodingReference(),  # type: ignore[arg-type]
        quote_outcomes={"quote-1": "win"},
        evidence_id="evidence-malformed-reference",
        evidence_sha256="b" * 64,
        available_at=_AT,
    )
    coordinator = _resolution_coordinator((record,), (resolution,))

    try:
        coordinator._settlement_resolutions(as_of=_AT)
    except continuous_session.ContinuousSessionError as exc:
        assert "malformed settlement resolution fields" in str(exc)
    else:
        raise AssertionError("non-string settlement reference reached equality dispatch")


def test_settlement_resolution_collection_rejects_runtime_replace_rebinding(monkeypatch) -> None:
    record = _ResolutionRecord("provider-a:event-1", "settlement-1")
    coordinator = _resolution_coordinator((record,), (_resolution(),))

    def attacker_replace(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("runtime-rebound dataclass replace executed")

    monkeypatch.setattr(continuous_session, "replace", attacker_replace)

    try:
        coordinator._settlement_resolutions(as_of=_AT)
    except continuous_session.ContinuousSessionError as exc:
        assert "validator authority changed" in str(exc)
    else:
        raise AssertionError("runtime-rebound settlement copy authority was accepted")


def test_settlement_consumer_rejects_runtime_replace_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    def attacker_replace(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("runtime-rebound settlement consumer copy executed")

    monkeypatch.setattr(continuous_session, "replace", attacker_replace)

    try:
        coordinator._settle(resolutions=(_resolution(),))
    except continuous_session.ContinuousSessionError as exc:
        assert "copy authority changed" in str(exc)
    else:
        raise AssertionError("runtime-rebound settlement consumer copy was accepted")


def test_settlement_consumer_ignores_instance_shadowed_book_and_scope_helpers() -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
        coordinator.workspace = Path(directory)
        coordinator.paper_book_path = Path(directory) / "paper_book.json"
        coordinator.initial_bankroll = "10000"

        def attacker_load_book() -> object:
            raise AssertionError("instance-shadowed settlement book loader executed")

        def attacker_scope(*_args: object, **_kwargs: object) -> set[str]:
            raise AssertionError("instance-shadowed settlement quote scope executed")

        coordinator._load_book = attacker_load_book
        coordinator._open_quote_keys_for_book = attacker_scope

        settled, evidence_ids = coordinator._settle(resolutions=(_resolution(),))

        assert settled == ()
        assert evidence_ids == ("evidence-1",)


def test_settlement_consumer_helper_class_bindings_are_immutable() -> None:
    original_load = continuous_session.ContinuousSessionCoordinator._load_book
    original_scope = (
        continuous_session.ContinuousSessionCoordinator._open_quote_keys_for_book
    )

    for name, replacement in (
        ("_load_book", lambda _self: None),
        ("_open_quote_keys_for_book", lambda _book, _identity: set()),
    ):
        try:
            setattr(
                continuous_session.ContinuousSessionCoordinator,
                name,
                replacement,
            )
        except TypeError as exc:
            assert "consumer entry binding is immutable" in str(exc)
        else:
            raise AssertionError(f"{name} class binding was mutable")

    assert continuous_session.ContinuousSessionCoordinator._load_book is original_load
    assert (
        continuous_session.ContinuousSessionCoordinator._open_quote_keys_for_book
        is original_scope
    )


def test_settlement_consumer_subclass_cannot_override_helper_graph() -> None:
    try:
        class AttackerCoordinator(continuous_session.ContinuousSessionCoordinator):
            def _load_book(self) -> object:
                raise AssertionError("subclass book loader executed")

    except TypeError as exc:
        assert "consumer entry binding is immutable" in str(exc)
    else:
        raise AssertionError("settlement consumer helper override subclass was accepted")


def test_learning_resolution_detach_rejects_runtime_replace_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    def attacker_replace(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("runtime-rebound learning callback copy executed")

    monkeypatch.setattr(continuous_session, "replace", attacker_replace)

    try:
        coordinator._detached_settlement_resolutions((_resolution(),))
    except continuous_session.ContinuousSessionError as exc:
        assert "callback copy authority changed" in str(exc)
    else:
        raise AssertionError("runtime-rebound learning callback copy was accepted")


def test_settlement_consumer_rejects_runtime_paperbook_save_rebinding(monkeypatch) -> None:
    coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)

    def attacker_save(_self: object, _path: object) -> None:
        raise AssertionError("runtime-rebound PaperBook.save executed")

    monkeypatch.setattr(continuous_session.PaperBook, "save", attacker_save)

    try:
        coordinator._settle(resolutions=(_resolution(),))
    except continuous_session.ContinuousSessionError as exc:
        assert "persistence authority changed" in str(exc)
    else:
        raise AssertionError("runtime-rebound settlement persistence was accepted")


def test_settlement_book_loader_rejects_runtime_load_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
        coordinator.paper_book_path = Path(directory) / "paper_book.json"
        coordinator.initial_bankroll = "10000"

        def attacker_load(_cls: type, _path: object) -> object:
            raise AssertionError("runtime-rebound PaperBook.load executed")

        monkeypatch.setattr(
            continuous_session.PaperBook,
            "load",
            classmethod(attacker_load),
        )

        try:
            coordinator._load_book()
        except continuous_session.ContinuousSessionError as exc:
            assert "book loader authority changed" in str(exc)
        else:
            raise AssertionError("runtime-rebound PaperBook.load was accepted")


def test_settlement_book_loader_rejects_module_paperbook_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
        coordinator.paper_book_path = Path(directory) / "paper_book.json"
        coordinator.initial_bankroll = "10000"

        canonical = continuous_session.PaperBook

        class AttackerPaperBook(canonical):
            pass

        monkeypatch.setattr(continuous_session, "PaperBook", AttackerPaperBook)

        try:
            coordinator._load_book()
        except continuous_session.ContinuousSessionError as exc:
            assert "book loader authority changed" in str(exc)
        else:
            raise AssertionError("runtime-rebound PaperBook type was accepted")


def test_settlement_book_loader_rejects_runtime_path_exists_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        coordinator = object.__new__(continuous_session.ContinuousSessionCoordinator)
        coordinator.paper_book_path = Path(directory) / "paper_book.json"
        coordinator.initial_bankroll = "10000"

        def attacker_exists(_path: Path) -> bool:
            raise AssertionError("runtime-rebound Path.exists executed")

        monkeypatch.setattr(Path, "exists", attacker_exists)

        try:
            coordinator._load_book()
        except continuous_session.ContinuousSessionError as exc:
            assert "book loader authority changed" in str(exc)
        else:
            raise AssertionError("runtime-rebound settlement existence authority was accepted")

def test_session_id_access_does_not_read_full_settlement_history() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _LARGE_HISTORY)

        def forbidden_reader():
            raise AssertionError(
                "session_id access must not read or validate retained settlement history"
            )

        with patch.object(state, "_read", forbidden_reader):
            assert state.session_id == "session-history-scaling"




def test_sidecar_reader_rejects_runtime_text_validator_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="CANONICAL_FAILURE")

        def attacker_text(_value: object, _field: str) -> str:
            raise AssertionError("runtime-rebound sidecar text validator executed")

        monkeypatch.setattr(continuous_session, "_text", attacker_text)

        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "parser code identity changed" in str(exc)
        else:
            raise AssertionError("runtime-rebound sidecar text validator was accepted")


def test_sidecar_reader_rejects_runtime_instant_validator_rebinding(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_success(
            at="2026-09-22T06:21:00+00:00",
            full_refresh=False,
            settlement_evidence=(),
        )
        state.record_failure(code="CANONICAL_FAILURE")

        def attacker_instant(_value: object, _field: str) -> object:
            raise AssertionError("runtime-rebound sidecar instant validator executed")

        monkeypatch.setattr(continuous_session, "_instant", attacker_instant)

        try:
            state._read_error_checkpoint()
        except continuous_session.ContinuousSessionError as exc:
            assert "parser code identity changed" in str(exc)
        else:
            raise AssertionError("runtime-rebound sidecar instant validator was accepted")


def test_sidecar_writer_rejects_runtime_text_validator_rebinding_before_publication(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        error_path = root / "continuous_session.json.operational_error.json"

        def attacker_text(_value: object, _field: str) -> str:
            raise AssertionError("runtime-rebound sidecar writer validator executed")

        monkeypatch.setattr(continuous_session, "_text", attacker_text)

        try:
            state._write_error_checkpoint("CANONICAL_FAILURE")
        except continuous_session.ContinuousSessionError as exc:
            assert "writer code identity changed" in str(exc)
        else:
            raise AssertionError("runtime-rebound sidecar writer validator was accepted")

        assert not error_path.exists()

def test_post_commit_cleanup_failure_keeps_success_generation_authoritative() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="PRE_SUCCESS_FAILURE")

        original_write_error = state._write_error_checkpoint

        def crash_during_cleanup(code: str | None) -> None:
            if code is None:
                raise RuntimeError("simulated cleanup failure after success commit")
            original_write_error(code)

        before = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        with patch.object(state, "_write_error_checkpoint", crash_during_cleanup):
            try:
                state.record_success(
                    at="2026-09-22T06:21:00+00:00",
                    full_refresh=False,
                    settlement_evidence=(),
                )
            except RuntimeError as exc:
                assert "cleanup failure" in str(exc)
            else:
                raise AssertionError("post-commit cleanup failure was not simulated")

        committed = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert committed["generation"] == before["generation"] + 1
        assert committed["cycles_completed"] == before["cycles_completed"] + 1
        assert state._generation == committed["generation"]
        assert state._cycles_completed == committed["cycles_completed"]
        assert state._last_success_at == committed["last_success_at"]
        assert state._state == committed["state"]

        publication = state.record_failure(code="POST_SUCCESS_FAILURE")
        assert publication.generation == committed["generation"]
        assert publication.cycles_completed == committed["cycles_completed"]
        assert publication.last_success_at == committed["last_success_at"]
        assert state.snapshot().last_error_code == "POST_SUCCESS_FAILURE"


def test_success_generation_tombstone_fences_stale_failure_without_prior_sidecar() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        stale = _state_with_history(root, _SMALL_HISTORY)
        current = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        current.record_success(
            at="2026-09-22T06:21:00+00:00",
            full_refresh=False,
            settlement_evidence=(),
        )

        canonical = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        sidecar = json.loads(
            (root / "continuous_session.json.operational_error.json").read_text(
                encoding="utf-8"
            )
        )
        assert sidecar["observed_generation"] == canonical["generation"]
        assert sidecar["observed_cycles_completed"] == canonical["cycles_completed"]
        assert sidecar["observed_last_success_at"] == canonical["last_success_at"]
        assert sidecar["last_error_code"] is None

        try:
            stale.record_failure(code="STALE_PRE_SUCCESS_FAILURE")
        except continuous_session.ContinuousSessionError as exc:
            assert "stale continuous session instance" in str(exc)
        else:
            raise AssertionError("stale pre-success instance returned a failure receipt")


def test_projection_checkpoint_identity_fences_stale_failure_publisher() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        stale = _state_with_history(root, _SMALL_HISTORY)
        current = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )

        current.record_source_projection(
            deltas=(_projection_delta(1),),
            backlog=False,
        )

        # Projection is not a successful session generation, so it must not
        # manufacture a tombstone sidecar merely to fence a stale publisher.
        # The canonical checkpoint identity token is the bounded stale-writer
        # fence for projection-only checkpoint replacement.
        error_path = root / "continuous_session.json.operational_error.json"
        assert not error_path.exists()

        try:
            stale.record_failure(code="STALE_PRE_PROJECTION_FAILURE")
        except continuous_session.ContinuousSessionError as exc:
            assert "stale continuous session instance" in str(exc)
        else:
            raise AssertionError("stale pre-projection instance returned a failure receipt")



def test_bounded_running_guard_refreshes_external_pause_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        stale = _state_with_history(root, _LARGE_HISTORY)
        current = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        current.set_state(
            continuous_session.SessionState.PAUSED,
            reason="OPERATOR_PAUSE",
        )

        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = stale

        try:
            coordinator._require_running()
        except continuous_session.SessionPausedError:
            pass
        else:
            raise AssertionError(
                "bounded running guard ignored an external durable PAUSE"
            )

        canonical = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        assert stale._state == continuous_session.SessionState.PAUSED.value
        assert stale._generation == canonical["generation"]


def test_bounded_running_guard_skips_full_history_when_checkpoint_unchanged() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _LARGE_HISTORY)
        coordinator = object.__new__(
            continuous_session.ContinuousSessionCoordinator
        )
        coordinator._state = state

        def forbidden_reader():
            raise AssertionError(
                "unchanged running guard must not read retained settlement history"
            )

        with patch.object(state, "_read", forbidden_reader):
            coordinator._require_running()


def test_stale_failure_writer_rejects_main_commit_without_tombstone() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        stale = _state_with_history(root, _SMALL_HISTORY)
        current = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        error_path = root / "continuous_session.json.operational_error.json"

        def crash_before_tombstone(_code: str | None) -> None:
            raise RuntimeError("simulated crash before tombstone publication")

        with patch.object(current, "_write_error_checkpoint", crash_before_tombstone):
            try:
                current.record_success(
                    at="2026-10-06T05:17:00+00:00",
                    full_refresh=False,
                    settlement_evidence=(),
                )
            except RuntimeError as exc:
                assert "simulated crash before tombstone publication" in str(exc)
            else:
                raise AssertionError("post-commit tombstone crash was not simulated")

        assert current.snapshot().cycles_completed == _SMALL_HISTORY + 1
        assert not error_path.exists()

        try:
            stale.record_failure(code="STALE_POST_COMMIT_FAILURE")
        except continuous_session.ContinuousSessionError as exc:
            assert "canonical checkpoint changed" in str(exc)
        else:
            raise AssertionError(
                "stale failure writer published after a main-only generation commit"
            )

        assert not error_path.exists()
        reopened = continuous_session._ContinuousSessionState(
            root / "continuous_session.json",
            session_id="session-history-scaling",
            source_id="provider-a",
            clock=lambda: _AT,
        )
        snapshot = reopened.snapshot()
        assert snapshot.cycles_completed == _SMALL_HISTORY + 1
        assert snapshot.last_error_code is None

def test_record_success_rejects_paused_state_without_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.set_state(
            continuous_session.SessionState.PAUSED,
            reason="OPERATOR_PAUSE",
        )
        path = root / "continuous_session.json"
        error_path = root / "continuous_session.json.operational_error.json"
        before_main = path.read_bytes()
        before_error = error_path.read_bytes()

        try:
            state.record_success(
                at="2026-10-06T05:17:00+00:00",
                full_refresh=False,
                settlement_evidence=(),
            )
        except continuous_session.SessionPausedError:
            pass
        else:
            raise AssertionError("record_success advanced a PAUSED session")

        assert path.read_bytes() == before_main
        assert error_path.read_bytes() == before_error
        snapshot = state.snapshot()
        assert snapshot.state is continuous_session.SessionState.PAUSED
        assert snapshot.last_error_code == "OPERATOR_PAUSE"


def test_record_success_rejects_stopped_state_without_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.set_state(
            continuous_session.SessionState.STOPPED,
            reason="OPERATOR_STOP",
        )
        path = root / "continuous_session.json"
        error_path = root / "continuous_session.json.operational_error.json"
        before_main = path.read_bytes()
        before_error = error_path.read_bytes()

        try:
            state.record_success(
                at="2026-10-06T05:17:00+00:00",
                full_refresh=False,
                settlement_evidence=(),
            )
        except continuous_session.SessionStoppedError:
            pass
        else:
            raise AssertionError("record_success advanced a STOPPED session")

        assert path.read_bytes() == before_main
        assert error_path.read_bytes() == before_error
        snapshot = state.snapshot()
        assert snapshot.state is continuous_session.SessionState.STOPPED
        assert snapshot.last_error_code == "OPERATOR_STOP"


def test_record_source_projection_rejects_paused_state_without_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.set_state(
            continuous_session.SessionState.PAUSED,
            reason="OPERATOR_PAUSE",
        )
        path = root / "continuous_session.json"
        error_path = root / "continuous_session.json.operational_error.json"
        before_main = path.read_bytes()
        before_error = error_path.read_bytes()

        try:
            state.record_source_projection(
                deltas=(_projection_delta(1),),
                backlog=False,
            )
        except continuous_session.SessionPausedError:
            pass
        else:
            raise AssertionError("source projection advanced a PAUSED session")

        assert path.read_bytes() == before_main
        assert error_path.read_bytes() == before_error


def test_record_source_projection_rejects_stopped_state_without_mutation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.set_state(
            continuous_session.SessionState.STOPPED,
            reason="OPERATOR_STOP",
        )
        path = root / "continuous_session.json"
        error_path = root / "continuous_session.json.operational_error.json"
        before_main = path.read_bytes()
        before_error = error_path.read_bytes()

        try:
            state.record_source_projection(
                deltas=(_projection_delta(1),),
                backlog=False,
            )
        except continuous_session.SessionStoppedError:
            pass
        else:
            raise AssertionError("source projection advanced a STOPPED session")

        assert path.read_bytes() == before_main
        assert error_path.read_bytes() == before_error

