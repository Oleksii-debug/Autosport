from __future__ import annotations

from pathlib import Path
import re
import unittest

from autosport.product_gui_worker import ProductGuiWorker


_SAFE_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


class ProductGuiErrorTypeSanitizationTests(unittest.TestCase):
    @staticmethod
    def _terminal_error_type(exception_name: str) -> str:
        hostile_error = type(exception_name, (RuntimeError,), {})

        def failing_builder(_workspace: Path, _source_factory: str, _bankroll: str):
            raise hostile_error("provider payload SECRET_MESSAGE_MUST_NOT_SURFACE")

        worker = ProductGuiWorker(runtime_builder=failing_builder)
        started = worker.start(
            workspace=Path("workspace"),
            source_factory="fixture:factory",
            poll_seconds=1.0,
        )
        assert started
        assert worker.join(5.0)
        message = worker.poll()
        assert message is not None
        assert message.kind == "ERROR"
        assert message.error_type is not None
        assert worker.poll() is None
        return message.error_type

    def test_hostile_exception_class_names_are_bounded_safe_identifiers(self) -> None:
        cases = (
            "RuntimeError\nBearer_SECRET_123",
            "Помилка_СЕКРЕТ",
            "RuntimeError-C:\\Users\\operator\\token.txt",
            "E" * 512,
        )
        for hostile_name in cases:
            with self.subTest(hostile_name=hostile_name[:40]):
                rendered = self._terminal_error_type(hostile_name)
                self.assertIsNotNone(_SAFE_ERROR_TYPE.fullmatch(rendered))
                self.assertNotIn("SECRET", rendered)
                self.assertNotIn("СЕКРЕТ", rendered)
                self.assertNotIn("\n", rendered)
                self.assertLessEqual(len(rendered), 64)


if __name__ == "__main__":
    unittest.main()
