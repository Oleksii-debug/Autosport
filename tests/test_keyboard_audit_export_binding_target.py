from __future__ import annotations

import autosport.keyboard_audit as keyboard_audit


class _BindingProbeApp:
    def __init__(self, *, action: str = "export", fail_update: bool = False) -> None:
        self.action = action
        self.fail_update = fail_update
        self.original_export_calls = 0
        self.wrong_action_calls = 0

    def export_evidence(self) -> None:
        self.original_export_calls += 1

    def wrong_action(self) -> None:
        self.wrong_action_calls += 1

    def bind(self, _sequence: str) -> str:
        return "bound-script"

    def event_generate(self, sequence: str) -> None:
        if sequence != "<Control-e>":
            return
        if self.action == "export":
            self.export_evidence()
        elif self.action == "wrong":
            self.wrong_action()
        elif self.action == "raise":
            raise RuntimeError("event dispatch failed")

    def update(self) -> None:
        if self.fail_update:
            raise RuntimeError("update failed")


def test_export_binding_probe_hits_dynamic_export_target_without_real_export() -> None:
    app = _BindingProbeApp(action="export")

    assert keyboard_audit._export_evidence_binding_dispatches(app) is True
    assert app.original_export_calls == 0
    assert "export_evidence" not in vars(app)

    app.export_evidence()
    assert app.original_export_calls == 1


def test_wrong_ctrl_e_target_fails_semantic_binding_check() -> None:
    app = _BindingProbeApp(action="wrong")

    bindings = keyboard_audit._binding_presence(app)

    assert bindings["<Control-e>"] is False
    assert app.wrong_action_calls == 1
    assert app.original_export_calls == 0
    assert "export_evidence" not in vars(app)


def test_probe_restores_export_method_when_event_dispatch_raises() -> None:
    app = _BindingProbeApp(action="raise")

    assert keyboard_audit._export_evidence_binding_dispatches(app) is False
    assert "export_evidence" not in vars(app)

    app.export_evidence()
    assert app.original_export_calls == 1


def test_probe_restores_export_method_when_update_raises() -> None:
    app = _BindingProbeApp(action="export", fail_update=True)

    assert keyboard_audit._export_evidence_binding_dispatches(app) is False
    assert app.original_export_calls == 0
    assert "export_evidence" not in vars(app)

    app.export_evidence()
    assert app.original_export_calls == 1
