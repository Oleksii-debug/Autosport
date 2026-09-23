from __future__ import annotations
'Non-backdateable prospective campaign inception authority.\n\nThis module composes the existing independent ``MonotonicWorkspaceAuthority`` with\ncanonical observation-position authorities.  It deliberately does not own campaign\nmanifest storage, provider/evaluation-universe storage, observation journals, or\nPaperCampaign promotion semantics.\n\nThe lifecycle is intentionally two-phase:\n\n1. ``begin_campaign_inception`` durably commits an anchor binding campaign + exact\n   manifest + exact universe + the complete required source set.\n2. Only after that anchor is durable, ``finalize_campaign_inception`` asks each\n   canonical observation authority for its current durable high-water position and\n   commits one immutable receipt containing that structured frontier.\n\nObservations committed between anchor durability and frontier capture are\nconservatively excluded because their position is at/before the captured frontier.\nThat closes the unsafe ``capture frontier -> later write receipt`` race without using\nwall-clock timestamps as causal evidence.\n'
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping, Protocol, runtime_checkable
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import AuthorityPhase, AuthorityRecord, MonotonicWorkspaceAuthority
from .workspace_lock import WorkspaceEconomicLock
SCHEMA: Final = 'autosport.campaign_inception'
SCHEMA_VERSION: Final = 1
AUTHORITY_DOMAIN: Final = 'research.forward-campaign-inception-causality'
_STATE_DIR: Final = 'campaign-inception-v1'
_HEX = frozenset('0123456789abcdef')

class CampaignInceptionError(RuntimeError):
    """Campaign inception evidence is absent, inconsistent, or not causally valid."""

class CampaignInceptionIntegrityError(CampaignInceptionError):
    """Persisted campaign inception evidence is malformed or tampered."""

class CampaignInceptionConflictError(CampaignInceptionError):
    """A campaign identity is already bound to different prospective authority."""

class CampaignObservationNotProspectiveError(CampaignInceptionError):
    """An observation cannot be proven strictly beyond the frozen inception frontier."""

def _text(value: object, name: str, *, max_length: int=512) -> str:
    if type(value) is not str or not value or value != value.strip() or ('\x00' in value) or (len(value) > max_length) or any((ord(ch) < 32 for ch in value)):
        raise CampaignInceptionIntegrityError(f'{name} must be non-empty canonical text')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise CampaignInceptionIntegrityError(f'{name} contains invalid Unicode') from exc
    return value

def _sha(value: object, name: str) -> str:
    raw = _text(value, name, max_length=64)
    if len(raw) != 64 or raw != raw.lower() or any((ch not in _HEX for ch in raw)):
        raise CampaignInceptionIntegrityError(f'{name} must be lowercase SHA-256 hex')
    return raw

def _position(value: object, name: str) -> int:
    if type(value) is not int or value < 0 or value > 2 ** 63 - 1:
        raise CampaignInceptionIntegrityError(f'{name} must be an integer in [0, 2^63-1]')
    return value

def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise CampaignInceptionIntegrityError('campaign inception evidence is outside canonical JSON domain') from exc

def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()

def _campaign_key(campaign_id: str) -> str:
    return hashlib.sha256(('campaign-inception-v1\x00' + campaign_id).encode('utf-8')).hexdigest()

def _state_path(workspace: Path, campaign_id: str) -> Path:
    return workspace / _STATE_DIR / f'{_campaign_key(campaign_id)}.json'

def _fsync_directory(path: Path) -> None:
    if os.name == 'nt':
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def _write_state(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical_bytes(dict(payload)) + b'\n'
    temporary = path.parent / f'.{path.name}.{uuid.uuid4().hex}.tmp'
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    if os.name != 'nt':
        flags |= getattr(os, 'O_NOFOLLOW', 0)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(temporary, flags, 384)
        created = True
        with os.fdopen(descriptor, 'wb', closefd=True) as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        created = False
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise

def _read_payload(path: Path) -> dict[str, object] | None:
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CampaignInceptionIntegrityError('cannot read campaign inception state') from exc
    try:
        payload = strict_json_loads(raw)
    except (ValueError, TypeError) as exc:
        raise CampaignInceptionIntegrityError('campaign inception state is invalid JSON') from exc
    if type(payload) is not dict:
        raise CampaignInceptionIntegrityError('campaign inception state must be a JSON object')
    return payload

@dataclass(frozen=True, slots=True)
class CampaignAuthorityBinding:
    campaign_id: str
    manifest_id: str
    manifest_sha256: str
    universe_receipt_id: str
    universe_receipt_sha256: str
    required_source_identities: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'campaign_id', _text(self.campaign_id, 'campaign_id', max_length=256))
        object.__setattr__(self, 'manifest_id', _text(self.manifest_id, 'manifest_id'))
        object.__setattr__(self, 'manifest_sha256', _sha(self.manifest_sha256, 'manifest_sha256'))
        object.__setattr__(self, 'universe_receipt_id', _text(self.universe_receipt_id, 'universe_receipt_id'))
        object.__setattr__(self, 'universe_receipt_sha256', _sha(self.universe_receipt_sha256, 'universe_receipt_sha256'))
        sources = tuple(sorted((_text(item, 'required source identity', max_length=512) for item in self.required_source_identities)))
        if not sources:
            raise CampaignInceptionIntegrityError('campaign inception requires at least one canonical source')
        if len(set(sources)) != len(sources):
            raise CampaignInceptionIntegrityError('required source identities must be unique')
        object.__setattr__(self, 'required_source_identities', sources)

    def payload(self) -> dict[str, object]:
        return {'campaign_id': self.campaign_id, 'manifest_id': self.manifest_id, 'manifest_sha256': self.manifest_sha256, 'universe_receipt_id': self.universe_receipt_id, 'universe_receipt_sha256': self.universe_receipt_sha256, 'required_source_identities': list(self.required_source_identities)}

    @property
    def binding_sha256(self) -> str:
        return _digest({'domain': 'campaign-inception-binding-v1', **self.payload()})

@dataclass(frozen=True, slots=True)
class ObservationAdmissionFrontier:
    source_identity: str
    authority_kind: str
    authority_version: str
    authority_identity_sha256: str
    source_generation: str
    high_water_position: int
    high_water_commitment_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, 'source_identity', _text(self.source_identity, 'source_identity'))
        object.__setattr__(self, 'authority_kind', _text(self.authority_kind, 'authority_kind', max_length=256))
        object.__setattr__(self, 'authority_version', _text(self.authority_version, 'authority_version', max_length=128))
        object.__setattr__(self, 'authority_identity_sha256', _sha(self.authority_identity_sha256, 'authority_identity_sha256'))
        object.__setattr__(self, 'source_generation', _text(self.source_generation, 'source_generation', max_length=256))
        object.__setattr__(self, 'high_water_position', _position(self.high_water_position, 'high_water_position'))
        object.__setattr__(self, 'high_water_commitment_sha256', _sha(self.high_water_commitment_sha256, 'high_water_commitment_sha256'))

    def payload(self) -> dict[str, object]:
        return {'source_identity': self.source_identity, 'authority_kind': self.authority_kind, 'authority_version': self.authority_version, 'authority_identity_sha256': self.authority_identity_sha256, 'source_generation': self.source_generation, 'high_water_position': self.high_water_position, 'high_water_commitment_sha256': self.high_water_commitment_sha256}

@dataclass(frozen=True, slots=True)
class ResolvedObservationPosition:
    observation_id: str
    source_identity: str
    authority_kind: str
    authority_version: str
    authority_identity_sha256: str
    source_generation: str
    durable_position: int
    observation_commitment_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, 'observation_id', _text(self.observation_id, 'observation_id'))
        object.__setattr__(self, 'source_identity', _text(self.source_identity, 'source_identity'))
        object.__setattr__(self, 'authority_kind', _text(self.authority_kind, 'authority_kind', max_length=256))
        object.__setattr__(self, 'authority_version', _text(self.authority_version, 'authority_version', max_length=128))
        object.__setattr__(self, 'authority_identity_sha256', _sha(self.authority_identity_sha256, 'authority_identity_sha256'))
        object.__setattr__(self, 'source_generation', _text(self.source_generation, 'source_generation', max_length=256))
        object.__setattr__(self, 'durable_position', _position(self.durable_position, 'durable_position'))
        object.__setattr__(self, 'observation_commitment_sha256', _sha(self.observation_commitment_sha256, 'observation_commitment_sha256'))

@runtime_checkable
class CanonicalObservationAuthority(Protocol):
    """Product-owned source adapter used to capture and later re-resolve chronology."""

    def capture_campaign_frontier(self) -> ObservationAdmissionFrontier:
        ...

    def resolve_campaign_observation(self, observation_id: str) -> ResolvedObservationPosition:
        ...

@dataclass(frozen=True, slots=True)
class CampaignInceptionAnchor:
    binding: CampaignAuthorityBinding
    authority_generation: int
    authority_record_sha256: str
    authority_tx_id: str
    authority_semantic_binding_sha256: str
    state_sha256: str

@dataclass(frozen=True, slots=True)
class CampaignInceptionReceipt:
    binding: CampaignAuthorityBinding
    anchor_generation: int
    anchor_record_sha256: str
    frontier: tuple[ObservationAdmissionFrontier, ...]
    authority_generation: int
    authority_tx_id: str
    authority_semantic_binding_sha256: str
    receipt_sha256: str

    @property
    def frontier_by_source(self) -> dict[str, ObservationAdmissionFrontier]:
        return {item.source_identity: item for item in self.frontier}

def _authority(*, workspace: Path, campaign_id: str, authority_root: str | Path | None) -> MonotonicWorkspaceAuthority:
    return MonotonicWorkspaceAuthority(workspace=workspace, domain=AUTHORITY_DOMAIN, key=f'campaign:{campaign_id}', authority_root=authority_root)

def _anchor_state_payload(binding: CampaignAuthorityBinding, *, authority_generation: int, authority_tx_id: str) -> dict[str, object]:
    return {'schema': SCHEMA, 'schema_version': SCHEMA_VERSION, 'state': 'ANCHORED', 'binding': binding.payload(), 'authority_generation': authority_generation, 'authority_tx_id': authority_tx_id, 'authority_semantic_binding_sha256': binding.binding_sha256}

def _receipt_payload(*, binding: CampaignAuthorityBinding, anchor_generation: int, anchor_record_sha256: str, frontier: tuple[ObservationAdmissionFrontier, ...], authority_generation: int, authority_tx_id: str, authority_semantic_binding_sha256: str) -> dict[str, object]:
    return {'schema': SCHEMA, 'schema_version': SCHEMA_VERSION, 'state': 'FINALIZED', 'binding': binding.payload(), 'anchor_generation': anchor_generation, 'anchor_record_sha256': anchor_record_sha256, 'frontier': [item.payload() for item in frontier], 'authority_generation': authority_generation, 'authority_tx_id': authority_tx_id, 'authority_semantic_binding_sha256': authority_semantic_binding_sha256}

def _binding_from_payload(raw: object) -> CampaignAuthorityBinding:
    if type(raw) is not dict:
        raise CampaignInceptionIntegrityError('binding must be an object')
    expected = {'campaign_id', 'manifest_id', 'manifest_sha256', 'universe_receipt_id', 'universe_receipt_sha256', 'required_source_identities'}
    if set(raw) != expected:
        raise CampaignInceptionIntegrityError('binding fields are not canonical')
    sources = raw['required_source_identities']
    if type(sources) is not list:
        raise CampaignInceptionIntegrityError('required source identities must be a list')
    return CampaignAuthorityBinding(campaign_id=raw['campaign_id'], manifest_id=raw['manifest_id'], manifest_sha256=raw['manifest_sha256'], universe_receipt_id=raw['universe_receipt_id'], universe_receipt_sha256=raw['universe_receipt_sha256'], required_source_identities=tuple(sources))

def _frontier_from_payload(raw: object) -> ObservationAdmissionFrontier:
    if type(raw) is not dict:
        raise CampaignInceptionIntegrityError('frontier entry must be an object')
    expected = {'source_identity', 'authority_kind', 'authority_version', 'authority_identity_sha256', 'source_generation', 'high_water_position', 'high_water_commitment_sha256'}
    if set(raw) != expected:
        raise CampaignInceptionIntegrityError('frontier fields are not canonical')
    return ObservationAdmissionFrontier(**raw)

def _require_binding(actual: CampaignAuthorityBinding, expected: CampaignAuthorityBinding) -> None:
    if actual != expected:
        raise CampaignInceptionConflictError('campaign identity is already bound to a different manifest/universe/source set')

def _anchor_from_state(payload: Mapping[str, object], *, expected_binding: CampaignAuthorityBinding, authority_record: AuthorityRecord) -> CampaignInceptionAnchor:
    expected = {'schema', 'schema_version', 'state', 'binding', 'authority_generation', 'authority_tx_id', 'authority_semantic_binding_sha256'}
    if set(payload) != expected or payload.get('schema') != SCHEMA or payload.get('schema_version') != SCHEMA_VERSION:
        raise CampaignInceptionIntegrityError('anchor state fields are not canonical')
    if payload.get('state') != 'ANCHORED':
        raise CampaignInceptionIntegrityError('expected anchored campaign state')
    binding = _binding_from_payload(payload['binding'])
    _require_binding(binding, expected_binding)
    generation = _position(payload['authority_generation'], 'authority_generation')
    tx_id = _text(payload['authority_tx_id'], 'authority_tx_id', max_length=256)
    semantic_binding_sha256 = _sha(payload['authority_semantic_binding_sha256'], 'authority_semantic_binding_sha256')
    if semantic_binding_sha256 != binding.binding_sha256:
        raise CampaignInceptionIntegrityError('anchor semantic binding does not match campaign binding')
    state_sha256 = _digest(dict(payload))
    if authority_record.generation != generation or authority_record.tx_id != tx_id or authority_record.semantic_binding_sha256 != semantic_binding_sha256 or (authority_record.intended_state_sha256 != state_sha256):
        raise CampaignInceptionIntegrityError('anchor does not match independent monotonic authority')
    return CampaignInceptionAnchor(binding=binding, authority_generation=generation, authority_record_sha256=authority_record.record_sha256, authority_tx_id=tx_id, authority_semantic_binding_sha256=semantic_binding_sha256, state_sha256=state_sha256)

def _receipt_from_state(payload: Mapping[str, object], *, expected_binding: CampaignAuthorityBinding) -> CampaignInceptionReceipt:
    expected = {'schema', 'schema_version', 'state', 'binding', 'anchor_generation', 'anchor_record_sha256', 'frontier', 'authority_generation', 'authority_tx_id', 'authority_semantic_binding_sha256'}
    if set(payload) != expected or payload.get('schema') != SCHEMA or payload.get('schema_version') != SCHEMA_VERSION:
        raise CampaignInceptionIntegrityError('receipt fields are not canonical')
    if payload.get('state') != 'FINALIZED':
        raise CampaignInceptionIntegrityError('expected finalized campaign state')
    binding = _binding_from_payload(payload['binding'])
    _require_binding(binding, expected_binding)
    raw_frontier = payload['frontier']
    if type(raw_frontier) is not list:
        raise CampaignInceptionIntegrityError('frontier must be a list')
    frontier = tuple((_frontier_from_payload(item) for item in raw_frontier))
    if tuple((item.source_identity for item in frontier)) != binding.required_source_identities:
        raise CampaignInceptionIntegrityError('receipt frontier does not exactly cover the frozen source set')
    return CampaignInceptionReceipt(binding=binding, anchor_generation=_position(payload['anchor_generation'], 'anchor_generation'), anchor_record_sha256=_sha(payload['anchor_record_sha256'], 'anchor_record_sha256'), frontier=frontier, authority_generation=_position(payload['authority_generation'], 'authority_generation'), authority_tx_id=_text(payload['authority_tx_id'], 'authority_tx_id', max_length=256), authority_semantic_binding_sha256=_sha(payload['authority_semantic_binding_sha256'], 'authority_semantic_binding_sha256'), receipt_sha256=_digest(dict(payload)))

def _read_validated_state(path: Path, binding: CampaignAuthorityBinding) -> tuple[dict[str, object] | None, str | None]:
    payload = _read_payload(path)
    if payload is None:
        return (None, None)
    if payload.get('state') == 'ANCHORED':
        _binding_from_payload(payload.get('binding'))
    elif payload.get('state') == 'FINALIZED':
        _receipt_from_state(payload, expected_binding=binding)
    else:
        raise CampaignInceptionIntegrityError('unknown campaign inception state')
    return (payload, _digest(payload))

def begin_campaign_inception(*, workspace: str | Path, binding: CampaignAuthorityBinding, authority_root: str | Path | None=None) -> CampaignInceptionAnchor | CampaignInceptionReceipt:
    """Durably establish the pre-frontier campaign anchor.

    The returned object may already be a finalized receipt on an exact idempotent
    reopen.  A missing local state with surviving independent authority is rollback,
    never a fresh bootstrap.
    """
    workspace_path = Path(workspace)
    if not workspace_path.is_absolute():
        raise CampaignInceptionIntegrityError('workspace must be an absolute path')
    path = _state_path(workspace_path, binding.campaign_id)
    with WorkspaceEconomicLock(path.parent):
        payload, observed_sha256 = _read_validated_state(path, binding)
        authority = _authority(workspace=workspace_path, campaign_id=binding.campaign_id, authority_root=authority_root)
        if payload is not None:
            if payload.get('state') == 'FINALIZED':
                receipt = _receipt_from_state(payload, expected_binding=binding)
                recovery = authority.recover(observed_state_sha256=receipt.receipt_sha256, tx_id=receipt.authority_tx_id, semantic_binding_sha256=receipt.authority_semantic_binding_sha256)
                if recovery.committed_generation != receipt.authority_generation:
                    raise CampaignInceptionIntegrityError('receipt generation does not match independent authority')
                return receipt
            tx_id = _text(payload.get('authority_tx_id'), 'authority_tx_id', max_length=256)
            semantic_binding_sha256 = _sha(payload.get('authority_semantic_binding_sha256'), 'authority_semantic_binding_sha256')
            recovery = authority.recover(observed_state_sha256=observed_sha256, tx_id=tx_id, semantic_binding_sha256=semantic_binding_sha256)
            if recovery.record is None:
                raise CampaignInceptionIntegrityError('anchored state lacks an independent authority record')
            return _anchor_from_state(payload, expected_binding=binding, authority_record=recovery.record)
        recovery = authority.recover(observed_state_sha256=None)
        if recovery.committed_generation != 0:
            raise CampaignInceptionIntegrityError('missing local campaign state is not pristine')
        history = authority.read_history()
        expected_generation = history[-1].generation + 1 if history else 1
        tx_id = f'campaign-anchor:{expected_generation}:{binding.binding_sha256}'
        anchor_payload = _anchor_state_payload(binding, authority_generation=expected_generation, authority_tx_id=tx_id)
        anchor_sha256 = _digest(anchor_payload)
        prepared = authority.prepare(tx_id=tx_id, observed_state_sha256=None, intended_state_sha256=anchor_sha256, semantic_binding_sha256=binding.binding_sha256)
        if prepared.generation != expected_generation:
            raise CampaignInceptionIntegrityError('unexpected campaign anchor generation')
        _write_state(path, anchor_payload)
        committed = authority.commit(tx_id=tx_id, observed_state_sha256=anchor_sha256, semantic_binding_sha256=binding.binding_sha256)
        return CampaignInceptionAnchor(binding=binding, authority_generation=committed.generation, authority_record_sha256=committed.record_sha256, authority_tx_id=tx_id, authority_semantic_binding_sha256=binding.binding_sha256, state_sha256=anchor_sha256)

def finalize_campaign_inception(*, workspace: str | Path, binding: CampaignAuthorityBinding, source_authorities: Mapping[str, CanonicalObservationAuthority], authority_root: str | Path | None=None) -> CampaignInceptionReceipt:
    """Capture the post-anchor structured frontier and durably finalize one receipt."""
    workspace_path = Path(workspace)
    if not workspace_path.is_absolute():
        raise CampaignInceptionIntegrityError('workspace must be an absolute path')
    if set(source_authorities) != set(binding.required_source_identities):
        raise CampaignInceptionConflictError('source authorities must exactly cover the frozen source set')
    path = _state_path(workspace_path, binding.campaign_id)
    with WorkspaceEconomicLock(path.parent):
        payload, observed_sha256 = _read_validated_state(path, binding)
        if payload is None:
            raise CampaignInceptionConflictError('campaign inception anchor is not durable')
        authority = _authority(workspace=workspace_path, campaign_id=binding.campaign_id, authority_root=authority_root)
        if payload.get('state') == 'FINALIZED':
            receipt = _receipt_from_state(payload, expected_binding=binding)
            recovery = authority.recover(observed_state_sha256=receipt.receipt_sha256, tx_id=receipt.authority_tx_id, semantic_binding_sha256=receipt.authority_semantic_binding_sha256)
            if recovery.committed_generation != receipt.authority_generation:
                raise CampaignInceptionIntegrityError('receipt generation does not match independent authority')
            return receipt
        anchor_tx_id = _text(payload.get('authority_tx_id'), 'authority_tx_id', max_length=256)
        anchor_semantic_binding = _sha(payload.get('authority_semantic_binding_sha256'), 'authority_semantic_binding_sha256')
        recovery = authority.recover(observed_state_sha256=observed_sha256, tx_id=anchor_tx_id, semantic_binding_sha256=anchor_semantic_binding)
        if recovery.record is None:
            raise CampaignInceptionIntegrityError('anchor lacks independent authority record')
        anchor = _anchor_from_state(payload, expected_binding=binding, authority_record=recovery.record)
        if any((record.phase is AuthorityPhase.ABORT and record.tx_id.startswith('campaign-final:') for record in authority.read_history())):
            raise CampaignInceptionConflictError('prior campaign frontier finalization aborted; exact frontier cannot be recaptured')
        captured: list[ObservationAdmissionFrontier] = []
        for source_identity in binding.required_source_identities:
            resolver = source_authorities[source_identity]
            frontier = resolver.capture_campaign_frontier()
            if type(frontier) is not ObservationAdmissionFrontier:
                raise CampaignInceptionIntegrityError('canonical source returned an invalid frontier type')
            if frontier.source_identity != source_identity:
                raise CampaignInceptionIntegrityError('canonical source frontier identity drifted')
            captured.append(frontier)
        frontier = tuple(captured)
        next_generation = anchor.authority_generation + 1
        frontier_sha256 = _digest([item.payload() for item in frontier])
        semantic_binding_sha256 = _digest({'domain': 'campaign-inception-final-v1', 'campaign_binding_sha256': binding.binding_sha256, 'anchor_generation': anchor.authority_generation, 'anchor_record_sha256': anchor.authority_record_sha256, 'frontier_sha256': frontier_sha256})
        tx_id = f'campaign-final:{semantic_binding_sha256}'
        receipt_payload = _receipt_payload(binding=binding, anchor_generation=anchor.authority_generation, anchor_record_sha256=anchor.authority_record_sha256, frontier=frontier, authority_generation=next_generation, authority_tx_id=tx_id, authority_semantic_binding_sha256=semantic_binding_sha256)
        receipt_sha256 = _digest(receipt_payload)
        prepared = authority.prepare(tx_id=tx_id, observed_state_sha256=anchor.state_sha256, intended_state_sha256=receipt_sha256, semantic_binding_sha256=semantic_binding_sha256)
        if prepared.generation != next_generation:
            raise CampaignInceptionIntegrityError('unexpected campaign finalization generation')
        _write_state(path, receipt_payload)
        committed = authority.commit(tx_id=tx_id, observed_state_sha256=receipt_sha256, semantic_binding_sha256=semantic_binding_sha256)
        if committed.generation != next_generation:
            raise CampaignInceptionIntegrityError('campaign receipt committed at an unexpected generation')
        return _receipt_from_state(receipt_payload, expected_binding=binding)

def load_campaign_inception(*, workspace: str | Path, binding: CampaignAuthorityBinding, authority_root: str | Path | None=None) -> CampaignInceptionReceipt:
    """Reopen only an exact finalized receipt proven current by independent authority."""
    state = begin_campaign_inception(workspace=workspace, binding=binding, authority_root=authority_root)
    if type(state) is not CampaignInceptionReceipt:
        raise CampaignInceptionConflictError('campaign inception is anchored but not finalized')
    return state

def admit_campaign_observation(*, receipt: CampaignInceptionReceipt, observation_id: str, source_authority: CanonicalObservationAuthority) -> ResolvedObservationPosition:
    """Re-resolve one observation and require strict causal position beyond its frontier."""
    if type(receipt) is not CampaignInceptionReceipt:
        raise TypeError('receipt must be an exact CampaignInceptionReceipt')
    observation_id = _text(observation_id, 'observation_id')
    resolved = source_authority.resolve_campaign_observation(observation_id)
    if type(resolved) is not ResolvedObservationPosition:
        raise CampaignInceptionIntegrityError('canonical source returned an invalid observation position')
    if resolved.observation_id != observation_id:
        raise CampaignInceptionIntegrityError('canonical source resolved a different observation identity')
    frontier = receipt.frontier_by_source.get(resolved.source_identity)
    if frontier is None:
        raise CampaignObservationNotProspectiveError('observation source is outside the frozen campaign frontier')
    if resolved.authority_kind != frontier.authority_kind or resolved.authority_version != frontier.authority_version or resolved.authority_identity_sha256 != frontier.authority_identity_sha256 or (resolved.source_generation != frontier.source_generation):
        raise CampaignObservationNotProspectiveError('observation authority/generation does not match the frozen source frontier')
    if resolved.durable_position <= frontier.high_water_position:
        raise CampaignObservationNotProspectiveError('observation canonical position is not strictly beyond campaign inception frontier')
    return resolved
