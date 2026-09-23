from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

import autosport.betdaq_rate_governor as subject
from autosport.betdaq_rate_governor import (
    BetdaqBlacklistStatus,
    BetdaqMethodRatePolicy,
    BetdaqRateAdmission,
    BetdaqRateDeferred,
    BetdaqRateGovernor,
    BetdaqRateGovernorError,
    BetdaqRatePolicy,
    BetdaqRatePriority,
    BetdaqRateTier,
    default_betdaq_rate_policy,
    resolve_betdaq_rate_governor,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeWallClock:
    def __init__(self, value: datetime | None = None) -> None:
        self.value = value or datetime(2026, 9, 23, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value = self.value.fromtimestamp(
            self.value.timestamp() + seconds,
            tz=timezone.utc,
        )


def policy(
    *methods: tuple[str, int, int],
    combined: int = 300,
    combined_reserve: int = 0,
    window: float = 60.0,
) -> BetdaqRatePolicy:
    return BetdaqRatePolicy(
        policy_revision="test-policy",
        methods=tuple(
            BetdaqMethodRatePolicy(
                method=name,
                capacity=capacity,
                safety_reserve=reserve,
            )
            for name, capacity, reserve in methods
        ),
        combined_capacity=combined,
        combined_safety_reserve=combined_reserve,
        window_seconds=window,
    )


def resolved(
    tmp_path: Path,
    configured: BetdaqRatePolicy,
    *,
    clock: FakeClock | None = None,
    wall: FakeWallClock | None = None,
):
    monotonic = clock or FakeClock()
    wall_clock = wall or FakeWallClock()
    workspace = (tmp_path / "workspace").resolve()
    authority_root = (tmp_path / "machine-authority").resolve()
    governor = resolve_betdaq_rate_governor(
        workspace,
        configured,
        clock=monotonic,
        wall_clock=wall_clock,
        authority_root=authority_root,
    )
    return governor, monotonic, wall_clock, workspace, authority_root


def make_ready(tmp_path: Path, configured: BetdaqRatePolicy):
    governor, clock, wall, workspace, authority_root = resolved(
        tmp_path, configured
    )
    clock.advance(float(configured.cold_start_seconds))
    return governor, clock, wall, workspace, authority_root


def simulate_process_restart(workspace: Path) -> None:
    key = subject._workspace_registry_key(workspace.resolve())
    subject._GOVERNORS.pop(key, None)
    subject._RUNTIME.pop(key, None)


def test_workspace_registry_identity_uses_os_case_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = policy(("GetPrices", 2, 0))
    clock = FakeClock()
    wall = FakeWallClock()
    authority_root = (tmp_path / "machine-authority").resolve()
    upper = (tmp_path / "BETDAQ-Workspace").resolve()
    lower = (tmp_path / "betdaq-workspace").resolve()

    monkeypatch.setattr(
        subject.os.path,
        "normcase",
        lambda value: value.casefold(),
    )

    first = resolve_betdaq_rate_governor(
        upper,
        configured,
        clock=clock,
        wall_clock=wall,
        authority_root=authority_root,
    )
    clock.advance(60.0)
    first.admit("GetPrices")

    second = resolve_betdaq_rate_governor(
        lower,
        configured,
        clock=clock,
        wall_clock=wall,
        authority_root=authority_root,
    )
    assert second is first

    second.admit("GetPrices")
    with pytest.raises(BetdaqRateDeferred) as denied:
        first.admit("GetPrices")
    assert denied.value.reason == "provider_rate_capacity_exhausted"


def test_direct_construction_is_not_a_supported_admission_authority(tmp_path: Path) -> None:
    configured = policy(("GetPrices", 2, 0))
    governor, clock, _, _, _ = make_ready(tmp_path, configured)
    assert governor.admit("GetPrices").grants_execution_authority is False

    with pytest.raises(TypeError):
        BetdaqRateGovernor(  # type: ignore[call-arg]
            tmp_path.resolve(),
            configured,
        )


def test_cold_start_prevents_restart_from_minting_a_fresh_window(tmp_path: Path) -> None:
    governor, clock, _, _, _ = resolved(
        tmp_path, policy(("GetPrices", 2, 0))
    )

    with pytest.raises(BetdaqRateDeferred) as first:
        governor.admit("GetPrices")
    assert first.value.reason == "cold_start_rate_window_unproven"
    assert first.value.retry_after_seconds == pytest.approx(60.0)

    clock.advance(59.0)
    with pytest.raises(BetdaqRateDeferred):
        governor.admit("GetPrices")

    clock.advance(1.0)
    governor.admit("GetPrices")


def test_131st_default_getprices_call_is_denied_before_combined_exhaustion(
    tmp_path: Path,
) -> None:
    configured = default_betdaq_rate_policy()
    governor, clock, _, _, _ = resolved(tmp_path, configured)
    clock.advance(60.0)

    for _ in range(130):
        governor.admit("GetPrices")

    with pytest.raises(BetdaqRateDeferred) as denied:
        governor.admit("GetPrices")
    assert denied.value.reason == "provider_rate_capacity_exhausted"
    assert denied.value.retry_after_seconds == pytest.approx(60.0)


def test_combined_axis_is_consumed_by_mixed_methods(tmp_path: Path) -> None:
    configured = policy(
        ("GetPrices", 2, 0),
        ("ListOrdersChangedSince", 2, 0),
        combined=3,
    )
    governor, _, _, _, _ = make_ready(tmp_path, configured)

    governor.admit("GetPrices")
    governor.admit("ListOrdersChangedSince")
    governor.admit("GetPrices")

    with pytest.raises(BetdaqRateDeferred) as denied:
        governor.admit("ListOrdersChangedSince")
    assert denied.value.reason == "provider_rate_capacity_exhausted"


def test_background_cannot_consume_configured_safety_reserve(tmp_path: Path) -> None:
    configured = policy(
        ("GetPrices", 3, 1),
        combined=3,
        combined_reserve=1,
    )
    governor, _, _, _, _ = make_ready(tmp_path, configured)

    governor.admit("GetPrices")
    governor.admit("GetPrices")

    with pytest.raises(BetdaqRateDeferred) as denied:
        governor.admit("GetPrices")
    assert denied.value.reason == "safety_capacity_reserved"

    safety = governor.admit(
        "GetPrices",
        priority=BetdaqRatePriority.SAFETY,
    )
    assert safety.method_remaining_total == 0
    assert safety.combined_remaining_total == 0


def test_every_retry_requires_a_fresh_token(tmp_path: Path) -> None:
    governor, _, _, _, _ = make_ready(
        tmp_path, policy(("GetPrices", 2, 0), combined=2)
    )

    first = governor.admit("GetPrices")
    retry = governor.admit("GetPrices")
    assert retry.sequence == first.sequence + 1

    with pytest.raises(BetdaqRateDeferred):
        governor.admit("GetPrices")


def test_unknown_method_never_inherits_ambiguous_any_row(tmp_path: Path) -> None:
    governor, _, _, _, _ = make_ready(
        tmp_path, policy(("GetPrices", 2, 0))
    )

    with pytest.raises(BetdaqRateDeferred) as denied:
        governor.admit("ListBlacklistInformation")
    assert denied.value.reason == "unmodeled_provider_rate_axis"
    assert denied.value.retry_after_seconds is None


def test_caller_cannot_enable_standard_tier_or_widen_documented_limits() -> None:
    with pytest.raises(ValueError):
        BetdaqRateTier("STANDARD")

    with pytest.raises(TypeError):
        BetdaqRatePolicy(  # type: ignore[call-arg]
            policy_revision="forged-standard",
            methods=(BetdaqMethodRatePolicy("GetPrices", 130),),
            tier="STANDARD",
        )

    with pytest.raises(BetdaqRateGovernorError, match="1..130"):
        BetdaqMethodRatePolicy("GetPrices", 131)

    with pytest.raises(BetdaqRateGovernorError, match="1..300"):
        BetdaqRatePolicy(
            policy_revision="too-wide",
            methods=(BetdaqMethodRatePolicy("GetPrices", 1),),
            combined_capacity=301,
        )


def test_stricter_local_policy_is_allowed(tmp_path: Path) -> None:
    configured = policy(
        ("GetPrices", 3, 1),
        combined=4,
        combined_reserve=1,
        window=120.0,
    )
    governor, clock, _, _, _ = resolved(tmp_path, configured)
    clock.advance(120.0)
    first = governor.admit("GetPrices")
    assert first.tier is BetdaqRateTier.DEFAULT
    assert first.any_axis_status == "UNKNOWN_UNMODELED"


def test_blacklist_is_api_scoped_and_unrelated_success_cannot_clear_it(
    tmp_path: Path,
) -> None:
    configured = policy(
        ("GetPrices", 3, 0),
        ("ListOrdersChangedSince", 3, 0),
        combined=6,
    )
    governor, _, _, _, _ = make_ready(tmp_path, configured)

    observation = governor.observe_blacklist(
        api_name="GetPrices",
        remaining_ms=60_000,
        provider_observation_sha256=SHA_A,
    )
    assert observation.api_name == "GetPrices"
    assert governor.blacklist_status("GetPrices") is BetdaqBlacklistStatus.BLACKLISTED
    assert (
        governor.blacklist_status("ListOrdersChangedSince")
        is BetdaqBlacklistStatus.UNKNOWN
    )

    unrelated = governor.admit("ListOrdersChangedSince")
    assert unrelated.method == "ListOrdersChangedSince"
    assert governor.blacklist_status("GetPrices") is BetdaqBlacklistStatus.BLACKLISTED

    with pytest.raises(BetdaqRateDeferred) as denied:
        governor.admit("GetPrices")
    assert denied.value.reason == "provider_api_blacklisted"
    assert denied.value.retry_after_seconds == pytest.approx(60.0)


def test_shorter_blacklist_observation_cannot_reduce_existing_horizon(
    tmp_path: Path,
) -> None:
    governor, clock, wall, _, _ = make_ready(
        tmp_path, policy(("GetPrices", 3, 0))
    )
    first = governor.observe_blacklist(
        api_name="GetPrices",
        remaining_ms=60_000,
        provider_observation_sha256=SHA_A,
    )
    clock.advance(10.0)
    wall.advance(10.0)
    second = governor.observe_blacklist(
        api_name="GetPrices",
        remaining_ms=1_000,
        provider_observation_sha256=SHA_B,
    )
    assert second == first

    with pytest.raises(BetdaqRateDeferred) as denied:
        governor.admit("GetPrices")
    assert denied.value.retry_after_seconds == pytest.approx(50.0)


def test_malformed_blacklist_remaining_ms_fails_without_healthy_upgrade(
    tmp_path: Path,
) -> None:
    governor, _, _, _, _ = make_ready(
        tmp_path, policy(("GetPrices", 3, 0))
    )
    for invalid in (-1, True, 1.5, "1000"):
        with pytest.raises(BetdaqRateGovernorError):
            governor.observe_blacklist(
                api_name="GetPrices",
                remaining_ms=invalid,  # type: ignore[arg-type]
                provider_observation_sha256=SHA_A,
            )
    assert governor.blacklist_status("GetPrices") is BetdaqBlacklistStatus.UNKNOWN


def test_blacklist_survives_process_restart_and_rate_window_cold_starts_again(
    tmp_path: Path,
) -> None:
    configured = policy(("GetPrices", 3, 0))
    governor, _, wall, workspace, authority_root = make_ready(
        tmp_path, configured
    )
    governor.observe_blacklist(
        api_name="GetPrices",
        remaining_ms=60_000,
        provider_observation_sha256=SHA_A,
    )

    simulate_process_restart(workspace)
    new_clock = FakeClock(10.0)
    reopened = resolve_betdaq_rate_governor(
        workspace,
        configured,
        clock=new_clock,
        wall_clock=wall,
        authority_root=authority_root,
    )
    assert reopened.blacklist_status("GetPrices") is BetdaqBlacklistStatus.BLACKLISTED

    with pytest.raises(BetdaqRateDeferred) as cold:
        reopened.admit("GetPrices")
    assert cold.value.reason == "cold_start_rate_window_unproven"

    new_clock.advance(60.0)
    with pytest.raises(BetdaqRateDeferred) as blacklisted:
        reopened.admit("GetPrices")
    assert blacklisted.value.reason == "provider_api_blacklisted"


def test_deleted_blacklist_state_is_detected_after_restart(tmp_path: Path) -> None:
    configured = policy(("GetPrices", 3, 0))
    governor, _, _, workspace, authority_root = make_ready(
        tmp_path, configured
    )
    governor.observe_blacklist(
        api_name="GetPrices",
        remaining_ms=60_000,
        provider_observation_sha256=SHA_A,
    )
    state_path = workspace / "betdaq-rate-governor-blacklist.json"
    state_path.unlink()
    simulate_process_restart(workspace)

    with pytest.raises(
        BetdaqRateGovernorError,
        match="missing, rolled back, or unproven",
    ):
        resolve_betdaq_rate_governor(
            workspace,
            configured,
            clock=FakeClock(),
            wall_clock=FakeWallClock(),
            authority_root=authority_root,
        )


def test_blacklist_file_is_secret_free_and_integrity_bound(tmp_path: Path) -> None:
    governor, _, _, workspace, _ = make_ready(
        tmp_path, policy(("GetPrices", 3, 0))
    )
    governor.observe_blacklist(
        api_name="GetPrices",
        remaining_ms=10_000,
        provider_observation_sha256=SHA_A,
    )
    raw = json.loads(
        (workspace / "betdaq-rate-governor-blacklist.json").read_text(
            encoding="utf-8"
        )
    )
    serialized = json.dumps(raw, sort_keys=True)
    assert "password" not in serialized.lower()
    assert "username" not in serialized.lower()
    assert "applicationidentifier" not in serialized.lower()
    assert len(raw["state_sha256"]) == 64


def test_monotonic_clock_rollback_fails_closed_permanently(tmp_path: Path) -> None:
    governor, clock, _, _, _ = make_ready(
        tmp_path, policy(("GetPrices", 3, 0))
    )
    governor.admit("GetPrices")
    clock.value -= 1.0

    with pytest.raises(BetdaqRateDeferred) as first:
        governor.admit("GetPrices")
    assert first.value.reason == "monotonic_clock_rollback"

    clock.value += 1000.0
    with pytest.raises(BetdaqRateDeferred) as second:
        governor.admit("GetPrices")
    assert second.value.reason == "monotonic_clock_failed_closed"


def test_same_workspace_shares_one_governor_and_rejects_policy_or_clock_rebinding(
    tmp_path: Path,
) -> None:
    configured = policy(("GetPrices", 3, 0))
    governor, clock, wall, workspace, authority_root = resolved(
        tmp_path, configured
    )
    same = resolve_betdaq_rate_governor(
        workspace,
        configured,
        clock=clock,
        wall_clock=wall,
        authority_root=authority_root,
    )
    assert same is governor

    with pytest.raises(BetdaqRateGovernorError, match="different rate policy"):
        resolve_betdaq_rate_governor(
            workspace,
            policy(("GetPrices", 2, 0)),
            clock=clock,
            wall_clock=wall,
            authority_root=authority_root,
        )
    with pytest.raises(BetdaqRateGovernorError, match="different clock"):
        resolve_betdaq_rate_governor(
            workspace,
            configured,
            clock=FakeClock(),
            wall_clock=wall,
            authority_root=authority_root,
        )
    with pytest.raises(BetdaqRateGovernorError, match="different authority root"):
        resolve_betdaq_rate_governor(
            workspace,
            configured,
            clock=clock,
            wall_clock=wall,
            authority_root=(tmp_path / "other-machine-authority").resolve(),
        )
    with pytest.raises(BetdaqRateGovernorError, match="different authority root"):
        resolve_betdaq_rate_governor(
            workspace,
            configured,
            clock=clock,
            wall_clock=wall,
            authority_root=None,
        )


def test_admission_receipt_is_deterministic_secret_free_non_authority(
    tmp_path: Path,
) -> None:
    governor, _, _, _, _ = make_ready(
        tmp_path, policy(("GetPrices", 3, 0))
    )
    receipt = governor.admit(
        "GetPrices",
        priority=BetdaqRatePriority.RECONCILIATION,
    )
    assert type(receipt) is BetdaqRateAdmission
    assert receipt.grants_execution_authority is False
    assert receipt.grants_write_permission is False
    assert receipt.grants_freshness is False
    assert receipt.multi_process_safe is False
    assert len(receipt.receipt_sha256) == 64
    assert receipt.receipt_sha256 == receipt.receipt_sha256
    assert receipt.documented_policy_sha256 == governor.documented_policy_sha256


def test_updateorders_uses_documented_changeorder_bucket_without_dispatching_legacy_name(
    tmp_path: Path,
) -> None:
    configured = default_betdaq_rate_policy()
    governor, _, _, _, _ = make_ready(tmp_path, configured)

    receipt = governor.admit("UpdateOrdersNoReceipt")

    assert receipt.operation_id == "UpdateOrdersNoReceipt"
    assert receipt.method == "UpdateOrdersNoReceipt"
    assert receipt.rate_policy_key == "ChangeOrderNoReceipt"
    assert receipt.documented_policy_sha256 == governor.documented_policy_sha256

    with pytest.raises(BetdaqRateDeferred) as legacy:
        governor.admit("ChangeOrderNoReceipt")
    assert legacy.value.reason == "unmodeled_provider_rate_axis"


def test_provider_blacklist_alias_preserves_raw_name_and_binds_canonical_operation(
    tmp_path: Path,
) -> None:
    governor, _, _, workspace, _ = make_ready(
        tmp_path, policy(("GetPrices", 3, 0))
    )

    observation = governor.observe_blacklist(
        api_name="GETPRICES",
        remaining_ms=60_000,
        provider_observation_sha256=SHA_A,
    )

    assert observation.api_name == "GETPRICES"
    assert observation.operation_id == "GetPrices"
    assert observation.mapped is True
    assert governor.blacklist_status("getprices") is BetdaqBlacklistStatus.BLACKLISTED

    raw = json.loads(
        (workspace / "betdaq-rate-governor-blacklist.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw["observations"][0]["api_name"] == "GETPRICES"
    assert raw["observations"][0]["operation_id"] == "GetPrices"

    with pytest.raises(BetdaqRateDeferred) as denied:
        governor.admit("GetPrices")
    assert denied.value.reason == "provider_api_blacklisted"


def test_unknown_provider_blacklist_name_remains_explicit_unmapped_evidence(
    tmp_path: Path,
) -> None:
    governor, _, _, workspace, _ = make_ready(
        tmp_path, policy(("GetPrices", 3, 0))
    )

    observation = governor.observe_blacklist(
        api_name="GetPricesX",
        remaining_ms=60_000,
        provider_observation_sha256=SHA_A,
    )

    assert observation.api_name == "GetPricesX"
    assert observation.operation_id is None
    assert observation.mapped is False
    assert governor.blacklist_status("GetPrices") is BetdaqBlacklistStatus.UNKNOWN

    raw = json.loads(
        (workspace / "betdaq-rate-governor-blacklist.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw["observations"][0]["api_name"] == "GetPricesX"
    assert raw["observations"][0]["operation_id"] is None

    receipt = governor.admit("GetPrices")
    assert receipt.blacklist_status is BetdaqBlacklistStatus.UNKNOWN


def test_forward_wall_clock_jump_cannot_shorten_remainingms_monotonic_horizon(
    tmp_path: Path,
) -> None:
    governor, clock, wall, _, _ = make_ready(
        tmp_path, policy(("GetPrices", 3, 0))
    )
    governor.observe_blacklist(
        api_name="GetPrices",
        remaining_ms=60_000,
        provider_observation_sha256=SHA_A,
    )

    wall.advance(3600.0)

    assert governor.blacklist_status("GetPrices") is BetdaqBlacklistStatus.BLACKLISTED
    with pytest.raises(BetdaqRateDeferred) as denied:
        governor.admit("GetPrices")
    assert denied.value.reason == "provider_api_blacklisted"
    assert denied.value.retry_after_seconds == pytest.approx(60.0)

    clock.advance(60.0)
    assert (
        governor.blacklist_status("GetPrices")
        is BetdaqBlacklistStatus.EXPIRED_OBSERVATION
    )


def test_restart_seeds_active_durable_blacklist_into_new_monotonic_horizon(
    tmp_path: Path,
) -> None:
    configured = policy(("GetPrices", 3, 0))
    governor, _, wall, workspace, authority_root = make_ready(
        tmp_path, configured
    )
    governor.observe_blacklist(
        api_name="GetPrices",
        remaining_ms=60_000,
        provider_observation_sha256=SHA_A,
    )

    simulate_process_restart(workspace)
    new_clock = FakeClock(10.0)
    reopened = resolve_betdaq_rate_governor(
        workspace,
        configured,
        clock=new_clock,
        wall_clock=wall,
        authority_root=authority_root,
    )

    wall.advance(3600.0)
    assert (
        reopened.blacklist_status("GetPrices")
        is BetdaqBlacklistStatus.BLACKLISTED
    )

    new_clock.advance(60.0)
    assert (
        reopened.blacklist_status("GetPrices")
        is BetdaqBlacklistStatus.EXPIRED_OBSERVATION
    )


def test_restart_after_forward_wall_jump_replays_full_relative_blacklist_horizon(
    tmp_path: Path,
) -> None:
    configured = policy(("GetPrices", 3, 0))
    governor, _, wall, workspace, authority_root = make_ready(
        tmp_path, configured
    )
    governor.observe_blacklist(
        api_name="GetPrices",
        remaining_ms=60_000,
        provider_observation_sha256=SHA_A,
    )

    wall.advance(3600.0)
    simulate_process_restart(workspace)
    new_clock = FakeClock(10.0)
    reopened = resolve_betdaq_rate_governor(
        workspace,
        configured,
        clock=new_clock,
        wall_clock=wall,
        authority_root=authority_root,
    )

    assert reopened.blacklist_status("GetPrices") is BetdaqBlacklistStatus.BLACKLISTED
    new_clock.advance(59.0)
    assert reopened.blacklist_status("GetPrices") is BetdaqBlacklistStatus.BLACKLISTED
    new_clock.advance(1.0)
    assert (
        reopened.blacklist_status("GetPrices")
        is BetdaqBlacklistStatus.EXPIRED_OBSERVATION
    )


def test_legacy_changeorder_blacklist_alias_fences_actual_update_operation(
    tmp_path: Path,
) -> None:
    governor, _, _, _, _ = make_ready(
        tmp_path,
        policy(("UpdateOrdersNoReceipt", 3, 0)),
    )

    observation = governor.observe_blacklist(
        api_name="ChangeOrderNoReceipt",
        remaining_ms=60_000,
        provider_observation_sha256=SHA_A,
    )

    assert observation.api_name == "ChangeOrderNoReceipt"
    assert observation.operation_id == "UpdateOrdersNoReceipt"
    with pytest.raises(BetdaqRateDeferred) as denied:
        governor.admit("UpdateOrdersNoReceipt")
    assert denied.value.reason == "provider_api_blacklisted"

    with pytest.raises(BetdaqRateDeferred) as fictitious:
        governor.admit("ChangeOrderNoReceipt")
    assert fictitious.value.reason == "unmodeled_provider_rate_axis"


def test_unknown_blacklist_observation_cannot_replace_known_operation_fence(
    tmp_path: Path,
) -> None:
    governor, _, _, _, _ = make_ready(
        tmp_path, policy(("GetPrices", 3, 0))
    )
    known = governor.observe_blacklist(
        api_name="GetPrices",
        remaining_ms=60_000,
        provider_observation_sha256=SHA_A,
    )
    unknown = governor.observe_blacklist(
        api_name="GetPricesX",
        remaining_ms=0,
        provider_observation_sha256=SHA_B,
    )

    assert known.operation_id == "GetPrices"
    assert unknown.operation_id is None
    assert governor.blacklist_status("GetPrices") is BetdaqBlacklistStatus.BLACKLISTED
    with pytest.raises(BetdaqRateDeferred) as denied:
        governor.admit("GetPrices")
    assert denied.value.reason == "provider_api_blacklisted"
