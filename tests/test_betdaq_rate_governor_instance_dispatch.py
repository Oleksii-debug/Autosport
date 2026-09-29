from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport.betdaq_rate_governor import (
    default_betdaq_rate_policy,
    resolve_betdaq_rate_governor,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _resolved_governor(tmp_path):
    clock = _Clock()
    governor = resolve_betdaq_rate_governor(
        tmp_path,
        default_betdaq_rate_policy(),
        clock=clock,
        wall_clock=lambda: datetime(2026, 9, 29, tzinfo=timezone.utc),
    )
    clock.value = 61.0
    return governor


@pytest.mark.parametrize(
    "attribute",
    (
        "admit",
        "_assert_policy_integrity",
        "_blacklist_state",
        "_now",
        "_prune",
        "blacklist_status",
        "observe_blacklist",
    ),
)
def test_resolved_governor_cannot_shadow_authority_methods(tmp_path, attribute):
    governor = _resolved_governor(tmp_path)

    assert not hasattr(governor, "__dict__")
    with pytest.raises(AttributeError):
        setattr(governor, attribute, lambda *args, **kwargs: None)

    admission = governor.admit("GetPrices")
    assert admission.method == "GetPrices"
    assert admission.sequence == 1
    assert admission.grants_execution_authority is False
    assert admission.grants_write_permission is False
    assert admission.multi_process_safe is False


def test_resolved_governor_cannot_inject_new_instance_dispatch_surface(tmp_path):
    governor = _resolved_governor(tmp_path)

    with pytest.raises(AttributeError):
        governor.caller_owned_rate_authority = lambda *args, **kwargs: None

    assert not hasattr(governor, "caller_owned_rate_authority")


@pytest.mark.parametrize(
    "attribute",
    (
        "workspace",
        "policy",
        "_runtime",
        "_blacklist_store",
        "policy_fingerprint",
        "_method_policies",
        "governor_id",
    ),
)
def test_resolved_governor_authority_bindings_are_write_once(tmp_path, attribute):
    governor = _resolved_governor(tmp_path)
    original = getattr(governor, attribute)

    with pytest.raises(
        AttributeError,
        match="authority bindings are write-once",
    ):
        setattr(governor, attribute, object())
    assert getattr(governor, attribute) is original

    with pytest.raises(
        AttributeError,
        match="authority bindings are write-once",
    ):
        delattr(governor, attribute)
    assert getattr(governor, attribute) is original

    admission = governor.admit("GetPrices")
    assert admission.method == "GetPrices"
    assert admission.sequence == 1
