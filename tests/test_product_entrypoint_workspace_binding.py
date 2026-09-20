from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.product_entrypoint import (
    ProductEntrypointError,
    _validated_source,
    run_product,
)


_SOURCE_FACTORY = "autosport.product_source:create_parlay_product_source"


def _source_environment(workspace: Path) -> dict[str, str]:
    return {
        "AUTOSPORT_PARLAY_API_KEY": "qualification-only-key",
        "AUTOSPORT_PRODUCT_WORKSPACE": str(workspace),
        "AUTOSPORT_PARLAY_LAWFUL_TERMS_REF": "qualification-terms-v1",
        "AUTOSPORT_PARLAY_RETENTION_REF": "qualification-retention-v1",
    }


class ProductEntrypointWorkspaceBindingTests(unittest.TestCase):
    def test_builtin_source_workspace_mismatch_fails_before_runtime_workspace_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_workspace = root / "runtime-a"
            source_workspace = root / "source-b"

            with patch.dict(
                os.environ,
                _source_environment(source_workspace),
                clear=False,
            ):
                with self.assertRaisesRegex(
                    ProductEntrypointError,
                    "source workspace must match product runtime workspace",
                ):
                    run_product(
                        workspace=runtime_workspace,
                        source_factory=_SOURCE_FACTORY,
                        max_cycles=1,
                        poll_seconds=0,
                        install_signal_handlers=False,
                    )

            self.assertFalse(runtime_workspace.exists())
            self.assertTrue(source_workspace.exists())
            self.assertFalse((runtime_workspace / "product_composition.json").exists())

    def test_builtin_source_same_workspace_validates_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "product"
            environment = _source_environment(workspace)

            with patch.dict(os.environ, environment, clear=False):
                first = _validated_source(_SOURCE_FACTORY, workspace=workspace)
                first_instance = first.workspace_instance_id
                first_state = first.state_path.read_bytes()

                second = _validated_source(_SOURCE_FACTORY, workspace=workspace)

            self.assertEqual(first.workspace, workspace.resolve(strict=False))
            self.assertEqual(second.workspace, first.workspace)
            self.assertEqual(second.workspace_instance_id, first_instance)
            self.assertEqual(second.state_path.read_bytes(), first_state)


if __name__ == "__main__":
    unittest.main()
