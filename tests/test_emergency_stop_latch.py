from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

import autosport.emergency_stop_latch as stop_module
from autosport.emergency_stop_latch import (
    EmergencyActionClass,
    EmergencyStopClearAuthorization,
    EmergencyStopClearAuthority,
    EmergencyStopIntegrityError,
    EmergencyStopLatch,
    EmergencyStopState,
    EmergencyStopTransitionError,
    _decode_snapshot,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = (tmp_path / "workspace").resolve()
    authority = (tmp_path / "machine-authority").resolve()
    workspace.mkdir()
    return workspace, authority


class _ClearAuthority:
    def __init__(self, evidence_seed: str = "resolved-clear-authority") -> None:
        self.evidence_seed = evidence_seed
        self.calls: list[dict[str, str]] = []

    def resolve_emergency_stop_clear(
        self,
        *,
        workspace_instance_id: str,
        stop_event_sha256: str,
        clear_event_id: str,
        reason: str,
        clear_recorded_at: str,
    ) -> EmergencyStopClearAuthorization:
        self.calls.append(
            {
                "workspace_instance_id": workspace_instance_id,
                "stop_event_sha256": stop_event_sha256,
                "clear_event_id": clear_event_id,
                "reason": reason,
                "clear_recorded_at": clear_recorded_at,
            }
        )
        return EmergencyStopClearAuthorization(
            workspace_instance_id=workspace_instance_id,
            stop_event_sha256=stop_event_sha256,
            clear_event_id=clear_event_id,
            clear_recorded_at=clear_recorded_at,
            authority_id="operator-confirmation:clear",
            evidence_sha256=_sha(self.evidence_seed),
        )


def _latch(
    tmp_path: Path,
    times: list[str] | None = None,
    *,
    clear_authority: EmergencyStopClearAuthority | None = None,
) -> EmergencyStopLatch:
    workspace, authority = _workspace(tmp_path)
    sequence = iter(
        times
        or [
            "2026-09-22T03:00:00.000001+00:00",
            "2026-09-22T03:00:00.000002+00:00",
            "2026-09-22T03:00:00.000003+00:00",
            "2026-09-22T03:00:00.000004+00:00",
            "2026-09-22T03:00:00.000005+00:00",
        ]
    )
    return EmergencyStopLatch(
        workspace=workspace,
        authority_root=authority,
        clock=lambda: next(sequence),
        clear_authority=clear_authority,
    )


def test_pristine_state_fails_closed_for_money_but_allows_readonly(
    tmp_path: Path,
) -> None:
    latch = _latch(tmp_path)
    blocked = latch.decision(EmergencyActionClass.NEW_EXPOSURE)
    assert blocked.stop_state is EmergencyStopState.UNKNOWN
    assert blocked.stop_veto is True
    assert blocked.may_proceed_past_stop_gate is False
    assert blocked.grants_execution_authority is False

    readonly = latch.decision(EmergencyActionClass.READ_ONLY_RECONCILIATION)
    assert readonly.stop_state is EmergencyStopState.UNKNOWN
    assert readonly.stop_veto is False
    assert readonly.may_proceed_past_stop_gate is True
    assert readonly.requires_separate_authority is False
    assert readonly.grants_execution_authority is False


def test_engaged_stop_vetoes_new_and_increased_exposure(tmp_path: Path) -> None:
    latch = _latch(tmp_path)
    snapshot = latch.engage(
        event_id="stop-1",
        reason="operator emergency stop",
        authority_id="operator-confirmation:1",
    )
    assert snapshot.state is EmergencyStopState.ENGAGED

    for action in (
        EmergencyActionClass.NEW_EXPOSURE,
        EmergencyActionClass.INCREASE_EXPOSURE,
    ):
        decision = latch.decision(action)
        assert decision.stop_state is EmergencyStopState.ENGAGED
        assert decision.stop_veto is True
        assert decision.may_proceed_past_stop_gate is False
        assert decision.grants_execution_authority is False


def test_engaged_stop_does_not_mint_cancel_or_hedge_authority(tmp_path: Path) -> None:
    latch = _latch(tmp_path)
    latch.engage(
        event_id="stop-1",
        reason="operator emergency stop",
        authority_id="operator-confirmation:1",
    )
    for action in (
        EmergencyActionClass.CANCEL_EXISTING,
        EmergencyActionClass.RISK_REDUCING_HEDGE,
    ):
        decision = latch.decision(action)
        assert decision.stop_state is EmergencyStopState.ENGAGED
        assert decision.stop_veto is False
        assert decision.may_proceed_past_stop_gate is True
        assert decision.requires_separate_authority is True
        assert decision.grants_execution_authority is False


def test_clear_without_configured_operator_authority_fails_closed(
    tmp_path: Path,
) -> None:
    latch = _latch(tmp_path)
    latch.engage(
        event_id="stop-1",
        reason="operator emergency stop",
        authority_id="operator-confirmation:1",
    )
    with pytest.raises(
        EmergencyStopTransitionError, match="configured separate operator authority"
    ):
        latch.clear(event_id="clear-1", reason="resume requested")
    assert latch.inspect() is not None
    assert latch.inspect().state is EmergencyStopState.ENGAGED


def test_clear_uses_resolver_bound_to_current_stop_and_only_removes_veto(
    tmp_path: Path,
) -> None:
    resolver = _ClearAuthority()
    latch = _latch(tmp_path, clear_authority=resolver)
    engaged = latch.engage(
        event_id="stop-1",
        reason="operator emergency stop",
        authority_id="operator-confirmation:1",
    )
    cleared = latch.clear(event_id="clear-1", reason="resume requested")
    assert cleared.state is EmergencyStopState.CLEARED
    assert resolver.calls == [
        {
            "workspace_instance_id": latch.workspace_instance_id,
            "stop_event_sha256": engaged.events[-1].event_sha256,
            "clear_event_id": "clear-1",
            "reason": "resume requested",
            "clear_recorded_at": "2026-09-22T03:00:00.000002+00:00",
        }
    ]
    decision = latch.decision(EmergencyActionClass.NEW_EXPOSURE)
    assert decision.stop_state is EmergencyStopState.CLEARED
    assert decision.stop_veto is False
    assert decision.may_proceed_past_stop_gate is True
    assert decision.requires_separate_authority is True
    assert decision.grants_execution_authority is False


def test_clear_rejects_stale_or_forged_resolver_binding(tmp_path: Path) -> None:
    class StaleAuthority(_ClearAuthority):
        def resolve_emergency_stop_clear(
            self,
            *,
            workspace_instance_id: str,
            stop_event_sha256: str,
            clear_event_id: str,
            reason: str,
            clear_recorded_at: str,
        ) -> EmergencyStopClearAuthorization:
            proof = super().resolve_emergency_stop_clear(
                workspace_instance_id=workspace_instance_id,
                stop_event_sha256=stop_event_sha256,
                clear_event_id=clear_event_id,
                reason=reason,
                clear_recorded_at=clear_recorded_at,
            )
            return EmergencyStopClearAuthorization(
                workspace_instance_id=proof.workspace_instance_id,
                stop_event_sha256=_sha("different-stop"),
                clear_event_id=proof.clear_event_id,
                clear_recorded_at=proof.clear_recorded_at,
                authority_id=proof.authority_id,
                evidence_sha256=proof.evidence_sha256,
            )

    latch = _latch(tmp_path, clear_authority=StaleAuthority())
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    with pytest.raises(EmergencyStopTransitionError, match="stale"):
        latch.clear(event_id="clear-1", reason="clear")
    assert latch.inspect().state is EmergencyStopState.ENGAGED


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("workspace", "workspace identity"),
        ("event", "event identity"),
        ("timestamp", "timestamp"),
        ("authority_id", "malformed CLEAR evidence"),
        ("evidence", "malformed CLEAR evidence"),
    ],
)
def test_clear_rejects_malformed_or_misbound_authority_fields(
    tmp_path: Path, mutation: str, match: str
) -> None:
    class MalformedAuthority(_ClearAuthority):
        def resolve_emergency_stop_clear(
            self,
            *,
            workspace_instance_id: str,
            stop_event_sha256: str,
            clear_event_id: str,
            reason: str,
            clear_recorded_at: str,
        ) -> EmergencyStopClearAuthorization:
            proof = super().resolve_emergency_stop_clear(
                workspace_instance_id=workspace_instance_id,
                stop_event_sha256=stop_event_sha256,
                clear_event_id=clear_event_id,
                reason=reason,
                clear_recorded_at=clear_recorded_at,
            )
            if mutation == "workspace":
                return replace(proof, workspace_instance_id="different-workspace")
            if mutation == "event":
                return replace(proof, clear_event_id="different-clear-event")
            if mutation == "timestamp":
                return replace(
                    proof, clear_recorded_at="2026-09-22T03:59:59.999999+00:00"
                )
            if mutation == "authority_id":
                return replace(proof, authority_id=" ")
            return replace(proof, evidence_sha256="not-a-sha256")

    latch = _latch(tmp_path, clear_authority=MalformedAuthority())
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")

    with pytest.raises(EmergencyStopTransitionError, match=match):
        latch.clear(event_id="clear-1", reason="resume")
    assert latch.inspect().state is EmergencyStopState.ENGAGED


def test_clear_rechecks_current_stop_after_authority_resolution(tmp_path: Path) -> None:
    workspace, authority_root = _workspace(tmp_path)
    racer_holder: dict[str, EmergencyStopLatch] = {}

    class RacingAuthority(_ClearAuthority):
        def resolve_emergency_stop_clear(
            self,
            *,
            workspace_instance_id: str,
            stop_event_sha256: str,
            clear_event_id: str,
            reason: str,
            clear_recorded_at: str,
        ) -> EmergencyStopClearAuthorization:
            proof = super().resolve_emergency_stop_clear(
                workspace_instance_id=workspace_instance_id,
                stop_event_sha256=stop_event_sha256,
                clear_event_id=clear_event_id,
                reason=reason,
                clear_recorded_at=clear_recorded_at,
            )
            racer_holder["latch"].engage(
                event_id="stop-raced",
                reason="new emergency arrived while CLEAR was resolving",
                authority_id="system:safety",
                recorded_at="2026-09-22T03:00:00.000003+00:00",
            )
            return proof

    resolver = RacingAuthority()
    latch = EmergencyStopLatch(
        workspace=workspace,
        authority_root=authority_root,
        clock=iter(
            [
                "2026-09-22T03:00:00.000001+00:00",
                "2026-09-22T03:00:00.000002+00:00",
            ]
        ).__next__,
        clear_authority=resolver,
    )
    racer_holder["latch"] = latch
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")

    with pytest.raises(EmergencyStopTransitionError, match="stale"):
        latch.clear(event_id="clear-1", reason="resume")

    current = latch.inspect()
    assert current is not None
    assert current.state is EmergencyStopState.ENGAGED
    assert current.latest_event_id == "stop-raced"


def test_clear_cannot_bootstrap_pristine_or_clear_twice(tmp_path: Path) -> None:
    resolver = _ClearAuthority()
    latch = _latch(tmp_path, clear_authority=resolver)
    with pytest.raises(EmergencyStopTransitionError, match="currently engaged"):
        latch.clear(event_id="clear-0", reason="invalid bootstrap clear")
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    latch.clear(event_id="clear-1", reason="clear")
    with pytest.raises(EmergencyStopTransitionError, match="currently engaged"):
        latch.clear(event_id="clear-2", reason="second clear")


def test_engaged_stop_survives_restart(tmp_path: Path) -> None:
    workspace, authority = _workspace(tmp_path)
    first = EmergencyStopLatch(
        workspace=workspace,
        authority_root=authority,
        clock=lambda: "2026-09-22T03:00:00.000001+00:00",
    )
    first.engage(event_id="stop-1", reason="stop", authority_id="operator:1")

    reopened = EmergencyStopLatch(workspace=workspace, authority_root=authority)
    assert reopened.inspect() is not None
    assert reopened.inspect().state is EmergencyStopState.ENGAGED
    assert reopened.decision(EmergencyActionClass.NEW_EXPOSURE).stop_veto is True


def test_restored_older_snapshot_is_rejected_by_independent_authority(
    tmp_path: Path,
) -> None:
    latch = _latch(tmp_path, clear_authority=_ClearAuthority())
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    old_bytes = latch.state_path.read_bytes()
    latch.clear(event_id="clear-1", reason="clear")
    latch.state_path.write_bytes(old_bytes)

    decision = latch.decision(EmergencyActionClass.NEW_EXPOSURE)
    assert decision.stop_state is EmergencyStopState.UNKNOWN
    assert decision.stop_veto is True
    with pytest.raises(EmergencyStopIntegrityError):
        latch.inspect()


def test_corrupt_state_fails_closed_without_blocking_readonly_reconciliation(
    tmp_path: Path,
) -> None:
    latch = _latch(tmp_path)
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    latch.state_path.write_text("{broken", encoding="utf-8")

    money = latch.decision(EmergencyActionClass.NEW_EXPOSURE)
    assert money.stop_state is EmergencyStopState.UNKNOWN
    assert money.stop_veto is True
    assert money.may_proceed_past_stop_gate is False

    readonly = latch.decision(EmergencyActionClass.READ_ONLY_RECONCILIATION)
    assert readonly.stop_state is EmergencyStopState.UNKNOWN
    assert readonly.may_proceed_past_stop_gate is True


def test_symlinked_state_path_fails_closed(tmp_path: Path) -> None:
    latch = _latch(tmp_path)
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    external = tmp_path / "external-stop-state.json"
    latch.state_path.replace(external)
    try:
        latch.state_path.symlink_to(external)
    except OSError:
        pytest.skip("symlink creation is unavailable on this runner")

    decision = latch.decision(EmergencyActionClass.NEW_EXPOSURE)
    assert decision.stop_state is EmergencyStopState.UNKNOWN
    assert decision.stop_veto is True
    with pytest.raises(EmergencyStopIntegrityError, match="non-symlink"):
        latch.inspect()


def test_hardlinked_state_path_fails_closed(tmp_path: Path) -> None:
    latch = _latch(tmp_path)
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    alias = tmp_path / "stop-state-hardlink.json"
    try:
        alias.hardlink_to(latch.state_path)
    except OSError:
        pytest.skip("hard-link creation is unavailable on this runner")

    decision = latch.decision(EmergencyActionClass.NEW_EXPOSURE)
    assert decision.stop_state is EmergencyStopState.UNKNOWN
    assert decision.stop_veto is True
    with pytest.raises(EmergencyStopIntegrityError, match="hard-link"):
        latch.inspect()


def test_stale_temp_name_does_not_permanently_block_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latch = _latch(tmp_path)
    parent = latch.state_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    raw = b"durability-probe\n"
    digest = hashlib.sha256(raw).hexdigest()

    class Token:
        def __init__(self, value: str) -> None:
            self.hex = value

    tokens = iter((Token("a" * 32), Token("b" * 32)))
    monkeypatch.setattr(stop_module.uuid, "uuid4", lambda: next(tokens))
    stale = parent / (
        f".emergency_stop_v1.json.{digest[:24]}.{'a' * 32}.tmp"
    )
    stale.write_bytes(b"stale-crash-prefix")

    with pytest.raises(FileExistsError):
        latch._publish_state_bytes(raw, digest)
    latch._publish_state_bytes(raw, digest)
    assert latch.state_path.read_bytes() == raw
    assert stale.read_bytes() == b"stale-crash-prefix"


def test_duplicate_event_id_is_rejected(tmp_path: Path) -> None:
    latch = _latch(tmp_path)
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    with pytest.raises(EmergencyStopTransitionError, match="duplicate"):
        latch.engage(event_id="stop-1", reason="repeat", authority_id="operator:1")


def test_timestamp_must_advance(tmp_path: Path) -> None:
    latch = _latch(tmp_path)
    latch.engage(
        event_id="stop-1",
        reason="stop",
        authority_id="operator:1",
        recorded_at="2026-09-22T03:00:00.000010+00:00",
    )
    with pytest.raises(EmergencyStopTransitionError, match="advance monotonically"):
        latch.engage(
            event_id="stop-2",
            reason="repeat",
            authority_id="operator:1",
            recorded_at="2026-09-22T03:00:00.000009+00:00",
        )


def test_noncanonical_timestamp_is_rejected(tmp_path: Path) -> None:
    latch = _latch(tmp_path)
    with pytest.raises(EmergencyStopTransitionError, match="canonical UTC"):
        latch.engage(
            event_id="stop-1",
            reason="stop",
            authority_id="operator:1",
            recorded_at="2026-09-22T05:00:00.000001+02:00",
        )


def test_hash_chain_tamper_is_rejected_by_decoder(tmp_path: Path) -> None:
    latch = _latch(tmp_path)
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    raw = json.loads(latch.state_path.read_text(encoding="utf-8"))
    raw["events"][0]["event_sha256"] = _sha("forged")
    encoded = (
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    with pytest.raises(EmergencyStopIntegrityError, match="event hash"):
        _decode_snapshot(
            encoded.encode("utf-8"),
            expected_workspace_instance_id=latch.workspace_instance_id,
        )


def test_sequence_gap_is_rejected_by_decoder(tmp_path: Path) -> None:
    latch = _latch(tmp_path)
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    latch.engage(event_id="stop-2", reason="stop again", authority_id="operator:1")
    raw = json.loads(latch.state_path.read_text(encoding="utf-8"))
    raw["events"][1]["sequence"] = 3
    with pytest.raises(EmergencyStopIntegrityError):
        _decode_snapshot(
            (
                json.dumps(
                    raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                + "\n"
            ).encode("utf-8"),
            expected_workspace_instance_id=latch.workspace_instance_id,
        )


def test_workspace_identity_drift_is_rejected_by_decoder(tmp_path: Path) -> None:
    latch = _latch(tmp_path)
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    raw = json.loads(latch.state_path.read_text(encoding="utf-8"))
    raw["workspace_instance_id"] = "different-workspace"
    with pytest.raises(EmergencyStopIntegrityError, match="workspace identity"):
        _decode_snapshot(
            (
                json.dumps(
                    raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                + "\n"
            ).encode("utf-8"),
            expected_workspace_instance_id=latch.workspace_instance_id,
        )


def test_float_schema_version_alias_is_rejected(tmp_path: Path) -> None:
    latch = _latch(tmp_path)
    latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    raw = json.loads(latch.state_path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1.0
    with pytest.raises(EmergencyStopIntegrityError, match="schema version"):
        _decode_snapshot(
            (
                json.dumps(
                    raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                + "\n"
            ).encode("utf-8"),
            expected_workspace_instance_id=latch.workspace_instance_id,
        )


def test_crash_after_local_publish_recovers_exact_prepared_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latch = _latch(tmp_path)
    original_commit = latch.authority.commit

    def crash_after_publish(**_kwargs: object) -> object:
        raise OSError("simulated crash after local publish")

    monkeypatch.setattr(latch.authority, "commit", crash_after_publish)
    with pytest.raises(EmergencyStopIntegrityError):
        latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    assert latch.state_path.is_file()

    monkeypatch.setattr(latch.authority, "commit", original_commit)
    reopened = EmergencyStopLatch(
        workspace=latch.workspace,
        authority_root=latch.authority.authority_root,
    )
    restored = reopened.inspect()
    assert restored is not None
    assert restored.state is EmergencyStopState.ENGAGED
    assert reopened.decision(EmergencyActionClass.NEW_EXPOSURE).stop_veto is True


def test_crash_after_prepare_before_publish_is_aborted_on_next_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latch = _latch(tmp_path)

    def crash_before_publish(_raw: bytes, _digest: str) -> None:
        raise OSError("simulated crash before local publish")

    monkeypatch.setattr(latch, "_publish_state_bytes", crash_before_publish)
    with pytest.raises(EmergencyStopIntegrityError):
        latch.engage(event_id="stop-1", reason="stop", authority_id="operator:1")
    assert not latch.state_path.exists()

    reopened = EmergencyStopLatch(
        workspace=latch.workspace,
        authority_root=latch.authority.authority_root,
    )
    assert reopened.inspect() is None
    assert reopened.decision(EmergencyActionClass.NEW_EXPOSURE).stop_veto is True


def test_ukrainian_reason_round_trips_canonically(tmp_path: Path) -> None:
    latch = _latch(tmp_path)
    snapshot = latch.engage(
        event_id="stop-ua",
        reason="Аварійна зупинка оператором",
        authority_id="operator:ua",
    )
    assert snapshot.events[-1].reason == "Аварійна зупинка оператором"
    assert latch.inspect() == snapshot
