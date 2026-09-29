"""Preserve provider-source scope when settlement identities include source prefixes.

The owning continuous-session implementation remains the only settlement/economic
engine. This composition repair replaces only the event-to-open-ticket lookup helpers,
seals the existing PaperBook load dependency, and adds a post-outcome/pre-learning scope
preflight before Wave M seals their dispatch. Source-scoped outcome evidence therefore
cannot be broadened by splitting an identity on an arbitrary colon, and the existing
quote-key-only SettlementEngine is never allowed to cross-settle another provider's
open ticket under the same legacy quote key.
"""

from __future__ import annotations

from typing import Any

from . import continuous_session as _session


def _install() -> None:
    session_module = _session
    coordinator = session_module.ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator, "__dict__")
    original_resolutions = coordinator_dict["_settlement_resolutions"]
    original_load_book = coordinator_dict["_load_book"]
    canonical_paper_book = session_module.PaperBook
    exact_type = type
    exact_set = set
    exact_id = id

    # `PaperBook` identity alone is not the load authority: a callback can preserve
    # the exact class object while retargeting its class-level loader or fresh-book
    # construction slots. Capture the already-composed class surface once, before any
    # outcome callback can run, and require it around every later book acquisition.
    paper_book_dict = type.__getattribute__(canonical_paper_book, "__dict__")
    missing_slot = object()
    canonical_init_descriptor = paper_book_dict.get("__init__", missing_slot)
    if canonical_init_descriptor is missing_slot:
        raise TypeError("canonical PaperBook __init__ dependency is unavailable")
    function_type = exact_type(canonical_init_descriptor)
    canonical_init_code = canonical_init_descriptor.__code__

    canonical_load_descriptor = paper_book_dict.get("load", missing_slot)
    if exact_type(canonical_load_descriptor) is not classmethod:
        raise TypeError("canonical PaperBook load dependency must be a classmethod")
    canonical_load_target = canonical_load_descriptor.__func__
    if exact_type(canonical_load_target) is not function_type:
        raise TypeError("canonical PaperBook load target must be an exact function")
    canonical_load_code = canonical_load_target.__code__
    canonical_load_globals = canonical_load_target.__globals__
    canonical_path_dependency = canonical_load_globals.get("Path", missing_slot)
    if canonical_path_dependency is missing_slot:
        raise TypeError("canonical PaperBook path dependency is unavailable")

    canonical_load_bytes_descriptor = paper_book_dict.get("load_bytes", missing_slot)
    if exact_type(canonical_load_bytes_descriptor) is not classmethod:
        raise TypeError("canonical PaperBook load_bytes dependency must be a classmethod")
    canonical_load_bytes_target = canonical_load_bytes_descriptor.__func__
    if exact_type(canonical_load_bytes_target) is not function_type:
        raise TypeError("canonical PaperBook load_bytes target must be an exact function")
    canonical_load_bytes_globals = canonical_load_bytes_target.__globals__
    canonical_json_dependency = canonical_load_bytes_globals.get("json", missing_slot)
    if canonical_json_dependency is missing_slot:
        raise TypeError("canonical PaperBook json dependency is unavailable")
    canonical_json_type = exact_type(canonical_json_dependency)
    canonical_json_loads = canonical_json_type.__getattribute__(
        canonical_json_dependency,
        "loads",
    )
    if exact_type(canonical_json_loads) is not function_type:
        raise TypeError("canonical PaperBook json.loads dependency must be an exact function")
    canonical_json_loads_code = canonical_json_loads.__code__
    canonical_duplicate_hook = canonical_load_bytes_globals.get(
        "_reject_duplicate_json_keys",
        missing_slot,
    )
    canonical_nonfinite_hook = canonical_load_bytes_globals.get(
        "_reject_nonfinite_json_constant",
        missing_slot,
    )
    for hook in (canonical_duplicate_hook, canonical_nonfinite_hook):
        if exact_type(hook) is not function_type:
            raise TypeError("canonical PaperBook json parser hook must be an exact function")
    canonical_duplicate_hook_code = canonical_duplicate_hook.__code__
    canonical_nonfinite_hook_code = canonical_nonfinite_hook.__code__

    canonical_new_descriptor = paper_book_dict.get("__new__", missing_slot)
    canonical_new_target = missing_slot
    canonical_new_code = None
    if canonical_new_descriptor is not missing_slot:
        if exact_type(canonical_new_descriptor) in {staticmethod, classmethod}:
            canonical_new_target = canonical_new_descriptor.__func__
        else:
            canonical_new_target = canonical_new_descriptor
        if exact_type(canonical_new_target) is function_type:
            canonical_new_code = canonical_new_target.__code__

    # `load` is only the first dispatch in the canonical on-disk reconstruction graph:
    # it calls `cls.load_bytes`, which calls `cls._from_raw_snapshot`, which in turn
    # calls the class-owned parser/validator helpers. Seal that already-existing exact
    # callable class surface so an outcome callback cannot preserve `load` while
    # retargeting a transitive `cls.*` authority underneath it.
    def paper_book_executable(slot: object) -> object | None:
        if exact_type(slot) in {classmethod, staticmethod}:
            target = slot.__func__
        elif exact_type(slot) is property:
            target = slot.fget
        elif exact_type(slot) is function_type:
            target = slot
        else:
            return None
        if exact_type(target) is not function_type:
            return None
        return target

    paper_book_callable_surface = tuple(
        (name, slot, target, target.__code__)
        for name, slot in tuple(paper_book_dict.items())
        if (target := paper_book_executable(slot)) is not None
    )

    # pathlib dispatch is also part of the positive durable-book read. Resolve the
    # methods through the concrete path MRO exactly as `Path(...).read_bytes()` will,
    # so adding an override to a more-derived path class cannot evade a witness kept
    # only on the original base owner. `exists()` is authority-bearing because it
    # decides whether the durable open book is read at all; its `stat()` child and the
    # underlying `os.stat` dispatch are part of that same positive existence proof.
    canonical_path_concrete_type = exact_type(canonical_path_dependency("."))
    canonical_path_mro = canonical_path_concrete_type.__mro__

    def resolve_path_callable(name: str):
        for owner in canonical_path_mro:
            owner_dict = type.__getattribute__(owner, "__dict__")
            slot = owner_dict.get(name, missing_slot)
            if slot is missing_slot:
                continue
            target = paper_book_executable(slot)
            if target is None:
                raise TypeError("canonical PaperBook pathlib callable is unavailable: " + name)
            return owner, slot, target, target.__code__
        raise TypeError("canonical PaperBook pathlib callable is unavailable: " + name)

    path_callable_surface = tuple(
        (name, *resolve_path_callable(name))
        for name in ("exists", "stat", "read_bytes", "open")
    )
    canonical_path_open_target = next(
        target
        for name, _owner, _slot, target, _code in path_callable_surface
        if name == "open"
    )
    canonical_path_open_globals = canonical_path_open_target.__globals__
    canonical_path_io_dependency = canonical_path_open_globals.get("io", missing_slot)
    if canonical_path_io_dependency is missing_slot:
        raise TypeError("canonical PaperBook pathlib io dependency is unavailable")
    canonical_path_io_type = exact_type(canonical_path_io_dependency)
    canonical_path_io_open = canonical_path_io_type.__getattribute__(
        canonical_path_io_dependency,
        "open",
    )
    canonical_path_stat_target = next(
        target
        for name, _owner, _slot, target, _code in path_callable_surface
        if name == "stat"
    )
    canonical_path_stat_globals = canonical_path_stat_target.__globals__
    canonical_path_os_dependency = canonical_path_stat_globals.get("os", missing_slot)
    if canonical_path_os_dependency is missing_slot:
        raise TypeError("canonical PaperBook pathlib os dependency is unavailable")
    canonical_path_os_type = exact_type(canonical_path_os_dependency)
    canonical_path_os_stat = canonical_path_os_type.__getattribute__(
        canonical_path_os_dependency,
        "stat",
    )

    def require_paper_book_authority() -> None:
        if session_module.PaperBook is not canonical_paper_book:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook load dependency changed"
            )
        if paper_book_dict.get("load", missing_slot) is not canonical_load_descriptor:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook load class-slot authority changed"
            )
        if canonical_load_target.__code__ is not canonical_load_code:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook load executable changed"
            )
        if canonical_load_globals.get("Path", missing_slot) is not canonical_path_dependency:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook path dispatch dependency changed"
            )
        if canonical_load_bytes_globals.get("json", missing_slot) is not canonical_json_dependency:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook json parser dependency changed"
            )
        if canonical_json_type.__getattribute__(
            canonical_json_dependency,
            "loads",
        ) is not canonical_json_loads:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook json.loads authority changed"
            )
        if canonical_json_loads.__code__ is not canonical_json_loads_code:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook json.loads executable changed"
            )
        if canonical_load_bytes_globals.get(
            "_reject_duplicate_json_keys",
            missing_slot,
        ) is not canonical_duplicate_hook:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook duplicate-key parser authority changed"
            )
        if canonical_duplicate_hook.__code__ is not canonical_duplicate_hook_code:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook duplicate-key parser executable changed"
            )
        if canonical_load_bytes_globals.get(
            "_reject_nonfinite_json_constant",
            missing_slot,
        ) is not canonical_nonfinite_hook:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook nonfinite parser authority changed"
            )
        if canonical_nonfinite_hook.__code__ is not canonical_nonfinite_hook_code:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook nonfinite parser executable changed"
            )
        for name, expected_owner, expected_slot, expected_target, expected_code in path_callable_surface:
            current_owner, current_slot, _, _ = resolve_path_callable(name)
            if current_owner is not expected_owner or current_slot is not expected_slot:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook pathlib dispatch changed: " + name
                )
            if expected_target.__code__ is not expected_code:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook pathlib executable changed: " + name
                )
        if canonical_path_open_globals.get("io", missing_slot) is not canonical_path_io_dependency:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook pathlib io dependency changed"
            )
        if canonical_path_io_type.__getattribute__(
            canonical_path_io_dependency,
            "open",
        ) is not canonical_path_io_open:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook pathlib io.open authority changed"
            )
        if canonical_path_stat_globals.get("os", missing_slot) is not canonical_path_os_dependency:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook pathlib os dependency changed"
            )
        if canonical_path_os_type.__getattribute__(
            canonical_path_os_dependency,
            "stat",
        ) is not canonical_path_os_stat:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook pathlib os.stat authority changed"
            )
        if paper_book_dict.get("__init__", missing_slot) is not canonical_init_descriptor:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook constructor authority changed"
            )
        if canonical_init_descriptor.__code__ is not canonical_init_code:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook constructor executable changed"
            )
        if paper_book_dict.get("__new__", missing_slot) is not canonical_new_descriptor:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook allocation authority changed"
            )
        if (
            canonical_new_code is not None
            and canonical_new_target.__code__ is not canonical_new_code
        ):
            raise session_module.ContinuousSessionError(
                "canonical PaperBook allocation executable changed"
            )
        for name, expected_slot, expected_target, expected_code in paper_book_callable_surface:
            if paper_book_dict.get(name, missing_slot) is not expected_slot:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook transitive class dispatch changed: " + name
                )
            if expected_target.__code__ is not expected_code:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook transitive executable changed: " + name
                )

    def load_book(self: Any):
        # Outcome and learning callbacks run before later book reads. Never let a
        # callback retarget the module-level PaperBook constructor/loader or its exact
        # class slots while the already-sealed coordinator method remains unchanged.
        require_paper_book_authority()
        book = original_load_book(self)
        require_paper_book_authority()
        if exact_type(book) is not canonical_paper_book:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook load authority returned a non-canonical book"
            )
        return book

    load_book.__name__ = original_load_book.__name__
    load_book.__qualname__ = original_load_book.__qualname__
    load_book.__doc__ = original_load_book.__doc__
    load_book.__annotations__ = original_load_book.__annotations__

    def leg_event_scope_match_kind(
        ticket: Any,
        leg: Any,
        event_identity: str,
    ) -> str | None:
        """Classify exact sourced versus compatibility-only legacy matches."""

        source_ids = ticket.provider_source_ids
        if exact_type(source_ids) is not tuple:
            raise session_module.ContinuousSessionError(
                "ticket provider source scope must be a canonical tuple"
            )
        for source_id in source_ids:
            if exact_type(source_id) is not str or not source_id:
                raise session_module.ContinuousSessionError(
                    "ticket provider source scope contains an invalid identity"
                )

        if source_ids:
            # Once durable provider provenance exists, only one of those exact source
            # identities may authorize this ticket. A bare event identity must never
            # erase that boundary.
            if any(
                event_identity == f"{source_id}:{leg.event_id}"
                for source_id in source_ids
            ):
                return "sourced"
            return None

        # Source-less tickets predate durable provider provenance. Preserve only the
        # historical first-prefix grammar: ``provider:event_id`` means the first
        # segment is the legacy provider label and the complete remainder is event_id.
        # This keeps colon-bearing event ids compatible while refusing to reinterpret
        # a colon-bearing provider source as authority for a source-less ticket.
        if leg.event_id == event_identity:
            return "legacy"
        source_prefix, separator, provider_event_id = event_identity.partition(":")
        if separator and source_prefix and provider_event_id == leg.event_id:
            return "legacy"
        return None

    def leg_matches_event_scope(ticket: Any, leg: Any, event_identity: str) -> bool:
        return leg_event_scope_match_kind(ticket, leg, event_identity) is not None

    def scoped_open_quote_keys(
        book: Any,
        event_identity: str,
        *,
        candidate_quote_keys: set[str] | None = None,
    ) -> set[str]:
        """Return matching open quote keys, rejecting source-scope contamination."""

        sourced_matching: set[str] = exact_set()
        legacy_matching: set[str] = exact_set()
        nonmatching: set[str] = exact_set()
        for ticket in book.tickets.values():
            if ticket.status.value != "open":
                continue
            for leg in ticket.legs:
                quote_key = leg.quote_key
                if candidate_quote_keys is not None and quote_key not in candidate_quote_keys:
                    continue
                match_kind = leg_event_scope_match_kind(ticket, leg, event_identity)
                if match_kind == "sourced":
                    sourced_matching.add(quote_key)
                elif match_kind == "legacy":
                    legacy_matching.add(quote_key)
                else:
                    nonmatching.add(quote_key)

        matching = sourced_matching.union(legacy_matching)

        # A source-less legacy ticket can be accepted only as a bounded compatibility
        # fallback. If the same quote key is simultaneously reachable through an exact
        # sourced match, the quote-key-only engine cannot prove that the provenance-free
        # ticket belongs to that provider. Fail before learning or economic mutation.
        if sourced_matching.intersection(legacy_matching):
            raise session_module.ContinuousSessionError(
                "settlement provider source scope is ambiguous across sourced and legacy tickets"
            )

        # SettlementEngine records outcomes by quote key rather than provider source.
        # If the same legacy quote key is open under another provider scope, recording
        # the source-scoped outcome would settle both tickets. Fail before any economic
        # or learning side effect until provider scope is carried end-to-end there.
        if matching.intersection(nonmatching):
            raise session_module.ContinuousSessionError(
                "settlement provider source scope is ambiguous across open tickets"
            )
        return matching

    def require_settlement_causal_for_open_tickets(book: Any, resolution: Any) -> None:
        available_at = session_module._instant(
            resolution.available_at,
            "settlement available_at",
        )
        resolution_quote_keys = exact_set(resolution.quote_outcomes)
        scoped_open_quote_keys(
            book,
            resolution.event_identity,
            candidate_quote_keys=resolution_quote_keys,
        )

        for ticket in book.tickets.values():
            if ticket.status.value != "open":
                continue
            matches_resolution = any(
                leg_matches_event_scope(ticket, leg, resolution.event_identity)
                and leg.quote_key in resolution_quote_keys
                for leg in ticket.legs
            )
            if not matches_resolution:
                continue
            placed_at = session_module._instant(
                ticket.placed_at,
                f"ticket {ticket.ticket_id} placed_at",
            )
            if available_at < placed_at:
                raise session_module.ContinuousSessionError(
                    "settlement evidence predates matching open ticket placement"
                )

    def open_quote_keys_for_book(book: Any, event_identity: str) -> set[str]:
        return scoped_open_quote_keys(book, event_identity)

    # Capture the positive pre-learning closure graph once during trusted package
    # composition. Canonical tick runs callback-capable collector/desktop/lifecycle work
    # before `_settlement_resolutions` is acquired; a lazy per-invocation snapshot could
    # therefore bless state that an earlier callback had already retargeted.
    canonical_prelearning_graph_list: list[
        tuple[object, object, tuple[tuple[object, object], ...]]
    ] = []
    pending_prelearning_targets = [
        load_book,
        scoped_open_quote_keys,
        leg_event_scope_match_kind,
    ]
    seen_prelearning_target_ids: set[int] = set()
    while pending_prelearning_targets:
        target = pending_prelearning_targets.pop()
        if exact_type(target) is not function_type:
            raise TypeError("canonical pre-learning helper must be an exact function")
        target_identity = exact_id(target)
        if target_identity in seen_prelearning_target_ids:
            continue
        seen_prelearning_target_ids.add(target_identity)
        target_code = target.__code__
        target_closure = target.__closure__ or ()
        if len(target_closure) != len(target_code.co_freevars):
            raise TypeError("canonical pre-learning helper closure is malformed")
        cell_witnesses: list[tuple[object, object]] = []
        for cell in target_closure:
            try:
                value = cell.cell_contents
            except ValueError as exc:
                raise TypeError(
                    "canonical pre-learning helper closure contains an empty cell"
                ) from exc
            cell_witnesses.append((cell, value))
            if exact_type(value) is function_type:
                pending_prelearning_targets.append(value)
        canonical_prelearning_graph_list.append(
            (target, target_code, tuple(cell_witnesses))
        )
    canonical_prelearning_graph = tuple(canonical_prelearning_graph_list)

    def settlement_resolutions(self: Any, *args: Any, **kwargs: Any):
        # Prove the install-time graph before outcome resolution. This rejects closure
        # drift introduced by an earlier callback in the same tick rather than taking a
        # fresh snapshot of already-hostile state and treating it as canonical.
        for target, expected_code, expected_cells in canonical_prelearning_graph:
            if target.__code__ is not expected_code:
                raise session_module.ContinuousSessionError(
                    "canonical pre-learning settlement authority changed"
                )
            current_closure = target.__closure__ or ()
            if len(current_closure) != len(expected_cells):
                raise session_module.ContinuousSessionError(
                    "canonical pre-learning settlement authority changed"
                )
            for current_cell, (expected_cell, expected_value) in zip(
                current_closure,
                expected_cells,
            ):
                if current_cell is not expected_cell:
                    raise session_module.ContinuousSessionError(
                        "canonical pre-learning settlement authority changed"
                    )
                try:
                    current_value = current_cell.cell_contents
                except ValueError as exc:
                    raise session_module.ContinuousSessionError(
                        "canonical pre-learning settlement authority changed"
                    ) from exc
                if current_value is not expected_value:
                    raise session_module.ContinuousSessionError(
                        "canonical pre-learning settlement authority changed"
                    )

        expected_scoped = scoped_open_quote_keys
        resolutions = original_resolutions(self, *args, **kwargs)

        # Outcome resolution is another callback boundary. Re-prove the same immutable
        # install-time graph before any durable book read, source-scope proof, or
        # settlement-learning handoff can observe the resolutions.
        for target, expected_code, expected_cells in canonical_prelearning_graph:
            if target.__code__ is not expected_code:
                raise session_module.ContinuousSessionError(
                    "canonical pre-learning settlement authority changed"
                )
            current_closure = target.__closure__ or ()
            if len(current_closure) != len(expected_cells):
                raise session_module.ContinuousSessionError(
                    "canonical pre-learning settlement authority changed"
                )
            for current_cell, (expected_cell, expected_value) in zip(
                current_closure,
                expected_cells,
            ):
                if current_cell is not expected_cell:
                    raise session_module.ContinuousSessionError(
                        "canonical pre-learning settlement authority changed"
                    )
                try:
                    current_value = current_cell.cell_contents
                except ValueError as exc:
                    raise session_module.ContinuousSessionError(
                        "canonical pre-learning settlement authority changed"
                    ) from exc
                if current_value is not expected_value:
                    raise session_module.ContinuousSessionError(
                        "canonical pre-learning settlement authority changed"
                    )

        if not resolutions:
            return resolutions

        # Outcome-authority callbacks have completed, but tick has not yet exposed
        # these resolutions to settlement-learning prepare. Prove that the current
        # durable open book can represent every source-scoped outcome without a
        # quote-key collision before the resolution crosses that boundary.
        book = self._load_book()
        for resolution in resolutions:
            expected_scoped(
                book,
                resolution.event_identity,
                candidate_quote_keys=exact_set(resolution.quote_outcomes),
            )
        return resolutions

    settlement_resolutions.__name__ = original_resolutions.__name__
    settlement_resolutions.__qualname__ = original_resolutions.__qualname__
    settlement_resolutions.__doc__ = original_resolutions.__doc__
    settlement_resolutions.__annotations__ = original_resolutions.__annotations__

    type.__setattr__(coordinator, "_load_book", load_book)
    type.__setattr__(
        coordinator,
        "_require_settlement_causal_for_open_tickets",
        staticmethod(require_settlement_causal_for_open_tickets),
    )
    type.__setattr__(
        coordinator,
        "_open_quote_keys_for_book",
        staticmethod(open_quote_keys_for_book),
    )
    type.__setattr__(
        coordinator,
        "_settlement_resolutions",
        settlement_resolutions,
    )


_install()
del _install