from __future__ import annotations
import json
import shutil
from pathlib import Path
import pytest
from autosport.campaign_inception import AUTHORITY_DOMAIN, CampaignAuthorityBinding, CampaignInceptionConflictError, CampaignInceptionIntegrityError, CampaignInceptionReceipt, CampaignObservationNotProspectiveError, ObservationAdmissionFrontier, ResolvedObservationPosition, admit_campaign_observation, begin_campaign_inception, finalize_campaign_inception, load_campaign_inception
from autosport.monotonic_workspace_authority import AuthorityPhase, MonotonicAuthorityRollbackError, MonotonicWorkspaceAuthority
from autosport.workspace_lock import WorkspaceEconomicLock
SHA_A = 'a' * 64
SHA_B = 'b' * 64
SHA_C = 'c' * 64
SHA_D = 'd' * 64
SHA_E = 'e' * 64
SHA_F = 'f' * 64

def _binding(*, campaign_id: str='campaign-1', manifest_sha256: str=SHA_A, universe_sha256: str=SHA_B):
    return CampaignAuthorityBinding(campaign_id=campaign_id, manifest_id='manifest-1', manifest_sha256=manifest_sha256, universe_receipt_id='universe-1', universe_receipt_sha256=universe_sha256, required_source_identities=('provider:a', 'provider:b'))

class _Source:

    def __init__(self, *, source_identity: str, frontier_position: int, authority_identity_sha256: str, generation: str='epoch-1', workspace: Path | None=None, authority_root: Path | None=None, campaign_id: str='campaign-1') -> None:
        self.source_identity = source_identity
        self.frontier_position = frontier_position
        self.authority_identity_sha256 = authority_identity_sha256
        self.generation = generation
        self.workspace = workspace
        self.authority_root = authority_root
        self.campaign_id = campaign_id
        self.capture_calls = 0
        self.resolved: dict[str, ResolvedObservationPosition] = {}

    def capture_campaign_frontier(self) -> ObservationAdmissionFrontier:
        self.capture_calls += 1
        if self.workspace is not None:
            # A canonical source is allowed to use the ordinary workspace writer
            # lock while issuing its durable frontier. Campaign inception must not
            # hold that lock across this external source call.
            with WorkspaceEconomicLock(self.workspace):
                pass
            authority = MonotonicWorkspaceAuthority(workspace=self.workspace, domain=AUTHORITY_DOMAIN, key=f'campaign:{self.campaign_id}', authority_root=self.authority_root)
            history = authority.read_history()
            assert history
            assert history[-1].phase is AuthorityPhase.COMMIT
            assert history[-1].generation >= 1
        return ObservationAdmissionFrontier(source_identity=self.source_identity, authority_kind='test-canonical-journal', authority_version='v1', authority_identity_sha256=self.authority_identity_sha256, source_generation=self.generation, high_water_position=self.frontier_position, high_water_commitment_sha256=SHA_D)

    def resolve_campaign_observation(self, observation_id: str) -> ResolvedObservationPosition:
        return self.resolved[observation_id]

    def add(self, observation_id: str, position: int, *, source_identity: str | None=None, generation: str | None=None, authority_identity_sha256: str | None=None) -> None:
        self.resolved[observation_id] = ResolvedObservationPosition(observation_id=observation_id, source_identity=source_identity or self.source_identity, authority_kind='test-canonical-journal', authority_version='v1', authority_identity_sha256=authority_identity_sha256 or self.authority_identity_sha256, source_generation=generation or self.generation, durable_position=position, observation_commitment_sha256=SHA_E)

def _sources(workspace: Path, authority_root: Path):
    return {'provider:a': _Source(source_identity='provider:a', frontier_position=100, authority_identity_sha256=SHA_C, workspace=workspace, authority_root=authority_root), 'provider:b': _Source(source_identity='provider:b', frontier_position=50, authority_identity_sha256=SHA_F, workspace=workspace, authority_root=authority_root)}

def _finalized(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    authority_root = tmp_path / 'machine'
    binding = _binding()
    anchor = begin_campaign_inception(workspace=workspace, binding=binding, authority_root=authority_root)
    sources = _sources(workspace, authority_root)
    receipt = finalize_campaign_inception(workspace=workspace, binding=binding, source_authorities=sources, authority_root=authority_root)
    return (workspace, authority_root, binding, anchor, sources, receipt)

def test_anchor_is_committed_before_any_frontier_capture(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    authority_root = tmp_path / 'machine'
    binding = _binding()
    anchor = begin_campaign_inception(workspace=workspace, binding=binding, authority_root=authority_root)
    assert anchor.authority_generation >= 1
    sources = _sources(workspace, authority_root)
    receipt = finalize_campaign_inception(workspace=workspace, binding=binding, source_authorities=sources, authority_root=authority_root)
    assert isinstance(receipt, CampaignInceptionReceipt)
    assert receipt.anchor_generation == anchor.authority_generation
    assert receipt.authority_generation > receipt.anchor_generation
    assert sources['provider:a'].capture_calls == 1
    assert sources['provider:b'].capture_calls == 1

def test_historical_equal_and_post_frontier_positions_are_strict(tmp_path: Path) -> None:
    _workspace, _root, _binding_value, _anchor, sources, receipt = _finalized(tmp_path)
    source = sources['provider:a']
    source.add('historical', 99)
    source.add('equal', 100)
    source.add('new', 101)
    with pytest.raises(CampaignObservationNotProspectiveError, match='strictly beyond'):
        admit_campaign_observation(receipt=receipt, observation_id='historical', source_authority=source)
    with pytest.raises(CampaignObservationNotProspectiveError, match='strictly beyond'):
        admit_campaign_observation(receipt=receipt, observation_id='equal', source_authority=source)
    admitted = admit_campaign_observation(receipt=receipt, observation_id='new', source_authority=source)
    assert admitted.durable_position == 101

def test_restart_reopens_exact_receipt_without_recapturing_frontier(tmp_path: Path) -> None:
    workspace, authority_root, binding, _anchor, sources, receipt = _finalized(tmp_path)
    reopened = load_campaign_inception(workspace=workspace, binding=binding, authority_root=authority_root)
    repeated_finalize = finalize_campaign_inception(workspace=workspace, binding=binding, source_authorities=sources, authority_root=authority_root)
    assert reopened == receipt
    assert repeated_finalize == receipt
    assert sources['provider:a'].capture_calls == 1
    assert sources['provider:b'].capture_calls == 1

def test_deleted_local_receipt_cannot_bootstrap_a_new_prospective_boundary(tmp_path: Path) -> None:
    workspace, authority_root, binding, _anchor, _sources_value, _receipt = _finalized(tmp_path)
    state_files = tuple((workspace / 'campaign-inception-v1').glob('*.json'))
    assert len(state_files) == 1
    state_files[0].unlink()
    with pytest.raises(MonotonicAuthorityRollbackError):
        begin_campaign_inception(workspace=workspace, binding=binding, authority_root=authority_root)

def test_missing_independent_authority_blocks_local_receipt(tmp_path: Path) -> None:
    workspace, authority_root, binding, _anchor, _sources_value, _receipt = _finalized(tmp_path)
    shutil.rmtree(authority_root)
    with pytest.raises(Exception) as exc_info:
        load_campaign_inception(workspace=workspace, binding=binding, authority_root=authority_root)
    assert 'authority' in str(exc_info.value).lower() or 'binding' in str(exc_info.value).lower()

def test_changed_manifest_or_universe_requires_a_new_campaign_identity(tmp_path: Path) -> None:
    workspace, authority_root, binding, _anchor, _sources_value, _receipt = _finalized(tmp_path)
    with pytest.raises(CampaignInceptionConflictError):
        begin_campaign_inception(workspace=workspace, binding=_binding(manifest_sha256=SHA_C), authority_root=authority_root)
    with pytest.raises(CampaignInceptionConflictError):
        begin_campaign_inception(workspace=workspace, binding=_binding(universe_sha256=SHA_D), authority_root=authority_root)
    new_binding = _binding(campaign_id='campaign-2', manifest_sha256=SHA_C, universe_sha256=SHA_D)
    new_anchor = begin_campaign_inception(workspace=workspace, binding=new_binding, authority_root=authority_root)
    assert new_anchor.binding.campaign_id == 'campaign-2'

def test_source_set_must_exactly_cover_frozen_universe_projection(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    authority_root = tmp_path / 'machine'
    binding = _binding()
    begin_campaign_inception(workspace=workspace, binding=binding, authority_root=authority_root)
    sources = _sources(workspace, authority_root)
    with pytest.raises(CampaignInceptionConflictError, match='exactly cover'):
        finalize_campaign_inception(workspace=workspace, binding=binding, source_authorities={'provider:a': sources['provider:a']}, authority_root=authority_root)

def test_unknown_source_authority_or_generation_cannot_cross_another_frontier(tmp_path: Path) -> None:
    _workspace, _root, _binding_value, _anchor, sources, receipt = _finalized(tmp_path)
    source = sources['provider:a']
    source.add('wrong-source', 1000, source_identity='provider:unknown')
    source.add('wrong-generation', 1000, generation='epoch-2')
    source.add('wrong-authority', 1000, authority_identity_sha256=SHA_A)
    with pytest.raises(CampaignObservationNotProspectiveError, match='outside'):
        admit_campaign_observation(receipt=receipt, observation_id='wrong-source', source_authority=source)
    with pytest.raises(CampaignObservationNotProspectiveError, match='authority/generation'):
        admit_campaign_observation(receipt=receipt, observation_id='wrong-generation', source_authority=source)
    with pytest.raises(CampaignObservationNotProspectiveError, match='authority/generation'):
        admit_campaign_observation(receipt=receipt, observation_id='wrong-authority', source_authority=source)

def test_caller_timestamp_is_not_an_admission_input(tmp_path: Path) -> None:
    _workspace, _root, _binding_value, _anchor, sources, receipt = _finalized(tmp_path)
    source = sources['provider:a']
    source.add('backdated-provider-time', 101)
    admitted = admit_campaign_observation(receipt=receipt, observation_id='backdated-provider-time', source_authority=source)
    assert admitted.durable_position == 101

def test_local_frontier_tamper_fails_against_independent_authority(tmp_path: Path) -> None:
    workspace, authority_root, binding, _anchor, _sources_value, _receipt = _finalized(tmp_path)
    state_path = next((workspace / 'campaign-inception-v1').glob('*.json'))
    payload = json.loads(state_path.read_text(encoding='utf-8'))
    payload['frontier'][0]['high_water_position'] = 0
    state_path.write_text(json.dumps(payload, sort_keys=True) + '\n', encoding='utf-8')
    with pytest.raises(MonotonicAuthorityRollbackError):
        load_campaign_inception(workspace=workspace, binding=binding, authority_root=authority_root)

def test_observation_cannot_be_admitted_before_receipt_finalization(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    authority_root = tmp_path / 'machine'
    binding = _binding()
    anchor = begin_campaign_inception(workspace=workspace, binding=binding, authority_root=authority_root)
    with pytest.raises(CampaignInceptionConflictError, match='not finalized'):
        load_campaign_inception(workspace=workspace, binding=binding, authority_root=authority_root)
    with pytest.raises(TypeError, match='exact CampaignInceptionReceipt'):
        admit_campaign_observation(receipt=anchor, observation_id='cannot-admit-yet', source_authority=_sources(workspace, authority_root)['provider:a'])

def test_corrupt_or_noncanonical_frontier_fails_closed(tmp_path: Path) -> None:
    workspace, authority_root, binding, _anchor, _sources_value, _receipt = _finalized(tmp_path)
    state_path = next((workspace / 'campaign-inception-v1').glob('*.json'))
    payload = json.loads(state_path.read_text(encoding='utf-8'))
    payload['frontier'][0]['unexpected'] = True
    state_path.write_text(json.dumps(payload, sort_keys=True) + '\n', encoding='utf-8')
    with pytest.raises(CampaignInceptionIntegrityError, match='frontier fields'):
        load_campaign_inception(workspace=workspace, binding=binding, authority_root=authority_root)
