import tempfile
import unittest
from pathlib import Path

import autosport._paperbook_preload_authority_guard as paper_guard
import autosport.run_transaction as run_transaction_module
from autosport.dataset import load_dataset
from autosport.paper import PaperBook
from autosport.session import AutosportSession


class _HostilePaperBookAuthority:
    def __init__(self) -> None:
        self.lookups: list[str] = []

    def __getattr__(self, name: str):
        self.lookups.append(name)
        raise AssertionError(
            f"mutable RunTransaction PaperBook authority alias executed: {name}"
        )


class RunTransactionPaperBookAuthorityDispatchTests(unittest.TestCase):
    def test_stage_and_promotion_ignore_retargeted_module_authority_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = AutosportSession(root, "10000")
            hostile = _HostilePaperBookAuthority()
            original = run_transaction_module._paperbook_authority
            run_transaction_module._paperbook_authority = hostile
            try:
                result = session.run_dataset(load_dataset(Path("examples/tt_demo")))
            finally:
                run_transaction_module._paperbook_authority = original
                session.close()

            self.assertEqual(hostile.lookups, [])
            self.assertEqual(
                PaperBook.load(root / "paper_book.json").balance,
                result.balance,
            )

    def test_stage_and_promotion_ignore_retargeted_guard_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = AutosportSession(root, "10000")
            calls: list[str] = []

            original_call = paper_guard._call_witnessed_delegate
            original_append = paper_guard._append_witness

            def hostile_call(*_args, **_kwargs):
                calls.append("_call_witnessed_delegate")
                raise AssertionError("mutable serializer dispatch executed")

            def hostile_append(*_args, **_kwargs):
                calls.append("_append_witness")
                raise AssertionError("mutable witness append dispatch executed")

            paper_guard._call_witnessed_delegate = hostile_call
            paper_guard._append_witness = hostile_append
            try:
                result = session.run_dataset(load_dataset(Path("examples/tt_demo")))
            finally:
                paper_guard._call_witnessed_delegate = original_call
                paper_guard._append_witness = original_append
                session.close()

            self.assertEqual(calls, [])
            self.assertEqual(
                PaperBook.load(root / "paper_book.json").balance,
                result.balance,
            )


if __name__ == "__main__":
    unittest.main()
