import unittest
from types import SimpleNamespace

from autosport.accessibility_audit import summarize_description
from autosport.gui import AUTOMATION_IDS


class _Named:
    def __init__(self, name):
        self.name = name


class AccessibilityAuditTests(unittest.TestCase):
    def _widget(self, automation_id, name, role="BUTTON", patterns=(), gaps=(), answers_rows=False):
        return SimpleNamespace(
            path=f".!widget{automation_id}",
            tk_class="Widget",
            role=_Named(role) if role is not None else None,
            name=name,
            value=None,
            automation_id=automation_id,
            patterns=tuple(_Named(item) for item in patterns),
            gaps=tuple(_Named(item) for item in gaps),
            answers_rows=answers_rows,
        )

    def _passing_description(self):
        return SimpleNamespace(
            strategy=_Named("PROVIDER"),
            widgets=(
                self._widget(AUTOMATION_IDS["choose_dataset"], "Вибрати replay dataset", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["run_replay"], "Запустити paper replay", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["replay_speed"], "Швидкість replay", role="COMBOBOX", patterns=("VALUE",)),
                self._widget(AUTOMATION_IDS["live_mode"], "Режим live observation", role="COMBOBOX", patterns=("VALUE",)),
                self._widget(AUTOMATION_IDS["live_refresh"], "Оновити live snapshot", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["tickets"], "Paper tickets і результати", role="LIST", answers_rows=True),
                self._widget(AUTOMATION_IDS["log"], "Журнал виконання", role="EDIT", patterns=("VALUE",)),
                self._widget(AUTOMATION_IDS["live_quotes"], "Live quotes", role="LIST", answers_rows=True),
            ),
            provider_trouble=(),
            providers_stood_down_because=None,
        )

    def test_critical_contract_passes_with_names_roles_patterns_and_rows(self):
        report = summarize_description(self._passing_description())
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(len(report["critical_controls"]), 8)
        self.assertFalse(report["nvda_verified"])
        self.assertFalse(report["human_tested"])

    def test_missing_invoke_fails_closed(self):
        description = self._passing_description()
        widgets = list(description.widgets)
        widgets[0] = self._widget(AUTOMATION_IDS["choose_dataset"], "Вибрати replay dataset", patterns=())
        report = summarize_description(SimpleNamespace(**{**description.__dict__, "widgets": tuple(widgets)}))
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("missing UIA patterns=INVOKE" in item for item in report["failures"]))

    def test_missing_live_control_and_provider_trouble_fail_closed(self):
        description = self._passing_description()
        widgets = tuple(
            item for item in description.widgets if item.automation_id != AUTOMATION_IDS["live_refresh"]
        )
        report = summarize_description(
            SimpleNamespace(
                **{
                    **description.__dict__,
                    "widgets": widgets,
                    "provider_trouble": ("provider-test-error",),
                }
            )
        )
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("critical control not found" in item for item in report["failures"]))
        self.assertTrue(any("provider trouble" in item for item in report["failures"]))

    def test_list_without_exposed_rows_fails_closed(self):
        description = self._passing_description()
        widgets = list(description.widgets)
        index = next(
            i for i, item in enumerate(widgets) if item.automation_id == AUTOMATION_IDS["live_quotes"]
        )
        widgets[index] = self._widget(
            AUTOMATION_IDS["live_quotes"], "Live quotes", role="LIST", answers_rows=False
        )
        report = summarize_description(SimpleNamespace(**{**description.__dict__, "widgets": tuple(widgets)}))
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("list rows are not exposed" in item for item in report["failures"]))


if __name__ == "__main__":
    unittest.main()
