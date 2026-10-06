
def _guard_paperbook_constructor_authority(method):
    """Seal PaperBook authority registration before instance state is accepted."""
    method_code = method.__code__
    operation_register = _register_paperbook_operation_lock
    operation_register_code = operation_register.__code__
    opening_register = _register_ticket_opening_authority_book
    opening_register_code = opening_register.__code__
    causal_register = _register_paperbook_causal_history_authority_book
    causal_register_code = causal_register.__code__
    type_authority = _require_paperbook_type_authority
    type_authority_code = type_authority.__code__
    canonical_decimal = _canonicalize_paperbook_constructor_decimal
    canonical_decimal_code = canonical_decimal.__code__
    runtime_helper_authority = _require_paperbook_runtime_helper_authority
    runtime_helper_authority_code = runtime_helper_authority.__code__
    runtime_module_globals = globals()
    constructor_module_dependencies = {
        "Decimal": Decimal,
        "DecimalException": DecimalException,
        "_MAX_PAPER_DECIMAL_TEXT_CHARS": _MAX_PAPER_DECIMAL_TEXT_CHARS,
    }
    constructor_registrar_closure_witnesses = {}
    for name, registrar in (
        ("operation lock", operation_register),
        ("opening registry", opening_register),
        ("causal-history registry", causal_register),
    ):
        closure = registrar.__closure__
        constructor_registrar_closure_witnesses[name] = (
            closure,
            None
            if closure is None
            else tuple(cell.cell_contents for cell in closure),
        )

    def require_constructor_module_dependencies() -> None:
        for name, dependency in constructor_module_dependencies.items():
            if runtime_module_globals.get(name) is not dependency:
                raise ValueError(
                    f"PaperBook constructor module dependency changed: {name}"
                )

    def require_runtime_helpers(book: object) -> None:
        if runtime_helper_authority.__code__ is not runtime_helper_authority_code:
            raise ValueError("PaperBook runtime helper dispatch authority changed")
        runtime_helper_authority(book)
        if runtime_helper_authority.__code__ is not runtime_helper_authority_code:
            raise ValueError("PaperBook runtime helper dispatch authority changed")

    def require_type(book: object) -> None:
        if type_authority.__code__ is not type_authority_code:
            raise ValueError("PaperBook canonical type authority changed")
        type_authority(book)
        if type_authority.__code__ is not type_authority_code:
            raise ValueError("PaperBook canonical type authority changed")

    def invoke(
        registrar,
        expected_code,
        label: str,
        book: object,
    ) -> None:
        if registrar.__code__ is not expected_code:
            raise ValueError(f"PaperBook {label} constructor authority changed")
        expected_closure, expected_values = constructor_registrar_closure_witnesses[label]
        if registrar.__closure__ is not expected_closure:
            raise ValueError(f"PaperBook {label} constructor authority closure changed")
        if expected_values is not None:
            current_closure = registrar.__closure__
            if (
                current_closure is None
                or len(current_closure) != len(expected_values)
                or any(
                    cell.cell_contents is not expected
                    for cell, expected in zip(current_closure, expected_values)
                )
            ):
                raise ValueError(f"PaperBook {label} constructor authority closure changed")
        registrar(book)
        if registrar.__code__ is not expected_code:
            raise ValueError(f"PaperBook {label} constructor authority changed")

    @wraps(method)
    def guarded(self, *args, **kwargs):
        if method.__code__ is not method_code:
            raise ValueError("PaperBook constructor callable authority changed")
        require_type(self)