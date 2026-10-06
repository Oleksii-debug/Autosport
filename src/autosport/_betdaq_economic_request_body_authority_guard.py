"""Seal BETDAQ economic request and DTO authority to product-issued evidence.

`BetdaqEconomicReadbackClient._call` binds evidence to canonical method/attribute
identity, but the actual network bytes are constructed later through the module-level
`_request_xml` callable. Rebinding or mutating that callable must therefore fail
before it can manufacture request bytes that disagree with the claimed authority.

The settlement DTOs are frozen dataclasses, but Python's low-level
``object.__setattr__`` can still overwrite a slotted frozen field after validation,
and ``dataclasses.replace`` can construct a structurally valid row with different
economics under an older provider-payload hash. This guard therefore also:

* converts every authority-bearing DTO slot into a write-once descriptor while
  retaining ordinary dataclass construction/copy semantics for a fresh instance;
* records replay-stable semantic commitments only after the canonical product client
  has actually produced the evidence/order/readback from provider bytes; and
* requires those issued commitments before economic identifiers or replay coalescence
  can be used.

Receive time is intentionally excluded from the replay-stable issuance commitment,
matching the existing evidence/readback identity law: an exact provider payload may
be represented by a later acquisition timestamp without changing its economics.

This guard adds no provider write surface and does not widen the economic READ
allowlist.
"""

from __future__ import annotations

from threading import RLock
from types import MemberDescriptorType

from . import betdaq_settlement_readback as _settlement


_ERROR = _settlement.BetdaqEconomicReadbackError
_CLIENT_TYPE = _settlement.BetdaqEconomicReadbackClient
_EVIDENCE_TYPE = _settlement.BetdaqEconomicEvidence
_ORDER_TYPE = _settlement.BetdaqOrderSettlementObservation
_POSTING_TYPE = _settlement.BetdaqPostingObservation
_READBACK_TYPE = _settlement.BetdaqPostingsReadback
_CANONICAL_REQUEST_XML = _settlement._request_xml
_CANONICAL_REQUEST_XML_CODE = getattr(_CANONICAL_REQUEST_XML, "__code__", None)
_CANONICAL_REQUEST_XML_DEFAULTS = getattr(_CANONICAL_REQUEST_XML, "__defaults__", None)
_CANONICAL_REQUEST_XML_KWDEFAULTS = getattr(
    _CANONICAL_REQUEST_XML,
    "__kwdefaults__",
    None,
)
_ORIGINAL_CALL = _CLIENT_TYPE._call
_ORIGINAL_READ_ORDER = _CLIENT_TYPE.read_order_details
_ORIGINAL_READ_POSTINGS = _CLIENT_TYPE.read_account_postings
_ORIGINAL_READ_POSTINGS_BY_ID = _CLIENT_TYPE.read_account_postings_by_id
_ORIGINAL_EVIDENCE_VALIDATOR = _EVIDENCE_TYPE.__post_init__
_ORIGINAL_ORDER_VALIDATOR = _ORDER_TYPE.__post_init__
_ORIGINAL_POSTING_VALIDATOR = _POSTING_TYPE.__post_init__
_ORIGINAL_READBACK_VALIDATOR = _READBACK_TYPE.__post_init__
_ORIGINAL_EVIDENCE_ID = _EVIDENCE_TYPE.evidence_id.fget
_ORIGINAL_ORDER_CANONICAL_DICT = _ORDER_TYPE.canonical_dict
_ORIGINAL_ORDER_OBSERVATION_ID = _ORDER_TYPE.observation_id.fget
_ORIGINAL_POSTING_CANONICAL_DICT = _POSTING_TYPE.canonical_dict
_ORIGINAL_POSTING_TRANSACTION_IDENTITY = _POSTING_TYPE.transaction_identity.fget
_ORIGINAL_POSTING_OBSERVATION_ID = _POSTING_TYPE.observation_id.fget
_ORIGINAL_READBACK_ID = _READBACK_TYPE.readback_id.fget
_ORIGINAL_COALESCE = _settlement.coalesce_posting_replays
_CANONICAL_SHA256 = _settlement._canonical_sha256
_CANONICAL_DECIMAL_TEXT = _settlement._decimal_text
_CANONICAL_OPTIONAL_DECIMAL_TEXT = _settlement._optional_decimal_text

if (
    _CANONICAL_REQUEST_XML_CODE is None
    or not callable(_ORIGINAL_CALL)
    or _ORIGINAL_EVIDENCE_ID is None
    or _ORIGINAL_ORDER_OBSERVATION_ID is None
    or _ORIGINAL_POSTING_TRANSACTION_IDENTITY is None
    or _ORIGINAL_POSTING_OBSERVATION_ID is None
    or _ORIGINAL_READBACK_ID is None
):
    raise RuntimeError("BETDAQ economic authority surface is unavailable")


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


def _install_economic_dto_integrity_guard() -> None:
    error_type = _ERROR
    client_type = _CLIENT_TYPE
    evidence_type = _EVIDENCE_TYPE
    order_type = _ORDER_TYPE
    posting_type = _POSTING_TYPE
    readback_type = _READBACK_TYPE
    canonical_sha256 = _CANONICAL_SHA256
    decimal_text = _CANONICAL_DECIMAL_TEXT
    optional_decimal_text = _CANONICAL_OPTIONAL_DECIMAL_TEXT
    original_evidence_validator = _ORIGINAL_EVIDENCE_VALIDATOR
    original_order_validator = _ORIGINAL_ORDER_VALIDATOR
    original_posting_validator = _ORIGINAL_POSTING_VALIDATOR
    original_readback_validator = _ORIGINAL_READBACK_VALIDATOR
    original_evidence_id = _ORIGINAL_EVIDENCE_ID
    original_order_canonical_dict = _ORIGINAL_ORDER_CANONICAL_DICT
    original_order_observation_id = _ORIGINAL_ORDER_OBSERVATION_ID
    original_posting_canonical_dict = _ORIGINAL_POSTING_CANONICAL_DICT
    original_posting_transaction_identity = _ORIGINAL_POSTING_TRANSACTION_IDENTITY
    original_posting_observation_id = _ORIGINAL_POSTING_OBSERVATION_ID
    original_readback_id = _ORIGINAL_READBACK_ID
    original_coalesce = _ORIGINAL_COALESCE
    original_read_order = _ORIGINAL_READ_ORDER
    original_read_postings = _ORIGINAL_READ_POSTINGS
    original_read_postings_by_id = _ORIGINAL_READ_POSTINGS_BY_ID

    dto_types = (evidence_type, order_type, posting_type, readback_type)
    raw_slots: dict[type, dict[str, MemberDescriptorType]] = {}
    sealed_slots: dict[type, dict[str, object]] = {}

    def make_write_once_descriptor(slot: MemberDescriptorType, field: str):
        class _WriteOnceDescriptor:
            __slots__ = ()

            def __get__(self, instance, owner=None):
                if instance is None:
                    return self
                return slot.__get__(instance, owner or type(instance))

            def __set__(self, instance, value) -> None:
                try:
                    slot.__get__(instance, type(instance))
                except AttributeError:
                    slot.__set__(instance, value)
                    return
                raise error_type(
                    f"canonical BETDAQ economic DTO field {field} is write-once"
                )

            def __delete__(self, instance) -> None:
                raise error_type(
                    f"canonical BETDAQ economic DTO field {field} is write-once"
                )

        return _WriteOnceDescriptor()

    for dto_type in dto_types:
        fields = tuple(dto_type.__dataclass_fields__)
        raw_by_name: dict[str, MemberDescriptorType] = {}
        sealed_by_name: dict[str, object] = {}
        for field in fields:
            slot = vars(dto_type).get(field)
            if type(slot) is not MemberDescriptorType:
                raise RuntimeError(
                    "BETDAQ economic DTO slot surface changed before integrity guard"
                )
            descriptor = make_write_once_descriptor(slot, field)
            raw_by_name[field] = slot
            sealed_by_name[field] = descriptor
            setattr(dto_type, field, descriptor)
        raw_slots[dto_type] = raw_by_name
        sealed_slots[dto_type] = sealed_by_name

    issued_evidence: set[str] = set()
    issued_orders: set[str] = set()
    issued_postings: set[str] = set()
    issued_readbacks: set[str] = set()
    issuance_guard = RLock()

    def require_types_current() -> None:
        expected = (
            ("BetdaqEconomicEvidence", evidence_type),
            ("BetdaqOrderSettlementObservation", order_type),
            ("BetdaqPostingObservation", posting_type),
            ("BetdaqPostingsReadback", readback_type),
        )
        if any(getattr(_settlement, name, None) is not value for name, value in expected):
            raise error_type("canonical BETDAQ economic DTO type authority was replaced")

    def require_slot_seal(dto_type: type) -> None:
        if any(
            vars(dto_type).get(name) is not descriptor
            for name, descriptor in sealed_slots[dto_type].items()
        ):
            raise error_type("canonical BETDAQ economic DTO field seal was replaced")

    def raw(value: object, dto_type: type, field: str):
        return raw_slots[dto_type][field].__get__(value, dto_type)

    def evidence_commitment(value) -> str:
        if type(value) is not evidence_type:
            raise error_type("BETDAQ economic evidence is not canonical")
        require_slot_seal(evidence_type)
        original_evidence_validator(value)
        return canonical_sha256(
            {
                "kind": "betdaq-economic-evidence-issued-v1",
                # observed_at is acquisition provenance, not replay-stable economics.
                "method": raw(value, evidence_type, "method"),
                "request_identity_sha256": raw(
                    value, evidence_type, "request_identity_sha256"
                ),
                "source_payload_sha256": raw(
                    value, evidence_type, "source_payload_sha256"
                ),
                "account_context_id": raw(value, evidence_type, "account_context_id"),
                "authenticated_principal_continuity_proven": raw(
                    value,
                    evidence_type,
                    "authenticated_principal_continuity_proven",
                ),
                "physical_account_identity_proven": raw(
                    value, evidence_type, "physical_account_identity_proven"
                ),
            }
        )

    def posting_commitment(value) -> str:
        if type(value) is not posting_type:
            raise error_type("BETDAQ posting observation is not canonical")
        require_slot_seal(posting_type)
        original_posting_validator(value)
        evidence = raw(value, posting_type, "evidence")
        return canonical_sha256(
            {
                "kind": "betdaq-posting-issued-v1",
                "posted_at": raw(value, posting_type, "posted_at"),
                "description": raw(value, posting_type, "description"),
                "amount": decimal_text(raw(value, posting_type, "amount")),
                "resulting_balance": decimal_text(
                    raw(value, posting_type, "resulting_balance")
                ),
                "posting_category": raw(value, posting_type, "posting_category"),
                "order_id": raw(value, posting_type, "order_id"),
                "market_id": raw(value, posting_type, "market_id"),
                "transaction_id": raw(value, posting_type, "transaction_id"),
                "currency": raw(value, posting_type, "currency"),
                "evidence": evidence_commitment(evidence),
            }
        )

    def order_commitment(value) -> str:
        if type(value) is not order_type:
            raise error_type("BETDAQ order settlement observation is not canonical")
        require_slot_seal(order_type)
        original_order_validator(value)
        evidence = raw(value, order_type, "evidence")
        return canonical_sha256(
            {
                "kind": "betdaq-order-settlement-issued-v1",
                "order_id": raw(value, order_type, "order_id"),
                "market_id": raw(value, order_type, "market_id"),
                "selection_id": raw(value, order_type, "selection_id"),
                "order_status_code": raw(value, order_type, "order_status_code"),
                "sequence_number": raw(value, order_type, "sequence_number"),
                "issued_at": raw(value, order_type, "issued_at"),
                "last_changed_at": raw(value, order_type, "last_changed_at"),
                "requested_stake": decimal_text(
                    raw(value, order_type, "requested_stake")
                ),
                "requested_price": decimal_text(
                    raw(value, order_type, "requested_price")
                ),
                "total_stake": decimal_text(raw(value, order_type, "total_stake")),
                "unmatched_stake": decimal_text(
                    raw(value, order_type, "unmatched_stake")
                ),
                "average_price": decimal_text(
                    raw(value, order_type, "average_price")
                ),
                "matching_timestamp": raw(value, order_type, "matching_timestamp"),
                "polarity_code": raw(value, order_type, "polarity_code"),
                "punter_reference_number": raw(
                    value, order_type, "punter_reference_number"
                ),
                "gross_settlement_amount": optional_decimal_text(
                    raw(value, order_type, "gross_settlement_amount")
                ),
                "order_commission": optional_decimal_text(
                    raw(value, order_type, "order_commission")
                ),
                "market_commission": optional_decimal_text(
                    raw(value, order_type, "market_commission")
                ),
                "market_settled_at": raw(value, order_type, "market_settled_at"),
                "currency": raw(value, order_type, "currency"),
                "denomination_proven": raw(
                    value, order_type, "denomination_proven"
                ),
                "scalar_economic_use_proven": raw(
                    value, order_type, "scalar_economic_use_proven"
                ),
                "final_settlement_proven": raw(
                    value, order_type, "final_settlement_proven"
                ),
                "evidence": evidence_commitment(evidence),
            }
        )

    def readback_commitment(value) -> str:
        if type(value) is not readback_type:
            raise error_type("BETDAQ postings readback is not canonical")
        require_slot_seal(readback_type)
        original_readback_validator(value)
        evidence = raw(value, readback_type, "evidence")
        postings = raw(value, readback_type, "postings")
        return canonical_sha256(
            {
                "kind": "betdaq-postings-readback-issued-v1",
                "method": raw(value, readback_type, "method"),
                "query_start_at": raw(value, readback_type, "query_start_at"),
                "query_end_at": raw(value, readback_type, "query_end_at"),
                "query_transaction_id": raw(
                    value, readback_type, "query_transaction_id"
                ),
                "currency": raw(value, readback_type, "currency"),
                "available_funds": decimal_text(
                    raw(value, readback_type, "available_funds")
                ),
                "balance": decimal_text(raw(value, readback_type, "balance")),
                "credit": decimal_text(raw(value, readback_type, "credit")),
                "exposure": decimal_text(raw(value, readback_type, "exposure")),
                "window_complete": raw(value, readback_type, "window_complete"),
                "postings": [posting_commitment(item) for item in postings],
                "evidence": evidence_commitment(evidence),
            }
        )

    def register_evidence(value) -> None:
        require_types_current()
        commitment = evidence_commitment(value)
        with issuance_guard:
            issued_evidence.add(commitment)

    def require_evidence(value) -> None:
        require_types_current()
        commitment = evidence_commitment(value)
        with issuance_guard:
            if commitment not in issued_evidence:
                raise error_type(
                    "BETDAQ economic evidence was not issued from canonical provider bytes"
                )

    def register_order(value) -> None:
        require_types_current()
        evidence = raw(value, order_type, "evidence")
        register_evidence(evidence)
        commitment = order_commitment(value)
        with issuance_guard:
            issued_orders.add(commitment)

    def require_order(value) -> None:
        require_types_current()
        require_evidence(raw(value, order_type, "evidence"))
        commitment = order_commitment(value)
        with issuance_guard:
            if commitment not in issued_orders:
                raise error_type(
                    "BETDAQ order settlement was not issued from canonical provider bytes"
                )

    def register_readback(value) -> None:
        require_types_current()
        evidence = raw(value, readback_type, "evidence")
        register_evidence(evidence)
        postings = raw(value, readback_type, "postings")
        posting_commitments = tuple(posting_commitment(item) for item in postings)
        commitment = readback_commitment(value)
        with issuance_guard:
            issued_postings.update(posting_commitments)
            issued_readbacks.add(commitment)

    def require_posting(value) -> None:
        require_types_current()
        require_evidence(raw(value, posting_type, "evidence"))
        commitment = posting_commitment(value)
        with issuance_guard:
            if commitment not in issued_postings:
                raise error_type(
                    "BETDAQ posting observation was not issued from canonical provider bytes"
                )

    def require_readback(value) -> None:
        require_types_current()
        require_evidence(raw(value, readback_type, "evidence"))
        commitment = readback_commitment(value)
        with issuance_guard:
            if commitment not in issued_readbacks:
                raise error_type(
                    "BETDAQ postings readback was not issued from canonical provider bytes"
                )

    request_guarded_call = client_type._call

    def issued_call(self, *args, **kwargs):
        result, evidence = request_guarded_call(self, *args, **kwargs)
        register_evidence(evidence)
        return result, evidence

    def issued_read_order(self, *args, **kwargs):
        value = original_read_order(self, *args, **kwargs)
        register_order(value)
        return value

    def issued_read_postings(self, *args, **kwargs):
        value = original_read_postings(self, *args, **kwargs)
        register_readback(value)
        return value

    def issued_read_postings_by_id(self, *args, **kwargs):
        value = original_read_postings_by_id(self, *args, **kwargs)
        register_readback(value)
        return value

    def guarded_evidence_id(self):
        require_evidence(self)
        return original_evidence_id(self)

    def guarded_order_canonical_dict(self):
        require_order(self)
        return original_order_canonical_dict(self)

    def guarded_order_observation_id(self):
        require_order(self)
        return original_order_observation_id(self)

    def guarded_posting_canonical_dict(self):
        require_posting(self)
        return original_posting_canonical_dict(self)

    def guarded_posting_transaction_identity(self):
        require_posting(self)
        return original_posting_transaction_identity(self)

    def guarded_posting_observation_id(self):
        require_posting(self)
        return original_posting_observation_id(self)

    def guarded_readback_id(self):
        require_readback(self)
        return original_readback_id(self)

    def guarded_coalesce(*readbacks):
        for readback in readbacks:
            require_readback(readback)
        return original_coalesce(*readbacks)

    client_type._call = issued_call
    client_type.read_order_details = issued_read_order
    client_type.read_account_postings = issued_read_postings
    client_type.read_account_postings_by_id = issued_read_postings_by_id
    evidence_type.evidence_id = property(
        guarded_evidence_id,
        doc=getattr(evidence_type.evidence_id, "__doc__", None),
    )
    order_type.canonical_dict = guarded_order_canonical_dict
    order_type.observation_id = property(
        guarded_order_observation_id,
        doc=getattr(order_type.observation_id, "__doc__", None),
    )
    posting_type.canonical_dict = guarded_posting_canonical_dict
    posting_type.transaction_identity = property(
        guarded_posting_transaction_identity,
        doc=getattr(posting_type.transaction_identity, "__doc__", None),
    )
    posting_type.observation_id = property(
        guarded_posting_observation_id,
        doc=getattr(posting_type.observation_id, "__doc__", None),
    )
    readback_type.readback_id = property(
        guarded_readback_id,
        doc=getattr(readback_type.readback_id, "__doc__", None),
    )
    _settlement.coalesce_posting_replays = guarded_coalesce


_install_request_body_authority_guard()
_install_economic_dto_integrity_guard()
del _install_request_body_authority_guard
del _install_economic_dto_integrity_guard
