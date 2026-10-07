from __future__ import annotations

from types import ModuleType

import pytest

from autosport.event_lifecycle import CatalogPage, EventLifecycleRecord
from autosport.product_runtime import (
    ProductCompositionError,
    _settlement_authority_identity,
    build_autonomous_product_runtime,
)


SHA_A = "a" * 64
NOW = "2026-09-20T10:00:00Z"


class _GlobalResolverHelper:
    @staticmethod
    def resolve_static(record: EventLifecycleRecord, *, as_of: str):
        return None

    @classmethod
    def resolve_class(cls, record: EventLifecycleRecord, *, as_of: str):
        return None


def _replacement_static(record: EventLifecycleRecord, *, as_of: str):
    if as_of == "never":
        raise AssertionError("replacement static helper semantics")
    return None


def _replacement_class(cls, record: EventLifecycleRecord, *, as_of: str):
    if as_of == "never":
        raise AssertionError("replacement class helper semantics")
    return None


class _ProductSource:
    source_id = "provider-a"
    stream_epoch = "epoch-1"
    settlement_authority_id = "provider-a-results-v1"
    settlement_configuration_sha256 = SHA_A

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


class _StaticHelperProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-global-static-helper-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        return _GlobalResolverHelper.resolve_static(record, as_of=as_of)


class _ClassHelperProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-global-class-helper-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        return _GlobalResolverHelper.resolve_class(record, as_of=as_of)


def _assert_restart_rejects_rebound_helper(
    tmp_path,
    *,
    source_type,
    helper_name: str,
    replacement,
    descriptor_type,
) -> None:
    source = source_type()
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=source,
        clock=lambda: NOW,
        outcome_authority=source,
    )
    expected_identity = runtime.manifest.settlement_authority_identity
    runtime.close()

    original_descriptor = vars(_GlobalResolverHelper)[helper_name]
    try:
        setattr(_GlobalResolverHelper, helper_name, descriptor_type(replacement))
        rebound_source = source_type()
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
        setattr(_GlobalResolverHelper, helper_name, original_descriptor)


def test_global_staticmethod_rebind_changes_authority_identity_and_restart_fails(
    tmp_path,
) -> None:
    _assert_restart_rejects_rebound_helper(
        tmp_path,
        source_type=_StaticHelperProductSource,
        helper_name="resolve_static",
        replacement=_replacement_static,
        descriptor_type=staticmethod,
    )


def test_global_classmethod_rebind_changes_authority_identity_and_restart_fails(
    tmp_path,
) -> None:
    _assert_restart_rejects_rebound_helper(
        tmp_path,
        source_type=_ClassHelperProductSource,
        helper_name="resolve_class",
        replacement=_replacement_class,
        descriptor_type=classmethod,
    )


class _InheritedGlobalResolverBase:
    @staticmethod
    def _semantic_helper(record: EventLifecycleRecord, *, as_of: str):
        return None

    @classmethod
    def resolve_inherited(cls, record: EventLifecycleRecord, *, as_of: str):
        return cls._semantic_helper(record, as_of=as_of)

    def resolve_instance(self, record: EventLifecycleRecord, *, as_of: str):
        return self._semantic_helper(record, as_of=as_of)


class _InheritedGlobalResolverChild(_InheritedGlobalResolverBase):
    pass


def _replacement_inherited_helper(
    record: EventLifecycleRecord,
    *,
    as_of: str,
):
    if as_of == "never":
        raise AssertionError("replacement inherited helper semantics")
    return None


class _InheritedClassHelperProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-inherited-global-class-helper-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        return _InheritedGlobalResolverChild.resolve_inherited(record, as_of=as_of)


class _InheritedInstanceHelperProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-inherited-global-instance-helper-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        return _InheritedGlobalResolverChild().resolve_instance(record, as_of=as_of)


def test_inherited_global_classmethod_binds_concrete_runtime_owner_and_restart_fails(
    tmp_path,
) -> None:
    source = _InheritedClassHelperProductSource()
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=source,
        clock=lambda: NOW,
        outcome_authority=source,
    )
    expected_identity = runtime.manifest.settlement_authority_identity
    runtime.close()

    assert "_semantic_helper" not in vars(_InheritedGlobalResolverChild)
    try:
        _InheritedGlobalResolverChild._semantic_helper = staticmethod(
            _replacement_inherited_helper
        )
        rebound_source = _InheritedClassHelperProductSource()
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
        delattr(_InheritedGlobalResolverChild, "_semantic_helper")


def test_inherited_global_ordinary_method_binds_concrete_runtime_owner_and_restart_fails(
    tmp_path,
) -> None:
    source = _InheritedInstanceHelperProductSource()
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=source,
        clock=lambda: NOW,
        outcome_authority=source,
    )
    expected_identity = runtime.manifest.settlement_authority_identity
    runtime.close()

    assert "_semantic_helper" not in vars(_InheritedGlobalResolverChild)
    try:
        _InheritedGlobalResolverChild._semantic_helper = staticmethod(
            _replacement_inherited_helper
        )
        rebound_source = _InheritedInstanceHelperProductSource()
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
        delattr(_InheritedGlobalResolverChild, "_semantic_helper")


class _CountingDescriptor:
    def __init__(self) -> None:
        self.calls = 0

    def __get__(self, instance, owner):
        self.calls += 1
        return _replacement_static


class _DescriptorGlobalResolverHelper:
    unsafe = _CountingDescriptor()


class _DescriptorProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-unsafe-global-descriptor-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        return _DescriptorGlobalResolverHelper.unsafe(record, as_of=as_of)


def test_global_custom_descriptor_fails_closed_without_invoking_get() -> None:
    descriptor = vars(_DescriptorGlobalResolverHelper)["unsafe"]
    assert type(descriptor) is _CountingDescriptor
    descriptor.calls = 0
    source = _DescriptorProductSource()

    with pytest.raises(
        ProductCompositionError,
        match="source-owned settlement resolve semantics cannot be fingerprinted safely",
    ):
        _settlement_authority_identity(
            source=source,
            source_id=source.source_id,
            outcome_authority=source,
        )

    assert descriptor.calls == 0



class _ConstructorProbeResolver:
    calls = 0

    def __init__(self) -> None:
        type(self).calls += 1

    def resolve_instance(self, record: EventLifecycleRecord, *, as_of: str):
        return None


class _ConstructorProbeProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-constructor-probe-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        return _ConstructorProbeResolver().resolve_instance(record, as_of=as_of)


def test_global_type_instance_dependency_fails_closed_without_running_constructor() -> None:
    _ConstructorProbeResolver.calls = 0
    source = _ConstructorProbeProductSource()

    with pytest.raises(
        ProductCompositionError,
        match="source-owned settlement resolve semantics cannot be fingerprinted safely",
    ):
        _settlement_authority_identity(
            source=source,
            source_id=source.source_id,
            outcome_authority=source,
        )

    assert _ConstructorProbeResolver.calls == 0


_qualified_module_rules = ModuleType("autosport_test_qualified_rules")
_qualified_module_rules.SAFE_SCALAR = "stable"
_qualified_module_rules.MUTABLE_RULES = {"winner": "home"}


class _QualifiedTypeRules:
    SAFE_SCALAR = "stable"
    MUTABLE_RULES = {"winner": "home"}


class _ModuleQualifiedScalarProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-module-qualified-scalar-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        if _qualified_module_rules.SAFE_SCALAR == "never":
            raise AssertionError("qualified scalar semantics")
        return None


class _ModuleQualifiedMutableProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-module-qualified-mutable-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        if _qualified_module_rules.MUTABLE_RULES.get("winner") == "never":
            raise AssertionError("qualified mutable semantics")
        return None


class _TypeQualifiedScalarProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-type-qualified-scalar-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        if _QualifiedTypeRules.SAFE_SCALAR == "never":
            raise AssertionError("qualified type scalar semantics")
        return None


class _TypeQualifiedMutableProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-type-qualified-mutable-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        if _QualifiedTypeRules.MUTABLE_RULES.get("winner") == "never":
            raise AssertionError("qualified type mutable semantics")
        return None


def test_module_qualified_scalar_rebind_changes_authority_identity() -> None:
    source = _ModuleQualifiedScalarProductSource()
    expected = _settlement_authority_identity(
        source=source,
        source_id=source.source_id,
        outcome_authority=source,
    )
    original = _qualified_module_rules.SAFE_SCALAR
    try:
        _qualified_module_rules.SAFE_SCALAR = "changed"
        assert _settlement_authority_identity(
            source=source,
            source_id=source.source_id,
            outcome_authority=source,
        ) != expected
    finally:
        _qualified_module_rules.SAFE_SCALAR = original


def test_module_qualified_mutable_data_fails_closed() -> None:
    source = _ModuleQualifiedMutableProductSource()
    with pytest.raises(
        ProductCompositionError,
        match="source-owned settlement resolve semantics cannot be fingerprinted safely",
    ):
        _settlement_authority_identity(
            source=source,
            source_id=source.source_id,
            outcome_authority=source,
        )


def test_global_type_qualified_scalar_rebind_changes_authority_identity() -> None:
    source = _TypeQualifiedScalarProductSource()
    expected = _settlement_authority_identity(
        source=source,
        source_id=source.source_id,
        outcome_authority=source,
    )
    original = _QualifiedTypeRules.SAFE_SCALAR
    try:
        _QualifiedTypeRules.SAFE_SCALAR = "changed"
        assert _settlement_authority_identity(
            source=source,
            source_id=source.source_id,
            outcome_authority=source,
        ) != expected
    finally:
        _QualifiedTypeRules.SAFE_SCALAR = original


def test_global_type_qualified_mutable_data_fails_closed() -> None:
    source = _TypeQualifiedMutableProductSource()
    with pytest.raises(
        ProductCompositionError,
        match="source-owned settlement resolve semantics cannot be fingerprinted safely",
    ):
        _settlement_authority_identity(
            source=source,
            source_id=source.source_id,
            outcome_authority=source,
        )


class _CustomLookupResolverBase:
    lookup_calls = 0

    def __getattribute__(self, name):
        if name == "resolve_instance":
            type(self).lookup_calls += 1
        return object.__getattribute__(self, name)

    def resolve_instance(self, record: EventLifecycleRecord, *, as_of: str):
        return None


class _CustomLookupResolverChild(_CustomLookupResolverBase):
    pass


class _CustomLookupProductSource(_ProductSource):
    settlement_resolver_implementation_id = "provider-a-custom-instance-lookup-v1"

    def resolve(self, record: EventLifecycleRecord, *, as_of: str):
        return _CustomLookupResolverChild().resolve_instance(record, as_of=as_of)


def test_global_type_instance_dependency_rejects_custom_lookup_without_callback() -> None:
    _CustomLookupResolverChild.lookup_calls = 0
    source = _CustomLookupProductSource()
    with pytest.raises(
        ProductCompositionError,
        match="source-owned settlement resolve semantics cannot be fingerprinted safely",
    ):
        _settlement_authority_identity(
            source=source,
            source_id=source.source_id,
            outcome_authority=source,
        )
    assert _CustomLookupResolverChild.lookup_calls == 0
