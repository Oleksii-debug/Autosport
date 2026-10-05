"""Seal BETDAQ economic SOAP request construction to its canonical executable.

`BetdaqEconomicReadbackClient._call` binds evidence to canonical method/attribute
identity, but the actual network bytes are constructed later through the module-level
`_request_xml` callable.  Rebinding or mutating that callable must therefore fail
before it can manufacture request bytes that disagree with the claimed authority.

This guard adds no provider write surface and does not widen the economic READ
allowlist.
"""

from __future__ import annotations

from . import betdaq_settlement_readback as _settlement


_ERROR = _settlement.BetdaqEconomicReadbackError
_CLIENT_TYPE = _settlement.BetdaqEconomicReadbackClient
_CANONICAL_REQUEST_XML = _settlement._request_xml
_CANONICAL_REQUEST_XML_CODE = getattr(_CANONICAL_REQUEST_XML, "__code__", None)
_CANONICAL_REQUEST_XML_DEFAULTS = getattr(_CANONICAL_REQUEST_XML, "__defaults__", None)
_CANONICAL_REQUEST_XML_KWDEFAULTS = getattr(
    _CANONICAL_REQUEST_XML,
    "__kwdefaults__",
    None,
)
_ORIGINAL_CALL = _CLIENT_TYPE._call

if _CANONICAL_REQUEST_XML_CODE is None or not callable(_ORIGINAL_CALL):
    raise RuntimeError("BETDAQ economic request-body authority is unavailable")


def _install_request_body_authority_guard() -> None:
    error_type = _ERROR
    client_type = _CLIENT_TYPE
    canonical_builder = _CANONICAL_REQUEST_XML
    canonical_code = _CANONICAL_REQUEST_XML_CODE
    canonical_defaults = _CANONICAL_REQUEST_XML_DEFAULTS
    canonical_kwdefaults = _CANONICAL_REQUEST_XML_KWDEFAULTS
    original_call = _ORIGINAL_CALL

    def _builder_current() -> bool:
        return (
            getattr(_settlement, "_request_xml", None) is canonical_builder
            and getattr(canonical_builder, "__code__", None) is canonical_code
            and getattr(canonical_builder, "__defaults__", None)
            is canonical_defaults
            and getattr(canonical_builder, "__kwdefaults__", None)
            is canonical_kwdefaults
        )

    def guarded_call(self, *args, **kwargs):
        if not _builder_current():
            raise error_type(
                "request body does not match exact economic request authority"
            )
        result = original_call(self, *args, **kwargs)
        if not _builder_current():
            raise error_type(
                "request body does not match exact economic request authority"
            )
        return result

    if client_type._call is not original_call:
        raise RuntimeError("BETDAQ economic call dispatch changed before request guard")
    client_type._call = guarded_call


_install_request_body_authority_guard()
del _install_request_body_authority_guard
