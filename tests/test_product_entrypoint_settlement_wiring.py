from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from autosport.event_lifecycle import CatalogPage
from autosport.product_entrypoint import ProductEntrypointError, run_product
from autosport.product_runtime import ProductCompositionError


class _BaseSource:
    source_id = "provider-settlement-test"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, checkpoint):
        position = 1 if checkpoint is None else int(getattr(checkpoint, "position")) + 1
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor=f"catalog-{position}",
            position=position,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()

    def resolve_event(self, delta):
        raise AssertionError("no market delta should be resolved in this test")


class _SettlementSource(_BaseSource):
    settlement_resolver_implementation_id = "test-source-settlement-v1"
    settlement_authority_id = "test:source-owned-results:v1"

    def __init__(self, configuration_sha256: str = "a" * 64) -> None:
        self.settlement_configuration_sha256 = configuration_sha256

    def resolve(self, record, *, as_of):
        return None


class _PartialSettlementSource(_BaseSource):
    settlement_authority_id = "test:partial-results:v1"


class _UnmarkedResolveSource(_BaseSource):
    def resolve(self, record, *, as_of):
        return None


def _module(factory) -> types.ModuleType:
    module = types.ModuleType("autosport_test_product_settlement_source")
    module.make_source = factory
    return module


class ProductEntrypointSettlementWiringTests(unittest.TestCase):
    def test_supported_start_binds_explicit_source_owned_settlement_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "product"
            module = _module(_SettlementSource)
            with patch.dict(
                sys.modules,
                {"autosport_test_product_settlement_source": module},
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = run_product(
                        workspace=workspace,
                        source_factory=(
                            "autosport_test_product_settlement_source:make_source"
                        ),
                        initial_bankroll="100",
                        max_cycles=1,
                        poll_seconds=0,
                        sleep=lambda _: self.fail("bounded run must not sleep"),
                        install_signal_handlers=False,
                    )

            self.assertEqual(code, 0)
            manifest = json.loads(
                (workspace / "product_composition.json").read_text(encoding="utf-8")
            )
            authority_identity = manifest["settlement_authority_identity"]
            self.assertIsInstance(authority_identity, str)
            self.assertEqual(len(authority_identity), 64)
            self.assertEqual(authority_identity, authority_identity.lower())
            int(authority_identity, 16)

    def test_partial_settlement_contract_fails_before_workspace_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "must-not-exist"
            module = _module(_PartialSettlementSource)
            with patch.dict(
                sys.modules,
                {"autosport_test_product_settlement_source": module},
            ):
                with self.assertRaisesRegex(
                    ProductEntrypointError,
                    "settlement authority contract is incomplete",
                ):
                    run_product(
                        workspace=workspace,
                        source_factory=(
                            "autosport_test_product_settlement_source:make_source"
                        ),
                        max_cycles=1,
                        poll_seconds=0,
                        install_signal_handlers=False,
                    )
            self.assertFalse(workspace.exists())

    def test_unmarked_resolve_method_does_not_gain_settlement_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "product"
            module = _module(_UnmarkedResolveSource)
            with patch.dict(
                sys.modules,
                {"autosport_test_product_settlement_source": module},
            ):
                code = run_product(
                    workspace=workspace,
                    source_factory=(
                        "autosport_test_product_settlement_source:make_source"
                    ),
                    max_cycles=1,
                    poll_seconds=0,
                    sleep=lambda _: None,
                    install_signal_handlers=False,
                )

            self.assertEqual(code, 0)
            manifest = json.loads(
                (workspace / "product_composition.json").read_text(encoding="utf-8")
            )
            self.assertIsNone(manifest["settlement_authority_identity"])

    def test_restart_rejects_changed_source_settlement_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "product"
            module = _module(lambda: _SettlementSource("a" * 64))
            with patch.dict(
                sys.modules,
                {"autosport_test_product_settlement_source": module},
            ):
                self.assertEqual(
                    run_product(
                        workspace=workspace,
                        source_factory=(
                            "autosport_test_product_settlement_source:make_source"
                        ),
                        max_cycles=1,
                        poll_seconds=0,
                        sleep=lambda _: None,
                        install_signal_handlers=False,
                    ),
                    0,
                )

                module.make_source = lambda: _SettlementSource("b" * 64)
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "settlement authority identity conflicts with durable product composition",
                ):
                    run_product(
                        workspace=workspace,
                        source_factory=(
                            "autosport_test_product_settlement_source:make_source"
                        ),
                        max_cycles=1,
                        poll_seconds=0,
                        install_signal_handlers=False,
                    )


if __name__ == "__main__":
    unittest.main()
