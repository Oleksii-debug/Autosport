import unittest

from autosport.gui import _safe_exception_text
from autosport.localization import DEFAULT_LOCALE, catalog, text


_DYNAMIC_ERROR_CASES = (
    (
        "ui.error.research_plan.rejected",
        "ui.status.research_plan.validation_failed",
        "План дослідження відхилено:",
    ),
    (
        "ui.error.dataset.rejected",
        "ui.status.dataset.validation_failed",
        "Набір даних відхилено:",
    ),
    (
        "ui.error.live.snapshot",
        "ui.status.live.failed",
        "Помилка поточного знімка:",
    ),
    (
        "ui.error.recovery.configuration",
        "ui.status.recovery.configuration_rejected",
        "Конфігурацію відновлення відхилено:",
    ),
    (
        "ui.error.recovery.failure",
        "ui.status.recovery.failed_until_fixed",
        "Відновлення робочої області відхилено закрито при помилці:",
    ),
    (
        "ui.error.recovery.worker",
        "ui.status.recovery.blocked",
        "Відновлення робочої області відхилено закрито при помилці:",
    ),
    (
        "ui.error.recovery.reopen",
        "ui.status.recovery.reopen_blocked",
        "Повторне відкриття робочої області після відновлення відхилено закрито при помилці:",
    ),
    (
        "ui.error.replay.configuration",
        "ui.status.replay.configuration_rejected",
        "Конфігурацію стратегії відхилено:",
    ),
    (
        "ui.error.replay.worker",
        "ui.status.replay.failed_recovery",
        "Помилка паперового повтору:",
    ),
    (
        "ui.error.replay.reopen",
        "ui.status.replay.reopen_blocked",
        "Повторне відкриття робочої області після повтору відхилено закрито при помилці:",
    ),
)


def _contains_cyrillic(value: str) -> bool:
    return any("\u0400" <= character <= "\u04ff" for character in value)


class _ExplodingStringException(Exception):
    def __str__(self) -> str:
        raise RuntimeError("exception detail must not escape")


class UkrainianDynamicErrorPresentationTests(unittest.TestCase):
    def test_dynamic_error_matrix_keeps_product_framing_ukrainian(self):
        self.assertEqual(DEFAULT_LOCALE, "uk-UA")
        external_detail = 'EXTERNAL_PROVIDER_DETAIL={"reason":"market closed"}\nsecond line {opaque}'

        for error_key, status_key, expected_prefix in _DYNAMIC_ERROR_CASES:
            with self.subTest(error_key=error_key):
                rendered = text(error_key, detail=external_detail)
                self.assertTrue(rendered.startswith(expected_prefix))
                self.assertEqual(rendered.count(external_detail), 1)
                self.assertTrue(_contains_cyrillic(rendered.split(external_detail, 1)[0]))

                status = text(status_key)
                self.assertTrue(_contains_cyrillic(status))
                self.assertNotIn("{detail}", status)

    def test_dynamic_error_templates_have_one_opaque_detail_slot(self):
        messages = catalog()

        for error_key, _status_key, _expected_prefix in _DYNAMIC_ERROR_CASES:
            with self.subTest(error_key=error_key):
                template = messages[error_key]
                self.assertEqual(template.count("{detail}"), 1)
                product_owned_frame = template.replace("{detail}", "")
                self.assertTrue(_contains_cyrillic(product_owned_frame))

    def test_safe_exception_detail_is_not_reformatted_by_localized_wrapper(self):
        raw_detail = "payload {missing_key} — EXTERNAL_ONLY"
        safe_detail = _safe_exception_text(RuntimeError(raw_detail))

        self.assertEqual(safe_detail, f"RuntimeError: {raw_detail}")
        rendered = text("ui.error.live.snapshot", detail=safe_detail)
        self.assertEqual(
            rendered,
            f"Помилка поточного знімка: RuntimeError: {raw_detail}",
        )

    def test_exception_stringification_failure_uses_localized_fail_closed_copy(self):
        rendered = _safe_exception_text(_ExplodingStringException())

        self.assertEqual(
            rendered,
            text(
                "ui.error.exception.message_unavailable",
                exception_type="_ExplodingStringException",
            ),
        )
        self.assertIn("повідомлення недоступне", rendered)
        self.assertNotIn("exception detail must not escape", rendered)


if __name__ == "__main__":
    unittest.main()
