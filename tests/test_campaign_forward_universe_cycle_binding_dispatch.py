from __future__ import annotations

import inspect

import pytest

import autosport.campaign_forward_universe_cycle_binding as binding


def test_authority_issuer_rebind_fails_before_hostile_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    def hostile(cls, **_kwargs):
        del cls
        called.append("issuer")
        raise AssertionError("hostile authority issuer executed")

    monkeypatch.setattr(
        binding.CampaignForwardUniverseCycleAuthority,
        "_issue",
        classmethod(hostile),
    )

    with pytest.raises(
        binding.CampaignForwardUniverseCycleBindingError,
        match="authority issuer is rebound",
    ):
        binding._require_dispatch_integrity()

    assert called == []


def test_authority_issuer_in_place_code_mutation_fails_closed() -> None:
    descriptor = inspect.getattr_static(
        binding.CampaignForwardUniverseCycleAuthority,
        "_issue",
    )
    target = descriptor.__func__
    original_code = target.__code__

    def hostile(cls, **_kwargs):
        del cls
        raise AssertionError("hostile authority issuer code executed")

    assert original_code.co_freevars == hostile.__code__.co_freevars
    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            binding.CampaignForwardUniverseCycleBindingError,
            match="authority issuer is rebound",
        ):
            binding._require_dispatch_integrity()
    finally:
        target.__code__ = original_code


def test_authority_field_descriptor_rebind_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        binding.CampaignForwardUniverseCycleAuthority,
        "campaign_id",
        property(lambda _self: "forged-campaign"),
    )

    with pytest.raises(
        binding.CampaignForwardUniverseCycleBindingError,
        match="authority field descriptor changed: campaign_id",
    ):
        binding._require_dispatch_integrity()


def test_artifact_kind_rebind_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(binding, "ARTIFACT_KIND", "forged-artifact-kind")

    with pytest.raises(
        binding.CampaignForwardUniverseCycleBindingError,
        match="authority class or artifact kind changed",
    ):
        binding._require_dispatch_integrity()


def test_authorize_rejects_rebound_resolver_before_hostile_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    def hostile(**_kwargs):
        called.append("resolver")
        raise AssertionError("hostile composite resolver executed")

    monkeypatch.setattr(
        binding,
        "resolve_campaign_forward_universe_cycle_authority",
        hostile,
    )

    with pytest.raises(
        binding.CampaignForwardUniverseCycleBindingError,
        match="campaign forward-cycle public authority surface changed",
    ):
        binding.authorize_campaign_forward_source_receipts(
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            protocol=None,
            event_lifecycle=None,
            opportunities=(),
        )

    assert called == []



def test_authorize_rejects_rebound_integrity_guard_before_hostile_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    def hostile() -> None:
        called.append("guard")
        raise AssertionError("hostile integrity guard executed")

    authorizer = binding.authorize_campaign_forward_source_receipts
    monkeypatch.setattr(binding, "_require_dispatch_integrity", hostile)

    with pytest.raises(
        binding.CampaignForwardUniverseCycleBindingError,
        match="campaign forward-cycle integrity guard changed",
    ):
        authorizer(
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            protocol=None,
            event_lifecycle=None,
            opportunities=(),
        )

    assert called == []


def test_authorize_rejects_rebound_reflection_module_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorizer = binding.authorize_campaign_forward_source_receipts
    monkeypatch.setattr(binding, "inspect", object())

    with pytest.raises(
        binding.CampaignForwardUniverseCycleBindingError,
        match="campaign forward-cycle reflection dispatch changed",
    ):
        authorizer(
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            protocol=None,
            event_lifecycle=None,
            opportunities=(),
        )


def test_saved_resolver_rejects_public_surface_rebind_before_hostile_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    resolver = binding.resolve_campaign_forward_universe_cycle_authority

    def hostile(**_kwargs):
        called.append("resolver")
        raise AssertionError("hostile public resolver executed")

    monkeypatch.setattr(
        binding,
        "resolve_campaign_forward_universe_cycle_authority",
        hostile,
    )

    with pytest.raises(
        binding.CampaignForwardUniverseCycleBindingError,
        match="campaign forward-cycle resolver surface changed",
    ):
        resolver(
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            protocol=None,
            event_lifecycle=None,
        )

    assert called == []


def test_saved_authorizer_rejects_public_surface_rebind_before_hostile_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    authorizer = binding.authorize_campaign_forward_source_receipts

    def hostile(**_kwargs):
        called.append("authorizer")
        raise AssertionError("hostile public authorizer executed")

    monkeypatch.setattr(
        binding,
        "authorize_campaign_forward_source_receipts",
        hostile,
    )

    with pytest.raises(
        binding.CampaignForwardUniverseCycleBindingError,
        match="campaign forward-cycle public authority surface changed",
    ):
        authorizer(
            precommit_locator=None,
            collector_store=None,
            source_spec=None,
            cycle_receipt=None,
            provider_evidence_store=None,
            universe_store=None,
            protocol=None,
            event_lifecycle=None,
            opportunities=(),
        )

    assert called == []
