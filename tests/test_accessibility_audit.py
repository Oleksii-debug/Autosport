import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import autosport.accessibility_audit as accessibility_audit
from autosport.accessibility_audit import summarize_description
from autosport.gui import AUTOMATION_IDS, _SPEEDS, _STRATEGY_CHOICES, strategy_id_from_display
from autosport.windows_gui import WINDOWS_BANKROLL_AUTOMATION_ID
from autosport.windows_layout import OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS, WINDOWS_SHELL_AUTOMATION_IDS
from autosport.windows_manual_calculation import WORKBENCH_AUTOMATION_IDS


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
        owner_dialog = OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS
        return SimpleNamespace(
            strategy=_Named("PROVIDED"),
            widgets=(
                self._widget(AUTOMATION_IDS["strategy"], "Стратегія replay", role="COMBO_BOX", patterns=("VALUE",)),
                self._widget(AUTOMATION_IDS["research_plan"], "Вибрати research plan", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["choose_dataset"], "Вибрати replay dataset", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["run_replay"], "Запустити paper replay", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["repair_workspace"], "Відновити workspace", patterns=("INVOKE",)),
                self._widget(AUTOMATION_IDS["export_evidence"], "Експортувати evidence", patterns=("INVOKE",)),
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
                self._widget(shell["owner_economic_open"], "Економічні межі власника", patterns=("INVOKE",)),
                self._widget(shell["owner_economic_status"], "Стан економічних меж власника", role="TEXT", patterns=("VALUE",)),
                self._widget(shell["owner_economic_readback"], "Точні економічні межі власника", role="LIST", answers_rows=True),
                self._widget(owner_dialog["readback"], "Точні економічні межі власника", role="LIST", answers_rows=True),
                self._widget(owner_dialog["close"], "Закрити", patterns=("INVOKE",)),
                self._widget(WORKBENCH_AUTOMATION_IDS["open"], "Відкрити робочу поверхню ручних розрахунків", patterns=("INVOKE",)),
                self._widget(WORKBENCH_AUTOMATION_IDS["operation"], "Операція ручного розрахунку", role="COMBO_BOX", patterns=("VALUE",)),
                self._widget(WORKBENCH_AUTOMATION_IDS["input"], "Вхідні значення ручного розрахунку", role="TEXT", patterns=("VALUE",)),
                self._widget(WORKBENCH_AUTOMATION_IDS["calculate"], "Обчислити ручний результат", patterns=("INVOKE",)),
                self._widget(WORKBENCH_AUTOMATION_IDS["result"], "Результат і evidence ручного розрахунку", role="TEXT", patterns=("VALUE",)),
                self._widget(WORKBENCH_AUTOMATION_IDS["clear"], "Очистити ручні значення", patterns=("INVOKE",)),
                self._widget(WORKBENCH_AUTOMATION_IDS["close"], "Закрити ручні розрахунки", patterns=("INVOKE",)),
            ),
            provider_trouble=(),
            providers_stood_down_because=None,
        )

    def _summarize(
        self,
        description,
        *,
        bankroll_readonly=True,
        shell_state_readonly=True,
        owner_economic_state_readonly=True,
        workbench_result_readonly=True,
    ):
        return summarize_description(
            description,
            bankroll_readonly=bankroll_readonly,
            shell_state_readonly=shell_state_readonly,
            owner_economic_state_readonly=owner_economic_state_readonly,
            workbench_result_readonly=workbench_result_readonly,
        )

    def test_exported_failure_does_not_leak_private_exception_text(self):
        marker = "PLAN6_SYNTHETIC_PRIVATE_VALUE_20261021"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "accessibility.json"
            with patch.object(
                accessibility_audit,
                "WindowsAutosportApp",
                side_effect=RuntimeError("provider_token=" + marker),
            ):
                self.assertEqual(accessibility_audit.run_accessibility_audit(destination), 1)
            raw = destination.read_text(encoding="utf-8")
            report = json.loads(raw)
        self.assertNotIn(marker, raw)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["failures"], ["RuntimeError: details redacted"])
        self.assertFalse(report["human_tested"])
        self.assertFalse(report["nvda_verified"])
        self.assertFalse(report["real_money_execution"])

    def test_untrusted_exception_class_and_stringification_are_private(self):
        marker = "PLAN6_SYNTHETIC_CLASS_MARKER_20261021"
        secret_exception = type(marker, (Exception,), {})
        self.assertEqual(
            accessibility_audit._safe_exception_detail(secret_exception("hidden=" + marker)),
            "Exception: details redacted",
        )
        self.assertEqual(
            accessibility_audit._safe_exception_detail(ValueError("hidden=" + marker)),
            "ValueError: details redacted",
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
        self.assertEqual(len(report["critical_controls"]), 30)
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

    def test_combined_window_descriptions_reject_secondary_provider_strategy(self):
        # A valid main window must not mask an untrusted owner dialog or
        # manual workbench description when all controls look otherwise valid.
        root = self._passing_description()
        extra = SimpleNamespace(
            strategy=_Named("PROVIDED"),
            widgets=(),
            provider_trouble=(),
            providers_stood_down_because=None,
        )
        self.assertEqual(
            self._summarize(accessibility_audit._combined_description(root, extra))["status"],
            "PASS",
        )
        secret_marker = "SYNTHETIC_PRIVATE_PROVIDER_STRATEGY"
        for bad_strategy in (None, _Named(secret_marker), _Named("PROVIDER")):
            with self.subTest(strategy=type(bad_strategy).__name__):
                extra.strategy = bad_strategy
                merged = accessibility_audit._combined_description(root, extra)
                report = self._summarize(merged)
                serialized = json.dumps(report, ensure_ascii=False)
                self.assertEqual(report["status"], "FAIL")
                self.assertEqual(report["strategy"], "UNRECOGNIZED")
                self.assertIn("unrecognized accessibility audit provider strategy", report["failures"])
                self.assertNotIn(secret_marker, serialized)
                self.assertFalse(report["human_tested"])
                self.assertFalse(report["nvda_verified"])
                self.assertFalse(report["real_money_execution"])

    def test_old_provider_label_is_not_a_real_tk_uia_strategy(self):
        # tk-uia 0.8.0's enum is PROVIDED, not PROVIDER; a typo must fail.
        description = self._passing_description()
        description.strategy = _Named("PROVIDER")
        report = self._summarize(description)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["strategy"], "UNRECOGNIZED")
        self.assertIn("unrecognized accessibility audit provider strategy", report["failures"])

    def test_unknown_provider_strategy_cannot_pass_even_with_complete_controls(self):
        description = self._passing_description()
        marker = "SYNTHETIC_PRIVATE_UIA_STRATEGY_20261021"
        description.strategy = _Named(marker)
        report = self._summarize(description)

        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["strategy"], "UNRECOGNIZED")
        self.assertIn(
            "unrecognized accessibility audit provider strategy",
            report["failures"],
        )
        self.assertNotIn(marker, json.dumps(report, ensure_ascii=False))
        self.assertFalse(report["human_tested"])
        self.assertFalse(report["nvda_verified"])

    def test_list_rows_reject_truthy_nonboolean_provider_state(self):
        for malformed_rows in ("false", 1, ["false"]):
            with self.subTest(value_type=type(malformed_rows).__name__):
                description = self._passing_description()
                target = next(
                    widget for widget in description.widgets
                    if widget.automation_id == AUTOMATION_IDS["tickets"]
                )
                target.answers_rows = malformed_rows
                report = self._summarize(description)

                self.assertEqual(report["status"], "FAIL")
                self.assertIn(
                    f"automation_id={AUTOMATION_IDS['tickets']}: list rows are not exposed through UIA",
                    report["failures"],
                )
                row = next(
                    item for item in report["critical_controls"]
                    if item["automation_id"] == AUTOMATION_IDS["tickets"]
                )
                self.assertIs(row["answers_rows"], False)
                json.dumps(report, ensure_ascii=False)
                self.assertFalse(report["human_tested"])
                self.assertFalse(report["nvda_verified"])

    def test_whitespace_and_malformed_critical_accessible_names_fail_closed(self):
        for bad_name in (" \t\u00a0", object()):
            with self.subTest(name_type=type(bad_name).__name__):
                description = self._passing_description()
                target = next(
                    widget for widget in description.widgets
                    if widget.automation_id == AUTOMATION_IDS["run_replay"]
                )
                target.name = bad_name
                report = self._summarize(description)

                self.assertEqual(report["status"], "FAIL")
                self.assertIn(
                    f"automation_id={AUTOMATION_IDS['run_replay']}: missing accessible name",
                    report["failures"],
                )
                # Machine-readable evidence must remain serializable when a
                # malformed provider does not return a text UIA Name.
                json.dumps(report, ensure_ascii=False)
                found = next(
                    control for control in report["critical_controls"]
                    if control["automation_id"] == AUTOMATION_IDS["run_replay"]
                )
                self.assertIsInstance(found["name"], str)
                self.assertFalse(report["human_tested"])
                self.assertFalse(report["nvda_verified"])

    def test_unicode_invisible_only_accessible_names_fail_closed(self):
        # Python str.strip() leaves these Unicode format controls/marks intact,
        # but none independently labels a screen-reader control.
        for bad_name in (
            "\u200b", "\u200d\u2060", "\ufeff",
            "\u0301", "\x00", " \u200b\t",
        ):
            with self.subTest(name=repr(bad_name)):
                description = self._passing_description()
                target = next(
                    widget for widget in description.widgets
                    if widget.automation_id == AUTOMATION_IDS["run_replay"]
                )
                target.name = bad_name
                report = self._summarize(description)
                self.assertEqual(report["status"], "FAIL")
                self.assertIn(
                    f"automation_id={AUTOMATION_IDS['run_replay']}: missing accessible name",
                    report["failures"],
                )
                record = next(
                    item for item in report["critical_controls"]
                    if item["automation_id"] == AUTOMATION_IDS["run_replay"]
                )
                self.assertEqual(record["name"], "")
                self.assertNotIn(bad_name, json.dumps(report, ensure_ascii=False))
                self.assertFalse(report["human_tested"])
                self.assertFalse(report["nvda_verified"])

        description = self._passing_description()
        target = next(
            widget for widget in description.widgets
            if widget.automation_id == AUTOMATION_IDS["run_replay"]
        )
        target.name = "\u200bЗапустити повтор"
        report = self._summarize(description)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(
            next(item["name"] for item in report["critical_controls"]
                 if item["automation_id"] == AUTOMATION_IDS["run_replay"]),
            "[present]",
        )

    def test_punctuation_or_symbol_only_critical_name_is_not_accessible(self):
        # A punctuation mark or icon is visible, but it cannot name the
        # critical replay action. Preserve report redaction and machine truth.
        target_id = AUTOMATION_IDS["run_replay"]
        for name in ("...", "—", "★", "⁉️", "\u200b!!!"):
            with self.subTest(name=repr(name)):
                description = self._passing_description()
                control = next(
                    item for item in description.widgets
                    if item.automation_id == target_id
                )
                control.name = name
                report = self._summarize(description)
                self.assertEqual(report["status"], "FAIL")
                self.assertIn(
                    f"automation_id={target_id}: missing accessible name",
                    report["failures"],
                )
                entry = next(
                    item for item in report["critical_controls"]
                    if item["automation_id"] == target_id
                )
                self.assertEqual(entry["name"], "")
                self.assertFalse(report["human_tested"])
                self.assertFalse(report["nvda_verified"])
                self.assertNotIn(name, json.dumps(report, ensure_ascii=False))

        description = self._passing_description()
        control = next(
            item for item in description.widgets
            if item.automation_id == target_id
        )
        control.name = "★ Запустити повтор"
        report = self._summarize(description)
        self.assertEqual(report["status"], "PASS")

    def test_unrecognized_critical_provider_gap_is_blocking_and_redacted(self):
        description = self._passing_description()
        target = next(
            widget for widget in description.widgets
            if widget.automation_id == AUTOMATION_IDS["run_replay"]
        )
        secret = "UNTRUSTED_UIA_PROVIDER_GAP_SECRET_20261021"
        target.gaps = (_Named(secret),)
        report = self._summarize(description)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn(
            f"automation_id={AUTOMATION_IDS['run_replay']}: blocking gaps=OTHER_GAP",
            report["failures"],
        )
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertFalse(report["human_tested"])
        self.assertFalse(report["nvda_verified"])

    def test_empty_or_none_provider_gap_is_not_silently_ignored(self):
        for malformed_gap in (None, ""):
            with self.subTest(gap=repr(malformed_gap)):
                description = self._passing_description()
                target = next(
                    widget for widget in description.widgets
                    if widget.automation_id == AUTOMATION_IDS["run_replay"]
                )
                target.gaps = (malformed_gap,)
                report = self._summarize(description)
                self.assertEqual(report["status"], "FAIL")
                self.assertIn(
                    f"automation_id={AUTOMATION_IDS['run_replay']}: blocking gaps=OTHER_GAP",
                    report["failures"],
                )
                json.dumps(report, ensure_ascii=False)
                self.assertFalse(report["human_tested"])
                self.assertFalse(report["nvda_verified"])
                self.assertFalse(report["real_money_execution"])

    def test_critical_automation_id_rejects_numeric_aliases(self):
        class IntAlias(int):
            pass

        expected_id = AUTOMATION_IDS["run_replay"]
        for malformed_id in (float(expected_id), IntAlias(expected_id)):
            with self.subTest(type=type(malformed_id).__name__):
                description = self._passing_description()
                target = next(
                    widget for widget in description.widgets
                    if widget.automation_id == expected_id
                )
                target.automation_id = malformed_id
                report = self._summarize(description)
                self.assertEqual(report["status"], "FAIL")
                self.assertIn(
                    f"automation_id={expected_id}: critical control not found",
                    report["failures"],
                )
                self.assertNotIn(
                    expected_id,
                    [item["automation_id"] for item in report["critical_controls"]],
                )
                self.assertFalse(report["human_tested"])
                self.assertFalse(report["nvda_verified"])

    def test_duplicate_critical_automation_id_fails_closed_without_overwrite(self):
        description = self._passing_description()
        original = next(
            item
            for item in description.widgets
            if item.automation_id == AUTOMATION_IDS["run_replay"]
        )
        duplicate = self._widget(
            original.automation_id,
            "Дублікат запуску replay",
            role="PUSH_BUTTON",
            patterns=("INVOKE",),
        )
        duplicate.path = ".!duplicate_run_replay"
        report = self._summarize(
            SimpleNamespace(
                **{
                    **description.__dict__,
                    "widgets": description.widgets + (duplicate,),
                }
            )
        )

        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(
            any(
                "duplicate critical control identity" in failure
                and f"automation_id={original.automation_id}" in failure
                for failure in report["failures"]
            )
        )
        matching_controls = [
            item
            for item in report["critical_controls"]
            if item["automation_id"] == original.automation_id
        ]
        self.assertEqual(len(matching_controls), 1)
        self.assertEqual(matching_controls[0]["path"], "[redacted]")

    def test_machine_uia_export_redacts_untrusted_provider_metadata(self):
        marker = "SYNTHETIC_PRIVATE_UIA_PROVIDER_CREDENTIAL_20261021"
        description = self._passing_description()
        target = next(
            widget for widget in description.widgets
            if widget.automation_id == AUTOMATION_IDS["run_replay"]
        )
        target.path = "/home/operator/.secret/" + marker
        target.tk_class = marker
        target.name = "credential=" + marker
        target.role = _Named(marker)
        target.patterns = (_Named(marker),)
        target.gaps = (_Named(marker),)
        description.provider_trouble = (RuntimeError("token=" + marker),)
        description.providers_stood_down_because = "account=" + marker

        report = self._summarize(description)
        exported = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(marker, exported)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("unexpected accessible role" in f for f in report["failures"]))
        self.assertTrue(any("missing UIA patterns" in f for f in report["failures"]))
        self.assertTrue(any("provider trouble" in f for f in report["failures"]))
        self.assertEqual(report["providers_stood_down_because"], "provider details redacted")
        critical = next(
            item for item in report["critical_controls"]
            if item["automation_id"] == AUTOMATION_IDS["run_replay"]
        )
        self.assertEqual(critical["path"], "[redacted]")
        self.assertEqual(critical["name"], "[present]")
        self.assertFalse(report["human_tested"])
        self.assertFalse(report["nvda_verified"])

    def test_missing_export_evidence_control_fails_closed(self):
        description = self._passing_description()
        widgets = tuple(
            item
            for item in description.widgets
            if item.automation_id != AUTOMATION_IDS["export_evidence"]
        )
        report = self._summarize(
            SimpleNamespace(**{**description.__dict__, "widgets": widgets})
        )
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(
            any(
                f"automation_id={AUTOMATION_IDS['export_evidence']}: critical control not found"
                == failure
                for failure in report["failures"]
            )
        )

    def test_owner_dialog_minimum_controls_are_required(self):
        description = self._passing_description()
        for key in ("readback", "close"):
            automation_id = OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS[key]
            with self.subTest(key=key):
                widgets = tuple(
                    item for item in description.widgets if item.automation_id != automation_id
                )
                report = self._summarize(
                    SimpleNamespace(**{**description.__dict__, "widgets": widgets})
                )
                self.assertEqual(report["status"], "FAIL")
                self.assertIn(
                    f"automation_id={automation_id}: critical control not found",
                    report["failures"],
                )

    def test_wrong_semantic_roles_fail_closed(self):
        cases = (
            (AUTOMATION_IDS["choose_dataset"], "BUTTON", "PUSH_BUTTON"),
            (AUTOMATION_IDS["replay_speed"], "COMBOBOX", "COMBO_BOX"),
            (AUTOMATION_IDS["tickets"], "TEXT", "LIST"),
            (AUTOMATION_IDS["log"], "EDIT", "TEXT"),
            (WINDOWS_BANKROLL_AUTOMATION_ID, "EDIT", "TEXT"),
            (WINDOWS_SHELL_AUTOMATION_IDS["navigation"], "TEXT", "COMBO_BOX"),
            (WINDOWS_SHELL_AUTOMATION_IDS["details"], "TEXT", "LIST"),
            (OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["readback"], "TEXT", "LIST"),
            (OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["close"], "TEXT", "PUSH_BUTTON"),
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

    def test_editable_owner_economic_state_fails_closed(self):
        report = self._summarize(self._passing_description(), owner_economic_state_readonly=False)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(any("owner economic state is not runtime readonly" in item for item in report["failures"]))

    def test_workbench_result_must_be_runtime_readonly(self):
        report = self._summarize(self._passing_description(), workbench_result_readonly=False)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(
            any("manual calculation result is not runtime readonly" in item for item in report["failures"])
        )

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

            audit_dialog = SimpleNamespace(
                _autosport_workbench_controls={},
                destroy=lambda: None,
            )
            owner_dialog = SimpleNamespace(destroy=lambda: None)
            with (
                patch.object(accessibility_audit, "WindowsAutosportApp", return_value=_AuditApp()),
                patch.object(
                    accessibility_audit,
                    "_open_owner_economic_dialog_for_audit",
                    return_value=owner_dialog,
                ),
                patch.object(
                    accessibility_audit,
                    "show_manual_calculation_workbench",
                    return_value=audit_dialog,
                ),
                patch.object(accessibility_audit.tk_uia, "describe", return_value=object()),
                patch.object(accessibility_audit, "show_manual_calculation_workbench", return_value=object()),
                patch.object(accessibility_audit, "_combined_description", return_value=object()),
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


    def test_infinite_widget_provider_enumeration_is_bounded_and_fails_closed(self):
        from itertools import chain, repeat

        description = self._passing_description()
        marker = "SECRET_SYNTHETIC_WIDGET_PATH_4B"
        description.widgets = chain(
            description.widgets,
            repeat(self._widget(-777, marker)),
        )
        result = self._summarize(description)
        raw = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(len(result["critical_controls"]), 30)
        self.assertIn(
            "accessibility audit widget enumeration exceeded safe limit",
            result["failures"],
        )
        self.assertNotIn(marker, raw)
        self.assertFalse(result["nvda_verified"])

    def test_infinite_provider_trouble_is_bounded_and_always_redacted(self):
        from itertools import repeat

        description = self._passing_description()
        marker = "SECRET_SYNTHETIC_PROVIDER_DETAIL_4B"
        description.provider_trouble = repeat(marker)
        result = self._summarize(description)
        raw = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(len(result["provider_trouble"]), 65)
        self.assertEqual(result["provider_trouble"][-1], "additional provider trouble redacted")
        self.assertIn(
            "provider trouble enumeration exceeded safe limit",
            result["failures"],
        )
        self.assertNotIn(marker, raw)

    def test_infinite_per_widget_gaps_and_patterns_cannot_fake_pass(self):
        from itertools import repeat

        for field, enum_name, diagnostic in (
            ("gaps", "NEVER_MAPPED", "UIA gap enumeration exceeded safe limit"),
            ("patterns", "INVOKE", "UIA pattern enumeration exceeded safe limit"),
        ):
            with self.subTest(field=field):
                description = self._passing_description()
                widget = next(
                    item for item in description.widgets
                    if item.automation_id == AUTOMATION_IDS["choose_dataset"]
                )
                setattr(widget, field, repeat(_Named(enum_name)))
                result = self._summarize(description)
                self.assertEqual(result["status"], "FAIL")
                self.assertTrue(
                    any(diagnostic in failure for failure in result["failures"])
                )

    def test_combined_dialog_evidence_does_not_materialize_infinite_input(self):
        from itertools import repeat

        descriptor = SimpleNamespace(
            strategy=_Named("PROVIDED"),
            widgets=repeat(self._widget(-777, "SECRET_SYNTHETIC_UIA_NAME_4B")),
            provider_trouble=repeat("SECRET_SYNTHETIC_COM_DETAIL_4B"),
            providers_stood_down_because=None,
        )
        combined = accessibility_audit._combined_description(descriptor)
        self.assertEqual(len(combined.widgets), 513)
        self.assertEqual(len(combined.provider_trouble), 65)
        result = self._summarize(combined)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn(
            "accessibility audit widget enumeration exceeded safe limit",
            result["failures"],
        )
        self.assertIn(
            "provider trouble enumeration exceeded safe limit",
            result["failures"],
        )
        self.assertNotIn("SECRET_SYNTHETIC", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
