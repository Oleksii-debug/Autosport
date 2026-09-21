from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

from autosport.product_runtime import ProductCompositionError, build_autonomous_product_runtime


_BASE_PATH = Path(__file__).with_name("test_product_runtime.py")
_SPEC = importlib.util.spec_from_file_location(
    "_product_runtime_parent_tests_for_manifest_rollback",
    _BASE_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("cannot load product runtime parent fixtures")
_base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_base)


class ProductCompositionManifestRollbackFalsifier(unittest.TestCase):
    @staticmethod
    def _build(root: Path, *, policy_id: str, episode_key: str):
        return build_autonomous_product_runtime(
            workspace=root,
            source=_base._Source(),
            clock=_base._Clock(),
            sleep=lambda _: None,
            initial_bankroll="100",
            deployment_authority=_base._deployment_authority(policy_id=policy_id),
            campaign_episode_key=episode_key,
        )

    def test_deleted_manifest_cannot_rebootstrap_different_campaign_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._build(
                root,
                policy_id="c" * 64,
                episode_key="paper-campaign-001",
            )
            first.close()

            manifest = root / "product_composition.json"
            self.assertTrue(manifest.exists())
            surviving_paths = tuple(path.name for path in root.iterdir() if path != manifest)
            self.assertTrue(
                surviving_paths,
                "falsifier requires a non-pristine workspace after manifest loss",
            )
            manifest.unlink()
            self.assertFalse(manifest.exists())

            rebound = None
            try:
                with self.assertRaises(
                    ProductCompositionError,
                    msg=(
                        "missing durable product composition in a non-pristine workspace "
                        "must not authorize a different campaign/deployment identity"
                    ),
                ):
                    rebound = self._build(
                        root,
                        policy_id="d" * 64,
                        episode_key="paper-campaign-002",
                    )
            finally:
                if rebound is not None:
                    rebound.close()

            if manifest.exists():
                payload = manifest.read_text(encoding="utf-8")
                self.assertNotIn("paper-campaign-002", payload)

    def test_self_consistent_alternate_manifest_cannot_replace_prior_campaign_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_root = root / "original"
            alternate_root = root / "alternate"

            original = self._build(
                original_root,
                policy_id="c" * 64,
                episode_key="paper-campaign-001",
            )
            original.close()

            alternate = self._build(
                alternate_root,
                policy_id="d" * 64,
                episode_key="paper-campaign-002",
            )
            alternate.close()

            original_manifest = original_root / "product_composition.json"
            alternate_manifest = alternate_root / "product_composition.json"
            original_bytes = original_manifest.read_bytes()
            alternate_bytes = alternate_manifest.read_bytes()
            self.assertNotEqual(original_bytes, alternate_bytes)

            # Replace only the mutable local manifest. All other original workspace
            # durable state remains untouched and continues to prove this is not a
            # pristine first initialization.
            shutil.copyfile(alternate_manifest, original_manifest)
            self.assertEqual(original_manifest.read_bytes(), alternate_bytes)

            rebound = None
            try:
                with self.assertRaises(
                    ProductCompositionError,
                    msg=(
                        "a self-consistent alternate manifest must not become durable "
                        "restart truth without an independent composition authority"
                    ),
                ):
                    rebound = self._build(
                        original_root,
                        policy_id="d" * 64,
                        episode_key="paper-campaign-002",
                    )
            finally:
                if rebound is not None:
                    rebound.close()

            self.assertNotEqual(
                original_manifest.read_bytes(),
                original_bytes,
                "the falsifier deliberately leaves the alternate local bytes in place",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
