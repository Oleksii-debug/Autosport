import hashlib
import os
import tempfile
import unittest

import autosport  # noqa: F401 - package composition installs the guard
import autosport._paperbook_preload_authority_guard as paper_guard
import autosport.run_transaction as run_transaction_module
from autosport.paper import PaperBook
from autosport.run_transaction import RunTransaction


def _sealed_inner_globals(method):
    closure = method.__closure__
    if closure is None:
        raise AssertionError("sealed RunTransaction consumer has no closure")
    freevars = method.__code__.co_freevars
    if "inner_globals" not in freevars:
        raise AssertionError("sealed RunTransaction consumer has no inner_globals")
    return closure[freevars.index("inner_globals")].cell_contents


class RunTransactionPaperBookDirectDispatchGuardTests(unittest.TestCase):
    def test_stage_and_promotion_are_detached_from_live_module_globals(self):
        stage_globals = _sealed_inner_globals(
            RunTransaction._stage_paper_book_snapshot
        )
        promotion_globals = _sealed_inner_globals(
            RunTransaction._promote_paper_book_snapshot
        )

        self.assertIsNot(stage_globals, run_transaction_module.__dict__)
        self.assertIsNot(promotion_globals, run_transaction_module.__dict__)
        self.assertIs(stage_globals["RunTransaction"], RunTransaction)
        self.assertIs(promotion_globals["RunTransaction"], RunTransaction)

    def test_promotion_reuses_witnessed_frozen_direct_dispatch_surface(self):
        promotion_globals = _sealed_inner_globals(
            RunTransaction._promote_paper_book_snapshot
        )
        frozen_os = promotion_globals["os"]
        frozen_tempfile = promotion_globals["tempfile"]
        frozen_hashlib = promotion_globals["hashlib"]
        frozen_paper_book = promotion_globals["PaperBook"]

        witnessed_surface_type = type(paper_guard.os)
        self.assertIs(type(frozen_os), witnessed_surface_type)
        self.assertIs(type(frozen_tempfile), witnessed_surface_type)
        self.assertIs(type(frozen_hashlib), witnessed_surface_type)
        self.assertIs(type(frozen_paper_book), witnessed_surface_type)

        self.assertIsNot(frozen_os, os)
        self.assertIs(frozen_os.close, os.close)
        self.assertIs(frozen_os.fsync, os.fsync)
        self.assertIs(frozen_os.replace, os.replace)
        self.assertEqual(frozen_os.name, os.name)
        self.assertIs(frozen_tempfile.mkstemp, tempfile.mkstemp)
        self.assertIs(frozen_hashlib.sha256, hashlib.sha256)
        self.assertEqual(frozen_paper_book.load_bytes, PaperBook.load_bytes)

        with self.assertRaises(AttributeError):
            frozen_os.replace = lambda *_args: None
        with self.assertRaises(AttributeError):
            del frozen_hashlib.sha256

    def test_live_run_transaction_alias_retarget_cannot_change_detached_targets(self):
        promotion_globals = _sealed_inner_globals(
            RunTransaction._promote_paper_book_snapshot
        )
        frozen_replace = promotion_globals["os"].replace
        frozen_mkstemp = promotion_globals["tempfile"].mkstemp
        frozen_sha256 = promotion_globals["hashlib"].sha256
        frozen_load_bytes = promotion_globals["PaperBook"].load_bytes

        original_os = run_transaction_module.os
        original_tempfile = run_transaction_module.tempfile
        original_hashlib = run_transaction_module.hashlib
        original_paper_book = run_transaction_module.PaperBook
        hostile = object()
        try:
            run_transaction_module.os = hostile
            run_transaction_module.tempfile = hostile
            run_transaction_module.hashlib = hostile
            run_transaction_module.PaperBook = hostile

            self.assertIs(promotion_globals["os"].replace, frozen_replace)
            self.assertIs(promotion_globals["tempfile"].mkstemp, frozen_mkstemp)
            self.assertIs(promotion_globals["hashlib"].sha256, frozen_sha256)
            self.assertEqual(
                promotion_globals["PaperBook"].load_bytes,
                frozen_load_bytes,
            )
        finally:
            run_transaction_module.os = original_os
            run_transaction_module.tempfile = original_tempfile
            run_transaction_module.hashlib = original_hashlib
            run_transaction_module.PaperBook = original_paper_book

    def test_metaclass_seal_removal_cannot_authorize_retargeted_existing_facade(self):
        """Direct promotion fails before logic if its shared facade seal was dismantled."""

        promotion_globals = _sealed_inner_globals(
            RunTransaction._promote_paper_book_snapshot
        )
        frozen_os = promotion_globals["os"]
        surface_type = type(frozen_os)
        surface_meta = type(surface_type)
        original_surface_getattr = vars(surface_type)["__getattr__"]
        original_meta_getattr = vars(surface_meta)["__getattr__"]
        hostile_replace = lambda *_args: None

        def hostile_getattr(_surface, name):
            if name == "replace":
                return hostile_replace
            raise AttributeError(name)

        try:
            # Calling type.__setattr__ on the metaclass itself bypasses the data
            # descriptor that protects assignment to the facade class. Once that seal
            # is replaced by an inert object, the pre-existing facade can be retargeted
            # without changing its identity or tuple payload.
            type.__setattr__(surface_meta, "__getattr__", object())
            type.__setattr__(surface_type, "__getattr__", hostile_getattr)
            self.assertIs(frozen_os.replace, hostile_replace)

            with self.assertRaisesRegex(
                ValueError,
                "RunTransaction frozen direct-dispatch",
            ):
                RunTransaction._promote_paper_book_snapshot(object())
        finally:
            # Restore the facade class while its metaclass seal is intentionally inert,
            # then restore the exact original metaclass descriptor last.
            type.__setattr__(surface_type, "__getattr__", original_surface_getattr)
            type.__setattr__(surface_meta, "__getattr__", original_meta_getattr)

        self.assertIs(frozen_os.replace, os.replace)


if __name__ == "__main__":
    unittest.main()
