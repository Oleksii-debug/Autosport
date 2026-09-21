from __future__ import annotations

import sys

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


def _provider_settlement_helper(record: EventLifecycleRecord, as_of: str):
    return None


def _replacement_settlement_helper(record: EventLifecycleRecord, as_of: str):
    if as_of == "never":
        raise AssertionError("replacement helper executable semantics")
    return None


class _HelperProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-results-helper-resolver-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        return _provider_settlement_helper(record, as_of)


class _RuntimeDispatchBase(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-runtime-dispatch-v1"

    def _settlement_helper(self, record: EventLifecycleRecord, *, as_of: str):
        return None

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        return self._settlement_helper(record, as_of=as_of)


class _RuntimeDispatchChild(_RuntimeDispatchBase):
    def _settlement_helper(self, record: EventLifecycleRecord, *, as_of: str):
        return None


def _replacement_runtime_dispatch_helper(
    self,
    record: EventLifecycleRecord,
    *,
    as_of: str,
):
    if as_of == "never":
        raise AssertionError("replacement runtime-dispatch helper semantics")
    return None


_PROVIDER_HELPER_MODULE = sys.modules[__name__]


class _ModuleHelperProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-module-helper-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        return _PROVIDER_HELPER_MODULE._provider_settlement_helper(record, as_of)


def test_resolver_semantic_fingerprint_ignores_install_relocation() -> None:
    resolver = _ProductSource.resolve
    original_code = resolver.__code__
    original = function_semantic_sha256(resolver)
    assert original == "e219274287e9f95dac1c098883adf26ab79c784233f1b02493918b876f5bd382"
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


def test_resolver_semantic_fingerprint_rejects_referenced_helper_code_mutation() -> None:
    source = _HelperProductSource()
    baseline = _settlement_authority_identity(
        source=source,
        source_id=source.source_id,
        outcome_authority=source,
    )
    helper = _provider_settlement_helper
    original_code = helper.__code__
    try:
        helper.__code__ = _replacement_settlement_helper.__code__
        with pytest.raises(
            ProductCompositionError,
            match="settlement resolve semantics cannot be fingerprinted safely",
        ):
            _settlement_authority_identity(
                source=source,
                source_id=source.source_id,
                outcome_authority=source,
            )
    finally:
        helper.__code__ = original_code
    assert (
        _settlement_authority_identity(
            source=source,
            source_id=source.source_id,
            outcome_authority=source,
        )
        == baseline
    )


def test_restart_rejects_rebinding_referenced_helper_with_same_resolver_code(
    tmp_path,
) -> None:
    source = _HelperProductSource()
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=source,
        clock=lambda: NOW,
        outcome_authority=source,
    )
    expected_identity = runtime.manifest.settlement_authority_identity
    runtime.close()

    original_helper = globals()["_provider_settlement_helper"]
    try:
        globals()["_provider_settlement_helper"] = _replacement_settlement_helper
        rebound_source = _HelperProductSource()
        rebound_identity = _settlement_authority_identity(
            source=rebound_source,
            source_id=rebound_source.source_id,
            outcome_authority=rebound_source,
        )
        assert rebound_identity != expected_identity
        with pytest.raises(
            ProductCompositionError,
            match="settlement authority identity conflicts with durable product composition",
        ):
            build_autonomous_product_runtime(
                workspace=tmp_path,
                source=rebound_source,
                clock=lambda: NOW,
                outcome_authority=rebound_source,
            )
    finally:
        globals()["_provider_settlement_helper"] = original_helper


def test_runtime_owner_dispatch_helper_rebinding_changes_authority_identity(
    tmp_path,
) -> None:
    source = _RuntimeDispatchChild()
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=source,
        clock=lambda: NOW,
        outcome_authority=source,
    )
    expected_identity = runtime.manifest.settlement_authority_identity
    runtime.close()

    original_helper = _RuntimeDispatchChild._settlement_helper
    try:
        _RuntimeDispatchChild._settlement_helper = _replacement_runtime_dispatch_helper
        rebound_source = _RuntimeDispatchChild()
        rebound_identity = _settlement_authority_identity(
            source=rebound_source,
            source_id=rebound_source.source_id,
            outcome_authority=rebound_source,
        )
        assert rebound_identity != expected_identity
        with pytest.raises(
            ProductCompositionError,
            match="settlement authority identity conflicts with durable product composition",
        ):
            build_autonomous_product_runtime(
                workspace=tmp_path,
                source=rebound_source,
                clock=lambda: NOW,
                outcome_authority=rebound_source,
            )
    finally:
        _RuntimeDispatchChild._settlement_helper = original_helper


def test_module_qualified_helper_rebinding_changes_authority_identity(
    tmp_path,
) -> None:
    source = _ModuleHelperProductSource()
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=source,
        clock=lambda: NOW,
        outcome_authority=source,
    )
    expected_identity = runtime.manifest.settlement_authority_identity
    runtime.close()

    original_helper = _PROVIDER_HELPER_MODULE._provider_settlement_helper
    try:
        _PROVIDER_HELPER_MODULE._provider_settlement_helper = (
            _replacement_settlement_helper
        )
        rebound_source = _ModuleHelperProductSource()
        rebound_identity = _settlement_authority_identity(
            source=rebound_source,
            source_id=rebound_source.source_id,
            outcome_authority=rebound_source,
        )
        assert rebound_identity != expected_identity
        with pytest.raises(
            ProductCompositionError,
            match="settlement authority identity conflicts with durable product composition",
        ):
            build_autonomous_product_runtime(
                workspace=tmp_path,
                source=rebound_source,
                clock=lambda: NOW,
                outcome_authority=rebound_source,
            )
    finally:
        _PROVIDER_HELPER_MODULE._provider_settlement_helper = original_helper


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
