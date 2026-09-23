from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping
from .integrity import atomic_write_json, sha256_file
from ._scientific_registry_read_authority import require_scientific_registry_read_authority
from .monotonic_workspace_authority import MonotonicWorkspaceAuthority
from .research_multiplicity import ExperimentFamilyPlan, SequentialDecision, SequentialLookEvidence, SequentialMultiplicityEvidenceStore
from .scientific_registry import PromotionEvidence, ResearchOutcome, ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock
_HEX = frozenset('0123456789abcdef')
_EVENT_KINDS = frozenset({'ATTEMPT_STARTED', 'ATTEMPT_COMPLETED', 'ATTEMPT_ABORTED'})
_AUTHORITY_DOMAIN = 'autosport.trial-family-accounting.v1'

class _SequentialReplay(RuntimeError):
    def __init__(self, decision: SequentialDecision) -> None:
        super().__init__('exact sequential evidence replay')
        self.decision = decision

def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f'{name} must be a non-empty canonical string')
    value.encode('utf-8')
    return value

def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any((char not in _HEX for char in text)):
        raise ValueError(f'{name} must be a canonical SHA-256 hex string')
    return text

def _iso(value: object, name: str) -> str:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'{name} must be an ISO-8601 timestamp') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f'{name} must include a timezone')
    return text

def _instant(value: object, name: str) -> datetime:
    return datetime.fromisoformat(_iso(value, name).replace('Z', '+00:00')).astimezone(timezone.utc)

def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()

def _state_sha256(payload: Mapping[str, Any]) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n'
    return hashlib.sha256(rendered.encode('utf-8')).hexdigest()

def _text_sha256(value: object, name: str) -> str:
    return hashlib.sha256(_text(value, name).encode('utf-8')).hexdigest()

def _registry_prefix_sha256(state: Mapping[str, Any], record_count: int) -> str:
    if type(record_count) is not int or record_count < 0:
        raise ValueError('registry_record_count must be a non-negative integer')
    if state.get('schema_version') != ScientificRegistry.SCHEMA_VERSION:
        raise ValueError('ScientificRegistry schema_version mismatch while proving attempt issuance')
    records = state.get('records')
    if type(records) is not list or record_count > len(records):
        raise ValueError('ScientificRegistry no longer contains the witnessed attempt prefix')
    return _digest({'schema_version': state['schema_version'], 'records': records[:record_count]})

def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON object key: {key}')
        result[key] = value
    return result

def _reject_nonfinite(value: str) -> None:
    raise ValueError(f'non-finite JSON constant: {value}')

class TrialAttemptStatus(StrEnum):
    OPEN = 'OPEN'
    COMPLETED = 'COMPLETED'
    ABORTED = 'ABORTED'

@dataclass(frozen=True, slots=True)
class TrialCandidateLineage:
    strategy_version_id: str
    dataset_snapshot_id: str
    feature_set_id: str
    seed: int
    config_sha256: str
    model_version_id: str | None = None

    def __post_init__(self) -> None:
        for name in ('strategy_version_id', 'dataset_snapshot_id', 'feature_set_id'):
            _text(getattr(self, name), name)
        if self.model_version_id is not None:
            _text(self.model_version_id, 'model_version_id')
        if type(self.seed) is not int:
            raise ValueError('seed must be an integer')
        _sha256(self.config_sha256, 'config_sha256')

    def to_payload(self) -> dict[str, Any]:
        return {'strategy_version_id': self.strategy_version_id, 'model_version_id': self.model_version_id, 'dataset_snapshot_id': self.dataset_snapshot_id, 'feature_set_id': self.feature_set_id, 'seed': self.seed, 'config_sha256': self.config_sha256.lower()}

    @property
    def semantic_sha256(self) -> str:
        return _digest(self.to_payload())

    @classmethod
    def from_payload(cls, payload: object) -> 'TrialCandidateLineage':
        if type(payload) is not dict:
            raise ValueError('trial candidate lineage must be an object')
        required = {'strategy_version_id', 'model_version_id', 'dataset_snapshot_id', 'feature_set_id', 'seed', 'config_sha256'}
        if set(payload) != required:
            raise ValueError('trial candidate lineage fields mismatch')
        return cls(**payload)

@dataclass(frozen=True, slots=True)
class TrialFamilyDefinition:
    family_id: str
    research_protocol_id: str
    protocol_sha256: str
    research_question_id: str
    hypothesis_id: str
    multiplicity_contract_sha256: str
    stopping_rule_sha256: str
    multiple_comparison_control_sha256: str
    frozen_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ('research_protocol_id', 'research_question_id', 'hypothesis_id'):
            _text(getattr(self, name), name)
        for name in ('protocol_sha256', 'multiplicity_contract_sha256', 'stopping_rule_sha256', 'multiple_comparison_control_sha256'):
            _sha256(getattr(self, name), name)
        _iso(self.frozen_at, 'frozen_at')
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError('schema_version must be 1')
        if self.family_id != _digest(self.identity_payload()):
            raise ValueError('family_id does not match canonical trial-family identity')

    def identity_payload(self) -> dict[str, Any]:
        return {'schema_version': self.schema_version, 'research_protocol_id': self.research_protocol_id, 'protocol_sha256': self.protocol_sha256.lower(), 'research_question_id': self.research_question_id, 'hypothesis_id': self.hypothesis_id, 'multiplicity_contract_sha256': self.multiplicity_contract_sha256.lower(), 'stopping_rule_sha256': self.stopping_rule_sha256.lower(), 'multiple_comparison_control_sha256': self.multiple_comparison_control_sha256.lower(), 'frozen_at': self.frozen_at}

    def to_payload(self) -> dict[str, Any]:
        return {**self.identity_payload(), 'family_id': self.family_id}

    @classmethod
    def from_payload(cls, payload: object) -> 'TrialFamilyDefinition':
        if type(payload) is not dict:
            raise ValueError('trial family definition must be an object')
        required = {'schema_version', 'family_id', 'research_protocol_id', 'protocol_sha256', 'research_question_id', 'hypothesis_id', 'multiplicity_contract_sha256', 'stopping_rule_sha256', 'multiple_comparison_control_sha256', 'frozen_at'}
        if set(payload) != required:
            raise ValueError('trial family definition fields mismatch')
        return cls(**payload)

    @classmethod
    def resolve(cls, registry: ScientificRegistry, plan: ExperimentFamilyPlan) -> 'TrialFamilyDefinition':
        if type(registry) is not ScientificRegistry:
            raise TypeError('registry must be ScientificRegistry')
        if type(plan) is not ExperimentFamilyPlan:
            raise TypeError('plan must be ExperimentFamilyPlan')
        protocol = registry.get('ResearchProtocol', plan.research_protocol_id)
        if protocol is None:
            raise ValueError('frozen ResearchProtocol is missing from ScientificRegistry')
        payload = protocol.payload
        if payload.get('protocol_sha256') != plan.protocol_sha256:
            raise ValueError('multiplicity plan protocol digest does not match ScientificRegistry')
        binding = payload.get('binding')
        if type(binding) is not dict:
            raise ValueError('ResearchProtocol binding payload is invalid')
        if binding.get('research_question_id') != plan.research_question_id:
            raise ValueError('multiplicity plan research question does not match ResearchProtocol')
        hypothesis_id = _text(binding.get('hypothesis_id'), 'hypothesis_id')
        hypothesis_sha = _sha256(binding.get('hypothesis_sha256'), 'binding.hypothesis_sha256')
        hypothesis = registry.get('Hypothesis', hypothesis_id)
        if hypothesis is None:
            raise ValueError('ResearchProtocol hypothesis is missing from ScientificRegistry')
        if _digest(hypothesis.payload) != hypothesis_sha:
            raise ValueError('durable Hypothesis does not match frozen ResearchProtocol binding')
        if hypothesis.payload.get('research_question_id') != plan.research_question_id:
            raise ValueError('durable Hypothesis belongs to another research question')
        if hypothesis.payload.get('primary_metric') != plan.primary_metric:
            raise ValueError('multiplicity plan primary metric does not match durable Hypothesis')
        stopping_rule = _text(binding.get('stopping_rule'), 'stopping_rule')
        if plan.stopping_rule != stopping_rule:
            raise ValueError('multiplicity plan stopping rule does not match ResearchProtocol')
        multiple_comparison_control = _text(binding.get('multiple_comparison_control'), 'multiple_comparison_control')
        for member in plan.members:
            if member.hypothesis_id != hypothesis_id:
                raise ValueError('experiment family member hypothesis does not match ResearchProtocol')
            if member.hypothesis_sha256 != hypothesis_sha:
                raise ValueError('experiment family member hypothesis digest does not match ScientificRegistry')
        frozen = _instant(plan.frozen_at, 'plan.frozen_at')
        if _instant(protocol.available_at, 'protocol.available_at') > frozen:
            raise ValueError('trial family cannot freeze before ResearchProtocol availability')
        if _instant(hypothesis.available_at, 'hypothesis.available_at') > frozen:
            raise ValueError('trial family cannot freeze before durable Hypothesis availability')
        identity = {'schema_version': 1, 'research_protocol_id': plan.research_protocol_id, 'protocol_sha256': plan.protocol_sha256.lower(), 'research_question_id': plan.research_question_id, 'hypothesis_id': hypothesis_id, 'multiplicity_contract_sha256': plan.plan_sha256, 'stopping_rule_sha256': _text_sha256(stopping_rule, 'stopping_rule'), 'multiple_comparison_control_sha256': _text_sha256(multiple_comparison_control, 'multiple_comparison_control'), 'frozen_at': plan.frozen_at}
        return cls(family_id=_digest(identity), **identity)

@dataclass(frozen=True, slots=True)
class TrialAttemptView:
    attempt_id: str
    semantic_attempt_id: str
    ordinal: int
    member_authority_id: str
    hypothesis_id: str
    candidate: TrialCandidateLineage
    created_at: str
    status: TrialAttemptStatus
    experiment_id: str | None = None
    experiment_record_sha256: str | None = None
    result_available_at: str | None = None
    outcome: ResearchOutcome | None = None
    aborted_at: str | None = None
    abort_reason: str | None = None

class _AttemptReplay(RuntimeError):
    def __init__(self, attempt: TrialAttemptView) -> None:
        super().__init__('exact trial attempt replay')
        self.attempt = attempt

@dataclass(frozen=True, slots=True)
class TrialFamilySnapshot:
    as_of: str
    total_attempts: int
    completed_attempts: int
    aborted_attempts: int
    open_attempts: int
    positive: int
    negative: int
    null: int
    harmful: int
    inconclusive: int
    registered_looks: int

class TrialFamilyAccountingStore:
    SCHEMA_VERSION = 3
    SEQUENTIAL_DIR = '.trial-family-sequential'

    def __init__(self, path: str | Path, *, workspace_root: str | Path | None=None, authority_root: str | Path | None=None) -> None:
        self.path = Path(path).resolve(strict=False)
        self.workspace_root = Path(workspace_root).resolve(strict=False) if workspace_root is not None else self.path.parent
        try:
            self.path.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError('trial-family store path must be inside workspace_root') from exc
        self.authority_root = None if authority_root is None else Path(authority_root).resolve(strict=False)
        self._read_state()

    @classmethod
    def initialize_workspace(cls, workspace_root: str | Path) -> Path:
        return SequentialMultiplicityEvidenceStore.initialize_workspace(workspace_root)

    def _authority(self, family_id: str) -> MonotonicWorkspaceAuthority:
        return MonotonicWorkspaceAuthority(workspace=self.workspace_root, domain=_AUTHORITY_DOMAIN, key=_sha256(family_id, 'family_id'), authority_root=self.authority_root)

    @classmethod
    def _sequential_path(cls, workspace: Path, family_id: str) -> Path:
        return workspace / cls.SEQUENTIAL_DIR / f'{family_id}.json'

    @classmethod
    def initialize_pristine(cls, path: str | Path, registry: ScientificRegistry, plan: ExperimentFamilyPlan, *, workspace_root: str | Path | None=None, authority_root: str | Path | None=None) -> 'TrialFamilyAccountingStore':
        if type(registry) is not ScientificRegistry:
            raise TypeError('registry must be ScientificRegistry')
        if type(plan) is not ExperimentFamilyPlan:
            raise TypeError('plan must be ExperimentFamilyPlan')
        target = Path(path).resolve(strict=False)
        workspace = Path(workspace_root).resolve(strict=False) if workspace_root is not None else target.parent
        try:
            target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError('trial-family store path must be inside workspace_root') from exc
        registry_path = registry.path.resolve(strict=False)
        if registry_path.parent != workspace:
            raise ValueError('ScientificRegistry must use the same workspace root as trial-family accounting')
        require_scientific_registry_read_authority()
        canonical_registry = ScientificRegistry(registry_path)
        relative_registry = registry_path.relative_to(workspace).as_posix()
        family = TrialFamilyDefinition.resolve(canonical_registry, plan)
        authority = MonotonicWorkspaceAuthority(workspace=workspace, domain=_AUTHORITY_DOMAIN, key=family.family_id, authority_root=authority_root)
        if target.exists():
            existing = cls(target, workspace_root=workspace, authority_root=authority_root)
            if existing.family.family_id != family.family_id:
                raise ValueError('existing store is bound to another trial family')
            if existing.plan.plan_sha256 != plan.plan_sha256:
                raise ValueError('existing store is bound to another multiplicity contract')
            if existing._registry().path.resolve(strict=False) != registry_path:
                raise ValueError('existing store is bound to another ScientificRegistry')
            return existing
        sequential_path = cls._sequential_path(workspace, family.family_id)
        sequential = SequentialMultiplicityEvidenceStore.initialize_pristine(sequential_path, plan, workspace_root=workspace)
        relative_sequential = sequential.path.relative_to(workspace).as_posix()
        relative_trial = target.relative_to(workspace).as_posix()
        with WorkspaceEconomicLock(workspace):
            if target.exists():
                existing = cls(target, workspace_root=workspace, authority_root=authority_root)
                if existing.family.family_id != family.family_id:
                    raise ValueError('existing store is bound to another trial family')
                if existing.plan.plan_sha256 != plan.plan_sha256:
                    raise ValueError('existing store is bound to another multiplicity contract')
                if existing._registry().path.resolve(strict=False) != registry_path:
                    raise ValueError('existing store is bound to another ScientificRegistry')
                return existing
            authority.recover(observed_state_sha256=None)
            state = {'schema_version': cls.SCHEMA_VERSION, 'trial_store': relative_trial, 'scientific_registry': relative_registry, 'family': family.to_payload(), 'multiplicity_plan': plan.to_payload(), 'sequential_store': relative_sequential, 'events': []}
            intended = _state_sha256(state)
            binding = _digest({'family_id': family.family_id, 'kind': 'INITIALIZE', 'trial_store': relative_trial, 'scientific_registry': relative_registry, 'sequential_store': relative_sequential})
            tx_id = f'init-{family.family_id}'
            authority.prepare(tx_id=tx_id, observed_state_sha256=None, intended_state_sha256=intended, semantic_binding_sha256=binding)
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(target, state)
            observed = sha256_file(target)
            if observed != intended:
                raise ValueError('published trial-family bytes do not match prepared authority digest')
            authority.commit(tx_id=tx_id, observed_state_sha256=observed, semantic_binding_sha256=binding)
        return cls(target, workspace_root=workspace, authority_root=authority_root)

    def _load_json(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding='utf-8')
        except FileNotFoundError as exc:
            raise ValueError('trial-family accounting store is missing') from exc
        try:
            state = json.loads(raw, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_nonfinite)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError('trial-family accounting store must be valid UTF-8 JSON') from exc
        if type(state) is not dict or set(state) != {'schema_version', 'trial_store', 'scientific_registry', 'family', 'multiplicity_plan', 'sequential_store', 'events'}:
            raise ValueError('trial-family accounting store fields mismatch')
        if state['schema_version'] != self.SCHEMA_VERSION:
            raise ValueError('trial-family accounting schema_version mismatch')
        return state

    def _read_state(self) -> dict[str, Any]:
        state = self._load_json()
        family = TrialFamilyDefinition.from_payload(state['family'])
        plan = ExperimentFamilyPlan.from_payload(state['multiplicity_plan'])
        if family.research_protocol_id != plan.research_protocol_id or family.protocol_sha256 != plan.protocol_sha256:
            raise ValueError('persisted family/plan protocol mismatch')
        if family.research_question_id != plan.research_question_id:
            raise ValueError('persisted family/plan research question mismatch')
        if family.multiplicity_contract_sha256 != plan.plan_sha256:
            raise ValueError('persisted multiplicity contract digest mismatch')
        if family.stopping_rule_sha256 != _text_sha256(plan.stopping_rule, 'plan.stopping_rule'):
            raise ValueError('persisted family/plan stopping rule mismatch')
        trial_rel = _text(state['trial_store'], 'trial_store')
        trial_path = (self.workspace_root / trial_rel).resolve(strict=False)
        try:
            trial_path.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError('trial store path escaped trial workspace') from exc
        if trial_path != self.path:
            raise ValueError('trial store path does not match canonical family authority')
        registry_rel = _text(state['scientific_registry'], 'scientific_registry')
        registry_path = (self.workspace_root / registry_rel).resolve(strict=False)
        try:
            registry_path.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError('ScientificRegistry path escaped trial workspace') from exc
        if registry_path.parent != self.workspace_root:
            raise ValueError('ScientificRegistry must share the trial workspace lock')
        # Pure trial-ledger reads must remain inspectable even when the external
        # ScientificRegistry executable/read authority is unavailable. Operations
        # that actually consume registry truth call _registry(), which performs
        # the full authority preflight before constructing ScientificRegistry.
        if not registry_path.is_file():
            raise ValueError('ScientificRegistry bound to trial family is missing')
        sequential_rel = _text(state['sequential_store'], 'sequential_store')
        sequential_path = (self.workspace_root / sequential_rel).resolve(strict=False)
        try:
            sequential_path.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError('sequential store escaped trial workspace') from exc
        expected_path = self._sequential_path(self.workspace_root, family.family_id).resolve(strict=False)
        if sequential_path != expected_path:
            raise ValueError('sequential store path is not canonical for trial family')
        sequential = SequentialMultiplicityEvidenceStore(sequential_path, workspace_root=self.workspace_root)
        if sequential.plan.plan_sha256 != plan.plan_sha256:
            raise ValueError('sequential evidence store is bound to another multiplicity contract')
        if type(state['events']) is not list:
            raise ValueError('trial-family events must be a list')
        previous_sha: str | None = None
        previous_at: datetime | None = None
        for expected_sequence, event in enumerate(state['events'], start=1):
            if type(event) is not dict or set(event) != {'sequence', 'kind', 'event_at', 'payload', 'prev_event_sha256', 'event_sha256'}:
                raise ValueError('trial-family event fields mismatch')
            if event['sequence'] != expected_sequence:
                raise ValueError('trial-family event sequence is not contiguous')
            if event['kind'] not in _EVENT_KINDS:
                raise ValueError('unsupported trial-family event kind')
            event_at = _instant(event['event_at'], 'event_at')
            if event_at < _instant(family.frozen_at, 'family.frozen_at'):
                raise ValueError('trial-family event predates frozen family')
            if previous_at is not None and event_at < previous_at:
                raise ValueError('trial-family event time is not monotonic')
            if event['prev_event_sha256'] != previous_sha:
                raise ValueError('trial-family event hash chain mismatch')
            envelope = {key: event[key] for key in ('sequence', 'kind', 'event_at', 'payload', 'prev_event_sha256')}
            if event['event_sha256'] != _digest(envelope):
                raise ValueError('trial-family event digest mismatch')
            previous_sha, previous_at = (event['event_sha256'], event_at)
        self._replay_events(family, plan, tuple(state['events']), cutoff=None)
        self._authority(family.family_id).recover(observed_state_sha256=sha256_file(self.path))
        return state

    @property
    def family(self) -> TrialFamilyDefinition:
        return TrialFamilyDefinition.from_payload(self._read_state()['family'])

    @property
    def plan(self) -> ExperimentFamilyPlan:
        return ExperimentFamilyPlan.from_payload(self._read_state()['multiplicity_plan'])

    def _registry(self, state: dict[str, Any] | None=None) -> ScientificRegistry:
        loaded = self._read_state() if state is None else state
        # Validate class/executable authority before constructing the registry.
        # ScientificRegistry.__init__ calls _read(), so checking after construction
        # would already have executed a caller-rebound read delegate.
        require_scientific_registry_read_authority()
        return ScientificRegistry(self.workspace_root / loaded['scientific_registry'])

    def _sequential(self, state: dict[str, Any] | None=None) -> SequentialMultiplicityEvidenceStore:
        loaded = self._read_state() if state is None else state
        return SequentialMultiplicityEvidenceStore(self.workspace_root / loaded['sequential_store'], workspace_root=self.workspace_root)

    @staticmethod
    def _replay_events(family: TrialFamilyDefinition, plan: ExperimentFamilyPlan, events: tuple[dict[str, Any], ...], *, cutoff: datetime | None) -> tuple[TrialAttemptView, ...]:
        attempts: list[TrialAttemptView] = []
        by_id: dict[str, TrialAttemptView] = {}
        semantic_ids: set[str] = set()
        experiment_ids: set[str] = set()
        for event in events:
            if cutoff is not None and _instant(event['event_at'], 'event_at') > cutoff:
                break
            kind, payload = (event['kind'], event['payload'])
            if type(payload) is not dict:
                raise ValueError('trial-family event payload must be an object')
            if kind == 'ATTEMPT_STARTED':
                required = {'attempt_id', 'semantic_attempt_id', 'ordinal', 'member_authority_id', 'hypothesis_id', 'candidate', 'created_at', 'registry_record_count', 'registry_prefix_sha256'}
                if set(payload) != required or payload['created_at'] != event['event_at']:
                    raise ValueError('attempt-start payload fields/time mismatch')
                if type(payload['ordinal']) is not int or payload['ordinal'] != len(attempts) + 1:
                    raise ValueError('attempt ordinal is not contiguous')
                if type(payload['registry_record_count']) is not int or payload['registry_record_count'] < 0:
                    raise ValueError('attempt registry_record_count must be a non-negative integer')
                _sha256(payload['registry_prefix_sha256'], 'registry_prefix_sha256')
                attempt_id = _sha256(payload['attempt_id'], 'attempt_id')
                semantic_id = _text(payload['semantic_attempt_id'], 'semantic_attempt_id')
                if attempt_id in by_id or semantic_id in semantic_ids:
                    raise ValueError('duplicate attempt identity in durable history')
                member = plan.member(payload['member_authority_id'])
                if payload['hypothesis_id'] != member.hypothesis_id or member.hypothesis_id != family.hypothesis_id:
                    raise ValueError('attempt hypothesis does not match frozen family')
                candidate = TrialCandidateLineage.from_payload(payload['candidate'])
                if candidate.semantic_sha256 != member.semantic_variant_sha256:
                    raise ValueError('attempt candidate does not match frozen family member')
                view = TrialAttemptView(attempt_id, semantic_id, payload['ordinal'], member.member_authority_id, member.hypothesis_id, candidate, payload['created_at'], TrialAttemptStatus.OPEN)
                attempts.append(view)
                by_id[attempt_id] = view
                semantic_ids.add(semantic_id)
                continue
            if kind not in {'ATTEMPT_COMPLETED', 'ATTEMPT_ABORTED'}:
                raise ValueError('unsupported trial-family event kind')
            attempt_id = _sha256(payload.get('attempt_id'), 'attempt_id')
            prior = by_id.get(attempt_id)
            if prior is None:
                raise ValueError('terminal event references unknown attempt')
            if prior.status is not TrialAttemptStatus.OPEN:
                raise ValueError('attempt has multiple terminal events')
            if kind == 'ATTEMPT_COMPLETED':
                required = {'attempt_id', 'experiment_id', 'experiment_record_sha256', 'result_available_at', 'outcome'}
                if set(payload) != required or payload['result_available_at'] != event['event_at']:
                    raise ValueError('attempt-completion payload fields/time mismatch')
                experiment_id = _text(payload['experiment_id'], 'experiment_id')
                if experiment_id in experiment_ids:
                    raise ValueError('experiment_id cannot complete multiple family attempts')
                experiment_ids.add(experiment_id)
                available = _iso(payload['result_available_at'], 'result_available_at')
                if _instant(available, 'result_available_at') < _instant(prior.created_at, 'created_at'):
                    raise ValueError('attempt result predates attempt creation')
                view = TrialAttemptView(prior.attempt_id, prior.semantic_attempt_id, prior.ordinal, prior.member_authority_id, prior.hypothesis_id, prior.candidate, prior.created_at, TrialAttemptStatus.COMPLETED, experiment_id=experiment_id, experiment_record_sha256=_sha256(payload['experiment_record_sha256'], 'experiment_record_sha256'), result_available_at=available, outcome=ResearchOutcome(payload['outcome']))
            else:
                required = {'attempt_id', 'aborted_at', 'reason'}
                if set(payload) != required or payload['aborted_at'] != event['event_at']:
                    raise ValueError('attempt-abort payload fields/time mismatch')
                aborted_at = _iso(payload['aborted_at'], 'aborted_at')
                if _instant(aborted_at, 'aborted_at') < _instant(prior.created_at, 'created_at'):
                    raise ValueError('attempt abort predates attempt creation')
                view = TrialAttemptView(prior.attempt_id, prior.semantic_attempt_id, prior.ordinal, prior.member_authority_id, prior.hypothesis_id, prior.candidate, prior.created_at, TrialAttemptStatus.ABORTED, aborted_at=aborted_at, abort_reason=_text(payload['reason'], 'abort reason'))
            attempts[prior.ordinal - 1] = view
            by_id[attempt_id] = view
        return tuple(attempts)

    def _append_event(self, kind: str, event_at: str, payload: dict[str, Any], *, locked_payload_factory: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None=None) -> None:
        if kind not in _EVENT_KINDS:
            raise ValueError('unsupported trial-family event kind')
        _iso(event_at, 'event_at')
        if locked_payload_factory is not None and not callable(locked_payload_factory):
            raise TypeError('locked_payload_factory must be callable')
        with WorkspaceEconomicLock(self.workspace_root):
            state = self._read_state()
            if locked_payload_factory is not None:
                payload = locked_payload_factory(state, payload)
                if type(payload) is not dict:
                    raise TypeError('locked_payload_factory must return a dict')
            family = TrialFamilyDefinition.from_payload(state['family'])
            events = list(state['events'])
            if _instant(event_at, 'event_at') < _instant(family.frozen_at, 'family.frozen_at'):
                raise ValueError('trial-family event predates frozen family')
            prior_assessments = self._sequential(state).assessments()
            if prior_assessments:
                latest_sequential_at = max(
                    _instant(value.evidence.observed_at, 'observed_at')
                    for value in prior_assessments
                )
                if _instant(event_at, 'event_at') < latest_sequential_at:
                    raise ValueError('event_at must not precede durable sequential history')
            if events and _instant(event_at, 'event_at') < _instant(events[-1]['event_at'], 'previous event_at'):
                raise ValueError('event_at must not precede durable event history')
            previous_sha = events[-1]['event_sha256'] if events else None
            envelope = {'sequence': len(events) + 1, 'kind': kind, 'event_at': event_at, 'payload': payload, 'prev_event_sha256': previous_sha}
            event = {**envelope, 'event_sha256': _digest(envelope)}
            next_state = {**state, 'events': [*events, event]}
            plan = ExperimentFamilyPlan.from_payload(state['multiplicity_plan'])
            self._replay_events(family, plan, tuple(next_state['events']), cutoff=None)
            observed = sha256_file(self.path)
            intended = _state_sha256(next_state)
            binding = _digest({'family_id': family.family_id, 'event_sha256': event['event_sha256']})
            tx_id = f"event-{event['event_sha256']}"
            authority = self._authority(family.family_id)
            authority.prepare(tx_id=tx_id, observed_state_sha256=observed, intended_state_sha256=intended, semantic_binding_sha256=binding)
            atomic_write_json(self.path, next_state)
            published = sha256_file(self.path)
            if published != intended:
                raise ValueError('published trial-family bytes do not match prepared authority digest')
            authority.commit(tx_id=tx_id, observed_state_sha256=published, semantic_binding_sha256=binding)
            self._read_state()

    def _attempts(self, *, as_of: str | None=None) -> tuple[TrialAttemptView, ...]:
        state = self._read_state()
        cutoff = None if as_of is None else _instant(as_of, 'as_of')
        return self._replay_events(TrialFamilyDefinition.from_payload(state['family']), ExperimentFamilyPlan.from_payload(state['multiplicity_plan']), tuple(state['events']), cutoff=cutoff)

    def start_attempt(self, *, semantic_attempt_id: str, member_authority_id: str, candidate: TrialCandidateLineage, created_at: str) -> TrialAttemptView:
        semantic_attempt_id = _text(semantic_attempt_id, 'semantic_attempt_id')
        if type(candidate) is not TrialCandidateLineage:
            raise TypeError('candidate must be TrialCandidateLineage')
        _iso(created_at, 'created_at')
        candidate_payload = candidate.to_payload()
        base_payload = {'semantic_attempt_id': semantic_attempt_id, 'member_authority_id': member_authority_id, 'candidate': candidate_payload, 'created_at': created_at}
        created_attempt_id: str | None = None

        def bind_attempt_under_lock(state: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
            nonlocal created_attempt_id
            family = TrialFamilyDefinition.from_payload(state['family'])
            plan = ExperimentFamilyPlan.from_payload(state['multiplicity_plan'])
            member = plan.member(member_authority_id)
            if candidate.semantic_sha256 != member.semantic_variant_sha256:
                raise ValueError('candidate lineage does not match frozen family member')
            attempts = self._replay_events(family, plan, tuple(state['events']), cutoff=None)
            for prior in attempts:
                if prior.semantic_attempt_id != semantic_attempt_id:
                    continue
                if prior.member_authority_id == member.member_authority_id and prior.candidate == candidate and prior.created_at == created_at:
                    raise _AttemptReplay(prior)
                raise ValueError('conflicting duplicate semantic attempt')
            ordinal = len(attempts) + 1
            attempt_id = _digest({'family_id': family.family_id, 'semantic_attempt_id': semantic_attempt_id, 'ordinal': ordinal, 'member_authority_id': member.member_authority_id, 'candidate': candidate_payload, 'created_at': created_at})
            created_attempt_id = attempt_id
            registry_state = self._registry(state)._read()
            record_count = len(registry_state['records'])
            return {'attempt_id': attempt_id, 'semantic_attempt_id': semantic_attempt_id, 'ordinal': ordinal, 'member_authority_id': member.member_authority_id, 'hypothesis_id': member.hypothesis_id, 'candidate': candidate_payload, 'created_at': created_at, 'registry_record_count': record_count, 'registry_prefix_sha256': _registry_prefix_sha256(registry_state, record_count)}

        try:
            self._append_event('ATTEMPT_STARTED', created_at, base_payload, locked_payload_factory=bind_attempt_under_lock)
        except _AttemptReplay as replay:
            return replay.attempt
        if created_attempt_id is None:
            raise RuntimeError('trial attempt publication did not produce an attempt identity')
        return next((v for v in self._attempts() if v.attempt_id == created_attempt_id))

    @staticmethod
    def _require_experiment_matches(attempt: TrialAttemptView, family: TrialFamilyDefinition, payload: Mapping[str, Any]) -> None:
        expected = {'research_protocol_id': family.research_protocol_id, 'strategy_version_id': attempt.candidate.strategy_version_id, 'model_version_id': attempt.candidate.model_version_id, 'dataset_snapshot_id': attempt.candidate.dataset_snapshot_id, 'feature_set_id': attempt.candidate.feature_set_id, 'seed': attempt.candidate.seed, 'config_sha256': attempt.candidate.config_sha256.lower()}
        for field, wanted in expected.items():
            if payload.get(field) != wanted:
                raise ValueError(f'Experiment {field} does not match durable trial candidate/research protocol')

    @staticmethod
    def _require_registry_record_after_attempt_witness(registry: ScientificRegistry, start_payload: Mapping[str, Any], record_type: str, record_id: str, label: str) -> int:
        registry_state = registry._read()
        record_count = start_payload.get('registry_record_count')
        witnessed = _sha256(start_payload.get('registry_prefix_sha256'), 'registry_prefix_sha256')
        if _registry_prefix_sha256(registry_state, record_count) != witnessed:
            raise ValueError('ScientificRegistry no longer matches the durable attempt-start prefix witness')
        position: int | None = None
        for index, raw in enumerate(registry_state['records']):
            if raw['record_type'] == record_type and raw['record_id'] == record_id:
                position = index
                break
        if position is None:
            raise ValueError(f'{label} is missing from ScientificRegistry')
        if position < record_count:
            raise ValueError(f'{label} was already durable before trial attempt publication')
        return position

    @classmethod
    def _require_evaluation_bundle_matches(cls, attempt: TrialAttemptView, family: TrialFamilyDefinition, registry: ScientificRegistry, start_payload: Mapping[str, Any], experiment_id: str, experiment_available_at: str, bundle: Any) -> None:
        expected = {
            'dataset_snapshot_id': attempt.candidate.dataset_snapshot_id,
            'protocol_sha256': family.protocol_sha256,
            'evaluated_strategy_version_id': attempt.candidate.strategy_version_id,
            'evaluated_model_version_id': attempt.candidate.model_version_id,
        }
        for field, wanted in expected.items():
            if bundle.payload.get(field) != wanted:
                raise ValueError(f'Experiment EvaluationBundle {field} does not match durable trial candidate/research protocol')
        cls._require_registry_record_after_attempt_witness(registry, start_payload, 'EvaluationBundle', bundle.record_id, 'Experiment EvaluationBundle')
        if _instant(bundle.available_at, 'EvaluationBundle.available_at') < _instant(attempt.created_at, 'attempt.created_at'):
            raise ValueError('Experiment EvaluationBundle predates trial attempt')
        if _instant(bundle.available_at, 'EvaluationBundle.available_at') > _instant(experiment_available_at, 'Experiment.available_at'):
            raise ValueError('Experiment EvaluationBundle became available after the Experiment result')
        if not ScientificRegistry.causal_precedes(registry, 'EvaluationBundle', bundle.record_id, 'Experiment', experiment_id):
            raise ValueError('Experiment EvaluationBundle must be durably published before the Experiment')

    def _require_canonical_registry(self, registry: ScientificRegistry, state: dict[str, Any]) -> ScientificRegistry:
        if type(registry) is not ScientificRegistry:
            raise TypeError('registry must be ScientificRegistry')
        canonical = self._registry(state)
        if registry.path.resolve(strict=False) != canonical.path.resolve(strict=False):
            raise ValueError('registry does not match the ScientificRegistry bound to this trial family')
        return canonical

    @staticmethod
    def _attempt_start_payload(state: Mapping[str, Any], attempt_id: str) -> dict[str, Any]:
        for event in state['events']:
            if event['kind'] == 'ATTEMPT_STARTED' and event['payload'].get('attempt_id') == attempt_id:
                return event['payload']
        raise ValueError('attempt start evidence is missing from durable trial history')

    @classmethod
    def _require_experiment_after_attempt_witness(cls, registry: ScientificRegistry, start_payload: Mapping[str, Any], experiment_id: str) -> None:
        cls._require_registry_record_after_attempt_witness(registry, start_payload, 'Experiment', experiment_id, 'Experiment')

    def complete_attempt(self, *, attempt_id: str, experiment_id: str, registry: ScientificRegistry) -> TrialAttemptView:
        attempt_id, experiment_id = (_sha256(attempt_id, 'attempt_id'), _text(experiment_id, 'experiment_id'))
        state = self._read_state()
        registry = self._require_canonical_registry(registry, state)
        family = TrialFamilyDefinition.from_payload(state['family'])
        plan = ExperimentFamilyPlan.from_payload(state['multiplicity_plan'])
        attempts = self._replay_events(family, plan, tuple(state['events']), cutoff=None)
        attempt = next((v for v in attempts if v.attempt_id == attempt_id), None)
        if attempt is None:
            raise ValueError('attempt_id is not in the durable trial family')
        for prior in attempts:
            if prior.attempt_id != attempt_id and prior.status is TrialAttemptStatus.COMPLETED and prior.experiment_id == experiment_id:
                raise ValueError('experiment_id is already consumed by another family attempt')
        entry = registry.get('Experiment', experiment_id)
        if entry is None:
            raise ValueError('Experiment is missing from ScientificRegistry')
        start_payload = self._attempt_start_payload(state, attempt_id)
        self._require_experiment_after_attempt_witness(registry, start_payload, experiment_id)
        self._require_experiment_matches(attempt, family, entry.payload)
        available = _iso(entry.available_at, 'Experiment.available_at')
        bundle_id = _text(entry.payload.get('evaluation_bundle_id'), 'evaluation_bundle_id')
        bundle = registry.get('EvaluationBundle', bundle_id)
        if bundle is None:
            raise ValueError('Experiment EvaluationBundle is missing from ScientificRegistry')
        self._require_evaluation_bundle_matches(attempt, family, registry, start_payload, experiment_id, available, bundle)
        outcome = ResearchOutcome(entry.payload.get('outcome'))
        if _instant(available, 'Experiment.available_at') < _instant(attempt.created_at, 'attempt.created_at'):
            raise ValueError('Experiment result predates trial attempt')
        if attempt.status is TrialAttemptStatus.COMPLETED:
            if attempt.experiment_id == experiment_id and attempt.experiment_record_sha256 == entry.record_sha256 and attempt.result_available_at == available and attempt.outcome is outcome:
                return attempt
            raise ValueError('attempt already completed with conflicting Experiment')
        if attempt.status is TrialAttemptStatus.ABORTED:
            raise ValueError('aborted attempt cannot later become completed')
        self._append_event('ATTEMPT_COMPLETED', available, {'attempt_id': attempt_id, 'experiment_id': experiment_id, 'experiment_record_sha256': entry.record_sha256, 'result_available_at': available, 'outcome': outcome.value})
        return next((v for v in self._attempts() if v.attempt_id == attempt_id))

    def abort_attempt(self, *, attempt_id: str, aborted_at: str, reason: str) -> TrialAttemptView:
        attempt_id, aborted_at, reason = (_sha256(attempt_id, 'attempt_id'), _iso(aborted_at, 'aborted_at'), _text(reason, 'reason'))
        attempt = next((v for v in self._attempts() if v.attempt_id == attempt_id), None)
        if attempt is None:
            raise ValueError('attempt_id is not in the durable trial family')
        if attempt.status is TrialAttemptStatus.ABORTED:
            if attempt.aborted_at == aborted_at and attempt.abort_reason == reason:
                return attempt
            raise ValueError('attempt already aborted with conflicting terminal evidence')
        if attempt.status is TrialAttemptStatus.COMPLETED:
            raise ValueError('completed attempt cannot later become aborted')
        if _instant(aborted_at, 'aborted_at') < _instant(attempt.created_at, 'attempt.created_at'):
            raise ValueError('attempt abort predates attempt creation')
        self._append_event('ATTEMPT_ABORTED', aborted_at, {'attempt_id': attempt_id, 'aborted_at': aborted_at, 'reason': reason})
        return next((v for v in self._attempts() if v.attempt_id == attempt_id))

    def register_sequential_look(self, *, attempt_id: str, evidence: SequentialLookEvidence, registry: ScientificRegistry) -> SequentialDecision:
        attempt_id = _sha256(attempt_id, 'attempt_id')
        if type(evidence) is not SequentialLookEvidence:
            raise TypeError('evidence must be SequentialLookEvidence')
        state = self._read_state()
        registry = self._require_canonical_registry(registry, state)
        store = self._sequential(state)

        def locked_precondition() -> None:
            state = self._read_state()
            family = TrialFamilyDefinition.from_payload(state['family'])
            plan = ExperimentFamilyPlan.from_payload(state['multiplicity_plan'])
            attempts = self._replay_events(family, plan, tuple(state['events']), cutoff=None)
            attempt = next((v for v in attempts if v.attempt_id == attempt_id), None)
            if attempt is None or attempt.status is not TrialAttemptStatus.COMPLETED:
                raise ValueError('sequential look requires a completed durable attempt')
            if evidence.member_authority_id != attempt.member_authority_id or evidence.experiment_id != attempt.experiment_id or evidence.classification is not attempt.outcome:
                raise ValueError('sequential look does not match completed durable attempt')
            if _instant(evidence.observed_at, 'observed_at') < _instant(attempt.result_available_at, 'result_available_at'):
                raise ValueError('sequential look predates durable experiment result')
            experiment = registry.get('Experiment', evidence.experiment_id)
            if experiment is None or experiment.record_sha256 != attempt.experiment_record_sha256:
                raise ValueError('sequential look Experiment does not match durable registry truth')
            bundle = registry.get('EvaluationBundle', evidence.evaluation_bundle_id)
            if bundle is None or experiment.payload.get('evaluation_bundle_id') != evidence.evaluation_bundle_id or bundle.payload.get('bundle_sha256') != evidence.evaluation_bundle_sha256:
                raise ValueError('sequential look EvaluationBundle does not match durable registry truth')
            start_payload = self._attempt_start_payload(state, attempt_id)
            self._require_evaluation_bundle_matches(attempt, family, registry, start_payload, evidence.experiment_id, experiment.available_at, bundle)
            prior_assessments = store.assessments()
            causal_times = [_instant(raw['event_at'], 'event_at') for raw in state['events']] + [_instant(value.evidence.observed_at, 'observed_at') for value in prior_assessments]
            if causal_times and _instant(evidence.observed_at, 'observed_at') < max(causal_times):
                raise ValueError('sequential look must not backdate durable family history')
            for prior in store.assessments(evidence.member_authority_id):
                if prior.evidence.look_index == evidence.look_index:
                    if prior.evidence.evidence_sha256 == evidence.evidence_sha256:
                        raise _SequentialReplay(prior.decision)
                    raise ValueError('conflicting sequential evidence reuses a durable look index')

        try:
            return store.append(evidence, locked_precondition=locked_precondition).decision
        except _SequentialReplay as replay:
            return replay.decision

    def attempts(self, *, as_of: str | None=None) -> tuple[TrialAttemptView, ...]:
        return self._attempts(as_of=as_of)

    def snapshot(self, *, as_of: str) -> TrialFamilySnapshot:
        cutoff_text = _iso(as_of, 'as_of')
        cutoff = _instant(cutoff_text, 'as_of')
        attempts = self._attempts(as_of=cutoff_text)
        outcomes = [v.outcome for v in attempts if v.status is TrialAttemptStatus.COMPLETED]
        looks = sum((_instant(a.evidence.observed_at, 'observed_at') <= cutoff for a in self._sequential().assessments()))
        return TrialFamilySnapshot(cutoff_text, len(attempts), sum((v.status is TrialAttemptStatus.COMPLETED for v in attempts)), sum((v.status is TrialAttemptStatus.ABORTED for v in attempts)), sum((v.status is TrialAttemptStatus.OPEN for v in attempts)), outcomes.count(ResearchOutcome.POSITIVE), outcomes.count(ResearchOutcome.NEGATIVE), outcomes.count(ResearchOutcome.NULL), outcomes.count(ResearchOutcome.HARMFUL), outcomes.count(ResearchOutcome.INCONCLUSIVE), looks)

    def assert_promotion_evidence_eligible(self, *, evidence: PromotionEvidence, registry: ScientificRegistry, accounted_attempt_count: int) -> TrialFamilySnapshot:
        if type(evidence) is not PromotionEvidence:
            raise TypeError('evidence must be PromotionEvidence')
        state = self._read_state()
        registry = self._require_canonical_registry(registry, state)
        if type(accounted_attempt_count) is not int or accounted_attempt_count < 0:
            raise ValueError('accounted_attempt_count must be a non-negative integer')
        family = TrialFamilyDefinition.from_payload(state['family'])
        if evidence.confirmation_trial_family_id != family.family_id:
            raise ValueError('PromotionEvidence is bound to another trial family')
        if evidence.research_protocol_id != family.research_protocol_id or evidence.research_question_id != family.research_question_id or evidence.hypothesis_id != family.hypothesis_id:
            raise ValueError('PromotionEvidence scientific lineage mismatch')
        if evidence.stopping_rule_sha256 != family.stopping_rule_sha256:
            raise ValueError('PromotionEvidence stopping rule does not match durable family')
        if evidence.multiple_comparison_control_sha256 != family.multiple_comparison_control_sha256:
            raise ValueError('PromotionEvidence multiple-comparison control does not match durable family')
        durable = registry.get('PromotionEvidence', evidence.promotion_evidence_id)
        if durable is None or durable.payload != evidence.to_payload():
            raise ValueError('PromotionEvidence is not exact durable ScientificRegistry truth')
        snapshot = self.snapshot(as_of=evidence.created_at)
        if snapshot.total_attempts != accounted_attempt_count:
            raise ValueError('accounted-attempt count does not equal durable trial-family truth')
        if snapshot.open_attempts:
            raise ValueError('trial-family attempt remains open at promotion evidence time')
        attempts = self._attempts(as_of=evidence.created_at)
        matching = [a for a in attempts if a.status is TrialAttemptStatus.COMPLETED and a.experiment_id == evidence.experiment_id]
        if len(matching) != 1 or matching[0].outcome is not ResearchOutcome.POSITIVE:
            raise ValueError('PromotionEvidence experiment is not exactly one positive completed family attempt')
        target = matching[0]
        if evidence.candidate_strategy_version_id != target.candidate.strategy_version_id or evidence.candidate_model_version_id != target.candidate.model_version_id or evidence.dataset_snapshot_id != target.candidate.dataset_snapshot_id:
            raise ValueError('PromotionEvidence candidate does not match durable attempt')
        for attempt in attempts:
            if attempt.status is TrialAttemptStatus.COMPLETED:
                durable_experiment = registry.get('Experiment', attempt.experiment_id or '')
                if durable_experiment is None or durable_experiment.record_sha256 != attempt.experiment_record_sha256:
                    raise ValueError('completed attempt no longer matches durable ScientificRegistry truth')
                self._require_experiment_matches(attempt, family, durable_experiment.payload)
                bundle_id = _text(durable_experiment.payload.get('evaluation_bundle_id'), 'evaluation_bundle_id')
                bundle = registry.get('EvaluationBundle', bundle_id)
                if bundle is None:
                    raise ValueError('completed attempt EvaluationBundle is missing from ScientificRegistry')
                start_payload = self._attempt_start_payload(state, attempt.attempt_id)
                self._require_evaluation_bundle_matches(attempt, family, registry, start_payload, durable_experiment.record_id, durable_experiment.available_at, bundle)
        target_looks = [a for a in self._sequential().assessments() if a.evidence.experiment_id == evidence.experiment_id and _instant(a.evidence.observed_at, 'observed_at') <= _instant(evidence.created_at, 'created_at')]
        if not target_looks:
            raise ValueError('promotion eligibility requires registered sequential evidence')
        if target_looks[-1].decision is not SequentialDecision.REJECT_NULL:
            raise ValueError('sequential evidence does not authorize rejecting the null hypothesis')
        return snapshot
__all__ = ['TrialAttemptStatus', 'TrialAttemptView', 'TrialCandidateLineage', 'TrialFamilyAccountingStore', 'TrialFamilyDefinition', 'TrialFamilySnapshot']