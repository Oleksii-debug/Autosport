from __future__ import annotations

import copy
import gc
import weakref
from datetime import datetime, timezone
import json

import pytest

import autosport.betfair_provider_billing_inputs as inputs
import autosport.betfair_provider_billing_inputs_authority as authority
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)


class _Clock:
    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return datetime(2026, 9, 22, 3, 0, self._tick, tzinfo=timezone.utc)


class _Transport:
    def post(
        self,
        _url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        assert headers["X-Application"] == "template-app"
        assert headers["X-Authentication"] == "template-session"
        assert timeout_seconds > 0
        request = json.loads(body)
        method = request["method"]
        if method == "AccountAPING/v1.0/getAccountDetails":
            result: object = {"currencyCode": "GBP"}
        elif method == "AccountAPING/v1.0/getDeveloperAppKeys":
            result = [
                {
                    "appId": 41,
                    "appName": "autosport",
                    "appVersions": [
                        {
                            "owner": "owner",
                            "versionId": 7,
                            "version": "1.0",
                            "applicationKey": "template-app",
                            "delayData": False,
                            "subscriptionRequired": False,
                            "ownerManaged": False,
                            "active": True,
                            "vendorId": "vendor-3",
                        }
                    ],
                }
            ]
        elif method == "AccountAPING/v1.0/getAccountStatement":
            offset = request["params"]["fromRecord"]
            result = {
                "accountStatement": [
                    {
                        "refId": f"row-{offset}",
                        "itemDate": f"2026-09-21T00:00:0{offset + 1}Z",
                        "amount": -1,
                        "balance": 100 - offset,
                        "itemClass": "UNKNOWN",
                        "itemClassData": {"offset": offset},
                    }
                ],
                "moreAvailable": offset == 0,
            }
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _template(offset: int):
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("template-app", "template-session"),
        transport=_Transport(),
        clock=_Clock(),
        venue_id="betfair",
        account_id="template-only",
    )
    return inputs.read_betfair_provider_billing_inputs(
        client,
        from_record=offset,
        record_count=1,
    )


def test_traversal_requires_one_authenticated_session_without_retaining_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    templates = {0: _template(0), 1: _template(1)}

    def fake_read(
        _client: BetfairReadOnlyClient,
        *,
        from_record: int = 0,
        **_kwargs: object,
    ):
        return copy.deepcopy(templates[from_record])

    previous_opener = authority._urllib_request.__dict__.get("_opener")
    monkeypatch.setattr(inputs, "read_betfair_provider_billing_inputs", fake_read)
    try:
        (
            read,
            _validate,
            read_traversal,
            validate_traversal,
        ) = authority._build_observation_authority()

        session_a = BetfairSessionCredentials("product-app", "session-a")
        session_a_traversal = read_traversal(session_a, record_count=1)
        assert len(session_a_traversal) == 2
        same_session_pages = validate_traversal(session_a_traversal)
        assert type(same_session_pages) is tuple
        assert tuple(session_a_traversal) == same_session_pages

        first, second_same_session = same_session_pages

        # The private capability object, not structural tuple equality, carries
        # traversal authority. Reconstructing the same capability class is rejected.
        reconstructed_capability = type(session_a_traversal)(
            tuple([first, second_same_session])
        )
        with pytest.raises(
            authority.BetfairProviderBillingInputsAuthorityError,
            match="issued by canonical pagination acquisition",
        ):
            validate_traversal(reconstructed_capability)

        with pytest.raises(TypeError, match="exact canonical capability"):
            validate_traversal(tuple([first, second_same_session]))

        # Even individually canonical pages from the same authenticated credentials
        # cannot be spliced into a caller-constructed positive traversal capability.
        reconstructed_session_a = BetfairSessionCredentials(
            "product-app", "session-a"
        )
        separately_issued_pages = (
            read(session_a, from_record=0, record_count=1),
            read(reconstructed_session_a, from_record=1, record_count=1),
        )
        forged_same_session = type(session_a_traversal)(separately_issued_pages)
        with pytest.raises(
            authority.BetfairProviderBillingInputsAuthorityError,
            match="issued by canonical pagination acquisition",
        ):
            validate_traversal(forged_same_session)

        closure = dict(
            zip(
                validate_traversal.__code__.co_freevars,
                (cell.cell_contents for cell in validate_traversal.__closure__ or ()),
                strict=True,
            )
        )
        issued_registry = closure["issued"]
        traversal_registry = closure["traversals"]
        assert issued_registry
        assert traversal_registry
        assert all(
            all(type(value) is not BetfairSessionCredentials for value in record)
            for record in issued_registry.values()
        )
        assert all(type(record[2]) is bytes for record in issued_registry.values())
        assert all(
            type(record[1]) is tuple and type(record[2]) is bytes
            for record in traversal_registry.values()
        )

        # A different authenticated session owns a distinct exact traversal.
        session_b = BetfairSessionCredentials("product-app", "session-b")
        session_b_traversal = read_traversal(session_b, record_count=1)
        session_b_pages = validate_traversal(session_b_traversal)
        cross_session_capability = type(session_a_traversal)(
            tuple([first, session_b_pages[1]])
        )
        with pytest.raises(
            authority.BetfairProviderBillingInputsAuthorityError,
            match="issued by canonical pagination acquisition",
        ):
            validate_traversal(cross_session_capability)

        # Issuance registries are not process-lifetime owners. Once external
        # possession disappears, weak-reference callbacks remove exact identities.
        ephemeral_source = read(session_a, from_record=0, record_count=1)
        ephemeral_source_id = id(ephemeral_source)
        ephemeral_source_ref = weakref.ref(ephemeral_source)
        assert ephemeral_source_id in issued_registry
        del ephemeral_source
        gc.collect()
        assert ephemeral_source_ref() is None
        assert ephemeral_source_id not in issued_registry

        ephemeral_traversal = read_traversal(session_a, record_count=1)
        ephemeral_traversal_id = id(ephemeral_traversal)
        ephemeral_traversal_ref = weakref.ref(ephemeral_traversal)
        ephemeral_page_ids = tuple(id(page) for page in ephemeral_traversal)
        ephemeral_page_refs = tuple(weakref.ref(page) for page in ephemeral_traversal)
        assert ephemeral_traversal_id in traversal_registry
        assert all(page_id in issued_registry for page_id in ephemeral_page_ids)
        del ephemeral_traversal
        gc.collect()
        assert ephemeral_traversal_ref() is None
        assert ephemeral_traversal_id not in traversal_registry
        assert all(page_ref() is None for page_ref in ephemeral_page_refs)
        assert all(page_id not in issued_registry for page_id in ephemeral_page_ids)
    finally:
        authority._urllib_request.__dict__["_opener"] = previous_opener
