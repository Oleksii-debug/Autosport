from __future__ import annotations

from autosport.localization import catalog, require_keys
from autosport.incident_risk_register import (
    RegisterEntryKind,
    RiskEvidenceState,
    RiskSeverity,
    RiskStatus,
)


_STATE_KEYS = {
    "ui.risk_register.state.unavailable",
    "ui.risk_register.state.empty",
    "ui.risk_register.state.available",
}
_NEXT_ACTION_KEYS = {
    "ui.risk_register.next_action.none",
    "ui.risk_register.next_action.verify_mitigation",
    "ui.risk_register.next_action.review",
    "ui.risk_register.next_action.monitor",
}
_UKRAINIAN = frozenset(
    "АБВГҐДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯ"
    "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
)


def _incident_register_keys() -> set[str]:
    return (
        _STATE_KEYS
        | _NEXT_ACTION_KEYS
        | {f"ui.risk_register.kind.{item.value}" for item in RegisterEntryKind}
        | {f"ui.risk_register.severity.{item.token}" for item in RiskSeverity}
        | {f"ui.risk_register.status.{item.value}" for item in RiskStatus}
        | {f"ui.risk_register.evidence.{item.value}" for item in RiskEvidenceState}
    )


def test_incident_register_emitted_keys_are_in_canonical_ukrainian_catalog() -> None:
    keys = _incident_register_keys()
    require_keys(keys)
    messages = catalog()

    assert len(keys) == 21
    for key in sorted(keys):
        value = messages[key]
        assert value
        assert value != key
        assert any(character in _UKRAINIAN for character in value)


def test_incident_register_critical_meanings_remain_textual() -> None:
    messages = catalog()

    assert messages["ui.risk_register.state.unavailable"] == (
        "Докази журналу інцидентів недоступні."
    )
    assert messages["ui.risk_register.severity.critical"] == "Критична важливість"
    assert messages["ui.risk_register.status.acknowledged"] == (
        "Підтверджено ознайомлення"
    )
    assert messages["ui.risk_register.next_action.review"] == (
        "Переглянути інцидент і пов’язані докази"
    )
