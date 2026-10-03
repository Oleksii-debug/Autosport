from __future__ import annotations

import inspect

import pytest

import autosport.campaign_forward_universe_cycle_binding as binding
from autosport.campaign_inception import CampaignInceptionReceipt
from autosport.forward_evaluation_universe_binding import ForwardUniverseAuthorityIdentity


def test_authority_preserves_distinct_plan_and_realized_universe_identity() -> None:
    identity = ForwardUniverseAuthorityIdentity(
        precommit_authority_sha256="a" * 64,
        prospective_evaluation_plan_sha256="b" * 64,
        backing_locator_sha256="c" * 64,
        universe_sha256="d" * 64,
        membership_sha256="e" * 64,
        member_count=2,
    )

    authority = binding.CampaignForwardUniverseCycleAuthority._issue(
        campaign_id="campaign-1",
        source_id="parlayapi:table_tennis",
        cycle_receipt_sha256="1" * 64,
        campaign_receipt_sha256="2" * 64,
        provider_evidence_sha256="3" * 64,
        provider_frame_sha256="4" * 64,
        collector_artifact_evidence_sha256="5" * 64,
        forward_identity=identity,
    )

    assert authority.prospective_evaluation_plan_sha256 == "b" * 64
    assert authority.universe_sha256 == "d" * 64
    assert authority.membership_sha256 == "e" * 64
    assert authority.prospective_evaluation_plan_sha256 != authority.universe_sha256
    assert len(authority.authority_sha256) == 64


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


def test_saved_resolver_rejects_internal_witness_table_erasure_before_hostile_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    resolver = binding.resolve_campaign_forward_universe_cycle_authority

    def hostile(*_args, **_kwargs):
        called.append("chronology")
        raise AssertionError("hostile chronology helper executed")

    monkeypatch.setattr(binding, "_INTERNAL_CALLABLES", ())
    monkeypatch.setattr(
        binding,
        "_require_cycle_observation_chronology",
        hostile,
    )

    with pytest.raises(
        binding.CampaignForwardUniverseCycleBindingError,
        match="witness tables changed",
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


def test_saved_authorizer_rejects_provider_witness_map_in_place_mutation() -> None:
    authorizer = binding.authorize_campaign_forward_source_receipts
    original = dict(binding._PROVIDER_UNIVERSE_VALUES)
    binding._PROVIDER_UNIVERSE_VALUES.clear()
    try:
        with pytest.raises(
            binding.CampaignForwardUniverseCycleBindingError,
            match="witness tables changed",
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
    finally:
        binding._PROVIDER_UNIVERSE_VALUES.update(original)


def test_saved_resolver_rejects_datetime_primitive_rebind_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = binding.resolve_campaign_forward_universe_cycle_authority
    monkeypatch.setattr(binding, "datetime", object())

    with pytest.raises(
        binding.CampaignForwardUniverseCycleBindingError,
        match="chronology/digest primitives changed",
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


def test_saved_authorizer_rejects_sha256_primitive_mutation_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorizer = binding.authorize_campaign_forward_source_receipts
    original_sha256 = binding.hashlib.sha256
    hostile_calls: list[str] = []

    def hostile_sha256(*_args, **_kwargs):
        hostile_calls.append("sha256")
        raise AssertionError("hostile sha256 executed")

    monkeypatch.setattr(binding.hashlib, "sha256", hostile_sha256)
    try:
        with pytest.raises(
            binding.CampaignForwardUniverseCycleBindingError,
            match="chronology/digest primitives changed",
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
    finally:
        monkeypatch.setattr(binding.hashlib, "sha256", original_sha256)

    assert hostile_calls == []


def test_private_integrity_guard_rejects_json_module_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(binding, "json", object())

    with pytest.raises(
        binding.CampaignForwardUniverseCycleBindingError,
        match="chronology/digest primitives changed",
    ):
        binding._require_dispatch_integrity()


def test_saved_resolver_rejects_issuer_and_witness_double_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = binding.resolve_campaign_forward_universe_cycle_authority
    hostile_calls: list[str] = []

    def hostile(cls, **_kwargs):
        del cls
        hostile_calls.append("issuer")
        raise AssertionError("hostile authority issuer executed")

    monkeypatch.setattr(
        binding.CampaignForwardUniverseCycleAuthority,
        "_issue",
        classmethod(hostile),
    )
    hostile_surface = inspect.getattr_static(
        binding.CampaignForwardUniverseCycleAuthority,
        "_issue",
    )
    monkeypatch.setattr(
        binding,
        "_CANONICAL_AUTHORITY_ISSUER",
        hostile_surface,
    )
    monkeypatch.setattr(
        binding,
        "_CANONICAL_AUTHORITY_ISSUER_FUNCTION",
        hostile_surface.__func__,
    )
    monkeypatch.setattr(
        binding,
        "_CANONICAL_AUTHORITY_ISSUER_CODE",
        hostile_surface.__func__.__code__,
    )

    with pytest.raises(
        binding.CampaignForwardUniverseCycleBindingError,
        match="authority witness globals changed",
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

    assert hostile_calls == []


def test_saved_resolver_rejects_inception_window_descriptor_replacement() -> None:
    resolver = binding.resolve_campaign_forward_universe_cycle_authority
    original = vars(CampaignInceptionReceipt)["observation_not_after"]

    type.__setattr__(
        CampaignInceptionReceipt,
        "observation_not_after",
        property(lambda _self: "2200-01-01T00:00:00+00:00"),
    )
    try:
        with pytest.raises(
            binding.CampaignForwardUniverseCycleBindingError,
            match="inception receipt field descriptor changed",
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
    finally:
        type.__setattr__(
            CampaignInceptionReceipt,
            "observation_not_after",
            original,
        )
