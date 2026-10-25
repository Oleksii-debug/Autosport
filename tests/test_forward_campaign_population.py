from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.forward_campaign_population import (
    ForwardCampaignPopulationError,
    ForwardCampaignPopulationManifest,
    ForwardPopulationSample,
    PopulationRole,
    TerminalEvidenceState,
    build_forward_campaign_manifest,
    build_forward_campaign_session,
    verify_forward_campaign_restart,
)
from autosport.forward_evidence_completeness import DecisionState


PROTOCOL = "a" * 64
RIGHTS = "b" * 64


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sample(
    ordinal: int,
    *,
    role: PopulationRole,
    session: int,
    action: bool = True,
    event_id: str | None = None,
    source_evidence_sha256: str | None = None,
    cutoff_offset: int = 2,
) -> ForwardPopulationSample:
    base = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc) + timedelta(
        days=session
    )
    observed = base + timedelta(minutes=ordinal % 40)
    decided = observed + timedelta(minutes=3)
    decision_state = DecisionState.ACTION if action else DecisionState.NO_BET
    terminal = (
        TerminalEvidenceState.SETTLED if action else TerminalEvidenceState.NEGATIVE
    )
    return ForwardPopulationSample(
        sample_id=f"sample-{ordinal}",
        session_id=f"session-{session}",
        role=role,
        source_id="parlayapi:table_tennis",
        event_id=event_id or f"event-{ordinal}",
        regime_id="table-tennis:pre-match" if ordinal % 2 else "table-tennis:live",
        source_evidence_sha256=source_evidence_sha256 or _sha(f"source-{ordinal}"),
        source_rights_manifest_sha256=RIGHTS,
        observed_at=_ts(observed),
        available_at=_ts(observed + timedelta(minutes=1)),
        decision_cutoff=_ts(observed + timedelta(minutes=cutoff_offset)),
        decided_at=_ts(decided),
        outcome_available_at=_ts(decided + timedelta(minutes=5)),
        settled_at=_ts(decided + timedelta(minutes=6)),
        decision_state=decision_state,
        terminal_state=terminal,
        terminal_evidence_sha256=_sha(f"terminal-{ordinal}"),
        observed_reward=Decimal("1.25") if action else None,
    )


def _campaign():
    samples = (
        _sample(1, role=PopulationRole.NEW, session=1),
        _sample(2, role=PopulationRole.REPLAY, session=1, action=False),
        _sample(3, role=PopulationRole.RETENTION, session=2),
        _sample(4, role=PopulationRole.HOLDOUT, session=2, action=False),
    )
    first_samples = tuple(value for value in samples if value.session_id == "session-1")
    second_samples = tuple(value for value in samples if value.session_id == "session-2")
    first = build_forward_campaign_session(
        session_id="session-1",
        session_index=1,
        opened_at="2026-10-02T08:00:00Z",
        closed_at="2026-10-02T09:00:00Z",
        samples=first_samples,
        drift_evidence_sha256=_sha("drift-1"),
    )
    second = build_forward_campaign_session(
        session_id="session-2",
        session_index=2,
        opened_at="2026-10-03T08:00:00Z",
        closed_at="2026-10-03T09:00:00Z",
        samples=second_samples,
        drift_evidence_sha256=_sha("drift-2"),
        predecessor_session_sha256=first.session_sha256,
    )
    manifest = build_forward_campaign_manifest(
        campaign_id="account-c-forward-1",
        scientific_protocol_sha256=PROTOCOL,
        created_at="2026-10-03T09:01:00Z",
        samples=samples,
        sessions=(first, second),
    )
    return manifest, samples, first, second


def test_multisession_manifest_is_restart_exact_and_never_claims_edge() -> None:
    manifest, _, _, _ = _campaign()

    assert verify_forward_campaign_restart(manifest)
    restored = ForwardCampaignPopulationManifest.from_payload(
        json.loads(json.dumps(manifest.canonical_payload()))
    )
    assert restored == manifest
    assert restored.manifest_sha256 == manifest.manifest_sha256
    assert restored.profitability_claim is False
    assert restored.promotion_authorized is False
    assert restored.real_money_execution is False
    assert {sample.role for sample in restored.samples} == set(PopulationRole)
    assert restored.sessions[0].regime_metrics
    assert restored.sessions[0].negative_evidence_sha256
    assert restored.sessions[0].drift_evidence_sha256 == _sha("drift-1")


def test_outcome_must_be_unavailable_until_strictly_after_decision() -> None:
    sample = _sample(10, role=PopulationRole.NEW, session=1)
    with pytest.raises(ForwardCampaignPopulationError, match="strictly after"):
        replace(sample, outcome_available_at=sample.decided_at)


def test_source_data_must_be_available_before_frozen_cutoff() -> None:
    sample = _sample(11, role=PopulationRole.NEW, session=1)
    with pytest.raises(ForwardCampaignPopulationError, match="causal order"):
        replace(sample, available_at="2026-10-02T08:10:00Z")


def test_non_action_is_retained_as_negative_evidence_without_reward() -> None:
    sample = _sample(12, role=PopulationRole.NEW, session=1, action=False)
    assert sample.terminal_state is TerminalEvidenceState.NEGATIVE
    with pytest.raises(ForwardCampaignPopulationError, match="negative evidence"):
        replace(sample, terminal_state=TerminalEvidenceState.SETTLED)
    with pytest.raises(ForwardCampaignPopulationError, match="cannot invent"):
        replace(sample, observed_reward=Decimal("0"))


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("sample_id", "sample identity overlaps"),
        ("event_id", "event identity overlaps"),
        ("source_evidence_sha256", "source evidence identity overlaps"),
    ),
)
def test_population_firewall_rejects_cross_role_overlap(field: str, message: str) -> None:
    manifest, samples, first, second = _campaign()
    altered = list(samples)
    altered[3] = replace(altered[3], **{field: getattr(altered[0], field)})

    with pytest.raises(ForwardCampaignPopulationError, match=message):
        build_forward_campaign_manifest(
            campaign_id=manifest.campaign_id,
            scientific_protocol_sha256=PROTOCOL,
            created_at=manifest.created_at,
            samples=tuple(altered),
            sessions=(first, second),
        )


def test_every_population_role_is_independently_present() -> None:
    manifest, samples, first, second = _campaign()
    without_holdout = tuple(
        sample for sample in samples if sample.role is not PopulationRole.HOLDOUT
    )
    with pytest.raises(ForwardCampaignPopulationError, match="roles are incomplete"):
        build_forward_campaign_manifest(
            campaign_id=manifest.campaign_id,
            scientific_protocol_sha256=PROTOCOL,
            created_at=manifest.created_at,
            samples=without_holdout,
            sessions=(first, second),
        )


def test_session_rebuild_detects_negative_or_regime_metric_tampering() -> None:
    manifest, samples, first, second = _campaign()
    tampered_first = replace(first, negative_evidence_sha256="c" * 64)
    with pytest.raises(ForwardCampaignPopulationError, match="metrics or negative"):
        build_forward_campaign_manifest(
            campaign_id=manifest.campaign_id,
            scientific_protocol_sha256=PROTOCOL,
            created_at=manifest.created_at,
            samples=samples,
            sessions=(tampered_first, second),
        )


def test_session_chain_and_temporal_order_are_immutable() -> None:
    manifest, samples, first, second = _campaign()
    broken = replace(second, predecessor_session_sha256="d" * 64)
    with pytest.raises(ForwardCampaignPopulationError, match="hash chain"):
        build_forward_campaign_manifest(
            campaign_id=manifest.campaign_id,
            scientific_protocol_sha256=PROTOCOL,
            created_at=manifest.created_at,
            samples=samples,
            sessions=(first, broken),
        )


def test_decision_cutoff_is_cryptographically_bound() -> None:
    early = _sample(20, role=PopulationRole.NEW, session=1, cutoff_offset=1)
    later = _sample(20, role=PopulationRole.NEW, session=1, cutoff_offset=2)
    assert early.sample_sha256 != later.sample_sha256


def test_payload_tamper_is_rejected_before_restart_claim() -> None:
    manifest, _, _, _ = _campaign()
    payload = manifest.canonical_payload()
    payload["samples"][0]["decision_cutoff"] = "2026-10-02T12:00:00Z"
    with pytest.raises(ForwardCampaignPopulationError):
        ForwardCampaignPopulationManifest.from_payload(payload)


@pytest.mark.parametrize(
    "claim",
    ("profitability_claim", "promotion_authorized", "real_money_execution"),
)
def test_paper_shadow_manifest_cannot_inflate_authority(claim: str) -> None:
    manifest, _, _, _ = _campaign()
    with pytest.raises(ForwardCampaignPopulationError, match=claim):
        replace(manifest, **{claim: True})


def test_bounded_multisession_endurance_is_deterministic() -> None:
    samples: list[ForwardPopulationSample] = []
    sessions = []
    predecessor = "GENESIS"
    roles = tuple(PopulationRole)
    ordinal = 1000
    for session_index in range(1, 41):
        session_samples = []
        for item in range(20):
            ordinal += 1
            sample = _sample(
                ordinal,
                role=roles[item % len(roles)],
                session=session_index,
                action=item % 3 != 0,
            )
            session_samples.append(sample)
            samples.append(sample)
        day = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(days=session_index)
        session = build_forward_campaign_session(
            session_id=f"session-{session_index}",
            session_index=session_index,
            opened_at=_ts(day.replace(hour=8)),
            closed_at=_ts(day.replace(hour=10)),
            samples=tuple(session_samples),
            drift_evidence_sha256=_sha(f"drift-{session_index}"),
            predecessor_session_sha256=predecessor,
        )
        sessions.append(session)
        predecessor = session.session_sha256

    first = build_forward_campaign_manifest(
        campaign_id="account-c-endurance",
        scientific_protocol_sha256=PROTOCOL,
        created_at="2026-12-01T00:00:00Z",
        samples=tuple(samples),
        sessions=tuple(sessions),
    )
    second = ForwardCampaignPopulationManifest.from_payload(first.canonical_payload())

    assert len(first.samples) == 800
    assert len(first.sessions) == 40
    assert second.manifest_sha256 == first.manifest_sha256
    assert verify_forward_campaign_restart(second)
