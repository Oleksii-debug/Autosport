from types import SimpleNamespace

import autosport.accessibility_audit as accessibility_audit
from autosport.gui import AUTOMATION_IDS


class _Named:
    def __init__(self, name: str):
        self.name = name


def _widget(automation_id: int, *, path: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        path=path or f".!control{automation_id}",
        tk_class="Widget",
        role=_Named(accessibility_audit._EXPECTED_ROLES[automation_id]),
        name=f"control-{automation_id}",
        value=None,
        automation_id=automation_id,
        patterns=tuple(
            _Named(pattern)
            for pattern in sorted(accessibility_audit._REQUIRED_PATTERNS[automation_id])
        ),
        gaps=(),
        answers_rows=automation_id in accessibility_audit._ROW_CONTROLS,
    )


def _description(*widgets: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        strategy=_Named("PROVIDER"),
        widgets=widgets,
        provider_trouble=(),
        providers_stood_down_because=None,
    )


def _summarize(description: SimpleNamespace) -> dict:
    return accessibility_audit.summarize_description(
        description,
        bankroll_readonly=True,
        shell_state_readonly=True,
        owner_economic_state_readonly=True,
        workbench_result_readonly=True,
    )


def test_duplicate_critical_automation_id_fails_closed_without_overwrite():
    canonical_widgets = tuple(
        _widget(automation_id)
        for automation_id in accessibility_audit._REQUIRED_PATTERNS
    )
    baseline = _summarize(_description(*canonical_widgets))
    assert baseline["status"] == "PASS"

    duplicate_id = AUTOMATION_IDS["choose_dataset"]
    duplicate = _widget(duplicate_id, path=".!duplicate-dataset-control")
    report = _summarize(_description(*canonical_widgets, duplicate))

    assert report["status"] == "FAIL"
    assert len(report["critical_controls"]) == len(accessibility_audit._REQUIRED_PATTERNS)
    assert any(
        failure
        == (
            f"automation_id={duplicate_id}: duplicate critical control identity "
            f"first_path=.!control{duplicate_id} "
            "duplicate_path=.!duplicate-dataset-control"
        )
        for failure in report["failures"]
    )
    canonical = next(
        item
        for item in report["critical_controls"]
        if item["automation_id"] == duplicate_id
    )
    assert canonical["path"] == f".!control{duplicate_id}"
    assert report["human_tested"] is False
    assert report["nvda_verified"] is False
    assert report["real_money_execution"] is False
