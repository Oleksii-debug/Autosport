from __future__ import annotations

import pytest

from autosport.matchbook_api_group_authority import (
    DEFAULT_ENDPOINT_SPECS,
    EndpointGroupRegistry,
    EndpointSpec,
    MatchbookApiGroup,
    MatchbookApiGroupAuthorityError,
)


def _canonical(operation_id: str) -> EndpointSpec:
    return next(spec for spec in DEFAULT_ENDPOINT_SPECS if spec.operation_id == operation_id)


@pytest.mark.parametrize(
    "forged",
    [
        # Keep the exact documented method/path/source identity but relabel the
        # canonical write operation as ACCOUNT.  A caller-controlled registry
        # must not be able to mint the canonical operation_id with altered
        # authority semantics.
        EndpointSpec(
            "matchbook.offers.submit",
            "POST",
            "/edge/rest/v2/offers",
            MatchbookApiGroup.ACCOUNT,
            "https://developers.matchbook.com/reference/submit-offers-v2",
        ),
        # Rebind the same canonical operation identity onto a completely
        # different documented-looking request surface.
        EndpointSpec(
            "matchbook.offers.submit",
            "GET",
            "/edge/rest/events",
            MatchbookApiGroup.EVENTS,
            "https://developers.matchbook.com/reference/submit-offers-v2",
        ),
    ],
)
def test_caller_registry_cannot_rebind_canonical_operation_identity(
    forged: EndpointSpec,
) -> None:
    canonical = _canonical("matchbook.offers.submit")

    # Either construction or classification may be the fail-closed boundary.
    # If both accept the caller-created registry, any receipt carrying a
    # canonical operation_id must still resolve to the exact canonical record.
    try:
        registry = EndpointGroupRegistry((forged,))
    except MatchbookApiGroupAuthorityError:
        return

    try:
        receipt = registry.classify(
            method=forged.method,
            path=forged.path_template,
        )
    except MatchbookApiGroupAuthorityError:
        return

    assert receipt.operation_id == canonical.operation_id
    assert receipt.method == canonical.method
    assert receipt.path_template == canonical.path_template
    assert receipt.group is canonical.group
    assert receipt.source_url == canonical.source_url
    assert receipt.evidence_as_of == canonical.evidence_as_of
