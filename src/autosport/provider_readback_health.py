from __future__ import annotations
from dataclasses import InitVar, dataclass, field, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Iterable
from .bookmaker_capability import BookmakerAccountSnapshot, BookmakerCapability

class ProviderReadbackHealthError(ValueError):
    pass

class ProviderReadbackHealthState(str, Enum):
    FRESH_COMPLETE = 'fresh_complete'
    FRESH_PARTIAL = 'fresh_partial'
    STALE_LAST_KNOWN = 'stale_last_known'
    UNAVAILABLE_TRANSIENT = 'unavailable_transient'
    UNAUTHORIZED_OR_SESSION_EXPIRED = 'unauthorized_or_session_expired'
    CONFLICTING = 'conflicting'
    UNKNOWN = 'unknown'

class ProviderReadbackFailureClass(str, Enum):
    TIMEOUT = 'timeout'
    THROTTLED = 'throttled'
    PROVIDER_5XX = 'provider_5xx'
    TRANSPORT = 'transport'
    UNAUTHORIZED = 'unauthorized'
    SESSION_EXPIRED = 'session_expired'
    MALFORMED_RESPONSE = 'malformed_response'
    CONFLICTING = 'conflicting'
    UNKNOWN = 'unknown'
_ISSUER = object()
_HEX = frozenset('0123456789abcdef')

def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or ('\x00' in value):
        raise ProviderReadbackHealthError(f'{field} must be non-empty canonical text')
    return value

def _sha(value: object, field: str) -> str:
    raw = _text(value, field)
    if len(raw) != 64 or any((ch not in _HEX for ch in raw)):
        raise ProviderReadbackHealthError(f'{field} must be lowercase SHA-256 hex')
    return raw

def _time(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ProviderReadbackHealthError(f'{field} must be ISO-8601') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderReadbackHealthError(f'{field} must be timezone-aware')
    return parsed

def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf-8')
    return sha256(payload).hexdigest()

def _canonical(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ProviderReadbackHealthError('snapshot contains non-finite Decimal')
        return str(value)
    if is_dataclass(value):
        return {field.name: _canonical(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, frozenset):
        normalized = [_canonical(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True, separators=(',', ':'), ensure_ascii=True))
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise ProviderReadbackHealthError(f'unsupported canonical evidence type: {type(value).__name__}')

def canonical_snapshot_sha256(snapshot: BookmakerAccountSnapshot) -> str:
    if not isinstance(snapshot, BookmakerAccountSnapshot):
        raise ProviderReadbackHealthError('snapshot must be canonical BookmakerAccountSnapshot')
    return _digest(_canonical(snapshot))

@dataclass(frozen=True, slots=True)
class ProviderReadScopeEvidence:
    capability: BookmakerCapability
    request_scope_sha256: str
    response_sha256: str
    observed_at: str
    snapshot_sha256: str | None = None
    complete: bool = True
    _issuer: InitVar[object | None] = None
    _issuer_token: object | None = field(init=False, repr=False, compare=False, default=None)
    _authority_seal_sha256: str | None = field(init=False, repr=False, compare=False, default=None)

    def __post_init__(self, _issuer: object | None) -> None:
        if not isinstance(self.capability, BookmakerCapability):
            raise ProviderReadbackHealthError('capability must be BookmakerCapability')
        _sha(self.request_scope_sha256, 'request_scope_sha256')
        _sha(self.response_sha256, 'response_sha256')
        _time(self.observed_at, 'observed_at')
        if self.snapshot_sha256 is not None:
            _sha(self.snapshot_sha256, 'snapshot_sha256')
        if type(self.complete) is not bool:
            raise ProviderReadbackHealthError('complete must be bool')
        if self.complete and _issuer is not _ISSUER:
            raise ProviderReadbackHealthError('complete provider read scope must be product-issued')
        issuer_token = _ISSUER if _issuer is _ISSUER else None
        object.__setattr__(self, '_issuer_token', issuer_token)
        object.__setattr__(
            self,
            '_authority_seal_sha256',
            self.evidence_id if self.complete and issuer_token is _ISSUER else None,
        )

    @property
    def evidence_id(self) -> str:
        return _digest({'capability': self.capability.value, 'request_scope_sha256': self.request_scope_sha256, 'response_sha256': self.response_sha256, 'observed_at': self.observed_at, 'snapshot_sha256': self.snapshot_sha256, 'complete': self.complete})

def _scope_has_intact_complete_authority(scope: ProviderReadScopeEvidence) -> bool:
    try:
        return (
            isinstance(scope, ProviderReadScopeEvidence)
            and scope.complete
            and scope._issuer_token is _ISSUER
            and scope._authority_seal_sha256 is not None
            and scope._authority_seal_sha256 == scope.evidence_id
        )
    except (AttributeError, ProviderReadbackHealthError, TypeError, ValueError):
        return False

def _issue_complete_provider_read_scope_evidence(*, capability: BookmakerCapability, request_scope_sha256: str, response_sha256: str, observed_at: str, snapshot: BookmakerAccountSnapshot) -> ProviderReadScopeEvidence:
    if not isinstance(snapshot, BookmakerAccountSnapshot):
        raise ProviderReadbackHealthError('snapshot must be canonical BookmakerAccountSnapshot')
    if not isinstance(capability, BookmakerCapability):
        raise ProviderReadbackHealthError('capability must be BookmakerCapability')
    if capability not in snapshot.observed_capabilities:
        raise ProviderReadbackHealthError('complete scope capability must be present in canonical snapshot')
    return ProviderReadScopeEvidence(capability=capability, request_scope_sha256=request_scope_sha256, response_sha256=response_sha256, observed_at=observed_at, snapshot_sha256=canonical_snapshot_sha256(snapshot), complete=True, _issuer=_ISSUER)

@dataclass(frozen=True, slots=True)
class ProviderReadbackHealth:
    venue_id: str
    account_id: str
    adapter_id: str
    state: ProviderReadbackHealthState
    evaluated_at: str
    freshness_limit_seconds: int
    required_capabilities: tuple[BookmakerCapability, ...]
    successful_scopes: tuple[ProviderReadScopeEvidence, ...]
    current_snapshot_sha256: str | None
    last_known_snapshot_sha256: str | None
    failure_class: ProviderReadbackFailureClass | None = None
    _issuer: InitVar[object | None] = None
    _issuer_token: object | None = field(init=False, repr=False, compare=False, default=None)
    _authority_seal_sha256: str | None = field(init=False, repr=False, compare=False, default=None)

    def __post_init__(self, _issuer: object | None) -> None:
        _text(self.venue_id, 'venue_id')
        _text(self.account_id, 'account_id')
        _text(self.adapter_id, 'adapter_id')
        if not isinstance(self.state, ProviderReadbackHealthState):
            raise ProviderReadbackHealthError('state must be ProviderReadbackHealthState')
        _time(self.evaluated_at, 'evaluated_at')
        if type(self.freshness_limit_seconds) is not int or self.freshness_limit_seconds < 0:
            raise ProviderReadbackHealthError('freshness_limit_seconds must be non-negative int')
        if not isinstance(self.required_capabilities, tuple) or not self.required_capabilities:
            raise ProviderReadbackHealthError('required_capabilities must be a non-empty tuple')
        if any((not isinstance(item, BookmakerCapability) for item in self.required_capabilities)):
            raise ProviderReadbackHealthError('required_capabilities must contain BookmakerCapability values')
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ProviderReadbackHealthError('required_capabilities contain duplicates')
        if not isinstance(self.successful_scopes, tuple):
            raise ProviderReadbackHealthError('successful_scopes must be tuple')
        if any((not isinstance(scope, ProviderReadScopeEvidence) for scope in self.successful_scopes)):
            raise ProviderReadbackHealthError('successful_scopes must contain ProviderReadScopeEvidence')
        scope_caps = tuple((scope.capability for scope in self.successful_scopes))
        if len(set(scope_caps)) != len(scope_caps):
            raise ProviderReadbackHealthError('successful_scopes contain duplicate capabilities')
        if self.current_snapshot_sha256 is not None:
            _sha(self.current_snapshot_sha256, 'current_snapshot_sha256')
        if self.last_known_snapshot_sha256 is not None:
            _sha(self.last_known_snapshot_sha256, 'last_known_snapshot_sha256')
        if self.failure_class is not None and (not isinstance(self.failure_class, ProviderReadbackFailureClass)):
            raise ProviderReadbackHealthError('failure_class must be ProviderReadbackFailureClass')
        if self.state is ProviderReadbackHealthState.FRESH_COMPLETE:
            if _issuer is not _ISSUER:
                raise ProviderReadbackHealthError('FRESH_COMPLETE health must be product-issued')
            if self.current_snapshot_sha256 is None or self.failure_class is not None:
                raise ProviderReadbackHealthError('FRESH_COMPLETE requires current snapshot and no failure')
            if set(scope_caps) != set(self.required_capabilities):
                raise ProviderReadbackHealthError('FRESH_COMPLETE requires every exact required scope')
            if any((not scope.complete for scope in self.successful_scopes)):
                raise ProviderReadbackHealthError('FRESH_COMPLETE requires complete scopes')
            if any((not _scope_has_intact_complete_authority(scope) for scope in self.successful_scopes)):
                raise ProviderReadbackHealthError('FRESH_COMPLETE requires intact product-issued complete scopes')
            if any((scope.snapshot_sha256 != self.current_snapshot_sha256 for scope in self.successful_scopes)):
                raise ProviderReadbackHealthError('FRESH_COMPLETE requires scopes bound to the exact current snapshot')

        issued_positive = (
            self.state is ProviderReadbackHealthState.FRESH_COMPLETE
            and _issuer is _ISSUER
        )
        object.__setattr__(
            self,
            '_issuer_token',
            _ISSUER if issued_positive else None,
        )
        object.__setattr__(
            self,
            '_authority_seal_sha256',
            _digest(self.to_canonical_dict()) if issued_positive else None,
        )

    def _positive_authority_is_intact(self) -> bool:
        if (
            self.state is not ProviderReadbackHealthState.FRESH_COMPLETE
            or self._issuer_token is not _ISSUER
            or self._authority_seal_sha256 is None
        ):
            return False
        try:
            if self.current_snapshot_sha256 is None or self.failure_class is not None:
                return False
            if set(scope.capability for scope in self.successful_scopes) != set(
                self.required_capabilities
            ):
                return False
            if any(
                not _scope_has_intact_complete_authority(scope)
                for scope in self.successful_scopes
            ):
                return False
            if any(
                scope.snapshot_sha256 != self.current_snapshot_sha256
                for scope in self.successful_scopes
            ):
                return False
            return self._authority_seal_sha256 == _digest(self.to_canonical_dict())
        except (AttributeError, ProviderReadbackHealthError, TypeError, ValueError):
            return False

    @property
    def can_authorize_current_state(self) -> bool:
        return self._positive_authority_is_intact()

    def scope_is_authoritative(self, capability: BookmakerCapability) -> bool:
        if not self.can_authorize_current_state:
            return False
        return any(
            scope.capability is capability
            and _scope_has_intact_complete_authority(scope)
            and scope.snapshot_sha256 == self.current_snapshot_sha256
            for scope in self.successful_scopes
        )

    @property
    def operator_status_uk(self) -> str:
        if (
            self.state is ProviderReadbackHealthState.FRESH_COMPLETE
            and not self.can_authorize_current_state
        ):
            return 'стан даних букмекера невідомий'
        return {ProviderReadbackHealthState.FRESH_COMPLETE: 'дані букмекера актуальні', ProviderReadbackHealthState.FRESH_PARTIAL: 'дані букмекера неповні', ProviderReadbackHealthState.STALE_LAST_KNOWN: 'дані букмекера застарілі', ProviderReadbackHealthState.UNAVAILABLE_TRANSIENT: 'дані букмекера тимчасово недоступні', ProviderReadbackHealthState.UNAUTHORIZED_OR_SESSION_EXPIRED: 'сесію букмекера треба відновити', ProviderReadbackHealthState.CONFLICTING: 'дані букмекера суперечливі', ProviderReadbackHealthState.UNKNOWN: 'стан даних букмекера невідомий'}[self.state]

    @property
    def result_id(self) -> str:
        return _digest(self.to_canonical_dict())

    def to_canonical_dict(self) -> dict[str, object]:
        return {'schema': 'autosport.provider_readback_health', 'schema_version': 1, 'venue_id': self.venue_id, 'account_id': self.account_id, 'adapter_id': self.adapter_id, 'state': self.state.value, 'evaluated_at': self.evaluated_at, 'freshness_limit_seconds': self.freshness_limit_seconds, 'required_capabilities': [item.value for item in self.required_capabilities], 'successful_scopes': [{'capability': scope.capability.value, 'complete': scope.complete, 'snapshot_sha256': scope.snapshot_sha256, 'evidence_id': scope.evidence_id} for scope in self.successful_scopes], 'current_snapshot_sha256': self.current_snapshot_sha256, 'last_known_snapshot_sha256': self.last_known_snapshot_sha256, 'failure_class': None if self.failure_class is None else self.failure_class.value}

def _ordered_capabilities(capabilities: Iterable[BookmakerCapability]) -> tuple[BookmakerCapability, ...]:
    values = tuple(capabilities)
    if not values:
        raise ProviderReadbackHealthError('required_capabilities must not be empty')
    if any((not isinstance(item, BookmakerCapability) for item in values)):
        raise ProviderReadbackHealthError('required_capabilities must contain BookmakerCapability values')
    if len(set(values)) != len(values):
        raise ProviderReadbackHealthError('required_capabilities must not contain duplicates')
    return tuple(sorted(values, key=lambda item: item.value))

def classify_provider_readback_health(*, required_capabilities: Iterable[BookmakerCapability], evaluated_at: str, freshness_limit_seconds: int, snapshot: BookmakerAccountSnapshot | None=None, successful_scopes: Iterable[ProviderReadScopeEvidence]=(), failure_class: ProviderReadbackFailureClass | None=None, last_known_snapshot: BookmakerAccountSnapshot | None=None) -> ProviderReadbackHealth:
    required = _ordered_capabilities(required_capabilities)
    now = _time(evaluated_at, 'evaluated_at')
    if type(freshness_limit_seconds) is not int or freshness_limit_seconds < 0:
        raise ProviderReadbackHealthError('freshness_limit_seconds must be non-negative int')
    scopes = tuple(sorted(tuple(successful_scopes), key=lambda item: item.capability.value))
    if any((not isinstance(scope, ProviderReadScopeEvidence) for scope in scopes)):
        raise ProviderReadbackHealthError('successful_scopes must contain ProviderReadScopeEvidence')
    if len({scope.capability for scope in scopes}) != len(scopes):
        raise ProviderReadbackHealthError('successful_scopes contain duplicate capabilities')
    if any((scope.capability not in required for scope in scopes)):
        raise ProviderReadbackHealthError('successful scope is outside required_capabilities')
    if failure_class is not None and (not isinstance(failure_class, ProviderReadbackFailureClass)):
        raise ProviderReadbackHealthError('failure_class must be ProviderReadbackFailureClass')
    scope_times = tuple((_time(scope.observed_at, 'successful_scope.observed_at') for scope in scopes))
    if any((item > now for item in scope_times)):
        raise ProviderReadbackHealthError('successful scope observation cannot be after health evaluation')
    scopes_fresh = all(((now - item).total_seconds() <= freshness_limit_seconds for item in scope_times))
    current_sha: str | None = None
    last_sha: str | None = None
    venue_id: str | None = None
    account_id: str | None = None
    adapter_id: str | None = None
    observed_capabilities: set[BookmakerCapability] = set()
    if snapshot is not None:
        current_sha = canonical_snapshot_sha256(snapshot)
        venue_id = snapshot.profile.venue_id
        account_id = snapshot.profile.account_id
        adapter_id = snapshot.profile.adapter_id
        observed_capabilities = set(snapshot.observed_capabilities)
        if any((scope.capability not in observed_capabilities for scope in scopes)):
            raise ProviderReadbackHealthError('successful scope is not present in canonical snapshot')
        if any((scope.snapshot_sha256 is not None and scope.snapshot_sha256 != current_sha for scope in scopes)):
            raise ProviderReadbackHealthError('successful scope is bound to a different canonical snapshot')
    if last_known_snapshot is not None:
        last_sha = canonical_snapshot_sha256(last_known_snapshot)
        last_identity = (last_known_snapshot.profile.venue_id, last_known_snapshot.profile.account_id, last_known_snapshot.profile.adapter_id)
        if venue_id is None:
            venue_id, account_id, adapter_id = last_identity
        elif last_identity != (venue_id, account_id, adapter_id):
            raise ProviderReadbackHealthError('last-known snapshot identity differs from current snapshot')
    if venue_id is None or account_id is None or adapter_id is None:
        raise ProviderReadbackHealthError('snapshot or last_known_snapshot is required to bind provider/account identity')
    snapshot_fresh = False
    if snapshot is not None:
        snapshot_time = _time(snapshot.observed_at, 'snapshot.observed_at')
        if snapshot_time > now:
            raise ProviderReadbackHealthError('snapshot observation cannot be after health evaluation')
        snapshot_fresh = (now - snapshot_time).total_seconds() <= freshness_limit_seconds
    required_set = set(required)
    scope_set = {scope.capability for scope in scopes}
    all_complete = scope_set == required_set and all(
        _scope_has_intact_complete_authority(scope) for scope in scopes
    )
    snapshot_covers_required = snapshot is not None and required_set.issubset(observed_capabilities)
    scopes_bind_current_snapshot = current_sha is not None and scope_set == required_set and all((scope.snapshot_sha256 == current_sha for scope in scopes))
    if failure_class in {ProviderReadbackFailureClass.UNAUTHORIZED, ProviderReadbackFailureClass.SESSION_EXPIRED}:
        state = ProviderReadbackHealthState.UNAUTHORIZED_OR_SESSION_EXPIRED
    elif failure_class is ProviderReadbackFailureClass.CONFLICTING:
        state = ProviderReadbackHealthState.CONFLICTING
    elif failure_class is not None:
        state = ProviderReadbackHealthState.FRESH_PARTIAL if scopes and scopes_fresh else ProviderReadbackHealthState.UNAVAILABLE_TRANSIENT
    elif snapshot is None:
        state = ProviderReadbackHealthState.UNKNOWN
    elif not snapshot_fresh or not scopes_fresh:
        state = ProviderReadbackHealthState.STALE_LAST_KNOWN
    elif snapshot_covers_required and all_complete and scopes_bind_current_snapshot:
        state = ProviderReadbackHealthState.FRESH_COMPLETE
    elif scopes or snapshot.observed_capabilities:
        state = ProviderReadbackHealthState.FRESH_PARTIAL
    else:
        state = ProviderReadbackHealthState.UNKNOWN
    if state is ProviderReadbackHealthState.STALE_LAST_KNOWN and last_sha is None and (current_sha is not None):
        last_sha = current_sha
    return ProviderReadbackHealth(venue_id=venue_id, account_id=account_id, adapter_id=adapter_id, state=state, evaluated_at=evaluated_at, freshness_limit_seconds=freshness_limit_seconds, required_capabilities=required, successful_scopes=scopes, current_snapshot_sha256=current_sha, last_known_snapshot_sha256=last_sha, failure_class=failure_class, _issuer=_ISSUER)
