from __future__ import annotations

import pytest

from autosport.event_lifecycle import CatalogPage, EventLifecycleRecord
from autosport.product_runtime import (
    ProductCompositionError,
    _settlement_authority_identity,
    build_autonomous_product_runtime,
)
from autosport.resolver_semantics import (
    ResolverSemanticIdentityError,
    function_semantic_sha256,
)


SHA_A = "a" * 64
NOW = "2026-09-20T10:00:00Z"


class _ProductSource:
    source_id = "provider-a"
    stream_epoch = "epoch-1"
    settlement_authority_id = "provider-a-results-v1"
    settlement_configuration_sha256 = SHA_A
    settlement_resolver_implementation_id = "provider-a-results-resolver-v1"

    def fetch_catalog_page(self, checkpoint):
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor="unused",
            position=0,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()

    def resolve_event(self, delta):
        raise AssertionError("resolver-semantics tests do not consume collector deltas")

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        return None


def _replacement_resolve(self, record: EventLifecycleRecord, *, as_of: str):
    if as_of == "never":
        raise AssertionError("replacement resolver executable semantics")
    return None


def test_resolver_semantic_fingerprint_ignores_install_relocation() -> None:
    resolver = _ProductSource.resolve
    original_code = resolver.__code__
    original = function_semantic_sha256(resolver)
    assert original == "b64b17edee2de42e77cf0cab0ab844a798fb960af2d836b8cd830d6558f69d6d"
    try:
        resolver.__code__ = original_code.replace(
            co_filename=r"C:\\relocated\\autosport\\provider.py",
            co_firstlineno=original_code.co_firstlineno + 100,
        )
        assert function_semantic_sha256(resolver) == original
    finally:
        resolver.__code__ = original_code


def test_resolver_semantic_fingerprint_rejects_executable_replacement() -> None:
    resolver = _ProductSource.resolve
    original_code = resolver.__code__
    function_semantic_sha256(resolver)
    try:
        resolver.__code__ = _replacement_resolve.__code__
        with pytest.raises(
            ResolverSemanticIdentityError,
            match="executable semantics do not match canonical module source",
        ):
            function_semantic_sha256(resolver)
    finally:
        resolver.__code__ = original_code


def test_product_runtime_restart_rejects_semantic_replacement_with_same_declared_identity(
    tmp_path,
) -> None:
    source = _ProductSource()
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=source,
        clock=lambda: NOW,
        outcome_authority=source,
    )
    original_identity = runtime.manifest.settlement_authority_identity
    runtime.close()

    resolver = _ProductSource.resolve
    original_code = resolver.__code__
    try:
        resolver.__code__ = _replacement_resolve.__code__
        mutated_source = _ProductSource()
        with pytest.raises(
            ProductCompositionError,
            match="settlement resolve semantics cannot be fingerprinted safely",
        ):
            _settlement_authority_identity(
                source=mutated_source,
                source_id=mutated_source.source_id,
                outcome_authority=mutated_source,
            )
        with pytest.raises(
            ProductCompositionError,
            match="settlement resolve semantics cannot be fingerprinted safely",
        ):
            build_autonomous_product_runtime(
                workspace=tmp_path,
                source=mutated_source,
                clock=lambda: NOW,
                outcome_authority=mutated_source,
            )
    finally:
        resolver.__code__ = original_code


def test_product_runtime_restart_accepts_identical_resolver_after_relocation(
    tmp_path,
) -> None:
    source = _ProductSource()
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=source,
        clock=lambda: NOW,
        outcome_authority=source,
    )
    expected_identity = runtime.manifest.settlement_authority_identity
    runtime.close()

    resolver = _ProductSource.resolve
    original_code = resolver.__code__
    try:
        resolver.__code__ = original_code.replace(
            co_filename=r"D:\\fresh-extraction\\autosport\\provider.py",
            co_firstlineno=original_code.co_firstlineno + 250,
        )
        restarted_source = _ProductSource()
        restarted = build_autonomous_product_runtime(
            workspace=tmp_path,
            source=restarted_source,
            clock=lambda: NOW,
            outcome_authority=restarted_source,
        )
        assert restarted.manifest.settlement_authority_identity == expected_identity
        restarted.close()
    finally:
        resolver.__code__ = original_code
