from __future__ import annotations

from pathlib import Path

import pytest

from autosport import _paper_execution_decision_origin as origin_module
from autosport import _paper_execution_decision_origin_instance_guard as instance_guard
from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger


DECISION_ID = "decision-origin-transitive-verifier"
RECORDED_AT = "2026-09-21T01:35:00+00:00"


def _record() -> DecisionRecord:
    return DecisionRecord(
        replay_run_id="origin-transitive-verifier-run",
        agent="origin-transitive-verifier-agent",
        observed_ts=RECORDED_AT,
        action="PAPER_TEST",
        payload={"value": "sealed-verifier"},
        context_hash="origin-transitive-verifier-context",
        decision_id=DECISION_ID,
        recorded_at=RECORDED_AT,
    )


def _ledger(tmp_path: Path) -> tuple[JsonlDecisionLedger, str]:
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    digest = ledger.append(_record())
    return ledger, digest


def test_origin_rejects_instance_shadowed_transitive_byte_verifier(
    tmp_path: Path,
) -> None:
    ledger, _ = _ledger(tmp_path)
    attacker_calls: list[str] = []

    def forged(raw: bytes) -> int:
        attacker_calls.append(raw.decode("utf-8"))
        return 1

    ledger._verify_bytes = forged  # type: ignore[method-assign]

    with pytest.raises(
        origin_module.PaperExecutionDecisionOriginError,
        match="shadows authority method _verify_bytes",
    ):
        origin_module.verified_decision_origin(ledger, DECISION_ID)

    assert attacker_calls == []


def test_origin_fails_closed_on_class_rebind_before_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, _ = _ledger(tmp_path)
    attacker_calls: list[bytes] = []

    def forged(cls, raw: bytes) -> int:
        attacker_calls.append(raw)
        return 1

    monkeypatch.setattr(JsonlDecisionLedger, "_verify_bytes", classmethod(forged))

    with pytest.raises(
        origin_module.PaperExecutionDecisionOriginError,
        match="executable authority seal changed",
    ):
        origin_module.verified_decision_origin(ledger, DECISION_ID)

    assert attacker_calls == []


def test_origin_fails_closed_when_verifier_rebind_races_filesystem_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, digest = _ledger(tmp_path)
    attacker_calls: list[bytes] = []
    original_descriptor = JsonlDecisionLedger.__dict__["_verify_bytes"]
    original_read_bytes = Path.read_bytes

    def forged(cls, raw: bytes) -> int:
        attacker_calls.append(raw)
        return 1

    def racing_read_bytes(path: Path) -> bytes:
        payload = original_read_bytes(path)
        setattr(JsonlDecisionLedger, "_verify_bytes", classmethod(forged))
        return payload

    monkeypatch.setattr(Path, "read_bytes", racing_read_bytes)
    try:
        with pytest.raises(
            origin_module.PaperExecutionDecisionOriginError,
            match="executable authority seal changed",
        ):
            origin_module.verified_decision_origin(ledger, DECISION_ID)
    finally:
        setattr(JsonlDecisionLedger, "_verify_bytes", original_descriptor)

    assert attacker_calls == []
    monkeypatch.setattr(Path, "read_bytes", original_read_bytes)
    origin = origin_module.verified_decision_origin(ledger, DECISION_ID)
    assert origin.record_sha256 == digest
    assert instance_guard._STABLE_VERIFY_BYTES_DESCRIPTOR is original_descriptor


@pytest.mark.parametrize(
    ("method_name", "descriptor_kind"),
    [
        ("_json_object_without_duplicate_keys", "static"),
        ("_reject_non_finite_json", "static"),
        ("_validate_record", "class"),
        ("_canonical_record", "static"),
        ("_validate_json_value", "class"),
        ("_require_utf8_text", "static"),
    ],
)
def test_origin_fails_closed_before_transitive_helper_rebind_executes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    descriptor_kind: str,
) -> None:
    ledger, _ = _ledger(tmp_path)
    attacker_calls: list[tuple[object, ...]] = []

    def forged(*args: object, **kwargs: object) -> object:
        attacker_calls.append((*args, kwargs))
        if method_name == "_canonical_record":
            return "{}"
        if method_name == "_validate_record":
            return args[-1] if args else {}
        if method_name == "_json_object_without_duplicate_keys":
            return {}
        return None

    descriptor = classmethod(forged) if descriptor_kind == "class" else staticmethod(forged)
    monkeypatch.setattr(JsonlDecisionLedger, method_name, descriptor)

    with pytest.raises(
        origin_module.PaperExecutionDecisionOriginError,
        match="executable authority seal changed",
    ):
        origin_module.verified_decision_origin(ledger, DECISION_ID)

    assert attacker_calls == []


def test_origin_fails_closed_when_transitive_helper_rebind_races_filesystem_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, digest = _ledger(tmp_path)
    attacker_calls: list[tuple[object, ...]] = []
    original_descriptor = JsonlDecisionLedger.__dict__["_validate_record"]
    original_read_bytes = Path.read_bytes

    def forged(cls: type[JsonlDecisionLedger], *args: object, **kwargs: object) -> object:
        attacker_calls.append((cls, *args, kwargs))
        return args[0] if args else {}

    def racing_read_bytes(path: Path) -> bytes:
        payload = original_read_bytes(path)
        setattr(JsonlDecisionLedger, "_validate_record", classmethod(forged))
        return payload

    monkeypatch.setattr(Path, "read_bytes", racing_read_bytes)
    try:
        with pytest.raises(
            origin_module.PaperExecutionDecisionOriginError,
            match="executable authority seal changed",
        ):
            origin_module.verified_decision_origin(ledger, DECISION_ID)
    finally:
        setattr(JsonlDecisionLedger, "_validate_record", original_descriptor)

    assert attacker_calls == []
    monkeypatch.setattr(Path, "read_bytes", original_read_bytes)
    origin = origin_module.verified_decision_origin(ledger, DECISION_ID)
    assert origin.record_sha256 == digest
