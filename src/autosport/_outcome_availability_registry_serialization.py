"""Serialize and causally publish RunRegistry outcome-availability authority.

Two independent properties matter here:

* whole-registry read/modify/write transitions must be serialized so a stale writer
  cannot erase a successful run or rewrite established first availability; and
* a positive ``first_available_at`` must be sampled only after the exact new
  revision identity is already durably observable by the product.

The guard reuses the canonical re-entrant/cross-process ``durable_path_lock``. New
lineage identity is first published with an UNKNOWN (``None``) availability suffix,
then exact-reread, then the sealed product clock is sampled and the positive
availability plus run admission are published. A crash between phases therefore
leaves durable identity but no favorable timestamp and no in-progress run.
"""

from __future__ import annotations

from typing import Callable, Final, TypeVar, cast

from .integrity import durable_path_lock
from . import outcome_trust as _outcome_trust
from . import run_registry as _run_registry

_F = TypeVar("_F", bound=Callable[..., object])
_ORIGINAL_BINDING_FROM_PAYLOAD = _outcome_trust.outcome_lineage_binding_from_payload
_ORIGINAL_BEGIN = _run_registry.RunRegistry.begin

# Capture the product wall-clock dependency exactly once when this guard is
# installed. A later monkeypatch/rebind of module globals must never be able to
# mint favorable historical product availability.
_PRODUCT_UTC_NOW: Final = _run_registry._utc_now


def _sealed_product_utc_now(
    _product_clock: Callable[[], str] = _PRODUCT_UTC_NOW,
    _run_registry_module=_run_registry,
) -> str:
    """Sample only the product clock captured when the authority was installed."""

    if _run_registry_module._utc_now is not _product_clock:
        raise _outcome_trust.OutcomeLineageTrustError(
            "product UTC clock authority was rebound"
        )
    return _product_clock()


def _binding_from_payload_with_unknown_suffix(
    value: object,
    *,
    context: str,
) -> _outcome_trust.OutcomeLineageBinding:
    """Accept only a monotonic available-prefix / UNKNOWN-suffix durable state."""

    try:
        return _ORIGINAL_BINDING_FROM_PAYLOAD(value, context=context)
    except _outcome_trust.OutcomeLineageTrustError as original_error:
        if type(value) is not dict:
            raise original_error
        raw_revisions = value.get("revisions")
        if type(raw_revisions) is not list or not raw_revisions:
            raise original_error
        if any(type(raw) is not dict for raw in raw_revisions):
            raise original_error
        flags = ["first_available_at" in raw for raw in raw_revisions]
        if not any(flags) or all(flags):
            raise original_error
        first_unknown = flags.index(False)
        if any(flags[first_unknown:]):
            raise _outcome_trust.OutcomeLineageTrustError(
                f"{context} product availability must be a monotonic prefix followed by UNKNOWN revisions"
            ) from original_error

        sanitized = dict(value)
        sanitized_revisions: list[dict[str, object]] = []
        for raw in raw_revisions:
            clean = dict(raw)
            clean.pop("first_available_at", None)
            sanitized_revisions.append(clean)
        sanitized["revisions"] = sanitized_revisions
        base = _ORIGINAL_BINDING_FROM_PAYLOAD(sanitized, context=context)

        rebuilt: list[_outcome_trust.TrustedOutcomeRevision] = []
        previous_available = None
        for index, (revision, raw, has_available) in enumerate(
            zip(base.revisions, raw_revisions, flags),
            start=1,
        ):
            available = None
            if has_available:
                available = _outcome_trust._canonical_timestamp(
                    raw.get("first_available_at"),
                    field=f"{context} revision {index} first_available_at",
                )
                available_dt = _outcome_trust._parse_timestamp(available)
                if previous_available is not None and available_dt < previous_available:
                    raise _outcome_trust.OutcomeLineageTrustError(
                        f"{context} product availability moves backwards"
                    )
                previous_available = available_dt
            rebuilt.append(
                _outcome_trust.TrustedOutcomeRevision(
                    revision=revision.revision,
                    revision_id=revision.revision_id,
                    record_sha256=revision.record_sha256,
                    first_available_at=available,
                )
            )
        return _outcome_trust.OutcomeLineageBinding(
            source_identity=base.source_identity,
            record_id=base.record_id,
            root_revision_id=base.root_revision_id,
            root_record_sha256=base.root_record_sha256,
            revisions=tuple(rebuilt),
        )


def _bind_availability_with_unknown_suffix(
    incoming: _outcome_trust.OutcomeLineageBinding,
    *,
    accepted_at: str,
    trusted: _outcome_trust.OutcomeLineageBinding | None = None,
) -> _outcome_trust.OutcomeLineageBinding:
    product_available_at = _outcome_trust._canonical_timestamp(
        accepted_at,
        field="outcome lineage product acceptance time",
    )
    accepted_dt = _outcome_trust._parse_timestamp(product_available_at)
    if trusted is not None:
        if (
            trusted.source_identity != incoming.source_identity
            or trusted.record_id != incoming.record_id
        ):
            raise _outcome_trust.OutcomeLineageTrustError(
                "cannot extend outcome availability from another source/record identity"
            )
        _outcome_trust.assert_compatible_outcome_lineages(trusted, incoming)
        if len(incoming.revisions) < len(trusted.revisions):
            raise _outcome_trust.OutcomeLineageTrustError(
                "cannot bind product availability from a stale outcome lineage prefix"
            )
        established_times = [
            revision.first_available_at
            for revision in trusted.revisions
            if revision.first_available_at is not None
        ]
        if established_times and any(
            revision.first_available_at is None for revision in trusted.revisions
        ):
            first_unknown = next(
                index
                for index, revision in enumerate(trusted.revisions)
                if revision.first_available_at is None
            )
            if any(
                revision.first_available_at is not None
                for revision in trusted.revisions[first_unknown:]
            ):
                raise _outcome_trust.OutcomeLineageTrustError(
                    "trusted outcome availability must be a monotonic prefix"
                )
        if established_times and accepted_dt < _outcome_trust._parse_timestamp(
            established_times[-1]
        ):
            raise _outcome_trust.OutcomeLineageTrustError(
                "product acceptance time predates already trusted outcome availability"
            )

    revisions: list[_outcome_trust.TrustedOutcomeRevision] = []
    for index, revision in enumerate(incoming.revisions):
        established = (
            trusted.revisions[index].first_available_at
            if trusted is not None
            and index < len(trusted.revisions)
            and trusted.revisions[index].first_available_at is not None
            else product_available_at
        )
        revisions.append(
            _outcome_trust.TrustedOutcomeRevision(
                revision=revision.revision,
                revision_id=revision.revision_id,
                record_sha256=revision.record_sha256,
                first_available_at=established,
            )
        )
    return _outcome_trust.OutcomeLineageBinding(
        source_identity=incoming.source_identity,
        record_id=incoming.record_id,
        root_revision_id=incoming.root_revision_id,
        root_record_sha256=incoming.root_record_sha256,
        revisions=tuple(revisions),
    )


def _resolve_with_unknown_suffix(
    binding: _outcome_trust.OutcomeLineageBinding,
    cutoff: str,
) -> _outcome_trust.TrustedOutcomeRevision | None:
    cutoff_dt = _outcome_trust._parse_timestamp(
        _outcome_trust._canonical_timestamp(cutoff, field="outcome as-of cutoff")
    )
    resolved = None
    previous_available = None
    saw_unknown = False
    for revision in binding.revisions:
        if revision.first_available_at is None:
            saw_unknown = True
            continue
        if saw_unknown:
            raise _outcome_trust.OutcomeLineageTrustError(
                "outcome lineage product availability resumes after UNKNOWN revision"
            )
        available_dt = _outcome_trust._parse_timestamp(revision.first_available_at)
        if previous_available is not None and available_dt < previous_available:
            raise _outcome_trust.OutcomeLineageTrustError(
                "outcome lineage product availability moves backwards"
            )
        previous_available = available_dt
        if available_dt <= cutoff_dt:
            resolved = revision
    return resolved


def _store_trust_binding(
    state: dict,
    binding: _outcome_trust.OutcomeLineageBinding,
) -> None:
    raw_trust = state[_run_registry._LINEAGE_TRUST_FIELD]
    identity = (binding.source_identity, binding.record_id)
    payload = _outcome_trust.outcome_lineage_payload(binding)
    for index, raw_binding in enumerate(raw_trust):
        candidate = _binding_from_payload_with_unknown_suffix(
            raw_binding,
            context="run registry outcome_lineage_trust",
        )
        if (candidate.source_identity, candidate.record_id) == identity:
            raw_trust[index] = payload
            break
    else:
        raw_trust.append(payload)
    raw_trust.sort(key=lambda value: (value["source_identity"], value["record_id"]))


def _stage_unavailable_identity(
    state: dict,
    incoming: _outcome_trust.OutcomeLineageBinding,
) -> tuple[_outcome_trust.OutcomeLineageBinding, bool]:
    if state.get("schema_version") == _run_registry._LEGACY_SCHEMA_VERSION:
        if any("outcome_lineage" in item for item in state["runs"].values()):
            raise ValueError(
                "legacy run registry cannot migrate outcome lineage evidence without durable trust binding"
            )
        state["schema_version"] = _run_registry._LINEAGE_TRUST_SCHEMA_VERSION
        state[_run_registry._LINEAGE_TRUST_FIELD] = []
        trusted = None
    else:
        bindings = _run_registry.RunRegistry._outcome_lineage_trust_bindings(state)
        trusted = bindings.get((incoming.source_identity, incoming.record_id))

    if trusted is not None:
        _outcome_trust.assert_compatible_outcome_lineages(trusted, incoming)
        if len(incoming.revisions) < len(trusted.revisions):
            raise _outcome_trust.OutcomeLineageTrustError(
                "cannot stage a stale outcome lineage prefix"
            )

    staged_revisions: list[_outcome_trust.TrustedOutcomeRevision] = []
    for index, revision in enumerate(incoming.revisions):
        available = (
            trusted.revisions[index].first_available_at
            if trusted is not None and index < len(trusted.revisions)
            else None
        )
        staged_revisions.append(
            _outcome_trust.TrustedOutcomeRevision(
                revision=revision.revision,
                revision_id=revision.revision_id,
                record_sha256=revision.record_sha256,
                first_available_at=available,
            )
        )
    staged = _outcome_trust.OutcomeLineageBinding(
        source_identity=incoming.source_identity,
        record_id=incoming.record_id,
        root_revision_id=incoming.root_revision_id,
        root_record_sha256=incoming.root_record_sha256,
        revisions=tuple(staged_revisions),
    )
    changed = trusted != staged
    if changed:
        _store_trust_binding(state, staged)
    return staged, changed


class _SerializedMethodBoundary:
    """Keep one unlocked predecessor outside public FunctionType metadata."""

    __slots__ = ("_function",)

    def __init__(self, function: _F) -> None:
        self._function = function

    def __call__(self, registry, *args, **kwargs):
        with durable_path_lock(registry.path):
            return self._function(registry, *args, **kwargs)


def _serialized(method: _F) -> _F:
    # Do not use functools.wraps here.  __wrapped__ would publish the original
    # unlocked RMW implementation as a directly callable bypass around the
    # serialization domain.  The public wrapper closes only over a non-FunctionType
    # boundary object, matching the metadata fence used by the causal begin guard.
    boundary = _SerializedMethodBoundary(method)

    def guarded(self, *args, **kwargs):
        return boundary(self, *args, **kwargs)

    guarded.__name__ = method.__name__
    guarded.__qualname__ = method.__qualname__
    guarded.__doc__ = method.__doc__
    guarded.__annotations__ = dict(method.__annotations__)
    setattr(guarded, "_autosport_registry_rmw_serialized", True)
    setattr(guarded, "_autosport_predecessor_unreachable", True)
    return cast(_F, guarded)


def _begin_with_causal_outcome_publication(
    self,
    market_sha256: str,
    results_sha256: str,
    strategy_id: str,
    run_id: str,
    allow_repeat: bool = False,
    *,
    base_paper_book_sha256: str | None = None,
    base_decision_ledger_sha256: str | None = None,
    outcome_lineage: _outcome_trust.OutcomeLineageBinding | None = None,
) -> str:
    if outcome_lineage is None:
        with durable_path_lock(self.path):
            return _ORIGINAL_BEGIN(
                self,
                market_sha256,
                results_sha256,
                strategy_id,
                run_id,
                allow_repeat,
                base_paper_book_sha256=base_paper_book_sha256,
                base_decision_ledger_sha256=base_decision_ledger_sha256,
                outcome_lineage=None,
            )

    _run_registry._require_canonical_sha256("market_sha256", market_sha256)
    _run_registry._require_canonical_sha256("results_sha256", results_sha256)
    _run_registry._require_nonempty_string("strategy_id", strategy_id)
    _run_registry._require_nonempty_string("run_id", run_id)
    if not isinstance(allow_repeat, bool):
        raise ValueError("allow_repeat must be a boolean")
    if (base_paper_book_sha256 is None) != (base_decision_ledger_sha256 is None):
        raise ValueError("base transaction hashes must be supplied together")
    if base_paper_book_sha256 is not None:
        _run_registry._require_canonical_sha256(
            "base_paper_book_sha256", base_paper_book_sha256
        )
        _run_registry._require_canonical_sha256(
            "base_decision_ledger_sha256", base_decision_ledger_sha256
        )
    if not isinstance(outcome_lineage, _outcome_trust.OutcomeLineageBinding):
        raise ValueError("outcome_lineage must be an OutcomeLineageBinding or null")

    with durable_path_lock(self.path):
        state = self._read()
        self._assert_outcome_lineage_admissible_for_new_run_state(
            state, outcome_lineage
        )
        base_identity = self.experiment_identity(
            market_sha256, results_sha256, strategy_id
        )
        existing_pairs = [
            (key, item)
            for key, item in state["runs"].items()
            if item.get("base_identity") == base_identity
        ]
        unresolved = [
            item
            for item in state["runs"].values()
            if item.get("status") == "in_progress"
        ]
        if unresolved:
            raise _run_registry.UnresolvedExperimentError(
                "Workspace has an unresolved economic run; repair it before starting another paper experiment."
            )
        completed = [
            item for _key, item in existing_pairs if item.get("status") == "completed"
        ]
        if completed and not allow_repeat:
            raise _run_registry.RepeatedExperimentError(
                "This dataset/strategy already completed in this workspace. Explicit allow_repeat is required for another experiment."
            )
        if any(item["run_id"] == run_id for item in state["runs"].values()):
            raise _run_registry.RepeatedExperimentError(
                "This run_id already has durable history in this workspace."
            )
        if not existing_pairs:
            key = base_identity
        elif completed:
            key = f"{base_identity}:repeat:{run_id}"
        else:
            key = f"{base_identity}:retry:{run_id}"
        if key in state["runs"]:
            raise _run_registry.RepeatedExperimentError(
                "This run_id already has durable history for this dataset/strategy."
            )

        current_bindings = self._outcome_lineage_trust_bindings(state)
        previous = current_bindings.get(
            (outcome_lineage.source_identity, outcome_lineage.record_id)
        )
        if (
            previous is not None
            and len(outcome_lineage.revisions) > len(previous.revisions)
        ):
            established = [
                revision.first_available_at
                for revision in previous.revisions
                if revision.first_available_at is not None
            ]
            if established:
                probe = _outcome_trust._canonical_timestamp(
                    _sealed_product_utc_now(),
                    field="outcome lineage product acceptance time",
                )
                if _outcome_trust._parse_timestamp(probe) < _outcome_trust._parse_timestamp(
                    established[-1]
                ):
                    raise _outcome_trust.OutcomeLineageTrustError(
                        "product acceptance time predates already trusted outcome availability"
                    )

        staged, changed = _stage_unavailable_identity(state, outcome_lineage)
        if changed:
            self._write(state)
            state = self._read()
            persisted = self._outcome_lineage_trust_bindings(state).get(
                (outcome_lineage.source_identity, outcome_lineage.record_id)
            )
            if persisted != staged:
                raise RuntimeError(
                    "outcome lineage identity publication did not exact-reread before availability binding"
                )
        else:
            persisted = staged

        needs_availability = any(
            revision.first_available_at is None for revision in persisted.revisions
        )
        if needs_availability:
            product_bound = _bind_availability_with_unknown_suffix(
                outcome_lineage,
                accepted_at=_sealed_product_utc_now(),
                trusted=persisted,
            )
            _store_trust_binding(state, product_bound)
        else:
            product_bound = persisted

        entry = {
            "base_identity": base_identity,
            "run_id": run_id,
            "market_sha256": market_sha256,
            "results_sha256": results_sha256,
            "strategy_id": strategy_id,
            "status": "in_progress",
        }
        if base_paper_book_sha256 is not None:
            entry["base_paper_book_sha256"] = base_paper_book_sha256
            entry["base_decision_ledger_sha256"] = base_decision_ledger_sha256
        entry["outcome_lineage"] = _outcome_trust.outcome_lineage_payload(product_bound)
        state["runs"][key] = entry
        self._validate_entry(key, entry)
        self._write(state)

        verified = self._read()
        verified_entry = verified["runs"].get(key)
        if verified_entry is None:
            raise RuntimeError("outcome-qualified run publication disappeared after write")
        verified_lineage = _binding_from_payload_with_unknown_suffix(
            verified_entry.get("outcome_lineage"),
            context="run registry outcome_lineage",
        )
        verified_trust = self._outcome_lineage_trust_bindings(verified).get(
            (outcome_lineage.source_identity, outcome_lineage.record_id)
        )
        if verified_lineage != product_bound or verified_trust != product_bound:
            raise RuntimeError(
                "outcome-qualified run publication failed exact durable reread"
            )
        return key


# Make partial UNKNOWN suffixes first-class durable fail-closed state for the
# two-phase transition. RunRegistry imported these functions by value, so patch
# both the source module and its local references.
_outcome_trust.outcome_lineage_binding_from_payload = _binding_from_payload_with_unknown_suffix
_outcome_trust.bind_outcome_lineage_availability = _bind_availability_with_unknown_suffix
_outcome_trust.resolve_outcome_revision_as_of = _resolve_with_unknown_suffix
_run_registry.outcome_lineage_binding_from_payload = _binding_from_payload_with_unknown_suffix
_run_registry.bind_outcome_lineage_availability = _bind_availability_with_unknown_suffix
_run_registry.resolve_outcome_revision_as_of = _resolve_with_unknown_suffix

_begin_with_causal_outcome_publication._autosport_registry_rmw_serialized = True
_begin_with_causal_outcome_publication._autosport_outcome_two_phase = True
_begin_with_causal_outcome_publication._autosport_product_clock_sealed = True
_run_registry.RunRegistry.begin = _begin_with_causal_outcome_publication

for _method_name in (
    "complete",
    "abort_uncommitted",
    "reconcile_completed_summary",
):
    _method = getattr(_run_registry.RunRegistry, _method_name)
    if not getattr(_method, "_autosport_registry_rmw_serialized", False):
        setattr(_run_registry.RunRegistry, _method_name, _serialized(_method))
