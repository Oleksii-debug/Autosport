from __future__ import annotations

import json
import os
import tempfile
import threading
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


def test_state_round_trip_does_not_resurrect_superseded_failure() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="STALE_PROVIDER_FAILURE")
        assert state.snapshot().last_error_code == "STALE_PROVIDER_FAILURE"

        state.set_state(continuous_session.SessionState.PAUSED)
        assert state.snapshot().last_error_code is None

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
        assert paused.snapshot().last_error_code is None
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


def test_non_superseding_source_projection_preserves_failure_generation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="PROVIDER_FAILURE")

        state.record_source_projection(deltas=(), backlog=True)

        snapshot = state.snapshot()
        assert snapshot.last_error_code == "PROVIDER_FAILURE"
        assert snapshot.source_state_projection_backlog is True

        canonical = json.loads(
            (root / "continuous_session.json").read_text(encoding="utf-8")
        )
        sidecar = json.loads(
            (
                root / "continuous_session.json.operational_error.json"
            ).read_text(encoding="utf-8")
        )
        assert canonical["generation"] == 0
        assert sidecar["observed_generation"] == 0


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
            state.record_source_projection(deltas=(), backlog=True)
        except continuous_session.ContinuousSessionError as exc:
            assert "read-modify-write authority" in str(exc)
        else:
            raise AssertionError("runtime-rebound durable path lock was accepted")


def test_state_transition_cleanup_cannot_erase_newer_failure() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="OLDER_FAILURE")

        original_write = state._write_error_checkpoint
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        failure_started = threading.Event()
        failure_completed = threading.Event()

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
            state.record_failure(code="NEWER_FAILURE")
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
        assert snapshot.last_error_code == "NEWER_FAILURE"


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

        stale.record_failure(code="POST_SUCCESS_FAILURE")

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
        assert payload["observed_generation"] < canonical["generation"]
        assert payload["observed_cycles_completed"] < canonical["cycles_completed"]
        assert payload["observed_last_success_at"] is None
        assert canonical["last_success_at"] == "2026-10-06T04:53:00+00:00"


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
        state.set_state(continuous_session.SessionState.PAUSED)
        state.record_failure(code="PAUSED_FAILURE")

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
        event_dedupe_key=f"event-dedupe-{index}",
        event_id=f"event-{index}",
        source_payload_digest=f"{index + 1:064x}"[-64:],
        canonical_event_digest=f"{index + 101:064x}"[-64:],
        source_observed_at=_AT,
        collector_received_at=_AT,
        collector_committed_at=_AT,
        desktop_available_at=_AT,
        gap_state=continuous_session.GapState.NONE,
        sync_state=continuous_session.SyncState.READY,
    )


def test_source_projection_change_advances_generation() -> None:
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
        assert after["generation"] == before.get("generation", 0) + 1
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


def test_projection_change_invalidates_predecessor_failure_overlay() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state = _state_with_history(root, _SMALL_HISTORY)
        state.record_failure(code="PRE_PROJECTION_FAILURE")
        assert state.snapshot().last_error_code == "PRE_PROJECTION_FAILURE"

        state.record_source_projection(
            deltas=(_projection_delta(1),),
            backlog=False,
        )

        assert state.snapshot().last_error_code is None


def test_failure_after_projection_binds_to_new_generation() -> None:
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
            assert "positions must increase" in str(exc)
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
            assert "positions must increase" in str(exc)
        else:
            raise AssertionError("duplicate projection positions were accepted")


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
