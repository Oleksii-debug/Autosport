from __future__ import annotations

import json
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
    # The real product still requires explicit owner-approved source-rights
    # evidence.  This local fixture only exercises its workspace binding.
    rights_path = workspace.parent / "qualification-source-rights.json"
    rights_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "autosport_source_rights_manifest",
                "source_identity": "parlayapi:table_tennis",
                "authorized_scopes": ["provider.market_data.read"],
                "privacy_classification": "NON_PERSONAL_DATA",
                "evidence_class": "HUMAN_APPROVED_SOURCE_RIGHTS",
                "terms_reference": "qualification-terms-v1",
                "retention_authority_reference": "qualification-retention-v1",
                "effective_at": "2026-09-01T00:00:00Z",
                "expires_at": "2030-01-01T00:00:00Z",
                "human_approved": True,
                "approved_by": "synthetic-test-owner",
                "approval_reference": "fixture:workspace-binding",
                "approved_at": "2026-08-31T00:00:00Z",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return {
        "AUTOSPORT_PARLAY_API_KEY": "qualification-only-key",
        "AUTOSPORT_PARLAY_SOURCE_RIGHTS_MANIFEST": str(rights_path),
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
