"""Seal the fixed-origin Parlay catalog acquirer against caller dispatch drift.

The owning acquisition module already captures its positive transport/clock objects in
closure/default cells and removes the direct origin issuer from module dispatch. Python
function identity alone is not sufficient, however: caller code can mutate a reachable
function object's ``__code__``, closure cells, or keyword-default dependency graph
without rebinding that outer object. Likewise, a caller-owned subclass of the public
acquisition DTO must not cross the conditional-acquisition seam and virtual-dispatch
authority-bearing prior fields before the canonical validator runs. This package guard
pins those direct boundaries and the exact redirect-refusal policy, failing closed before
or after acquisition.

No second provider client, store, scheduler, or origin authority is introduced.
"""

from __future__ import annotations

from . import parlay_sport_catalog_acquisition as _catalog


def _closure_witness(function):
    cells = function.__closure__ or ()
    names = function.__code__.co_freevars
    if len(cells) != len(names):
        raise RuntimeError("invalid function closure")
    return tuple(
        (
            name,
            cell,
            cell.cell_contents,
            getattr(cell.cell_contents, "__code__", None),
        )
        for name, cell in zip(names, cells)
    )


def _kwdefaults_witness(function):
    defaults = function.__kwdefaults__ or {}
    return tuple((name, value, getattr(value, "__code__", None)) for name, value in defaults.items())


def _install_guard() -> None:
    public_acquirer = _catalog.acquire_parlay_sport_catalog
    public_acquirer_code = public_acquirer.__code__
    public_closure = _closure_witness(public_acquirer)
    public_kwdefaults = _kwdefaults_witness(public_acquirer)
    acquisition_type = _catalog.ParlaySportCatalogAcquisition
    error_type = _catalog.ParlaySportCatalogEvidenceError

    function_names = (
        "_perform_catalog_http_response",
        "_bounded_read",
        "_validate_raw_response",
        "_validate_timestamp",
        "_timestamp_instant",
        "_single_header",
        "_acquisition_id",
        "_validate_prior_acquisition",
        "_utc_now_iso",
    )
    functions = tuple((name, getattr(_catalog, name)) for name in function_names)
    function_codes = tuple(
        (name, function, function.__code__) for name, function in functions
    )

    product_transport = _catalog._default_transport
    product_transport_code = product_transport.__code__
    product_transport_closure = _closure_witness(product_transport)
    product_transport_kwdefaults = _kwdefaults_witness(product_transport)

    redirect_handler_type = _catalog._PRODUCT_REDIRECT_HANDLER
    redirect_request = redirect_handler_type.redirect_request
    redirect_request_code = getattr(redirect_request, "__code__", None)

    request_type = _catalog.urllib.request.Request
    request_init = request_type.__init__
    request_init_code = getattr(request_init, "__code__", None)
    build_opener = _catalog.urllib.request.build_opener
    build_opener_code = getattr(build_opener, "__code__", None)

    def require_function_cells(function, witnesses, *, label: str) -> None:
        cells = function.__closure__ or ()
        names = function.__code__.co_freevars
        if len(cells) != len(witnesses) or len(names) != len(witnesses):
            raise error_type(f"Parlay sport-catalog {label} closure changed")
        for index, (name, expected_cell, expected_value, expected_code) in enumerate(witnesses):
            if names[index] != name or cells[index] is not expected_cell:
                raise error_type(f"Parlay sport-catalog {label} closure changed")
            try:
                current_value = cells[index].cell_contents
            except ValueError as exc:
                raise error_type(f"Parlay sport-catalog {label} closure changed") from exc
            if current_value is not expected_value:
                raise error_type(f"Parlay sport-catalog {label} closure changed")
            if getattr(expected_value, "__code__", None) is not expected_code:
                raise error_type(f"Parlay sport-catalog {label} closure executable changed")

    def require_kwdefaults(function, witnesses, *, label: str) -> None:
        defaults = function.__kwdefaults__ or {}
        if tuple(defaults) != tuple(name for name, _value, _code in witnesses):
            raise error_type(f"Parlay sport-catalog {label} keyword defaults changed")
        for name, expected_value, expected_code in witnesses:
            current = defaults.get(name)
            if current is not expected_value:
                raise error_type(f"Parlay sport-catalog {label} keyword defaults changed")
            if getattr(expected_value, "__code__", None) is not expected_code:
                raise error_type(
                    f"Parlay sport-catalog {label} keyword-default executable changed"
                )

    def require_canonical_execution() -> None:
        if (
            _catalog.acquire_parlay_sport_catalog is not guarded_acquirer
            or getattr(public_acquirer, "__code__", None) is not public_acquirer_code
            or _catalog.ParlaySportCatalogAcquisition is not acquisition_type
            or _catalog._default_transport is not product_transport
            or getattr(product_transport, "__code__", None) is not product_transport_code
            or _catalog._PRODUCT_REDIRECT_HANDLER is not redirect_handler_type
            or redirect_handler_type.redirect_request is not redirect_request
            or getattr(redirect_request, "__code__", None) is not redirect_request_code
        ):
            raise error_type("Parlay sport-catalog product acquirer dispatch changed")

        require_function_cells(public_acquirer, public_closure, label="public acquirer")
        require_kwdefaults(public_acquirer, public_kwdefaults, label="public acquirer")
        require_function_cells(
            product_transport,
            product_transport_closure,
            label="product transport",
        )
        require_kwdefaults(
            product_transport,
            product_transport_kwdefaults,
            label="product transport",
        )

        for name, function, code in function_codes:
            if (
                getattr(_catalog, name, None) is not function
                or getattr(function, "__code__", None) is not code
            ):
                raise error_type(
                    f"Parlay sport-catalog executable dependency {name!r} changed"
                )

        if (
            _catalog.urllib.request.Request is not request_type
            or request_type.__init__ is not request_init
            or getattr(request_init, "__code__", None) is not request_init_code
            or _catalog.urllib.request.build_opener is not build_opener
            or getattr(build_opener, "__code__", None) is not build_opener_code
        ):
            raise error_type("Parlay sport-catalog urllib execution authority changed")

    def guarded_acquirer(*args, **kwargs):
        require_canonical_execution()
        prior = kwargs.get("prior")
        if prior is not None and type(prior) is not acquisition_type:
            raise error_type(
                "conditional acquisition prior must be exact ParlaySportCatalogAcquisition"
            )
        result = public_acquirer(*args, **kwargs)
        require_canonical_execution()
        if type(result) is not acquisition_type:
            raise error_type("Parlay sport-catalog acquirer returned non-canonical evidence")
        return result

    guarded_acquirer.__name__ = public_acquirer.__name__
    guarded_acquirer.__qualname__ = public_acquirer.__qualname__
    guarded_acquirer.__doc__ = public_acquirer.__doc__
    guarded_acquirer.__module__ = public_acquirer.__module__
    _catalog.acquire_parlay_sport_catalog = guarded_acquirer


_install_guard()
del _install_guard