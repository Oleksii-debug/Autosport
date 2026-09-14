import tempfile
import unittest
from pathlib import Path

from autosport.run_transaction import RunTransaction, RunTransactionError


class RunTransactionPathSafetyTests(unittest.TestCase):
    def test_rejects_windows_unsafe_run_ids_before_filesystem_mutation(self):
        invalid_run_ids = (
            "",
            ".",
            "..",
            " leading",
            "nested/run",
            "nested\\run",
            "bad:name",
            'bad"name',
            "bad<name",
            "bad>name",
            "bad|name",
            "bad?name",
            "bad*name",
            "control\x01name",
            "trailing.",
            "trailing ",
            "CON",
            "con.txt",
            "NUL.tar.gz",
            "COM1",
            "com9.log",
            "COM¹",
            "LPT3",
            "lpt³.txt",
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for run_id in invalid_run_ids:
                with self.subTest(run_id=repr(run_id)):
                    with self.assertRaisesRegex(
                        RunTransactionError,
                        "run_id is not a safe workspace path component",
                    ):
                        RunTransaction(workspace, run_id)
            self.assertFalse((workspace / RunTransaction.ROOT_NAME).exists())

    def test_rejects_non_string_run_id_with_domain_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                RunTransactionError,
                "run_id is not a safe workspace path component",
            ):
                RunTransaction(Path(tmp), 123)  # type: ignore[arg-type]

    def test_accepts_unicode_and_uuid_like_portable_run_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for run_id in (
                "запуск-001",
                "5c75992e-2665-4a1c-8669-fc85b459fbad",
                ".hidden-run",
                "run.name-001",
                "internal space",
            ):
                with self.subTest(run_id=run_id):
                    transaction = RunTransaction(workspace, run_id)
                    self.assertEqual(transaction.run_id, run_id)
                    self.assertEqual(
                        transaction.root,
                        workspace / RunTransaction.ROOT_NAME / run_id,
                    )

    def test_start_accepts_cyrillic_portable_component_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            transaction = RunTransaction.start(
                workspace,
                run_id="запуск-002",
                experiment_key="experiment",
                market_sha256="a" * 64,
                results_sha256="b" * 64,
                strategy_id="baseline-v1",
                base_paper_book_sha256="c" * 64,
                base_decision_ledger_sha256="d" * 64,
            )
            self.assertTrue(transaction.manifest_path.is_file())
            self.assertEqual(transaction.run_id, "запуск-002")


if __name__ == "__main__":
    unittest.main()
