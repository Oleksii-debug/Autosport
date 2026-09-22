from datetime import datetime, timezone
from decimal import Decimal

import pytest

import autosport.betdaq_account_readonly as betdaq_account_module
from autosport.betdaq_account_readonly import (
    ADAPTER_ID,
    BetdaqAccountReadOnlyClient,
    BetdaqAccountReadOnlyError,
    BetdaqCredentials,
    UrllibBetdaqSoapTransport,
)
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerPositionState,
)


NS = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


class QueueTransport:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def post(self, url, *, headers, body, timeout_seconds):
        self.calls.append((url, headers, body, timeout_seconds))
        if not self.results:
            raise AssertionError("unexpected network call")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class QueueUrlopen:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        if not self.results:
            raise AssertionError("unexpected urlopen call")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return _FakeHttpResponse(result)


def clock():
    return datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)


def client(*results, credentials=None, account_id="acct"):
    transport = QueueTransport(*results)
    credentials = credentials or BetdaqCredentials("alice", "p@ss", "app-id")
    value = BetdaqAccountReadOnlyClient(
        credentials,
        transport=transport,
        clock=clock,
        account_id=account_id,
    )
    return value, transport


def canonical_client(monkeypatch, *results, credentials=None, account_id="acct"):
    opener = QueueUrlopen(*results)
    monkeypatch.setattr(betdaq_account_module, "urlopen", opener)
    credentials = credentials or BetdaqCredentials("alice", "p@ss", "app-id")
    value = BetdaqAccountReadOnlyClient(
        credentials,
        clock=clock,
        account_id=account_id,
    )
    return value, opener


def soap(method, result_attributes="", inner="", return_status_code="0"):
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP}" xmlns="{NS}">'
        f"<soap:Body><{method}Response><{method}Result {result_attributes}>"
        f'<ReturnStatus Code="{return_status_code}" Description="fixture-status" '
        f'CallId="fixture-call" />'
        f"{inner}</{method}Result></{method}Response></soap:Body></soap:Envelope>"
    ).encode()


def balance(currency="EUR", available="100.01", total="120.02", exposure="-20.01", credit="0"):
    return soap(
        "GetAccountBalances",
        (
            f'Currency="{currency}" Balance="{total}" Exposure="{exposure}" '
            f'AvailableFunds="{available}" Credit="{credit}"'
        ),
    )


def order(
    order_id,
    sequence,
    *,
    status=1,
    polarity=1,
    unmatched="5.00",
    matched="0",
    matched_price=None,
    punter_ref="77",
    market_id="200",
    selection_id="300",
):
    matched_price_attr = "" if matched_price is None else f' MatchedPrice="{matched_price}"'
    return (
        f'<Order Id="{order_id}" MarketId="{market_id}" SelectionId="{selection_id}" '
        f'SequenceNumber="{sequence}" IssuedAt="2026-09-22T14:00:00Z" '
        f'Polarity="{polarity}" UnmatchedStake="{unmatched}" RequestedPrice="2.50"'
        f'{matched_price_attr} MatchedStake="{matched}" '
        f'TotalForSideMakeStake="{matched}" TotalForSideTakeStake="0" '
        f'MatchedAgainstStake="0" Status="{status}" RestrictOrderToBroker="false" '
        f'PunterReferenceNumber="{punter_ref}" CancelOnInRunning="false" '
        f'CancelIfSelectionReset="true" IsCurrentlyInRunning="false" '
        f'PunterCommissionBasis="1" MakeCommissionRate="2" TakeCommissionRate="2" '
        f'ExpectedSelectionResetCount="4" ExpectedWithdrawalSequenceNumber="6" '
        f'OrderFillType="1" FillOrKillThreshold="0" />'
    )


def bootstrap(maximum, *orders):
    return soap(
        "ListBootstrapOrders",
        f'MaximumSequenceNumber="{maximum}"',
        f"<Orders>{''.join(orders)}</Orders>",
    )


def changed(*orders):
    return soap(
        "ListOrdersChangedSince",
        "",
        f"<Orders>{''.join(orders)}</Orders>",
    )


def test_credentials_and_client_repr_do_not_expose_secure_values():
    credentials = BetdaqCredentials("alice", "secret-pass", "secret-app")
    value = repr(credentials)
    assert "alice" not in value
    assert "secret-pass" not in value
    assert "secret-app" not in value
    c = BetdaqAccountReadOnlyClient(credentials, transport=QueueTransport(), clock=clock)
    assert "secret" not in repr(c)


def test_injected_transport_cannot_publish_canonical_account_evidence():
    value, transport = client(balance())

    with pytest.raises(
        BetdaqAccountReadOnlyError,
        match="requires product-owned HTTPS transport",
    ):
        value.read_account_evidence(
            frozenset({BookmakerCapability.BALANCE_READ})
        )

    assert transport.calls == []


def test_shadowed_builtin_transport_cannot_publish_canonical_account_evidence():
    transport = UrllibBetdaqSoapTransport()
    transport.post = lambda *args, **kwargs: balance()
    value = BetdaqAccountReadOnlyClient(
        BetdaqCredentials("alice", "p@ss", "app-id"),
        transport=transport,
        clock=clock,
    )

    with pytest.raises(
        BetdaqAccountReadOnlyError,
        match="transport was replaced or shadowed",
    ):
        value.read_account_evidence(
            frozenset({BookmakerCapability.BALANCE_READ})
        )


def test_canonical_account_scope_ignores_caller_label_for_same_auth_context(
    monkeypatch,
):
    requested = frozenset({BookmakerCapability.BALANCE_READ})
    first, _ = canonical_client(
        monkeypatch,
        balance(),
        credentials=BetdaqCredentials("alice", "p@ss", "app-id"),
        account_id="caller-label-a",
    )
    first_evidence = first.read_account_evidence(requested)

    second, _ = canonical_client(
        monkeypatch,
        balance(),
        credentials=BetdaqCredentials("alice", "p@ss", "app-id"),
        account_id="caller-label-b",
    )
    second_evidence = second.read_account_evidence(requested)

    first_scope = first_evidence.snapshot.profile.account_id
    second_scope = second_evidence.snapshot.profile.account_id
    assert first_scope == second_scope
    assert first_scope == first_evidence.account_context.session_context_id
    assert second_scope == second_evidence.account_context.session_context_id
    assert first_scope not in {"caller-label-a", "caller-label-b"}
    assert first_evidence.account_context.stable_account_identity_proven is False
    assert first_evidence.account_context.cross_session_equivalence_proven is False
    assert (
        BookmakerCapability.ACCOUNT_IDENTITY_READ
        not in first_evidence.snapshot.observed_capabilities
    )


def test_distinct_auth_contexts_cannot_collapse_under_same_caller_label(
    monkeypatch,
):
    requested = frozenset({BookmakerCapability.BALANCE_READ})
    first, _ = canonical_client(
        monkeypatch,
        balance(),
        credentials=BetdaqCredentials("alice-a", "p@ss-a", "app-a"),
        account_id="same-caller-label",
    )
    first_evidence = first.read_account_evidence(requested)

    second, _ = canonical_client(
        monkeypatch,
        balance(),
        credentials=BetdaqCredentials("alice-b", "p@ss-b", "app-b"),
        account_id="same-caller-label",
    )
    second_evidence = second.read_account_evidence(requested)

    assert first_evidence.snapshot.profile.account_id != (
        second_evidence.snapshot.profile.account_id
    )
    assert first_evidence.snapshot.profile.profile_id != (
        second_evidence.snapshot.profile.profile_id
    )


def test_public_account_context_and_snapshot_identity_never_expose_credentials(
    monkeypatch,
):
    credentials = BetdaqCredentials("secret-user", "secret-pass", "secret-app")
    value, _ = canonical_client(
        monkeypatch,
        balance(),
        credentials=credentials,
        account_id="friendly-label",
    )

    evidence = value.read_account_evidence(
        frozenset({BookmakerCapability.BALANCE_READ})
    )
    public_text = " ".join(
        (
            repr(evidence.account_context),
            evidence.snapshot.profile.account_id,
            evidence.snapshot.profile.source_ref,
            evidence.snapshot.profile.source_payload_sha256,
        )
    )
    for secret in ("secret-user", "secret-pass", "secret-app"):
        assert secret not in public_text
    assert "friendly-label" not in evidence.snapshot.profile.account_id


def test_credential_rotation_during_snapshot_fails_before_canonical_publication(
    monkeypatch,
):
    credentials = BetdaqCredentials("alice", "before-pass", "app-id")

    class MutatingQueueUrlopen(QueueUrlopen):
        def __call__(self, request, *, timeout):
            response = super().__call__(request, timeout=timeout)
            if len(self.calls) == 1:
                object.__setattr__(credentials, "password", "after-pass")
            return response

    opener = MutatingQueueUrlopen(
        balance(),
        bootstrap(0),
        changed(),
    )
    monkeypatch.setattr(betdaq_account_module, "urlopen", opener)
    value = BetdaqAccountReadOnlyClient(
        credentials,
        clock=clock,
        account_id="caller-label",
    )

    with pytest.raises(
        BetdaqAccountReadOnlyError,
        match="authenticated account context changed during acquisition",
    ):
        value.read_account_evidence(
            frozenset({BookmakerCapability.OPEN_POSITIONS_READ})
        )


def test_http_200_provider_return_status_failure_never_becomes_success():
    payload = balance().replace(
        b'Description="fixture-status"',
        b'Description="failed with p@ss app-id"',
    )
    payload = payload.replace(b'Code="0"', b'Code="17"', 1)
    c, _ = client(payload)

    with pytest.raises(BetdaqAccountReadOnlyError, match="failure code 17") as exc:
        c.read_account_balance()

    assert "p@ss" not in str(exc.value)
    assert "app-id" not in str(exc.value)


def test_missing_or_malformed_return_status_fails_closed():
    success = balance()
    status = (
        b'<ReturnStatus Code="0" Description="fixture-status" '
        b'CallId="fixture-call" />'
    )

    c, _ = client(success.replace(status, b""))
    with pytest.raises(BetdaqAccountReadOnlyError, match="exactly one ReturnStatus"):
        c.read_account_balance()

    c, _ = client(success.replace(b'Code="0"', b'Code="not-an-int"', 1))
    with pytest.raises(BetdaqAccountReadOnlyError, match="Code must be provider integer"):
        c.read_account_balance()


def test_balance_preserves_decimal_text_without_binary_float():
    c, _ = client(balance(available="100.01000000000000000001"))
    result = c.read_account_balance()
    assert result.available_funds == Decimal("100.01000000000000000001")
    assert result.balance == Decimal("120.02")
    assert result.exposure == Decimal("-20.01")
    assert result.currency == "EUR"


def test_secure_request_contains_credentials_only_in_transport_body():
    c, transport = client(balance())
    result = c.read_account_balance()
    assert result.currency == "EUR"
    body = transport.calls[0][2]
    assert b"alice" in body and b"p@ss" in body and b"app-id" in body
    assert "alice" not in repr(result.evidence)
    assert "p@ss" not in repr(result.evidence)


def test_transport_exception_text_is_not_propagated_or_logged_as_error_detail():
    c, _ = client(RuntimeError("failed with p@ss app-id"))
    with pytest.raises(BetdaqAccountReadOnlyError) as exc:
        c.read_account_balance()
    assert "p@ss" not in str(exc.value)
    assert "app-id" not in str(exc.value)


def test_soap_fault_fails_closed():
    payload = (
        f'<soap:Envelope xmlns:soap="{SOAP}"><soap:Body>'
        f"<soap:Fault><faultcode>x</faultcode></soap:Fault>"
        f"</soap:Body></soap:Envelope>"
    ).encode()
    c, _ = client(payload)
    with pytest.raises(BetdaqAccountReadOnlyError, match="SOAP Fault"):
        c.read_account_balance()


@pytest.mark.parametrize(
    "payload",
    [
        b"<not-xml",
        b'<!DOCTYPE x [<!ENTITY e "x">]><x/>',
        b'<Envelope><Body /></Envelope>',
        (
            b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
            b"<soap:Body><GetAccountBalancesResponse>"
            b"<GetAccountBalancesResult Currency='EUR' Balance='1' Exposure='0' "
            b"AvailableFunds='1' Credit='0'/></GetAccountBalancesResponse>"
            b"</soap:Body></soap:Envelope>"
        ),
    ],
)
def test_malformed_or_wrong_namespace_payload_never_becomes_success(payload):
    c, _ = client(payload)
    with pytest.raises(BetdaqAccountReadOnlyError):
        c.read_account_balance()


def test_bootstrap_freezes_first_cutoff_and_then_closes_race_with_changed_since():
    c, transport = client(
        bootstrap(10, order("1", 5)),
        bootstrap(999, order("2", 10, status=2, matched="3", unmatched="0", matched_price="2.25")),
        changed(order("1", 11, status=2, matched="5", unmatched="0", matched_price="2.50")),
        changed(),
    )
    book = c.read_complete_current_orders()
    assert book.bootstrap_cutoff_sequence == 10
    assert book.final_sequence == 11
    assert [(o.order_id, o.sequence_number) for o in book.orders] == [("2", 10), ("1", 11)]
    bodies = [call[2].decode() for call in transport.calls]
    assert "<ns1:SequenceNumber>-1</ns1:SequenceNumber>" in bodies[0] or ">-1<" in bodies[0]
    assert ">5<" in bodies[1]
    assert ">10<" in bodies[2]
    assert ">11<" in bodies[3]


def test_empty_bootstrap_still_runs_changed_since_from_first_cutoff():
    c, _ = client(
        bootstrap(8),
        changed(order("9", 9)),
        changed(),
    )
    book = c.read_complete_current_orders()
    assert book.bootstrap_cutoff_sequence == 8
    assert book.final_sequence == 9
    assert book.orders[0].order_id == "9"


def test_bootstrap_timeout_never_publishes_partial_current_order_book():
    c, _ = client(
        bootstrap(10, order("1", 5)),
        RuntimeError("network died"),
    )
    with pytest.raises(BetdaqAccountReadOnlyError, match="transport failed"):
        c.read_complete_current_orders()


def test_changed_since_failure_never_publishes_bootstrap_prefix_as_complete():
    c, _ = client(
        bootstrap(10, order("1", 10)),
        RuntimeError("network died"),
    )
    with pytest.raises(BetdaqAccountReadOnlyError, match="transport failed"):
        c.read_complete_current_orders()


def test_response_sequence_must_be_strictly_after_cursor_and_ordered():
    c, _ = client(bootstrap(10, order("1", 5), order("2", 4)))
    with pytest.raises(BetdaqAccountReadOnlyError, match="strictly ordered"):
        c.read_complete_current_orders()

    c, _ = client(bootstrap(10, order("1", 10)), changed(order("1", 10)))
    with pytest.raises(BetdaqAccountReadOnlyError, match="strictly after"):
        c.read_complete_current_orders()


def test_same_order_and_sequence_with_conflicting_content_fails_closed():
    c, _ = client(
        bootstrap(10, order("1", 10, status=1)),
        changed(order("1", 11, status=2, matched="5", unmatched="0", matched_price="2.5")),
        changed(),
    )
    assert c.read_complete_current_orders().orders[0].status_name == "MATCHED"

    c, _ = client(
        bootstrap(10, order("1", 5)),
        bootstrap(10, order("1", 5, polarity=2)),
    )
    with pytest.raises(BetdaqAccountReadOnlyError):
        c.read_complete_current_orders()


def test_terminal_order_state_cannot_regress_to_open_on_newer_sequence():
    c, _ = client(
        bootstrap(10, order("1", 10, status=4, matched="5", unmatched="0", matched_price="2.5")),
        changed(order("1", 11, status=1)),
        changed(),
    )
    with pytest.raises(BetdaqAccountReadOnlyError, match="terminal"):
        c.read_complete_current_orders()


@pytest.mark.parametrize("reopened_status", [1, 2, 6])
def test_cancelled_order_cannot_reopen_on_newer_provider_sequence(reopened_status):
    c, _ = client(
        bootstrap(
            10,
            order(
                "1",
                10,
                status=3,
                unmatched="0",
                matched="1",
                matched_price="2.5",
            ),
        ),
        changed(
            order(
                "1",
                11,
                status=reopened_status,
                unmatched="1",
                matched="1",
                matched_price="2.5",
            )
        ),
        changed(),
    )
    with pytest.raises(BetdaqAccountReadOnlyError, match="reopens a cancelled order"):
        c.read_complete_current_orders()


@pytest.mark.parametrize("later_status", [4, 5])
def test_cancelled_matched_order_may_progress_to_provider_settled_or_void(later_status):
    c, _ = client(
        bootstrap(
            10,
            order(
                "1",
                10,
                status=3,
                unmatched="0",
                matched="1",
                matched_price="2.5",
            ),
        ),
        changed(
            order(
                "1",
                11,
                status=later_status,
                unmatched="0",
                matched="1",
                matched_price="2.5",
            )
        ),
        changed(),
    )
    book = c.read_complete_current_orders()
    assert book.orders[0].status_code == later_status


@pytest.mark.parametrize("status", [0, 7, 255])
def test_unknown_order_status_fails_closed(status):
    c, _ = client(bootstrap(1, order("1", 1, status=status)))
    with pytest.raises(BetdaqAccountReadOnlyError, match="OrderStatus"):
        c.read_complete_current_orders()


@pytest.mark.parametrize("polarity", [0, 3, 255])
def test_unknown_polarity_fails_closed(polarity):
    c, _ = client(bootstrap(1, order("1", 1, polarity=polarity)))
    with pytest.raises(BetdaqAccountReadOnlyError, match="Polarity"):
        c.read_complete_current_orders()


def test_for_and_against_map_to_back_and_lay_without_aliasing_order_identity():
    c, _ = client(
        bootstrap(2, order("1", 1, polarity=1), order("2", 2, polarity=2, punter_ref="77")),
        changed(),
    )
    book = c.read_complete_current_orders()
    assert [(o.order_id, o.polarity) for o in book.orders] == [("1", "BACK"), ("2", "LAY")]
    assert len(book.orders) == 2


@pytest.mark.parametrize(
    "replacement",
    [
        'UnmatchedStake="-1"',
        'RequestedPrice="NaN"',
        'MatchedStake="Infinity"',
        'RequestedPrice="0"',
    ],
)
def test_invalid_economic_decimal_fails_before_publication(replacement):
    raw = order("1", 1)
    field = replacement.split("=")[0]
    import re
    raw = re.sub(fr'{field}="[^"]+"', replacement, raw)
    c, _ = client(bootstrap(1, raw))
    with pytest.raises(BetdaqAccountReadOnlyError):
        c.read_complete_current_orders()


def test_canonical_snapshot_includes_balance_and_complete_nonterminal_positions(
    monkeypatch,
):
    c, _ = canonical_client(
        monkeypatch,
        balance(),
        bootstrap(
            4,
            order("1", 1, status=1, unmatched="5", matched="0"),
            order("2", 2, status=2, unmatched="0", matched="3", matched_price="2.2"),
            order("3", 3, status=3, unmatched="0", matched="1", matched_price="3.1"),
            order("4", 4, status=6, unmatched="2", matched="0"),
        ),
        changed(),
    )
    evidence = c.read_account_evidence(
        frozenset({BookmakerCapability.OPEN_POSITIONS_READ, BookmakerCapability.BET_READBACK})
    )
    snapshot = evidence.snapshot
    assert snapshot.profile.adapter_id == ADAPTER_ID
    assert BookmakerCapability.BALANCE_READ in snapshot.observed_capabilities
    assert snapshot.balance.available_balance == Decimal("100.01")
    assert [p.external_position_id for p in snapshot.open_positions] == ["1", "2", "3", "4"]
    assert all(p.state is BookmakerPositionState.OPEN for p in snapshot.open_positions)
    assert [p.provider_amount for p in snapshot.open_positions] == [
        Decimal("5"),
        Decimal("3"),
        Decimal("1"),
        Decimal("2"),
    ]
    assert all(
        p.provider_amount_semantics == "betdaq_matched_plus_active_unmatched_stake"
        for p in snapshot.open_positions
    )
    assert [p.decimal_odds for p in snapshot.open_positions] == [
        Decimal("2.50"),
        Decimal("2.2"),
        Decimal("3.1"),
        Decimal("2.50"),
    ]
    assert snapshot.open_positions[0].provider_side == "BACK"


def test_partially_matched_order_preserves_live_unmatched_remainder_without_false_single_odds(
    monkeypatch,
):
    c, _ = canonical_client(
        monkeypatch,
        balance(),
        bootstrap(
            1,
            order(
                "1",
                1,
                status=1,
                unmatched="3",
                matched="2",
                matched_price="2.2",
            ),
        ),
        changed(),
    )
    evidence = c.read_account_evidence(
        frozenset({BookmakerCapability.OPEN_POSITIONS_READ})
    )

    raw = evidence.current_orders.orders[0]
    position = evidence.snapshot.open_positions[0]
    assert raw.matched_stake == Decimal("2")
    assert raw.unmatched_stake == Decimal("3")
    assert position.provider_amount == Decimal("5")
    assert (
        position.provider_amount_semantics
        == "betdaq_matched_plus_active_unmatched_stake"
    )
    assert position.decimal_odds is None


def test_active_order_amount_sum_is_exact_beyond_ambient_decimal_context_precision(
    monkeypatch,
):
    c, _ = canonical_client(
        monkeypatch,
        balance(),
        bootstrap(
            1,
            order(
                "1",
                1,
                status=1,
                unmatched="0.0000000000000000000000000009",
                matched="1234567890123456789012345678.1",
                matched_price="2.2",
            ),
        ),
        changed(),
    )
    position = c.read_account_snapshot(
        frozenset({BookmakerCapability.OPEN_POSITIONS_READ})
    ).open_positions[0]

    assert position.provider_amount == Decimal(
        "1234567890123456789012345678.1000000000000000000000000009"
    )
    assert position.decimal_odds is None


def test_cancelled_unmatched_only_order_is_not_laundered_into_open_position(
    monkeypatch,
):
    c, _ = canonical_client(
        monkeypatch,
        balance(),
        bootstrap(1, order("1", 1, status=3, unmatched="5", matched="0")),
        changed(),
    )
    snapshot = c.read_account_snapshot(
        frozenset({BookmakerCapability.OPEN_POSITIONS_READ})
    )
    assert snapshot.open_positions == ()
    assert snapshot.settled_positions == ()


def test_settled_and_void_raw_evidence_are_preserved_but_not_misreported_as_complete_settled_history(
    monkeypatch,
):
    c, _ = canonical_client(
        monkeypatch,
        balance(),
        bootstrap(
            2,
            order("1", 1, status=4, unmatched="0", matched="5", matched_price="2"),
            order("2", 2, status=5, unmatched="0", matched="5", matched_price="2"),
        ),
        changed(),
    )
    evidence = c.read_account_evidence(
        frozenset({BookmakerCapability.OPEN_POSITIONS_READ, BookmakerCapability.BET_READBACK})
    )
    assert [item.status_name for item in evidence.current_orders.orders] == ["SETTLED", "VOID"]
    assert evidence.snapshot.open_positions == ()
    assert evidence.snapshot.settled_positions == ()
    assert BookmakerCapability.SETTLED_POSITIONS_READ not in evidence.snapshot.observed_capabilities


def test_complete_settled_positions_capability_is_refused_until_a_complete_provider_source_exists():
    c, transport = client()
    with pytest.raises(BetdaqAccountReadOnlyError, match="cannot prove complete capability"):
        c.read_account_snapshot(frozenset({BookmakerCapability.SETTLED_POSITIONS_READ}))
    assert transport.calls == []


def test_repeated_punter_reference_is_correlation_only_not_dedupe_identity():
    c, _ = client(
        bootstrap(
            2,
            order("101", 1, punter_ref="9"),
            order("102", 2, punter_ref="9"),
        ),
        changed(),
    )
    book = c.read_complete_current_orders()
    assert [o.order_id for o in book.orders] == ["101", "102"]
    assert {o.punter_reference_number for o in book.orders} == {"9"}


def test_no_provider_write_methods_are_exposed():
    public = {name.lower() for name in dir(BetdaqAccountReadOnlyClient)}
    for forbidden in (
        "placeorders",
        "place_order",
        "updateorders",
        "update_order",
        "cancelorders",
        "cancel_order",
        "suspendorders",
        "suspend_order",
        "unsuspendorders",
        "unsuspend_order",
    ):
        assert forbidden not in public
