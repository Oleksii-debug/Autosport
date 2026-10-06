from __future__ import annotations

import json
import os
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
                assert "replaced" in str(exc) or "changed" in str(exc)
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
        assert payload["schema_version"] == 2

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
        assert fresh_payload["schema_version"] == 2
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
        assert payload["schema_version"] == 2
        assert payload["last_error_code"] == "CANONICAL_FAILURE"


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
        assert snapshot.cycles_completed == 1
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

        assert state.snapshot().cycles_completed == 0


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
