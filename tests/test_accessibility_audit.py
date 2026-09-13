import unittest
from types import SimpleNamespace

from autosport.accessibility_audit import summarize_description
from autosport.gui import AUTOMATION_IDS, _SPEEDS, _STRATEGY_CHOICES, strategy_id_from_display


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
                self._widget(AUTOMATION_IDS["strategy"], "Стратегія replay", role="COMBOBOX", patterns=("VALUE",)),
                self._widget(AUTOMATION_IDS["research_plan"], "Вибрати research plan", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["choose_dataset"], "Вибрати replay dataset", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["run_replay"], "Запустити paper replay", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["repair_workspace"], "Відновити workspace", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["replay_speed"], "Швидкість replay", role="COMBOBOX", patterns=("VALUE",)),
                self._widget(AUTOMATION_IDS["live_mode"], "Режим live observation", role="COMBOBOX", patterns=("VALUE",)),
                self._widget(AUTOMATION_IDS["live_refresh"], "Оновити live snapshot", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["tickets"], "Paper tickets і результати", role="LIST", answers_rows=True),
                self._widget(AUTOMATION_IDS["evaluation"], "Evaluation і portfolio evidence", role="LIST", answers_rows=True),
                self._widget(AUTOMATION_IDS["log"], "Журнал виконання", role="EDIT", patterns=("VALUE",)),
                self._widget(AUTOMATION_IDS["live_quotes"], "Live quotes", role="LIST", answers_rows=True),
            ),
            provider_trouble=(),
            providers_stood_down_because=None,
        )

    def test_v1_replay_speed_contract_includes_event_jump_and_1000x(self):
        self.assertEqual(
            _SPEEDS,
            {
                "Подієвий — максимально швидко": 0.0,
                "1× реальний час": 1.0,
                "10×": 10.0,
                "100×": 100.0,
                "1000×": 1000.0,
            },
        )

    def test_gui_strategy_choices_are_canonical_registry_entries(self):
        self.assertEqual(
            set(_STRATEGY_CHOICES.values()),
            {"baseline-v1", "observe-only-v1", "research-replay-v1"},
        )
        for display, strategy_id in _STRATEGY_CHOICES.items():
            self.assertEqual(strategy_id_from_display(display), strategy_id)

    def test_critical_contract_passes_with_names_roles_patterns_and_rows(self):
        report = summarize_description(self._passing_description())
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(len(report["critical_controls"]), 12)
        self.assertFalse(report["nvda_verified"])
        self.assertFalse(report["human_tested"])

    def test_missing_invoke_fails_closed(self):
        description = self._passing_description()
        widgets = list(description.widgets)
        index = next(
            i for i, item in enumerate(widgets) if item.automation_id == AUTOMATION_IDS["research_plan"]
        )
        widgets[index] = self._widget(AUTOMATION_IDS["research_plan"], "Вибрати research plan", patterns=())
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
            i for i, item in enumerate(widgets) if item.automation_id == AUTOMATION_IDS["evaluation"]
        )
        widgets[index] = self._widget(
            AUTOMATION_IDS["evaluation"], "Evaluation і portfolio evidence", role="LIST", answers_rows=False
        )
        report = summarize_description(SimpleNamespace(**{**description.__dict__, "widgets": tuple(widgets)}))
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("list rows are not exposed" in item for item in report["failures"]))


if __name__ == "__main__":
    unittest.main()
