from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import autosport._policy_evaluation_issuance_dispatch_guard as guard_module
import autosport.external_validity_policy_issuance as issuance
import autosport.strategy_model_factory as factory_module
from autosport.monotonic_workspace_binding import WorkspaceIdentityBinding
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore


def _bound_workspace(tmp_path, monkeypatch):
    authority_root = (tmp_path / "machine-authority").resolve()
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root),
    )
    workspace = (tmp_path / "canonical").resolve()
    binding = WorkspaceIdentityBinding.resolve(
        workspace=workspace,
        authority_root=authority_root,
        requested_workspace_instance_id=None,
    )
    binding.ensure_bound()
    ScientificRegistry.initialize_pristine(workspace / "scientific_registry.json")
    FactoryArtifactStore(workspace / "factory-artifacts")
    authority = issuance.ProductPolicyEvaluationWorkspace.open(
        workspace,
        expected_workspace_instance_id=binding.workspace_instance_id,
    )
    return authority


def test_product_issuer_rejects_same_class_store_constructor_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
):
    authority = _bound_workspace(tmp_path, monkeypatch)
    called = False

    def forged_init(self, root, *, clock=None):
        nonlocal called
        called = True
        self.root = root
        self._clock = clock or (lambda: datetime(2000, 1, 1, tzinfo=timezone.utc))

    monkeypatch.setattr(FactoryArtifactStore, "__init__", forged_init)

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="FactoryArtifactStore constructor/clock dispatch",
    ):
        issuance._open_canonical_authorities(authority)

    assert called is False


def test_product_issuer_rejects_in_place_store_constructor_code_rebind(
    tmp_path,
    monkeypatch,
):
    authority = _bound_workspace(tmp_path, monkeypatch)
    canonical_init = FactoryArtifactStore.__init__
    canonical_code = canonical_init.__code__

    closure_shape_marker = object()

    def forged_init(self, root, *, clock=None):
        _ = closure_shape_marker
        raise AssertionError("forged constructor code must not execute")

    canonical_init.__code__ = forged_init.__code__
    try:
        with pytest.raises(
            issuance.ProductPolicyEvaluationIssuanceError,
            match="FactoryArtifactStore constructor/clock dispatch: constructor executable",
        ):
            issuance._open_canonical_authorities(authority)
    finally:
        canonical_init.__code__ = canonical_code


def test_product_issuer_rejects_same_class_store_setattr_rebind_before_dispatch(
    tmp_path,
    monkeypatch,
):
    authority = _bound_workspace(tmp_path, monkeypatch)
    original_setattr = FactoryArtifactStore.__setattr__
    called = False

    def forged_setattr(self, name, value):
        nonlocal called
        called = True
        return original_setattr(self, name, value)

    monkeypatch.setattr(
        FactoryArtifactStore,
        "__setattr__",
        forged_setattr,
        raising=False,
    )

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="FactoryArtifactStore constructor/clock dispatch",
    ):
        issuance._open_canonical_authorities(authority)

    assert called is False


def test_product_issuer_rejects_factory_datetime_rebind_before_store_open(
    tmp_path,
    monkeypatch,
):
    authority = _bound_workspace(tmp_path, monkeypatch)

    class ForgedDateTime:
        @classmethod
        def now(cls, tz=None):
            del tz
            return datetime(2000, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(factory_module, "datetime", ForgedDateTime)

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="FactoryArtifactStore constructor/clock dispatch",
    ):
        issuance._open_canonical_authorities(authority)


def test_product_issuer_replaces_default_store_clock_with_closure_local_utc_source(
    tmp_path,
    monkeypatch,
):
    authority = _bound_workspace(tmp_path, monkeypatch)
    _registry, store = issuance._open_canonical_authorities(authority)

    observed = store._clock()

    assert type(observed) is datetime
    assert observed.tzinfo is not None
    assert observed.utcoffset() == timezone.utc.utcoffset(observed)


def test_product_issue_rejects_direct_helper_rebind_before_argument_dispatch(
    monkeypatch,
):
    called = False

    def forged_derive(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("forged derive must not run")

    monkeypatch.setattr(issuance, "_derive_policy_evaluation", forged_derive)

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="direct helper graph",
    ):
        issuance.issue_product_policy_evaluation(
            None,
            None,
            source_evaluation_bundle_id="caller-forged",
        )

    assert called is False


def test_product_issue_rejects_open_authority_helper_rebind_before_dispatch(
    monkeypatch,
):
    called = False

    def forged_open(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("forged open must not run")

    monkeypatch.setattr(issuance, "_open_canonical_authorities", forged_open)

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="direct helper graph",
    ):
        issuance.issue_product_policy_evaluation(
            None,
            None,
            source_evaluation_bundle_id="caller-forged",
        )

    assert called is False


def test_guard_module_issuer_proxy_cannot_hide_real_issuer_rebind(monkeypatch):
    snapshot = SimpleNamespace(**vars(issuance))
    monkeypatch.setattr(guard_module, "_issuance", snapshot)

    def forged_derive(*args, **kwargs):
        raise AssertionError("forged derive must not run")

    monkeypatch.setattr(issuance, "_derive_policy_evaluation", forged_derive)

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="direct helper graph",
    ):
        issuance.issue_product_policy_evaluation(
            None,
            None,
            source_evaluation_bundle_id="caller-forged",
        )


def test_guard_module_factory_proxy_cannot_hide_real_clock_rebind(
    tmp_path,
    monkeypatch,
):
    authority = _bound_workspace(tmp_path, monkeypatch)
    snapshot = SimpleNamespace(**vars(factory_module))
    monkeypatch.setattr(guard_module, "_factory_module", snapshot)

    class ForgedDateTime:
        @classmethod
        def now(cls, tz=None):
            del tz
            return datetime(2000, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(factory_module, "datetime", ForgedDateTime)

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="FactoryArtifactStore constructor/clock dispatch",
    ):
        issuance._open_canonical_authorities(authority)


def test_public_issue_dispatch_fails_closed_on_dispatch_state_class_rebind(monkeypatch):
    called = False

    def forged_issue(self, *args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("forged state issue must not run")

    monkeypatch.setattr(guard_module._DispatchState, "issue", forged_issue)

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="dispatch state class",
    ):
        issuance.issue_product_policy_evaluation(
            None,
            None,
            source_evaluation_bundle_id="caller-forged",
        )

    assert called is False


def test_policy_guard_fails_closed_on_internal_checker_class_rebind(monkeypatch):
    monkeypatch.setattr(
        guard_module._DispatchState,
        "_require_common_dispatch",
        lambda self: None,
    )

    def forged_derive(*args, **kwargs):
        raise AssertionError("forged derive must not run")

    monkeypatch.setattr(issuance, "_derive_policy_evaluation", forged_derive)

    with pytest.raises(
        issuance.ProductPolicyEvaluationIssuanceError,
        match="dispatch state class",
    ):
        issuance.issue_product_policy_evaluation(
            None,
            None,
            source_evaluation_bundle_id="caller-forged",
        )
