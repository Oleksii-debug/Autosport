"""Canonical import surface for the local-compute tariff authority.

The record/schema implementation remains byte-preserved in the internal module.
This shim closes the runtime trust seams identified during #1859 review: tariff
rollback operations are non-virtual, the exact constructor-owned #1864 basis
store is sealed, and EconomicGoal identity is re-resolved only through that
stronger canonical basis composition.
"""
from __future__ import annotations

from dataclasses import dataclass
import sys
import weakref

from . import _local_compute_tariff_authority_impl as _impl


_Store = _impl.LocalComputeTariffAuthorityStore
_BasisStore = _impl._CANONICAL_ALLOCATION_BASIS_STORE_CLASS
_BasisResolveCurrent = _impl._CANONICAL_ALLOCATION_BASIS_RESOLVE_CURRENT
_BasisGetattribute = _BasisStore.__getattribute__
_BasisInit = _BasisStore.__init__
_Authority = _impl.MonotonicWorkspaceAuthority
_AuthorityInit = _Authority.__init__
_AuthorityPhase = _impl.AuthorityPhase
_Lock = _impl.WorkspaceEconomicLock
_Path = _impl.Path
_root_resolver = _impl._CANONICAL_LOCAL_COMPUTE_MONOTONIC_AUTHORITY_ROOT
_root_code = getattr(_root_resolver, "__code__", None)
_root_closure = getattr(_root_resolver, "__closure__", None)
try:
    _root_closure_state = tuple(cell.cell_contents for cell in (_root_closure or ()))
except ValueError as exc:  # pragma: no cover - import-time corruption
    raise _impl.LocalComputeTariffError(
        "canonical local-compute authority root closure is invalid"
    ) from exc

_load_records = _Store._load
_product_utc_now = _impl._product_utc_now
_text = _impl._text
_sha = _impl._sha
_currency = _impl._currency
_time = _impl._time
_instant = _impl._instant
_state_payload = _impl._state_payload
_canonical_bytes = _impl._canonical_bytes
_digest = _impl._digest
_intervals_overlap = _impl._intervals_overlap
_atomic_write_json = _impl.atomic_write_json
_sha256_file = _impl.sha256_file
_hashlib = _impl.hashlib
_Record = _impl.LocalComputeTariffRecord
_Treatment = _impl.LocalComputeCostTreatment
_Error = _impl.LocalComputeTariffError
_file_name = _impl.FILE_NAME
_authority_domain = _impl.AUTHORITY_DOMAIN
_authority_key = _impl.AUTHORITY_KEY

_authority_operations = {
    name: getattr(_Authority, name)
    for name in ("read_history", "recover", "prepare", "abort", "commit")
}
_authority_codes = {
    name: getattr(method, "__code__", None)
    for name, method in _authority_operations.items()
}

# No authority-bearing object is recovered from mutable instance attributes.
# The public attributes remain inspectable for compatibility, but every trusted
# operation first requires them to still be the exact constructor-owned objects.
_sealed_state: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class _GoalProjection:
    goal_id: str
    revision: int
    bankroll_id: str
    currency: str


def _require_root() -> object:
    if getattr(_root_resolver, "__code__", None) is not _root_code:
        raise _Error("canonical local-compute authority root resolver code changed")
    live_closure = getattr(_root_resolver, "__closure__", None)
    try:
        live_state = tuple(cell.cell_contents for cell in (live_closure or ()))
    except ValueError as exc:
        raise _Error("canonical local-compute authority root closure changed") from exc
    if (
        len(live_state) != len(_root_closure_state)
        or any(current is not frozen for current, frozen in zip(live_state, _root_closure_state))
    ):
        raise _Error("canonical local-compute authority root closure changed")
    return _root_resolver()


def _require_authority_dispatch(authority: object) -> None:
    if type(authority) is not _Authority:
        raise _Error("local compute tariff authority object changed")
    try:
        instance_state = object.__getattribute__(authority, "__dict__")
    except AttributeError:
        instance_state = {}
    for name, method in _authority_operations.items():
        live = getattr(_Authority, name, None)
        if (
            name in instance_state
            or live is not method
            or getattr(live, "__code__", None) is not _authority_codes[name]
        ):
            raise _Error("local compute tariff authority dispatch changed")


def _state(self):
    try:
        frozen = _sealed_state[self]
    except (KeyError, TypeError) as exc:
        raise _Error("local compute tariff runtime authority is not sealed") from exc
    (
        workspace,
        path,
        basis_store,
        basis_workspace,
        basis_path,
        basis_authority,
        authority,
        authority_root,
    ) = frozen
    try:
        live_workspace = object.__getattribute__(self, "workspace")
        live_path = object.__getattribute__(self, "path")
        live_basis = object.__getattribute__(self, "_basis_store")
        live_authority = object.__getattribute__(self, "_authority")
    except AttributeError as exc:
        raise _Error("local compute tariff runtime authority changed") from exc
    if (
        live_workspace != workspace
        or live_path != path
        or live_basis is not basis_store
        or live_authority is not authority
    ):
        raise _Error("local compute tariff runtime authority changed")
    if type(basis_store) is not _BasisStore:
        raise _Error("local compute allocation basis authority instance changed")
    try:
        live_basis_workspace = object.__getattribute__(basis_store, "workspace")
        live_basis_path = object.__getattribute__(basis_store, "path")
        live_basis_authority = object.__getattribute__(basis_store, "_authority")
    except AttributeError as exc:
        raise _Error("local compute allocation basis authority state changed") from exc
    if (
        live_basis_workspace != basis_workspace
        or live_basis_path != basis_path
        or live_basis_authority is not basis_authority
        or basis_workspace != workspace
    ):
        raise _Error("local compute allocation basis authority state changed")
    if (
        _BasisStore.__getattribute__ is not _BasisGetattribute
        or _BasisStore.__init__ is not _BasisInit
        or _BasisStore.resolve_current is not _BasisResolveCurrent
    ):
        raise _Error("local compute allocation basis authority dispatch changed")
    _require_authority_dispatch(authority)
    if authority.authority_root != authority_root:
        raise _Error("local compute tariff authority root changed")
    if basis_authority.authority_root != authority_root:
        raise _Error("tariff and allocation basis authority roots differ")
    if _require_root() != authority_root:
        raise _Error("canonical local-compute authority root changed")
    return frozen


def _observed(self) -> str | None:
    _workspace, path, *_rest = _state(self)
    return _sha256_file(path) if path.exists() else None


def _sealed_recover(self) -> None:
    frozen = _state(self)
    authority = frozen[6]
    observed = _observed(self)
    history = _authority_operations["read_history"](authority)
    if history and history[-1].phase is _AuthorityPhase.PREPARE:
        pending = history[-1]
        _authority_operations["recover"](
            authority,
            observed_state_sha256=observed,
            tx_id=pending.tx_id,
            semantic_binding_sha256=pending.semantic_binding_sha256,
        )
        return
    _authority_operations["recover"](
        authority,
        observed_state_sha256=observed,
    )


def _basis_authority(self):
    return _state(self)[2]


def _current_goal(self):
    basis_store = _basis_authority(self)
    # #1864's custom __getattribute__ returns its closure-owned sealed current-goal
    # resolver for this name. Calling the captured descriptor means a later class
    # rebind cannot substitute a weaker EconomicGoalStore path.
    current_goal = _BasisGetattribute(basis_store, "_current_goal")
    values = current_goal()
    if type(values) is not tuple or len(values) != 5:
        raise _Error("canonical allocation basis current EconomicGoal projection changed")
    goal_id, revision, bankroll_id, currency, goal_sha256 = values
    if (
        type(goal_id) is not str
        or not goal_id
        or type(revision) is not int
        or isinstance(revision, bool)
        or revision < 1
        or type(bankroll_id) is not str
        or not bankroll_id
        or type(currency) is not str
        or type(goal_sha256) is not str
        or len(goal_sha256) != 64
    ):
        raise _Error("canonical allocation basis current EconomicGoal projection changed")
    return _GoalProjection(goal_id, revision, bankroll_id, currency), goal_sha256


def _sealed_init(self, workspace) -> None:
    authority_root = _require_root()
    workspace_path = _Path(workspace).absolute().resolve(strict=False)
    workspace_path.mkdir(parents=True, exist_ok=True)
    path = workspace_path / _file_name

    # Invoke the captured #1864 initializer on an exact object so rebinding its
    # class constructor cannot redirect this tariff's dependency.
    basis_store = object.__new__(_BasisStore)
    _BasisInit(basis_store, workspace_path)
    basis_workspace = object.__getattribute__(basis_store, "workspace")
    basis_path = object.__getattribute__(basis_store, "path")
    basis_authority = object.__getattribute__(basis_store, "_authority")
    if basis_authority.authority_root != authority_root:
        raise _Error("tariff and allocation basis authority roots differ")

    if _Authority.__init__ is not _AuthorityInit:
        raise _Error("local compute tariff authority constructor changed")
    authority = object.__new__(_Authority)
    _AuthorityInit(
        authority,
        workspace=workspace_path,
        domain=_authority_domain,
        key=_authority_key,
        authority_root=authority_root,
    )

    object.__setattr__(self, "workspace", workspace_path)
    object.__setattr__(self, "path", path)
    object.__setattr__(self, "_basis_store", basis_store)
    object.__setattr__(self, "_authority", authority)
    _sealed_state[self] = (
        workspace_path,
        path,
        basis_store,
        basis_workspace,
        basis_path,
        basis_authority,
        authority,
        authority_root,
    )
    _state(self)
    with _Lock(workspace_path):
        _sealed_recover(self)
        object.__setattr__(self, "_records", _load_records(self))


def _recover(self) -> None:
    _sealed_recover(self)


def _publish_owner_tariff(
    self,
    *,
    tariff_id: str,
    backend_id: str,
    model_id: str,
    config_sha256: str,
    effective_from: str,
    effective_until: str | None,
    allocation_policy_id: str,
    allocation_basis_id: str,
):
    canonical_tariff_id = _text(tariff_id, "tariff_id")
    canonical_backend = _text(backend_id, "backend_id")
    canonical_model = _text(model_id, "model_id")
    canonical_config = _sha(config_sha256, "config_sha256")
    canonical_start = _time(effective_from, "effective_from")
    canonical_end = None if effective_until is None else _time(
        effective_until, "effective_until"
    )
    canonical_policy = _text(allocation_policy_id, "allocation_policy_id")
    canonical_basis_id = _text(allocation_basis_id, "allocation_basis_id")

    goal_for_basis, _ = _current_goal(self)
    basis = _BasisResolveCurrent(
        _basis_authority(self),
        basis_id=canonical_basis_id,
        backend_id=canonical_backend,
        model_id=canonical_model,
        config_sha256=canonical_config,
        allocation_policy_id=canonical_policy,
        bankroll_id=goal_for_basis.bankroll_id,
        currency=goal_for_basis.currency,
    )
    if basis is None:
        raise _Error("product-owned allocation basis is missing or not causally available")
    canonical_amount = basis.amount_per_request
    canonical_basis = basis.basis_sha256
    canonical_basis_at = basis.available_at

    workspace = _state(self)[0]
    with _Lock(workspace):
        _sealed_recover(self)
        records = _load_records(self)
        object.__setattr__(self, "_records", records)
        goal, goal_sha256 = _current_goal(self)
        if (
            basis.owner_goal_id != goal.goal_id
            or basis.owner_goal_revision != goal.revision
            or basis.owner_bankroll_id != goal.bankroll_id
            or basis.owner_goal_sha256 != goal_sha256
            or basis.currency != goal.currency
        ):
            raise _Error("allocation basis no longer matches the current EconomicGoal")

        for existing in records:
            if existing.tariff_id != canonical_tariff_id:
                continue
            same_request = (
                existing.backend_id == canonical_backend
                and existing.model_id == canonical_model
                and existing.config_sha256 == canonical_config
                and existing.amount_per_request == canonical_amount
                and existing.currency == goal.currency
                and existing.effective_from == canonical_start
                and existing.effective_until == canonical_end
                and existing.allocation_policy_id == canonical_policy
                and existing.allocation_basis_id == canonical_basis_id
                and existing.allocation_basis_sha256 == canonical_basis
                and existing.basis_available_at == canonical_basis_at
                and existing.owner_goal_id == goal.goal_id
                and existing.owner_goal_revision == goal.revision
                and existing.owner_bankroll_id == goal.bankroll_id
                and existing.owner_goal_sha256 == goal_sha256
            )
            if same_request:
                return existing
            raise _Error("tariff_id is immutable")

        current_time = _product_utc_now()
        previous_recorded_at = max(
            (_instant(item.recorded_at, "recorded_at") for item in records),
            default=None,
        )
        if previous_recorded_at is not None and current_time <= previous_recorded_at:
            raise _Error("product clock did not advance before tariff publication")
        recorded_at = _time(current_time.isoformat(), "recorded_at")

        record = _Record(
            tariff_id=canonical_tariff_id,
            backend_id=canonical_backend,
            model_id=canonical_model,
            config_sha256=canonical_config,
            amount_per_request=canonical_amount,
            currency=goal.currency,
            effective_from=canonical_start,
            effective_until=canonical_end,
            allocation_treatment=_Treatment.FULLY_ALLOCATED_PER_REQUEST,
            allocation_policy_id=canonical_policy,
            allocation_basis_id=canonical_basis_id,
            allocation_basis_sha256=canonical_basis,
            basis_available_at=canonical_basis_at,
            recorded_at=recorded_at,
            owner_goal_id=goal.goal_id,
            owner_goal_revision=goal.revision,
            owner_bankroll_id=goal.bankroll_id,
            owner_goal_sha256=goal_sha256,
        )
        new_start = _instant(record.effective_from, "effective_from")
        new_end = None if record.effective_until is None else _instant(
            record.effective_until, "effective_until"
        )
        for existing in records:
            if (
                existing.backend_id,
                existing.model_id,
                existing.config_sha256,
                existing.owner_goal_sha256,
            ) != (
                record.backend_id,
                record.model_id,
                record.config_sha256,
                record.owner_goal_sha256,
            ):
                continue
            old_start = _instant(existing.effective_from, "effective_from")
            old_end = None if existing.effective_until is None else _instant(
                existing.effective_until, "effective_until"
            )
            if _intervals_overlap(new_start, new_end, old_start, old_end):
                raise _Error("same compute identity cannot have overlapping owner tariffs")

        staged = (*records, record)
        payload = _state_payload(staged)
        intended = _hashlib.sha256(_canonical_bytes(payload)).hexdigest()
        observed = _observed(self)
        binding = _digest(
            {
                "kind": "OWNER_LOCAL_COMPUTE_TARIFF_PUBLISH",
                "tariff_sha256": record.tariff_sha256,
                "owner_goal_sha256": record.owner_goal_sha256,
                "observed_state_sha256": observed,
                "intended_state_sha256": intended,
            }
        )
        tx_id = f"local-compute-tariff-{record.tariff_sha256}"
        authority = _state(self)[6]
        _authority_operations["prepare"](
            authority,
            tx_id=tx_id,
            observed_state_sha256=observed,
            intended_state_sha256=intended,
            semantic_binding_sha256=binding,
        )
        try:
            _atomic_write_json(_state(self)[1], payload)
        except Exception:
            _authority_operations["abort"](
                authority,
                tx_id=tx_id,
                observed_state_sha256=observed,
                semantic_binding_sha256=binding,
            )
            raise
        published = _observed(self)
        if published != intended:
            raise _Error("published tariff bytes do not match prepared monotonic state")
        _authority_operations["commit"](
            authority,
            tx_id=tx_id,
            observed_state_sha256=published,
            semantic_binding_sha256=binding,
        )
        object.__setattr__(self, "_records", staged)
        return record


def _resolve_current(
    self,
    *,
    backend_id: str,
    model_id: str,
    config_sha256: str,
    bankroll_id: str,
    currency: str,
):
    canonical_backend = _text(backend_id, "backend_id")
    canonical_model = _text(model_id, "model_id")
    canonical_config = _sha(config_sha256, "config_sha256")
    canonical_bankroll = _text(bankroll_id, "bankroll_id")
    canonical_currency = _currency(currency)

    workspace = _state(self)[0]
    with _Lock(workspace):
        _sealed_recover(self)
        records = _load_records(self)
        goal, goal_sha256 = _current_goal(self)
        if goal.bankroll_id != canonical_bankroll or goal.currency != canonical_currency:
            raise _Error("intent bankroll/currency does not match current owner EconomicGoal")
        cutoff = _product_utc_now()
        matches = []
        for record in records:
            if (record.backend_id, record.model_id, record.config_sha256) != (
                canonical_backend,
                canonical_model,
                canonical_config,
            ):
                continue
            if (
                record.owner_goal_id != goal.goal_id
                or record.owner_goal_revision != goal.revision
                or record.owner_bankroll_id != goal.bankroll_id
                or record.owner_goal_sha256 != goal_sha256
                or record.currency != goal.currency
            ):
                continue
            if _instant(record.recorded_at, "recorded_at") > cutoff:
                continue
            if _instant(record.basis_available_at, "basis_available_at") > cutoff:
                continue
            if cutoff < _instant(record.effective_from, "effective_from"):
                continue
            if record.effective_until is not None and cutoff >= _instant(
                record.effective_until, "effective_until"
            ):
                continue
            matches.append(record)
        if len(matches) > 1:
            raise _Error("ambiguous overlapping local compute tariff authority")
        resolved = None if not matches else matches[0]

    if resolved is None:
        return None
    basis = _BasisResolveCurrent(
        _basis_authority(self),
        basis_id=resolved.allocation_basis_id,
        backend_id=resolved.backend_id,
        model_id=resolved.model_id,
        config_sha256=resolved.config_sha256,
        allocation_policy_id=resolved.allocation_policy_id,
        bankroll_id=resolved.owner_bankroll_id,
        currency=resolved.currency,
    )
    if basis is None:
        return None
    if (
        basis.basis_sha256 != resolved.allocation_basis_sha256
        or basis.available_at != resolved.basis_available_at
        or basis.amount_per_request != resolved.amount_per_request
        or basis.currency != resolved.currency
    ):
        raise _Error("resolved allocation basis no longer matches tariff authority")
    return resolved


# Install the hardening on the preserved implementation class. These assignments
# happen once during canonical module import; authority-bearing methods themselves
# never redispatch through mutable class members.
_Store.__init__ = _sealed_init
_Store._recover = _recover
_Store._basis_authority = _basis_authority
_Store._current_goal = _current_goal
_Store.publish_owner_tariff = _publish_owner_tariff
_Store.resolve_current = _resolve_current

# Consumers (including existing tests) receive the preserved implementation module
# object so all pre-existing private/public names remain available. The class above
# is the same object, now with the sealed runtime boundary installed.
sys.modules[__name__] = _impl
