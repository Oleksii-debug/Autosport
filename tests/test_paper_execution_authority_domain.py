from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.paper_execution_reality import (
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
)


class PaperExecutionAuthorityDomainTests(unittest.TestCase):
    def test_configured_witness_authority_cannot_resolve_inside_ledger_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()

            configured_roots = (
                workspace,
                workspace / "authority",
                workspace / "nested" / "authority",
            )
            for index, configured_root in enumerate(configured_roots):
                with self.subTest(configured_root=configured_root):
                    with patch.dict(
                        os.environ,
                        {
                            "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(
                                configured_root
                            )
                        },
                        clear=False,
                    ):
                        with self.assertRaisesRegex(
                            PaperExecutionIntegrityError,
                            "must resolve outside ledger workspace",
                        ):
                            PaperExecutionLedger(
                                workspace / f"paper-{index}.jsonl"
                            )

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available")
    def test_configured_alias_resolving_inside_workspace_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            nested = workspace / "authority"
            nested.mkdir()
            alias = root / "authority-alias"
            alias.symlink_to(nested, target_is_directory=True)

            with patch.dict(
                os.environ,
                {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(alias)},
                clear=False,
            ):
                with self.assertRaisesRegex(
                    PaperExecutionIntegrityError,
                    "must resolve outside ledger workspace",
                ):
                    PaperExecutionLedger(workspace / "paper.jsonl")


if __name__ == "__main__":
    unittest.main()
