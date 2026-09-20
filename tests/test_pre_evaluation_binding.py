import json
from pathlib import Path

import pytest

from autosport.pre_evaluation_binding import (
    BoundPreEvaluationEvidenceStore,
    PreEvaluationDenominatorContext,
    ProviderMemberIdentity,
    bind_pre_evaluation_session,
)
from autosport.pre_evaluation_evidence import (
    CanonicalCandidateFacts,
    PreEvaluationEvidenceAuthority,
    PreEvaluationPolicy,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _facts(row_key: str) -> CanonicalCandidateFacts:
    return CanonicalCandidateFacts(
        candidate_id=row_key,
        observed_at_ns=900,
        config_enabled=True,
        risk_required_micros=10,
        risk_available_micros=10,
        cost_estimate_micros=4,
        cost_limit_micros=4,
        source_authority_id=f"canonical:{row_key}",
        source_revision="rev-1",
    )


def _evidence(*row_keys: str):
    authority = PreEvaluationEvidenceAuthority(PreEvaluationPolicy(max_age_ns=200))
    by_key = {row_key: _facts(row_key) for row_key in row_keys}
    return authority.evaluate_session(
        session_id="session-1",
        candidate_ids=row_keys,
        resolver=by_key.get,
        evaluated_at_ns=1000,
    )


def _context(
    *,
    campaign_id: str = "campaign-1",
    provider_sha: str = SHA_B,
) -> PreEvaluationDenominatorContext:
    return PreEvaluationDenominatorContext(
        session_id="session-1",
        campaign_id=campaign_id,
        research_protocol_id="protocol-1",
        protocol_sha256=SHA_A,
        provider_evidence_sha256=provider_sha,
    )


def _members() -> tuple[ProviderMemberIdentity, ...]:
    return (
        ProviderMemberIdentity(row_key="row-a", member_sha256=SHA_C),
        ProviderMemberIdentity(row_key="row-b", member_sha256=SHA_D),
    )


def test_binding_owns_exact_context_member_set_and_per_row_authority() -> None:
    evidence = _evidence("row-b", "row-a")
    bound = bind_pre_evaluation_session(
        evidence,
        context=_context(),
        provider_members=reversed(_members()),
    )

    assert tuple(member.row_key for member in bound.members) == ("row-a", "row-b")
    assert bound.resolve_slot("row-a").candidate_id == "row-a"
    assert bound.context.provider_evidence_sha256 == SHA_B
    assert bound.evidence.authority_digest == evidence.authority_digest
    assert bound.member_authority_sha256 == (
        ("row-a", bound.member_authority_digest("row-a")),
        ("row-b", bound.member_authority_digest("row-b")),
    )
    assert bound.member_authority_digest("row-a") != bound.member_authority_digest(
        "row-b"
    )


def test_binding_rejects_missing_extra_duplicate_and_reassigned_members() -> None:
    evidence = _evidence("row-a", "row-b")

    with pytest.raises(ValueError, match="exactly equal"):
        bind_pre_evaluation_session(
            evidence,
            context=_context(),
            provider_members=(_members()[0],),
        )

    with pytest.raises(ValueError, match="exactly equal"):
        bind_pre_evaluation_session(
            evidence,
            context=_context(),
            provider_members=(
                *_members(),
                ProviderMemberIdentity(row_key="row-c", member_sha256=SHA_A),
            ),
        )

    with pytest.raises(ValueError, match="duplicate provider row_key"):
        bind_pre_evaluation_session(
            evidence,
            context=_context(),
            provider_members=(_members()[0], _members()[0]),
        )

    reassigned = (
        ProviderMemberIdentity(row_key="row-a", member_sha256=SHA_D),
        ProviderMemberIdentity(row_key="row-b", member_sha256=SHA_C),
    )
    original = bind_pre_evaluation_session(
        evidence,
        context=_context(),
        provider_members=_members(),
    )
    changed = bind_pre_evaluation_session(
        evidence,
        context=_context(),
        provider_members=reassigned,
    )
    assert original.authority_digest != changed.authority_digest
    assert original.member_authority_digest("row-a") != changed.member_authority_digest(
        "row-a"
    )


def test_durable_binding_is_immutable_and_context_replay_fails_closed(
    tmp_path: Path,
) -> None:
    evidence = _evidence("row-a", "row-b")
    bound = bind_pre_evaluation_session(
        evidence,
        context=_context(),
        provider_members=_members(),
    )
    store = BoundPreEvaluationEvidenceStore(tmp_path / "binding.json")
    store.save(bound)
    store.save(bound)

    loaded = store.load_expected(context=_context(), provider_members=_members())
    assert loaded == bound
    assert loaded.authority_digest == bound.authority_digest

    with pytest.raises(ValueError, match="context mismatch"):
        store.load_expected(
            context=_context(campaign_id="campaign-2"),
            provider_members=_members(),
        )

    conflicting = bind_pre_evaluation_session(
        evidence,
        context=_context(campaign_id="campaign-2"),
        provider_members=_members(),
    )
    with pytest.raises(ValueError, match="conflicting authority"):
        store.save(conflicting)


def test_durable_binding_rejects_provider_identity_replay_and_tamper(
    tmp_path: Path,
) -> None:
    evidence = _evidence("row-a", "row-b")
    bound = bind_pre_evaluation_session(
        evidence,
        context=_context(),
        provider_members=_members(),
    )
    store = BoundPreEvaluationEvidenceStore(tmp_path / "binding.json")
    store.save(bound)

    wrong_members = (
        ProviderMemberIdentity(row_key="row-a", member_sha256=SHA_A),
        _members()[1],
    )
    with pytest.raises(ValueError, match="provider member identity mismatch"):
        store.load_expected(context=_context(), provider_members=wrong_members)

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["payload"]["context"]["provider_evidence_sha256"] = SHA_A
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        store.load()


def test_member_digest_binds_session_authority_and_provider_context() -> None:
    left = bind_pre_evaluation_session(
        _evidence("row-a", "row-b"),
        context=_context(provider_sha=SHA_B),
        provider_members=_members(),
    )
    right = bind_pre_evaluation_session(
        _evidence("row-a", "row-b"),
        context=_context(provider_sha=SHA_C),
        provider_members=_members(),
    )

    assert left.evidence.authority_digest == right.evidence.authority_digest
    assert left.member_authority_digest("row-a") != right.member_authority_digest(
        "row-a"
    )
    assert left.authority_digest != right.authority_digest
