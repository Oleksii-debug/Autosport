from dataclasses import replace

import pytest

from autosport.betfair_api_routing import (
    GLOBAL_ACCOUNTS_JSON_RPC_ENDPOINT,
    GLOBAL_BETTING_JSON_RPC_ENDPOINT,
    NEW_ZEALAND_ACCOUNTS_JSON_RPC_ENDPOINT,
    NEW_ZEALAND_BETTING_JSON_RPC_ENDPOINT,
    ROUTING_SCHEMA_VERSION,
    BetfairApiRouteClass,
    BetfairApiRouteContract,
    BetfairApiRouteResolutionStatus,
    BetfairApiRoutingError,
    BetfairApiService,
    documented_api_contract,
    require_endpoint_matches_contract,
    routing_contract_for_login_jurisdiction,
)
from autosport.betfair_session_origin import BetfairLoginJurisdiction


def test_documented_route_table_binds_betting_and_accounts_together():
    global_contract = documented_api_contract(BetfairApiRouteClass.GLOBAL)
    nz_contract = documented_api_contract(BetfairApiRouteClass.NEW_ZEALAND)

    assert (
        global_contract.betting_json_rpc_endpoint
        == GLOBAL_BETTING_JSON_RPC_ENDPOINT
    )
    assert (
        global_contract.accounts_json_rpc_endpoint
        == GLOBAL_ACCOUNTS_JSON_RPC_ENDPOINT
    )
    assert (
        nz_contract.betting_json_rpc_endpoint
        == NEW_ZEALAND_BETTING_JSON_RPC_ENDPOINT
    )
    assert (
        nz_contract.accounts_json_rpc_endpoint
        == NEW_ZEALAND_ACCOUNTS_JSON_RPC_ENDPOINT
    )
    assert ".com.au/" not in global_contract.betting_json_rpc_endpoint
    assert ".com.au/" in nz_contract.betting_json_rpc_endpoint


@pytest.mark.parametrize(
    "jurisdiction",
    (
        BetfairLoginJurisdiction.GLOBAL_COM,
        BetfairLoginJurisdiction.ITALY,
        BetfairLoginJurisdiction.SPAIN,
        BetfairLoginJurisdiction.ROMANIA,
    ),
)
def test_jurisdiction_specific_login_hosts_do_not_suffix_route_api(
    jurisdiction,
):
    resolution = routing_contract_for_login_jurisdiction(jurisdiction)

    assert (
        resolution.status
        is BetfairApiRouteResolutionStatus.RESOLVED_CONTRACT
    )
    assert resolution.contract is not None
    assert resolution.contract.route_class is BetfairApiRouteClass.GLOBAL
    assert (
        resolution.contract.betting_json_rpc_endpoint
        == "https://api.betfair.com/exchange/betting/json-rpc/v1"
    )
    assert (
        resolution.contract.accounts_json_rpc_endpoint
        == "https://api.betfair.com/exchange/account/json-rpc/v1"
    )


def test_au_nz_login_origin_does_not_guess_new_zealand_country():
    resolution = routing_contract_for_login_jurisdiction(
        BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND
    )

    assert (
        resolution.status
        is BetfairApiRouteResolutionStatus.COUNTRY_AUTHORITY_REQUIRED
    )
    assert resolution.contract is None
    assert resolution.authoritative is False
    assert resolution.execution_authorized is False


def test_caller_locale_currency_or_region_cannot_select_new_zealand_route():
    with pytest.raises(TypeError):
        routing_contract_for_login_jurisdiction(
            BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND,
            currency_code="NZD",  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        routing_contract_for_login_jurisdiction(
            BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND,
            locale="en_NZ",  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        routing_contract_for_login_jurisdiction(
            BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND,
            region="NZL",  # type: ignore[call-arg]
        )


def test_route_contract_is_copyable_but_never_authorizing():
    contract = documented_api_contract(BetfairApiRouteClass.GLOBAL)
    copied = replace(contract)

    assert copied == contract
    assert copied.route_id == contract.route_id
    for value in (contract, copied):
        assert value.authoritative is False
        assert value.provider_io_authorized is False
        assert value.provider_write_authorized is False
        assert value.execution_authorized is False


def test_constructor_cannot_relabel_route_class_to_different_endpoint_pair():
    with pytest.raises(BetfairApiRoutingError, match="explicit route table"):
        BetfairApiRouteContract(
            route_class=BetfairApiRouteClass.NEW_ZEALAND,
            betting_json_rpc_endpoint=GLOBAL_BETTING_JSON_RPC_ENDPOINT,
            accounts_json_rpc_endpoint=GLOBAL_ACCOUNTS_JSON_RPC_ENDPOINT,
        )


def test_endpoint_guard_fails_closed_for_wrong_host_in_both_directions():
    global_contract = documented_api_contract(BetfairApiRouteClass.GLOBAL)
    nz_contract = documented_api_contract(BetfairApiRouteClass.NEW_ZEALAND)

    with pytest.raises(BetfairApiRoutingError, match="does not match"):
        require_endpoint_matches_contract(
            global_contract,
            BetfairApiService.BETTING,
            NEW_ZEALAND_BETTING_JSON_RPC_ENDPOINT,
        )
    with pytest.raises(BetfairApiRoutingError, match="does not match"):
        require_endpoint_matches_contract(
            nz_contract,
            BetfairApiService.ACCOUNTS,
            GLOBAL_ACCOUNTS_JSON_RPC_ENDPOINT,
        )

    assert require_endpoint_matches_contract(
        global_contract,
        BetfairApiService.ACCOUNTS,
        GLOBAL_ACCOUNTS_JSON_RPC_ENDPOINT,
    ) == GLOBAL_ACCOUNTS_JSON_RPC_ENDPOINT


def test_route_identity_binds_version_class_and_both_endpoints():
    global_contract = documented_api_contract(BetfairApiRouteClass.GLOBAL)
    nz_contract = documented_api_contract(BetfairApiRouteClass.NEW_ZEALAND)

    assert ROUTING_SCHEMA_VERSION == 1
    assert len(global_contract.route_id) == 64
    assert len(nz_contract.route_id) == 64
    assert global_contract.route_id != nz_contract.route_id


def test_non_enum_inputs_fail_closed_instead_of_string_coercion():
    with pytest.raises(BetfairApiRoutingError, match="exact BetfairLoginJurisdiction"):
        routing_contract_for_login_jurisdiction("SPAIN")  # type: ignore[arg-type]
    with pytest.raises(BetfairApiRoutingError, match="exact BetfairApiRouteClass"):
        documented_api_contract("NEW_ZEALAND")  # type: ignore[arg-type]

    contract = documented_api_contract(BetfairApiRouteClass.GLOBAL)
    with pytest.raises(BetfairApiRoutingError, match="exact BetfairApiService"):
        contract.endpoint_for("BETTING")  # type: ignore[arg-type]


def test_resolution_objects_are_descriptive_not_authoritative():
    resolved = routing_contract_for_login_jurisdiction(
        BetfairLoginJurisdiction.SPAIN
    )

    assert resolved.authoritative is False
    assert resolved.execution_authorized is False
    assert resolved.contract is not None
    assert resolved.contract.authoritative is False
