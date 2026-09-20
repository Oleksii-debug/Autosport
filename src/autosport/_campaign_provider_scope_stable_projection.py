"""Preserve immutable T0 campaign/provider scope across later source re-verification.

The canonical #660 resolver may reacquire authenticated provider evidence after a
restart.  That later T1 evidence is validation of the already-issued T0
applicability scope; it must not replace the T0 projection and silently mint a new
applicability digest merely because provider evidence ids/timestamps changed.
"""
from __future__ import annotations

from weakref import ref

from . import campaign_provider_scope_authority as scope
from . import _campaign_provider_scope_devapp_identity as _devapp  # noqa: F401


_RAW_RESOLVE = scope.resolve_campaign_provider_scope

# These fields describe the later provider evidence instance, not the frozen T0
# campaign/action/account/market applicability identity.  They may legitimately
# change when the same durable external effect is re-read after restart.
_REVERIFICATION_FIELDS = frozenset(
    {
        "provider_capture_sha256",
        "provider_evidence_id",
        "provider_source_sha256",
        "source_interval_start",
        "source_interval_end",
        "observed_at",
        "available_at",
    }
)


def _stable_payload(projection: scope.CampaignProviderScopeProjection) -> dict[str, object]:
    payload = projection.payload()
    return {
        key: value
        for key, value in payload.items()
        if key not in _REVERIFICATION_FIELDS
    }


def _validate_original_t0_projection(
    original: scope.CampaignProviderScopeProjection,
    current: scope.CampaignProviderScopeProjection,
    *,
    expected_applicability_digest: str,
    execution_ledger,
    attempt_id: str,
) -> None:
    if type(original) is not scope.CampaignProviderScopeProjection:
        raise scope.CampaignProviderScopeError(
            "restart re-verification requires exact original T0 provider scope projection"
        )
    expected = scope._sha(
        expected_applicability_digest,
        "expected_applicability_digest",
    )
    if original.applicability_digest != expected:
        raise scope.CampaignProviderScopeError(
            "original T0 provider scope digest does not match durable expected digest"
        )
    if _stable_payload(original) != _stable_payload(current):
        raise scope.CampaignProviderScopeError(
            "provider re-verification changed immutable T0 applicability scope"
        )

    # The old projection must still identify the provider evidence that was durably
    # bound to the execution attempt at T0.  This prevents a caller from pairing a
    # self-consistent forged old projection with its own digest.
    binding = execution_ledger.provider_evidence_binding(attempt_id)
    if binding is None:
        raise scope.CampaignProviderScopeError(
            "execution attempt lacks durable provider evidence binding"
        )
    if binding.get("evidence_id") != original.provider_evidence_id:
        raise scope.CampaignProviderScopeError(
            "original T0 provider scope evidence is not durable attempt authority"
        )
    if scope._canonical_instant(
        binding.get("observed_at"),
        "provider binding observed_at",
    ) != scope._canonical_instant(
        original.observed_at,
        "original provider scope observed_at",
    ):
        raise scope.CampaignProviderScopeError(
            "original T0 provider scope time is not durable attempt authority"
        )
    if scope._instant(current.available_at, "current provider scope available_at") < scope._instant(
        original.available_at,
        "original provider scope available_at",
    ):
        raise scope.CampaignProviderScopeError(
            "provider re-verification predates original T0 applicability evidence"
        )



def _install_stable_projection_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}

    def register(projection: scope.CampaignProviderScopeProjection) -> scope.CampaignProviderScopeProjection:
        key = id(projection)

        def forget(_weakref: object, *, projection_key: int = key) -> None:
            issued.pop(projection_key, None)

        issued[key] = (ref(projection, forget), projection.applicability_digest)
        return projection

    def authoritative_resolve(
        authority,
        *,
        session_id: str,
        execution_ledger,
        plan_id: str,
        attempt_id: str,
        capture,
        expected_applicability_digest: str | None = None,
        expected_projection: scope.CampaignProviderScopeProjection | None = None,
    ) -> scope.CampaignProviderScopeProjection:
        if expected_projection is not None and expected_applicability_digest is None:
            raise scope.CampaignProviderScopeError(
                "original T0 projection requires its durable applicability digest"
            )

        # Always let the canonical restart-safe resolver validate the fresh T1
        # capture against current campaign/execution/provider authority.  Do not pass
        # the old digest into it: a legitimate later evidence instance is expected to
        # have different capture/evidence/timestamp fields.
        current = _RAW_RESOLVE(
            authority,
            session_id=session_id,
            execution_ledger=execution_ledger,
            plan_id=plan_id,
            attempt_id=attempt_id,
            capture=capture,
            expected_applicability_digest=None,
        )

        if expected_applicability_digest is None:
            return register(current)

        expected = scope._sha(
            expected_applicability_digest,
            "expected_applicability_digest",
        )
        if expected_projection is None:
            # Backwards-compatible exact re-resolution is safe only when the fresh
            # projection is byte-semantically identical.  A changed T1 evidence
            # instance cannot mint a replacement projection from a digest alone.
            if current.applicability_digest != expected:
                raise scope.CampaignProviderScopeError(
                    "later provider re-verification cannot replace immutable T0 projection; "
                    "original projection is required"
                )
            return register(current)

        _validate_original_t0_projection(
            expected_projection,
            current,
            expected_applicability_digest=expected,
            execution_ledger=execution_ledger,
            attempt_id=attempt_id,
        )
        # T1 is append-only verification.  Return the exact T0 authority object and
        # re-issue it in this process only after all fresh source checks passed.
        return register(expected_projection)

    def assert_authoritative(projection: scope.CampaignProviderScopeProjection) -> None:
        if type(projection) is not scope.CampaignProviderScopeProjection:
            raise scope.CampaignProviderScopeError(
                "campaign provider scope projection type is not canonical"
            )
        record = issued.get(id(projection))
        if record is None or record[0]() is not projection:
            raise scope.CampaignProviderScopeError(
                "campaign provider scope projection was not issued by canonical resolver"
            )
        if record[1] != projection.applicability_digest:
            raise scope.CampaignProviderScopeError(
                "campaign provider scope projection changed after resolution"
            )

    scope.resolve_campaign_provider_scope = authoritative_resolve
    scope.assert_campaign_provider_scope_authoritative = assert_authoritative


_install_stable_projection_authority()
del _install_stable_projection_authority
