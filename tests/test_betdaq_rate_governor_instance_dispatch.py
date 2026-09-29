from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport.betdaq_rate_governor import (
    BetdaqRateGovernor,
    BetdaqRateGovernorError,
    admit_betdaq_rate_request,
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



def test_supported_dispatch_preserves_admission_receipt_semantics(tmp_path):
    governor = _resolved_governor(tmp_path)

    admission = admit_betdaq_rate_request(governor, "GetPrices")

    assert admission.method == "GetPrices"
    assert admission.sequence == 1
    assert admission.grants_execution_authority is False
    assert admission.grants_write_permission is False
    assert admission.multi_process_safe is False


@pytest.mark.parametrize(
    "attribute",
    (
        "admit",
        "_assert_policy_integrity",
        "_blacklist_state",
        "_now",
        "_prune",
        "policy",
        "_runtime",
        "_blacklist_store",
        "policy_fingerprint",
        "_method_policies",
        "governor_id",
    ),
)
def test_supported_dispatch_rejects_class_surface_replacement(
    tmp_path,
    attribute,
):
    governor = _resolved_governor(tmp_path)
    original = vars(BetdaqRateGovernor)[attribute]
    hostile_calls = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile class dispatch executed")

    type.__setattr__(BetdaqRateGovernor, attribute, hostile)
    try:
        with pytest.raises(
            BetdaqRateGovernorError,
            match="canonical rate governor class dispatch was replaced",
        ):
            admit_betdaq_rate_request(governor, "GetPrices")
    finally:
        type.__setattr__(BetdaqRateGovernor, attribute, original)

    assert hostile_calls == []



@pytest.mark.parametrize(
    "attribute",
    (
        "admit",
        "_assert_policy_integrity",
        "_blacklist_state",
        "_now",
        "_prune",
    ),
)
def test_supported_dispatch_rejects_in_place_class_method_code_replacement(
    tmp_path,
    attribute,
):
    governor = _resolved_governor(tmp_path)
    target = vars(BetdaqRateGovernor)[attribute]
    original_code = target.__code__
    hostile_calls = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile class method code executed")

    target.__code__ = hostile.__code__
    try:
        with pytest.raises(
            BetdaqRateGovernorError,
            match="canonical rate governor class dispatch was replaced",
        ):
            admit_betdaq_rate_request(governor, "GetPrices")
    finally:
        target.__code__ = original_code

    assert hostile_calls == []


def test_supported_dispatch_rejects_class_getattribute_injection(tmp_path):
    governor = _resolved_governor(tmp_path)
    assert "__getattribute__" not in vars(BetdaqRateGovernor)
    hostile_calls = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile __getattribute__ executed")

    type.__setattr__(BetdaqRateGovernor, "__getattribute__", hostile)
    try:
        with pytest.raises(
            BetdaqRateGovernorError,
            match="canonical rate governor class dispatch was replaced",
        ):
            admit_betdaq_rate_request(governor, "GetPrices")
    finally:
        type.__delattr__(BetdaqRateGovernor, "__getattribute__")

    assert hostile_calls == []
