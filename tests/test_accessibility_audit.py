import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import autosport.accessibility_audit as accessibility_audit
from autosport.accessibility_audit import summarize_description
from autosport.gui import AUTOMATION_IDS, _SPEEDS, _STRATEGY_CHOICES, strategy_id_from_display
from autosport.windows_gui import WINDOWS_BANKROLL_AUTOMATION_ID
from autosport.windows_layout import WINDOWS_SHELL_AUTOMATION_IDS


class _Named:
    def __init__(self, name):
        self.name = name


class AccessibilityAuditTests(unittest.TestCase):
    def _widget(self, automation_id, name, role="PUSH_BUTTON", patterns=(), gaps=(), answers_rows=False):
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
        shell = WINDOWS_SHELL_AUTOMATION_IDS
        return SimpleNamespace(
            strategy=_Named("PROVIDER"),
            widgets=(
                self._widget(AUTOMATION_IDS["strategy"], "Стратегія replay", role="COMBO_BOX", patterns=("VALUE",)),
                self._widget(AUTOMATION_IDS["research_plan"], "Вибрати research plan", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["choose_dataset"], "Вибрати replay dataset", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["run_replay"], "Запустити paper replay", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["repair_workspace"], "Відновити workspace", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["replay_speed"], "Швидкість replay", role="COMBO_BOX", patterns=("VALUE",)),
                self._widget(AUTOMATION_IDS["live_mode"], "Режим live observation", role="COMBO_BOX", patterns=("VALUE",)),
                self._widget(AUTOMATION_IDS["live_refresh"], "Оновити live snapshot", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["tickets"], "Paper tickets і результати", role="LIST", answers_rows=True),
                self._widget(AUTOMATION_IDS["evaluation"], "Evaluation і portfolio evidence", role="LIST", answers_rows=True),
                self._widget(AUTOMATION_IDS["log"], "Журнал виконання", role="TEXT", patterns=("VALUE",)),
                self._widget(AUTOMATION_IDS["live_quotes"], "Live quotes", role="LIST", answers_rows=True),
                self._widget(WINDOWS_BANKROLL_AUTOMATION_ID, "Віртуальний банк", role="TEXT", patterns=("VALUE",)),
                self._widget(shell["navigation"], "Навігація екранами Автоспорт", role="COMBO_BOX", patterns=("VALUE",)),
                self._widget(shell["state"], "Стан вибраної поверхні", role="TEXT", patterns=("VALUE",)),
                self._widget(shell["open"], "Перейти до робочої поверхні", patterns=("INVOKE",)),
                self._widget(shell["details"], "Контракт вибраного екрана", role="LIST", answers_rows=True),
            ),
            provider_trouble=(),
            providers_stood_down_because=None,
        )

    def _summarize(self, description, *, bankroll_readonly=True, shell_state_readonly=True):
        return summarize_description(
            description,
            bankroll_readonly=bankroll_readonly,
            shell_state_readonly=shell_state_readonly,
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

    def test_critical_contract_passes_with_names_roles_patterns_rows_and_readonly(self):
        report = self._summarize(self._passing_description())
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(len(report["critical_controls"]), 17)
        bankroll = next(
            item
            for item in report["critical_controls"]
            if item["automation_id"] == WINDOWS_BANKROLL_AUTOMATION_ID
        )
        shell_state = next(
            item
            for item in report["critical_controls"]
            if item["automation_id"] == WINDOWS_SHELL_AUTOMATION_IDS["state"]
        )
        self.assertTrue(bankroll["read_only"])
        self.assertTrue(shell_state["read_only"])
        self.assertFalse(report["nvda_verified"])
        self.assertFalse(report["human_tested"])

    def test_wrong_semantic_roles_fail_closed(self):
        cases = (
            (AUTOMATION_IDS["choose_dataset"], "BUTTON", "PUSH_BUTTON"),
            (AUTOMATION_IDS["replay_speed"], "COMBOBOX", "COMBO_BOX"),
            (AUTOMATION_IDS["tickets"], "TEXT", "LIST"),
            (AUTOMATION_IDS["log"], "EDIT", "TEXT"),
            (WINDOWS_BANKROLL_AUTOMATION_ID, "EDIT", "TEXT"),
            (WINDOWS_SHELL_AUTOMATION_IDS["navigation"], "TEXT", "COMBO_BOX"),
            (WINDOWS_SHELL_AUTOMATION_IDS["details"], "TEXT", "LIST"),
        )
        for automation_id, wrong_role, expected_role in cases:
            with self.subTest(automation_id=automation_id):
                description = self._passing_description()
                widgets = list(description.widgets)
                index = next(
                    i for i, item in enumerate(widgets) if item.automation_id == automation_id
                )
                original = widgets[index]
                widgets[index] = self._widget(
                    automation_id,
                    original.name,
                    role=wrong_role,
                    patterns=tuple(item.name for item in original.patterns),
                    answers_rows=original.answers_rows,
                )
                report = self._summarize(
                    SimpleNamespace(**{**description.__dict__, "widgets": tuple(widgets)})
                )
                self.assertEqual(report["status"], "FAIL")
                self.assertTrue(
                    any(
                        f"unexpected accessible role={wrong_role} expected={expected_role}" in failure
                        for failure in report["failures"]
                    )
                )

    def test_editable_bankroll_summary_fails_closed(self):
        report = self._summarize(self._passing_description(), bankroll_readonly=False)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(
            any("bankroll summary is not runtime readonly" in item for item in report["failures"])
        )
        bankroll = next(
            item
            for item in report["critical_controls"]
            if item["automation_id"] == WINDOWS_BANKROLL_AUTOMATION_ID
        )
        self.assertFalse(bankroll["read_only"])

    def test_editable_shell_state_fails_closed(self):
        report = self._summarize(self._passing_description(), shell_state_readonly=False)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("shell state is not runtime readonly" in item for item in report["failures"]))

    def test_missing_bankroll_runtime_state_evidence_fails_closed(self):
        report = summarize_description(self._passing_description(), shell_state_readonly=True)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(
            any("bankroll summary is not runtime readonly" in item for item in report["failures"])
        )

    def test_missing_invoke_fails_closed(self):
        description = self._passing_description()
        widgets = list(description.widgets)
        index = next(
            i for i, item in enumerate(widgets) if item.automation_id == AUTOMATION_IDS["research_plan"]
        )
        widgets[index] = self._widget(AUTOMATION_IDS["research_plan"], "Вибрати research plan", patterns=())
        report = self._summarize(SimpleNamespace(**{**description.__dict__, "widgets": tuple(widgets)}))
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("missing UIA patterns=INVOKE" in item for item in report["failures"]))

    def test_bankroll_summary_requires_value_pattern(self):
        description = self._passing_description()
        widgets = list(description.widgets)
        index = next(
            i for i, item in enumerate(widgets) if item.automation_id == WINDOWS_BANKROLL_AUTOMATION_ID
        )
        widgets[index] = self._widget(
            WINDOWS_BANKROLL_AUTOMATION_ID,
            "Віртуальний банк",
            role="TEXT",
            patterns=(),
        )
        report = self._summarize(SimpleNamespace(**{**description.__dict__, "widgets": tuple(widgets)}))
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("missing UIA patterns=VALUE" in item for item in report["failures"]))

    def test_missing_live_control_and_provider_trouble_fail_closed(self):
        description = self._passing_description()
        widgets = tuple(
            item for item in description.widgets if item.automation_id != AUTOMATION_IDS["live_refresh"]
        )
        report = self._summarize(
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
            i for i, item in enumerate(widgets) if item.automation_id == WINDOWS_SHELL_AUTOMATION_IDS["details"]
        )
        widgets[index] = self._widget(
            WINDOWS_SHELL_AUTOMATION_IDS["details"],
            "Контракт вибраного екрана",
            role="LIST",
            answers_rows=False,
        )
        report = self._summarize(SimpleNamespace(**{**description.__dict__, "widgets": tuple(widgets)}))
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("list rows are not exposed" in item for item in report["failures"]))

    def test_machine_evidence_publication_failure_preserves_existing_file(self):
        class _AuditApp:
            def update_idletasks(self):
                return None

            def update(self):
                return None

            def close_app(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "accessibility.json"
            original = '{"status":"PREVIOUS"}\n'
            destination.write_text(original, encoding="utf-8")
            nonfinite_report = {
                "status": "PASS",
                "probe": float("nan"),
                "human_tested": False,
                "nvda_verified": False,
                "real_money_execution": False,
            }

            with (
                patch.object(accessibility_audit, "WindowsAutosportApp", return_value=_AuditApp()),
                patch.object(accessibility_audit.tk_uia, "describe", return_value=object()),
                patch.object(
                    accessibility_audit,
                    "summarize_description",
                    return_value=nonfinite_report,
                ),
            ):
                with self.assertRaises(ValueError):
                    accessibility_audit.run_accessibility_audit(destination)

            self.assertEqual(destination.read_text(encoding="utf-8"), original)
            self.assertEqual(list(destination.parent.glob(f".{destination.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
