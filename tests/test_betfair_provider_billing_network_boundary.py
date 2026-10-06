from __future__ import annotations

import urllib.request as urllib_request

import pytest

from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.betfair_provider_billing_inputs_authority import (
    BetfairProviderBillingInputsAuthorityError,
    PROVIDER_BILLING_ORIGIN_EXCLUDES,
    PROVIDER_BILLING_ORIGIN_PROTECTS,
    PROVIDER_BILLING_ORIGIN_TRUST_SCOPE,
    read_verified_betfair_provider_billing_inputs,
)


def test_verified_read_rejects_abstract_http_handler_do_open_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attacker_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def attacker(*args: object, **kwargs: object) -> object:
        attacker_calls.append((args, kwargs))
        raise AssertionError("attacker network hook executed")

    monkeypatch.setattr(urllib_request.AbstractHTTPHandler, "do_open", attacker)

    # The product authority exact-type-checks credentials and then executes its
    # network provenance fence before any credential fields are consumed. Using an
    # uninitialized exact instance keeps this falsifier offline and proves failure
    # occurs at the executable boundary, before any provider I/O or attacker hook.
    credentials = object.__new__(BetfairSessionCredentials)

    with pytest.raises(
        BetfairProviderBillingInputsAuthorityError,
        match="provider billing lower HTTP handler executable drifted",
    ):
        read_verified_betfair_provider_billing_inputs(credentials)

    assert attacker_calls == []

def test_provider_billing_origin_threat_scope_is_explicit_and_bounded() -> None:
    """Do not silently turn defense-in-depth fences into an OS-sandbox claim."""

    assert PROVIDER_BILLING_ORIGIN_TRUST_SCOPE == "trusted-autosport-process-v1"
    assert PROVIDER_BILLING_ORIGIN_PROTECTS == (
        "caller-created-observation",
        "caller-injected-client-transport-clock",
        "consumer-api-misuse",
        "issued-object-tamper",
    )
    assert PROVIDER_BILLING_ORIGIN_EXCLUDES == (
        "arbitrary-same-process-code-injection",
        "arbitrary-stdlib-runtime-monkeypatch",
    )

