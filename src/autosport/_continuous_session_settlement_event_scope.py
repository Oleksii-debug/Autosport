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
    exact_getattr = getattr
    exact_len = len
    exact_any = any
    exact_zip = zip
    exact_tuple = tuple

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

    def capture_frozen_function_graph(
        root: object,
        *,
        label: str,
    ) -> tuple[tuple[object, ...], ...]:
        """Freeze the Python-function/global graph used by a detached delegate."""

        if exact_type(root) is not function_type:
            raise TypeError(
                f"canonical PaperBook {label} dependency must be an exact function"
            )
        pending = [root]
        seen: set[int] = exact_set()
        graph: list[tuple[object, ...]] = []
        while pending:
            dependency = pending.pop()
            dependency_identity = exact_id(dependency)
            if dependency_identity in seen:
                continue
            seen.add(dependency_identity)
            if exact_type(dependency) is not function_type:
                raise TypeError(
                    f"canonical PaperBook {label} dependency must be an exact function"
                )

            dependency_globals = dependency.__globals__
            dependency_closure = dependency.__closure__
            dependency_closure_cells = (
                () if dependency_closure is None else dependency_closure
            )
            try:
                dependency_closure_values = exact_tuple(
                    cell.cell_contents for cell in dependency_closure_cells
                )
            except ValueError as exc:
                raise TypeError(
                    f"canonical PaperBook {label} dependency closure is unavailable"
                ) from exc

            dependency_kwdefaults = dependency.__kwdefaults__
            dependency_kwdefault_items = (
                ()
                if dependency_kwdefaults is None
                else exact_tuple(dependency_kwdefaults.items())
            )
            dependency_bindings: list[tuple[str, object]] = []
            for dependency_name in dependency.__code__.co_names:
                expected_dependency = dependency_globals.get(
                    dependency_name,
                    missing_slot,
                )
                dependency_bindings.append(
                    (dependency_name, expected_dependency)
                )
                if (
                    expected_dependency is not missing_slot
                    and exact_type(expected_dependency) is function_type
                ):
                    pending.append(expected_dependency)

            graph.append(
                (
                    dependency,
                    dependency.__code__,
                    dependency_globals,
                    dependency.__defaults__,
                    dependency_kwdefaults,
                    dependency_kwdefault_items,
                    dependency_closure,
                    exact_tuple(dependency_closure_cells),
                    dependency_closure_values,
                    exact_tuple(dependency_bindings),
                )
            )
        return exact_tuple(graph)

    def capture_builtin_fallback_graph(
        graph: tuple[tuple[object, ...], ...],
        *,
        label: str,
    ) -> tuple[tuple[object, ...], ...]:
        """Witness builtin fallback only for names absent from function globals."""

        builtin_graph: list[tuple[object, ...]] = []
        for dependency_witness in graph:
            dependency = dependency_witness[0]
            dependency_bindings = dependency_witness[9]
            dependency_builtins = dependency.__builtins__
            if exact_type(dependency_builtins) is not dict:
                raise TypeError(
                    f"canonical PaperBook {label} builtins must be an exact dict"
                )
            builtin_bindings = exact_tuple(
                (
                    dependency_name,
                    dependency_builtins.get(dependency_name, missing_slot),
                )
                for dependency_name, expected_global in dependency_bindings
                if expected_global is missing_slot
            )
            builtin_graph.append(
                (
                    dependency,
                    dependency_builtins,
                    builtin_bindings,
                )
            )
        return exact_tuple(builtin_graph)

    canonical_load_descriptor = paper_book_dict.get("load", missing_slot)
    if exact_type(canonical_load_descriptor) is not classmethod:
        raise TypeError("canonical PaperBook load dependency must be a classmethod")
    canonical_load_target = canonical_load_descriptor.__func__
    if exact_type(canonical_load_target) is not function_type:
        raise TypeError("canonical PaperBook load target must be an exact function")
    canonical_load_code = canonical_load_target.__code__
    canonical_load_globals = canonical_load_target.__globals__
    canonical_load_closure = canonical_load_target.__closure__
    canonical_load_closure_cells = (
        () if canonical_load_closure is None else exact_tuple(canonical_load_closure)
    )
    try:
        canonical_load_closure_values = exact_tuple(
            cell.cell_contents for cell in canonical_load_closure_cells
        )
    except ValueError as exc:
        raise TypeError("canonical PaperBook load closure is unavailable") from exc

    # #1789 composes the positive PaperBook loader in two layers. The inner
    # load-dispatch wrapper exposes _FROZEN_LOAD in its globals, while the final
    # wrapper-helper closure intentionally copies those globals into a private
    # trusted_globals dict and reconstructs the inner wrapper for each call. Resolve
    # that exact private execution mapping without assuming which composition layer is
    # currently public, and keep the closure cell itself under witness.
    canonical_load_authority_globals = canonical_load_globals
    canonical_load_trusted_globals_cell = None
    canonical_load_trusted_globals = None
    if "_FROZEN_LOAD" not in canonical_load_authority_globals:
        freevars = canonical_load_code.co_freevars
        if canonical_load_closure is not None and "trusted_globals" in freevars:
            trusted_index = freevars.index("trusted_globals")
            canonical_load_trusted_globals_cell = canonical_load_closure[trusted_index]
            try:
                canonical_load_trusted_globals = (
                    canonical_load_trusted_globals_cell.cell_contents
                )
            except ValueError as exc:
                raise TypeError(
                    "canonical PaperBook load trusted globals are unavailable"
                ) from exc
            if exact_type(canonical_load_trusted_globals) is not dict:
                raise TypeError(
                    "canonical PaperBook load trusted globals must be an exact dict"
                )
            canonical_load_authority_globals = canonical_load_trusted_globals

    # The executing positive path reader is the frozen preload delegate in the
    # composed graph. Legacy/raw composition remains supported only when that delegate
    # is genuinely absent; then the raw loader's Path global is the authority.
    canonical_frozen_load = canonical_load_authority_globals.get(
        "_FROZEN_LOAD",
        missing_slot,
    )
    if canonical_frozen_load is missing_slot:
        canonical_frozen_load = None
        canonical_frozen_load_code = None
        canonical_path_globals = canonical_load_authority_globals
        canonical_path_dependency_name = "Path"
    else:
        if exact_type(canonical_frozen_load) is not function_type:
            raise TypeError("canonical PaperBook frozen load dependency must be an exact function")
        canonical_frozen_load_code = canonical_frozen_load.__code__
        canonical_path_globals = canonical_frozen_load.__globals__
        canonical_path_dependency_name = "_PATH"

    canonical_frozen_load_dependency_graph = (
        ()
        if canonical_frozen_load is None
        else capture_frozen_function_graph(
            canonical_frozen_load,
            label="frozen load",
        )
    )
    canonical_frozen_load_builtin_graph = capture_builtin_fallback_graph(
        canonical_frozen_load_dependency_graph,
        label="frozen load",
    )

    canonical_path_dependency = canonical_path_globals.get(
        canonical_path_dependency_name,
        missing_slot,
    )
    if canonical_path_dependency is missing_slot:
        raise TypeError("canonical PaperBook path dependency is unavailable")

    # Settlement writes happen after outcome-authority callbacks. #1789 seals save
    # behind the same final wrapper-helper shape as load, so capture the closure-private
    # execution mapping and its frozen durable delegate before any callback can retarget
    # it while preserving the public PaperBook.save slot and code object.
    canonical_save_descriptor = paper_book_dict.get("save", missing_slot)
    if exact_type(canonical_save_descriptor) is not function_type:
        raise TypeError("canonical PaperBook save dependency must be an exact function")
    canonical_save_target = canonical_save_descriptor
    canonical_save_code = canonical_save_target.__code__
    canonical_save_globals = canonical_save_target.__globals__
    canonical_save_closure = canonical_save_target.__closure__
    canonical_save_closure_cells = (
        () if canonical_save_closure is None else exact_tuple(canonical_save_closure)
    )
    try:
        canonical_save_closure_values = exact_tuple(
            cell.cell_contents for cell in canonical_save_closure_cells
        )
    except ValueError as exc:
        raise TypeError("canonical PaperBook save closure is unavailable") from exc
    canonical_save_authority_globals = canonical_save_globals
    canonical_save_trusted_globals_cell = None
    canonical_save_trusted_globals = None
    if "_FROZEN_SAVE" not in canonical_save_authority_globals:
        save_freevars = canonical_save_code.co_freevars
        if canonical_save_closure is not None and "trusted_globals" in save_freevars:
            trusted_index = save_freevars.index("trusted_globals")
            canonical_save_trusted_globals_cell = canonical_save_closure[trusted_index]
            try:
                canonical_save_trusted_globals = (
                    canonical_save_trusted_globals_cell.cell_contents
                )
            except ValueError as exc:
                raise TypeError(
                    "canonical PaperBook save trusted globals are unavailable"
                ) from exc
            if exact_type(canonical_save_trusted_globals) is not dict:
                raise TypeError(
                    "canonical PaperBook save trusted globals must be an exact dict"
                )
            canonical_save_authority_globals = canonical_save_trusted_globals

    canonical_frozen_save = canonical_save_authority_globals.get(
        "_FROZEN_SAVE",
        missing_slot,
    )
    if canonical_frozen_save is missing_slot:
        canonical_frozen_save = None
        canonical_frozen_save_code = None
        canonical_frozen_save_globals = None
    else:
        if exact_type(canonical_frozen_save) is not function_type:
            raise TypeError("canonical PaperBook frozen save dependency must be an exact function")
        canonical_frozen_save_code = canonical_frozen_save.__code__
        canonical_frozen_save_globals = canonical_frozen_save.__globals__

    canonical_frozen_save_dependency_graph = (
        ()
        if canonical_frozen_save is None
        else capture_frozen_function_graph(
            canonical_frozen_save,
            label="frozen save",
        )
    )
    canonical_frozen_save_builtin_graph = capture_builtin_fallback_graph(
        canonical_frozen_save_dependency_graph,
        label="frozen save",
    )

    # The final #1789 save wrapper has its own private trusted-globals dispatch graph.
    # A callback that changes one of those verifier bindings can otherwise survive the
    # public save-slot/code checks until the eventual durable write, after learning
    # prepare has already observed the settlement. Snapshot and actively validate that
    # exact private graph at the pre-learning boundary.
    canonical_save_authority_binding_snapshot: tuple[tuple[str, object], ...] = ()
    canonical_save_require_delegate = None
    canonical_save_require_class = None
    canonical_save_require_value = None
    canonical_save_delegate_witnesses = None
    canonical_save_class_witnesses = None
    canonical_save_value_witnesses = None
    canonical_save_verifier_dependency_graph: tuple[tuple[object, ...], ...] = ()
    if canonical_frozen_save is not None and canonical_save_trusted_globals is not None:
        required_save_names = (
            "_EXACT_TYPE",
            "_CANONICAL_PAPER_BOOK",
            "_SAVE_DELEGATE_GRAPH_WITNESSES",
            "_SAVE_CLASS_CALLABLE_GRAPH_WITNESSES",
            "_VALUE_TYPE_CALLABLE_WITNESSES",
            "_require_delegate_graph_witnesses",
            "_require_class_callable_graph_witnesses",
            "_require_value_type_callable_witnesses",
        )
        for name in required_save_names:
            if canonical_save_authority_globals.get(name, missing_slot) is missing_slot:
                raise TypeError(
                    "canonical PaperBook save wrapper authority is unavailable: " + name
                )
        if canonical_save_authority_globals["_EXACT_TYPE"] is not exact_type:
            raise TypeError("canonical PaperBook save exact-type authority changed")
        if canonical_save_authority_globals["_CANONICAL_PAPER_BOOK"] is not canonical_paper_book:
            raise TypeError("canonical PaperBook save class authority changed")
        canonical_save_require_delegate = canonical_save_authority_globals[
            "_require_delegate_graph_witnesses"
        ]
        canonical_save_require_class = canonical_save_authority_globals[
            "_require_class_callable_graph_witnesses"
        ]
        canonical_save_require_value = canonical_save_authority_globals[
            "_require_value_type_callable_witnesses"
        ]
        canonical_save_verifier_executables = (
            canonical_save_require_delegate,
            canonical_save_require_class,
            canonical_save_require_value,
        )
        canonical_save_verifier_witness_list: list[tuple[object, ...]] = []
        for verifier in canonical_save_verifier_executables:
            if exact_type(verifier) is not function_type:
                raise TypeError("canonical PaperBook save verifier must be an exact function")
            closure = verifier.__closure__
            closure_values: tuple[object, ...] | None = None
            if closure is not None:
                try:
                    closure_values = exact_tuple(cell.cell_contents for cell in closure)
                except ValueError as exc:
                    raise TypeError(
                        "canonical PaperBook save verifier closure is unavailable"
                    ) from exc
            canonical_save_verifier_witness_list.append(
                (
                    verifier,
                    verifier.__code__,
                    verifier.__globals__,
                    verifier.__defaults__,
                    None if verifier.__kwdefaults__ is None else dict(verifier.__kwdefaults__),
                    closure,
                    closure_values,
                )
            )
        canonical_save_verifier_witnesses = exact_tuple(canonical_save_verifier_witness_list)

        # The final wrapper-helper shallow-copies the original load-dispatch globals,
        # while these verifier FunctionTypes keep executing against that ORIGINAL
        # globals mapping. Witness the recursively reachable Python-function binding
        # graph itself so a callback cannot retarget a helper, execute it during this
        # pre-learning check, then self-restore before the later wrapper sees it.
        verifier_dependency_pending = [*canonical_save_verifier_executables]
        verifier_dependency_seen: set[int] = exact_set()
        verifier_dependency_graph_list: list[tuple[object, ...]] = []
        while verifier_dependency_pending:
            dependency = verifier_dependency_pending.pop()
            dependency_identity = exact_id(dependency)
            if dependency_identity in verifier_dependency_seen:
                continue
            verifier_dependency_seen.add(dependency_identity)
            if exact_type(dependency) is not function_type:
                raise TypeError(
                    "canonical PaperBook save verifier dependency must be an exact function"
                )

            dependency_globals = dependency.__globals__
            dependency_closure = dependency.__closure__
            dependency_closure_cells = (
                () if dependency_closure is None else dependency_closure
            )
            try:
                dependency_closure_values = exact_tuple(
                    cell.cell_contents for cell in dependency_closure_cells
                )
            except ValueError as exc:
                raise TypeError(
                    "canonical PaperBook save verifier dependency closure is unavailable"
                ) from exc

            dependency_kwdefaults = dependency.__kwdefaults__
            dependency_kwdefault_items = (
                ()
                if dependency_kwdefaults is None
                else exact_tuple(dependency_kwdefaults.items())
            )
            dependency_bindings: list[tuple[str, object]] = []
            for dependency_name in dependency.__code__.co_names:
                expected_dependency = dependency_globals.get(
                    dependency_name,
                    missing_slot,
                )
                # Absence is authority too: Python LOAD_GLOBAL falls back to builtins
                # only while the name remains absent from the function globals. A
                # callback must not be able to add a transient hostile global shadow,
                # execute it during preflight, then remove it before later wrappers.
                dependency_bindings.append(
                    (dependency_name, expected_dependency)
                )
                if (
                    expected_dependency is not missing_slot
                    and exact_type(expected_dependency) is function_type
                ):
                    verifier_dependency_pending.append(expected_dependency)

            verifier_dependency_graph_list.append(
                (
                    dependency,
                    dependency.__code__,
                    dependency_globals,
                    dependency.__defaults__,
                    dependency_kwdefaults,
                    dependency_kwdefault_items,
                    dependency_closure,
                    exact_tuple(dependency_closure_cells),
                    dependency_closure_values,
                    exact_tuple(dependency_bindings),
                )
            )
        canonical_save_verifier_dependency_graph = exact_tuple(
            verifier_dependency_graph_list
        )
        canonical_save_verifier_builtin_graph = capture_builtin_fallback_graph(
            canonical_save_verifier_dependency_graph,
            label="save verifier",
        )
        canonical_save_delegate_witnesses = canonical_save_authority_globals[
            "_SAVE_DELEGATE_GRAPH_WITNESSES"
        ]
        canonical_save_class_witnesses = canonical_save_authority_globals[
            "_SAVE_CLASS_CALLABLE_GRAPH_WITNESSES"
        ]
        canonical_save_value_witnesses = canonical_save_authority_globals[
            "_VALUE_TYPE_CALLABLE_WITNESSES"
        ]
        canonical_save_authority_binding_snapshot = exact_tuple(
            canonical_save_authority_globals.items()
        )

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
    # #1789 narrows the parser module to an immutable _FrozenSurface. Normal getattr
    # is the canonical surface lookup because its __getattr__ validates the frozen
    # member graph; direct type.__getattribute__ intentionally bypasses that contract.
    canonical_json_loads = exact_getattr(
        canonical_json_dependency,
        "loads",
        missing_slot,
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

    paper_book_callable_surface = exact_tuple(
        (name, slot, target, target.__code__)
        for name, slot in exact_tuple(paper_book_dict.items())
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

    path_callable_surface = exact_tuple(
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
        if canonical_load_target.__closure__ is not canonical_load_closure:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook load closure authority changed"
            )
        try:
            current_load_closure_values = exact_tuple(
                cell.cell_contents for cell in canonical_load_closure_cells
            )
        except ValueError as exc:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook load closure contents changed"
            ) from exc
        if (
            exact_len(current_load_closure_values) != exact_len(canonical_load_closure_values)
            or exact_any(
                current is not expected
                for current, expected in exact_zip(
                    current_load_closure_values,
                    canonical_load_closure_values,
                )
            )
        ):
            raise session_module.ContinuousSessionError(
                "canonical PaperBook load closure contents changed"
            )
        if canonical_load_trusted_globals_cell is not None:
            try:
                current_trusted_globals = (
                    canonical_load_trusted_globals_cell.cell_contents
                )
            except ValueError as exc:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook load trusted globals changed"
                ) from exc
            if current_trusted_globals is not canonical_load_trusted_globals:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook load trusted globals changed"
                )
        if canonical_frozen_load is not None:
            if (
                canonical_load_authority_globals.get(
                    "_FROZEN_LOAD",
                    missing_slot,
                )
                is not canonical_frozen_load
            ):
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook frozen load dependency changed"
                )
            if (
                canonical_frozen_load.__code__ is not canonical_frozen_load_code
                or canonical_frozen_load.__globals__ is not canonical_path_globals
            ):
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook frozen load executable changed"
                )
            for (
                dependency,
                expected_dependency_code,
                expected_dependency_globals,
                expected_dependency_defaults,
                expected_dependency_kwdefaults,
                expected_dependency_kwdefault_items,
                expected_dependency_closure,
                expected_dependency_closure_cells,
                expected_dependency_closure_values,
                expected_dependency_bindings,
            ) in canonical_frozen_load_dependency_graph:
                if (
                    exact_type(dependency) is not function_type
                    or dependency.__code__ is not expected_dependency_code
                    or dependency.__globals__ is not expected_dependency_globals
                    or dependency.__defaults__ is not expected_dependency_defaults
                    or dependency.__kwdefaults__ is not expected_dependency_kwdefaults
                    or dependency.__closure__ is not expected_dependency_closure
                ):
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook frozen load dependency executable changed"
                    )
                if expected_dependency_kwdefaults is not None and (
                    exact_len(expected_dependency_kwdefaults)
                    != exact_len(expected_dependency_kwdefault_items)
                    or exact_any(
                        expected_dependency_kwdefaults.get(key, missing_slot)
                        is not value
                        for key, value in expected_dependency_kwdefault_items
                    )
                ):
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook frozen load dependency defaults changed"
                    )
                current_dependency_closure = dependency.__closure__
                current_dependency_cells = (
                    ()
                    if current_dependency_closure is None
                    else current_dependency_closure
                )
                if (
                    exact_len(current_dependency_cells)
                    != exact_len(expected_dependency_closure_cells)
                    or exact_any(
                        current is not expected
                        for current, expected in exact_zip(
                            current_dependency_cells,
                            expected_dependency_closure_cells,
                        )
                    )
                ):
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook frozen load dependency closure changed"
                    )
                try:
                    current_dependency_closure_values = exact_tuple(
                        cell.cell_contents for cell in current_dependency_cells
                    )
                except ValueError as exc:
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook frozen load dependency closure changed"
                    ) from exc
                if (
                    exact_len(current_dependency_closure_values)
                    != exact_len(expected_dependency_closure_values)
                    or exact_any(
                        current is not expected
                        for current, expected in exact_zip(
                            current_dependency_closure_values,
                            expected_dependency_closure_values,
                        )
                    )
                ):
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook frozen load dependency closure changed"
                    )
                for dependency_name, expected_dependency in expected_dependency_bindings:
                    if (
                        expected_dependency_globals.get(
                            dependency_name,
                            missing_slot,
                        )
                        is not expected_dependency
                    ):
                        raise session_module.ContinuousSessionError(
                            "canonical PaperBook frozen load dependency global changed: "
                            + dependency_name
                        )
            for (
                dependency,
                expected_builtins,
                expected_builtin_bindings,
            ) in canonical_frozen_load_builtin_graph:
                if dependency.__builtins__ is not expected_builtins:
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook frozen load builtins authority changed"
                    )
                for builtin_name, expected_builtin in expected_builtin_bindings:
                    if (
                        expected_builtins.get(builtin_name, missing_slot)
                        is not expected_builtin
                    ):
                        raise session_module.ContinuousSessionError(
                            "canonical PaperBook frozen load builtin binding changed: "
                            + builtin_name
                        )
        if (
            canonical_path_globals.get(
                canonical_path_dependency_name,
                missing_slot,
            )
            is not canonical_path_dependency
        ):
            raise session_module.ContinuousSessionError(
                "canonical PaperBook path dispatch dependency changed"
            )
        if paper_book_dict.get("save", missing_slot) is not canonical_save_descriptor:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook save class-slot authority changed"
            )
        if canonical_save_target.__code__ is not canonical_save_code:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook save executable changed"
            )
        if canonical_save_target.__closure__ is not canonical_save_closure:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook save closure authority changed"
            )
        try:
            current_save_closure_values = exact_tuple(
                cell.cell_contents for cell in canonical_save_closure_cells
            )
        except ValueError as exc:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook save closure contents changed"
            ) from exc
        if (
            exact_len(current_save_closure_values) != exact_len(canonical_save_closure_values)
            or exact_any(
                current is not expected
                for current, expected in exact_zip(
                    current_save_closure_values,
                    canonical_save_closure_values,
                )
            )
        ):
            raise session_module.ContinuousSessionError(
                "canonical PaperBook save closure contents changed"
            )
        if canonical_save_trusted_globals_cell is not None:
            try:
                current_save_trusted_globals = (
                    canonical_save_trusted_globals_cell.cell_contents
                )
            except ValueError as exc:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook save trusted globals changed"
                ) from exc
            if current_save_trusted_globals is not canonical_save_trusted_globals:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook save trusted globals changed"
                )
        if canonical_frozen_save is not None:
            if (
                canonical_save_authority_globals.get(
                    "_FROZEN_SAVE",
                    missing_slot,
                )
                is not canonical_frozen_save
            ):
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook frozen save dependency changed"
                )
            if (
                canonical_frozen_save.__code__ is not canonical_frozen_save_code
                or canonical_frozen_save.__globals__ is not canonical_frozen_save_globals
            ):
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook frozen save executable changed"
                )
            for (
                dependency,
                expected_dependency_code,
                expected_dependency_globals,
                expected_dependency_defaults,
                expected_dependency_kwdefaults,
                expected_dependency_kwdefault_items,
                expected_dependency_closure,
                expected_dependency_closure_cells,
                expected_dependency_closure_values,
                expected_dependency_bindings,
            ) in canonical_frozen_save_dependency_graph:
                if (
                    exact_type(dependency) is not function_type
                    or dependency.__code__ is not expected_dependency_code
                    or dependency.__globals__ is not expected_dependency_globals
                    or dependency.__defaults__ is not expected_dependency_defaults
                    or dependency.__kwdefaults__ is not expected_dependency_kwdefaults
                    or dependency.__closure__ is not expected_dependency_closure
                ):
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook frozen save dependency executable changed"
                    )
                if expected_dependency_kwdefaults is not None and (
                    exact_len(expected_dependency_kwdefaults)
                    != exact_len(expected_dependency_kwdefault_items)
                    or exact_any(
                        expected_dependency_kwdefaults.get(key, missing_slot)
                        is not value
                        for key, value in expected_dependency_kwdefault_items
                    )
                ):
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook frozen save dependency defaults changed"
                    )
                current_dependency_closure = dependency.__closure__
                current_dependency_cells = (
                    ()
                    if current_dependency_closure is None
                    else current_dependency_closure
                )
                if (
                    exact_len(current_dependency_cells)
                    != exact_len(expected_dependency_closure_cells)
                    or exact_any(
                        current is not expected
                        for current, expected in exact_zip(
                            current_dependency_cells,
                            expected_dependency_closure_cells,
                        )
                    )
                ):
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook frozen save dependency closure changed"
                    )
                try:
                    current_dependency_closure_values = exact_tuple(
                        cell.cell_contents for cell in current_dependency_cells
                    )
                except ValueError as exc:
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook frozen save dependency closure changed"
                    ) from exc
                if (
                    exact_len(current_dependency_closure_values)
                    != exact_len(expected_dependency_closure_values)
                    or exact_any(
                        current is not expected
                        for current, expected in exact_zip(
                            current_dependency_closure_values,
                            expected_dependency_closure_values,
                        )
                    )
                ):
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook frozen save dependency closure changed"
                    )
                for dependency_name, expected_dependency in expected_dependency_bindings:
                    if (
                        expected_dependency_globals.get(
                            dependency_name,
                            missing_slot,
                        )
                        is not expected_dependency
                    ):
                        raise session_module.ContinuousSessionError(
                            "canonical PaperBook frozen save dependency global changed: "
                            + dependency_name
                        )
            for (
                dependency,
                expected_builtins,
                expected_builtin_bindings,
            ) in canonical_frozen_save_builtin_graph:
                if dependency.__builtins__ is not expected_builtins:
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook frozen save builtins authority changed"
                    )
                for builtin_name, expected_builtin in expected_builtin_bindings:
                    if (
                        expected_builtins.get(builtin_name, missing_slot)
                        is not expected_builtin
                    ):
                        raise session_module.ContinuousSessionError(
                            "canonical PaperBook frozen save builtin binding changed: "
                            + builtin_name
                        )
        if canonical_save_authority_binding_snapshot:
            if (
                exact_len(canonical_save_authority_globals)
                != exact_len(canonical_save_authority_binding_snapshot)
            ):
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook save wrapper authority changed"
                )
            for name, expected in canonical_save_authority_binding_snapshot:
                if canonical_save_authority_globals.get(name, missing_slot) is not expected:
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook save wrapper authority changed: " + name
                    )
            for (
                dependency,
                expected_dependency_code,
                expected_dependency_globals,
                expected_dependency_defaults,
                expected_dependency_kwdefaults,
                expected_dependency_kwdefault_items,
                expected_dependency_closure,
                expected_dependency_closure_cells,
                expected_dependency_closure_values,
                expected_dependency_bindings,
            ) in canonical_save_verifier_dependency_graph:
                if (
                    exact_type(dependency) is not function_type
                    or dependency.__code__ is not expected_dependency_code
                    or dependency.__globals__ is not expected_dependency_globals
                    or dependency.__defaults__ is not expected_dependency_defaults
                    or dependency.__kwdefaults__ is not expected_dependency_kwdefaults
                    or dependency.__closure__ is not expected_dependency_closure
                ):
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook save verifier dependency executable changed"
                    )
                if expected_dependency_kwdefaults is not None:
                    if (
                        exact_len(expected_dependency_kwdefaults)
                        != exact_len(expected_dependency_kwdefault_items)
                        or exact_any(
                            expected_dependency_kwdefaults.get(
                                key,
                                missing_slot,
                            )
                            is not value
                            for key, value in expected_dependency_kwdefault_items
                        )
                    ):
                        raise session_module.ContinuousSessionError(
                            "canonical PaperBook save verifier dependency defaults changed"
                        )

                current_dependency_closure = dependency.__closure__
                current_dependency_cells = (
                    ()
                    if current_dependency_closure is None
                    else current_dependency_closure
                )
                if (
                    exact_len(current_dependency_cells)
                    != exact_len(expected_dependency_closure_cells)
                    or exact_any(
                        current is not expected
                        for current, expected in exact_zip(
                            current_dependency_cells,
                            expected_dependency_closure_cells,
                        )
                    )
                ):
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook save verifier dependency closure changed"
                    )
                try:
                    current_dependency_closure_values = exact_tuple(
                        cell.cell_contents for cell in current_dependency_cells
                    )
                except ValueError as exc:
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook save verifier dependency closure changed"
                    ) from exc
                if (
                    exact_len(current_dependency_closure_values)
                    != exact_len(expected_dependency_closure_values)
                    or exact_any(
                        current is not expected
                        for current, expected in exact_zip(
                            current_dependency_closure_values,
                            expected_dependency_closure_values,
                        )
                    )
                ):
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook save verifier dependency closure changed"
                    )

                for dependency_name, expected_dependency in expected_dependency_bindings:
                    if (
                        expected_dependency_globals.get(
                            dependency_name,
                            missing_slot,
                        )
                        is not expected_dependency
                    ):
                        raise session_module.ContinuousSessionError(
                            "canonical PaperBook save verifier dependency global changed: "
                            + dependency_name
                        )

            for (
                dependency,
                expected_builtins,
                expected_builtin_bindings,
            ) in canonical_save_verifier_builtin_graph:
                if dependency.__builtins__ is not expected_builtins:
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook save verifier builtins authority changed"
                    )
                for builtin_name, expected_builtin in expected_builtin_bindings:
                    if (
                        expected_builtins.get(builtin_name, missing_slot)
                        is not expected_builtin
                    ):
                        raise session_module.ContinuousSessionError(
                            "canonical PaperBook save verifier builtin binding changed: "
                            + builtin_name
                        )

            for (
                verifier,
                expected_code,
                expected_globals,
                expected_defaults,
                expected_kwdefaults,
                expected_closure,
                expected_closure_values,
            ) in canonical_save_verifier_witnesses:
                if (
                    exact_type(verifier) is not function_type
                    or verifier.__code__ is not expected_code
                    or verifier.__globals__ is not expected_globals
                    or verifier.__defaults__ != expected_defaults
                    or verifier.__kwdefaults__ != expected_kwdefaults
                    or verifier.__closure__ is not expected_closure
                ):
                    raise session_module.ContinuousSessionError(
                        "canonical PaperBook save verifier executable changed"
                    )
                if expected_closure is not None:
                    try:
                        current_closure_values = exact_tuple(
                            cell.cell_contents for cell in expected_closure
                        )
                    except ValueError as exc:
                        raise session_module.ContinuousSessionError(
                            "canonical PaperBook save verifier closure changed"
                        ) from exc
                    if (
                        expected_closure_values is None
                        or exact_len(current_closure_values) != exact_len(expected_closure_values)
                        or exact_any(
                            current is not expected
                            for current, expected in exact_zip(
                                current_closure_values,
                                expected_closure_values,
                            )
                        )
                    ):
                        raise session_module.ContinuousSessionError(
                            "canonical PaperBook save verifier closure changed"
                        )
            try:
                canonical_save_require_delegate(canonical_save_delegate_witnesses)
                canonical_save_require_class(
                    canonical_paper_book,
                    canonical_save_class_witnesses,
                )
                canonical_save_require_value(canonical_save_value_witnesses)
            except (TypeError, ValueError) as exc:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook save verifier graph changed"
                ) from exc
        if canonical_load_bytes_globals.get("json", missing_slot) is not canonical_json_dependency:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook json parser dependency changed"
            )
        if (
            exact_getattr(
                canonical_json_dependency,
                "loads",
                missing_slot,
            )
            is not canonical_json_loads
        ):
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
            if exact_any(
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
            matches_resolution = exact_any(
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
        if exact_len(target_closure) != exact_len(target_code.co_freevars):
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
            (target, target_code, exact_tuple(cell_witnesses))
        )
    canonical_prelearning_graph = exact_tuple(canonical_prelearning_graph_list)

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
            if exact_len(current_closure) != exact_len(expected_cells):
                raise session_module.ContinuousSessionError(
                    "canonical pre-learning settlement authority changed"
                )
            for current_cell, (expected_cell, expected_value) in exact_zip(
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
            if exact_len(current_closure) != exact_len(expected_cells):
                raise session_module.ContinuousSessionError(
                    "canonical pre-learning settlement authority changed"
                )
            for current_cell, (expected_cell, expected_value) in exact_zip(
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