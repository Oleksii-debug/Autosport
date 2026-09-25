"""Fail closed on private ForwardEconomicEvidence recorded-step identity drift.

ForwardEconomicEvidenceAccumulator already snapshots caller-visible protocol and step
values and independently re-derives statistical/currency aggregate caches.  A frozen
``ForwardEconomicStep`` is nevertheless mutable through ``object.__setattr__`` if a
caller reaches the private ``_steps`` list.  Keep an installation-private digest
registry so changing a committed step cannot silently rebind the later evidence
hash while leaving aggregate caches numerically coherent.
"""

from __future__ import annotations

from weakref import ReferenceType, ref

from . import forward_economic_evidence as _evidence


def _install_forward_economic_step_identity_guard() -> None:
    accumulator_type = _evidence.ForwardEconomicEvidenceAccumulator
    if vars(accumulator_type).get("_recorded_step_identity_guard_installed") is True:
        return

    raw_init = accumulator_type.__init__
    raw_record = accumulator_type.record
    raw_validate_aggregate = accumulator_type._validated_aggregate_state
    raw_summary = accumulator_type.summary
    raw_steps_property = accumulator_type.steps
    raw_next_sequence_property = accumulator_type.next_sequence
    step_type = _evidence.ForwardEconomicStep
    step_to_payload = step_type.to_payload
    step_to_payload_code = getattr(step_to_payload, "__code__", None)
    summary_type = _evidence.ForwardEconomicEvidenceSummary
    summary_init = summary_type.__init__
    summary_init_code = getattr(summary_init, "__code__", None)
    summary_to_payload = summary_type.to_payload
    summary_to_payload_code = getattr(summary_to_payload, "__code__", None)
    summary_field_descriptors = tuple(
        (name, getattr(summary_type, name))
        for name in summary_type.__dataclass_fields__
    )
    canonical_digest = _evidence._canonical_digest
    canonical_digest_code = getattr(canonical_digest, "__code__", None)
    instant_text = _evidence._instant_text
    instant_text_code = getattr(instant_text, "__code__", None)
    decimal_text = _evidence._decimal_text
    decimal_text_code = getattr(decimal_text, "__code__", None)
    instant = _evidence._instant
    instant_code = getattr(instant, "__code__", None)
    decimal = _evidence._decimal
    decimal_code = getattr(decimal, "__code__", None)
    json_module = _evidence.json
    json_dumps = json_module.dumps
    json_dumps_code = getattr(json_dumps, "__code__", None)
    json_encoder_module = json_module.encoder
    json_encoder_type = json_module.JSONEncoder
    json_encoder_init = json_encoder_type.__init__
    json_encoder_init_code = getattr(json_encoder_init, "__code__", None)
    json_encoder_encode = json_encoder_type.encode
    json_encoder_encode_code = getattr(json_encoder_encode, "__code__", None)
    json_encoder_iterencode = json_encoder_type.iterencode
    json_encoder_iterencode_code = getattr(json_encoder_iterencode, "__code__", None)
    json_encoder_c_make_encoder = getattr(json_encoder_module, "c_make_encoder", None)
    json_encoder_make_iterencode = getattr(json_encoder_module, "_make_iterencode", None)
    json_encoder_make_iterencode_code = getattr(
        json_encoder_make_iterencode,
        "__code__",
        None,
    )
    json_encode_basestring = getattr(json_encoder_module, "encode_basestring", None)
    json_encode_basestring_ascii = getattr(
        json_encoder_module,
        "encode_basestring_ascii",
        None,
    )
    json_encoder_infinity = getattr(json_encoder_module, "INFINITY", None)
    hashlib_module = _evidence.hashlib
    hashlib_sha256 = hashlib_module.sha256
    datetime_type = _evidence.datetime
    timezone_type = _evidence.timezone
    decimal_type = _evidence.Decimal
    max_decimal_text_chars = _evidence._MAX_CANONICAL_DECIMAL_TEXT_CHARS

    if (
        type(raw_steps_property) is not property
        or raw_steps_property.fget is None
        or type(raw_next_sequence_property) is not property
        or raw_next_sequence_property.fget is None
    ):
        raise RuntimeError("ForwardEconomicEvidenceAccumulator property surface changed")

    raw_steps_getter = raw_steps_property.fget
    raw_next_sequence_getter = raw_next_sequence_property.fget
    registries: dict[
        int,
        tuple[
            ReferenceType[_evidence.ForwardEconomicEvidenceAccumulator],
            tuple[str, ...],
            str,
        ],
    ] = {}

    def _require_executable_identity() -> None:
        if (
            _evidence.ForwardEconomicStep is not step_type
            or getattr(step_type, "to_payload", None) is not step_to_payload
            or getattr(step_to_payload, "__code__", None) is not step_to_payload_code
            or _evidence.ForwardEconomicEvidenceSummary is not summary_type
            or getattr(summary_type, "__init__", None) is not summary_init
            or getattr(summary_init, "__code__", None) is not summary_init_code
            or getattr(summary_type, "to_payload", None) is not summary_to_payload
            or getattr(summary_to_payload, "__code__", None) is not summary_to_payload_code
            or any(
                getattr(summary_type, name, None) is not descriptor
                for name, descriptor in summary_field_descriptors
            )
            or getattr(_evidence, "_canonical_digest", None) is not canonical_digest
            or getattr(canonical_digest, "__code__", None) is not canonical_digest_code
            or getattr(_evidence, "_instant_text", None) is not instant_text
            or getattr(instant_text, "__code__", None) is not instant_text_code
            or getattr(_evidence, "_decimal_text", None) is not decimal_text
            or getattr(decimal_text, "__code__", None) is not decimal_text_code
            or getattr(_evidence, "_instant", None) is not instant
            or getattr(instant, "__code__", None) is not instant_code
            or getattr(_evidence, "_decimal", None) is not decimal
            or getattr(decimal, "__code__", None) is not decimal_code
            or getattr(_evidence, "json", None) is not json_module
            or getattr(json_module, "dumps", None) is not json_dumps
            or getattr(json_dumps, "__code__", None) is not json_dumps_code
            or getattr(json_module, "encoder", None) is not json_encoder_module
            or getattr(json_module, "JSONEncoder", None) is not json_encoder_type
            or getattr(json_encoder_type, "__init__", None) is not json_encoder_init
            or getattr(json_encoder_init, "__code__", None) is not json_encoder_init_code
            or getattr(json_encoder_type, "encode", None) is not json_encoder_encode
            or getattr(json_encoder_encode, "__code__", None) is not json_encoder_encode_code
            or getattr(json_encoder_type, "iterencode", None) is not json_encoder_iterencode
            or getattr(json_encoder_iterencode, "__code__", None)
            is not json_encoder_iterencode_code
            or getattr(json_encoder_module, "c_make_encoder", None)
            is not json_encoder_c_make_encoder
            or getattr(json_encoder_module, "_make_iterencode", None)
            is not json_encoder_make_iterencode
            or getattr(json_encoder_make_iterencode, "__code__", None)
            is not json_encoder_make_iterencode_code
            or getattr(json_encoder_module, "encode_basestring", None)
            is not json_encode_basestring
            or getattr(json_encoder_module, "encode_basestring_ascii", None)
            is not json_encode_basestring_ascii
            or getattr(json_encoder_module, "INFINITY", None) is not json_encoder_infinity
            or getattr(_evidence, "hashlib", None) is not hashlib_module
            or getattr(hashlib_module, "sha256", None) is not hashlib_sha256
            or getattr(_evidence, "datetime", None) is not datetime_type
            or getattr(_evidence, "timezone", None) is not timezone_type
            or getattr(_evidence, "Decimal", None) is not decimal_type
            or getattr(_evidence, "_MAX_CANONICAL_DECIMAL_TEXT_CHARS", None)
            != max_decimal_text_chars
        ):
            raise _evidence.ForwardEconomicEvidenceError(
                "internal recorded step identity executable integrity drift"
            )

    def _registry_for(
        self: _evidence.ForwardEconomicEvidenceAccumulator,
    ) -> tuple[str, ...]:
        record = registries.get(id(self))
        if record is None or record[0]() is not self:
            raise _evidence.ForwardEconomicEvidenceError(
                "internal recorded step identity registry origin drift"
            )
        return record[1]

    def _expected_evidence_sha256(
        self: _evidence.ForwardEconomicEvidenceAccumulator,
    ) -> str:
        record = registries.get(id(self))
        if record is None or record[0]() is not self:
            raise _evidence.ForwardEconomicEvidenceError(
                "internal recorded step identity registry origin drift"
            )
        return record[2]

    def _evidence_digest(
        self: _evidence.ForwardEconomicEvidenceAccumulator,
        steps: tuple[_evidence.ForwardEconomicStep, ...],
    ) -> str:
        _require_executable_identity()
        digest = canonical_digest(
            {
                "schema_version": 1,
                "protocol_sha256": self._protocol_sha256,
                "steps": [step_to_payload(step) for step in steps],
            }
        )
        _require_executable_identity()
        return digest

    def _validate_step_prefix(
        self: _evidence.ForwardEconomicEvidenceAccumulator,
        digests: tuple[str, ...],
        *,
        require_exact_length: bool,
    ) -> tuple[_evidence.ForwardEconomicStep, ...]:
        _require_executable_identity()
        protocol = self._validated_protocol()
        steps = self._steps
        if type(steps) is not list:
            raise _evidence.ForwardEconomicEvidenceError(
                "internal recorded step identity registry drift"
            )
        if require_exact_length:
            length_invalid = len(digests) != len(steps)
        else:
            length_invalid = len(steps) < len(digests)
        if length_invalid:
            raise _evidence.ForwardEconomicEvidenceError(
                "internal recorded step identity registry drift"
            )

        expected_sequence = protocol.start_sequence
        for index, expected_digest in enumerate(digests):
            step = steps[index]
            if type(step) is not _evidence.ForwardEconomicStep:
                raise _evidence.ForwardEconomicEvidenceError(
                    "internal recorded step type integrity drift"
                )
            if step.sequence != expected_sequence:
                raise _evidence.ForwardEconomicEvidenceError(
                    "internal recorded step sequence integrity drift"
                )
            expected_sequence += 1
            try:
                digest = canonical_digest(step_to_payload(step))
            except Exception as exc:
                raise _evidence.ForwardEconomicEvidenceError(
                    "internal recorded step identity payload became invalid"
                ) from exc
            if digest != expected_digest:
                raise _evidence.ForwardEconomicEvidenceError(
                    "internal recorded step identity integrity drift"
                )
        return tuple(steps)

    def _validated_recorded_steps(
        self: _evidence.ForwardEconomicEvidenceAccumulator,
    ) -> tuple[_evidence.ForwardEconomicStep, ...]:
        return _validate_step_prefix(
            self,
            _registry_for(self),
            require_exact_length=True,
        )

    def guarded_init(
        self: _evidence.ForwardEconomicEvidenceAccumulator,
        protocol: _evidence.ForwardEconomicProtocol,
    ) -> None:
        # The private registry must never bootstrap from a serializer that was
        # already replaced before accumulator construction.  Validate the whole
        # canonical JSON dispatch graph before raw_init can compute any protocol
        # or evidence identity, then validate it again before registry creation.
        _require_executable_identity()
        raw_init(self, protocol)
        _require_executable_identity()
        key = id(self)

        def forget(_weakref: object, *, registry_key: int = key) -> None:
            registries.pop(registry_key, None)

        _require_executable_identity()
        registries[key] = (
            ref(self, forget),
            (),
            _evidence_digest(self, ()),
        )

    def guarded_steps(
        self: _evidence.ForwardEconomicEvidenceAccumulator,
    ) -> tuple[_evidence.ForwardEconomicStep, ...]:
        _validated_recorded_steps(self)
        return raw_steps_getter(self)

    def guarded_next_sequence(
        self: _evidence.ForwardEconomicEvidenceAccumulator,
    ) -> int:
        _validated_recorded_steps(self)
        return raw_next_sequence_getter(self)

    def guarded_validate_aggregate(
        self: _evidence.ForwardEconomicEvidenceAccumulator,
    ) -> tuple:
        _validated_recorded_steps(self)
        return raw_validate_aggregate(self)

    def guarded_summary(
        self: _evidence.ForwardEconomicEvidenceAccumulator,
    ) -> _evidence.ForwardEconomicEvidenceSummary:
        _validated_recorded_steps(self)
        expected_evidence_sha256 = _expected_evidence_sha256(self)
        _require_executable_identity()
        summary = raw_summary(self)
        _require_executable_identity()
        _validated_recorded_steps(self)
        if type(summary) is not summary_type:
            raise _evidence.ForwardEconomicEvidenceError(
                "forward economic summary publication type authority drift"
            )
        if summary.evidence_sha256 != expected_evidence_sha256:
            raise _evidence.ForwardEconomicEvidenceError(
                "recorded step evidence publication identity drift"
            )
        return summary

    def guarded_record(
        self: _evidence.ForwardEconomicEvidenceAccumulator,
        observation: _evidence.ForwardDecisionObservation,
        resolver: _evidence.EconomicAuthorityResolver,
    ) -> _evidence.ForwardEconomicStep:
        before_digests = _registry_for(self)
        before_evidence_sha256 = _expected_evidence_sha256(self)
        _validated_recorded_steps(self)
        before_count = len(self._steps)
        published_step = raw_record(self, observation, resolver)
        if len(self._steps) != before_count + 1:
            raise _evidence.ForwardEconomicEvidenceError(
                "recorded step append cardinality integrity drift"
            )

        # Resolver callbacks run after the original aggregate pre-check. Recheck
        # the whole committed prefix before authorizing the newly appended step so
        # a caller resolver cannot mutate older evidence during resolve().
        _validate_step_prefix(
            self,
            before_digests,
            require_exact_length=False,
        )
        stored_step = self._steps[-1]
        if (
            type(stored_step) is not _evidence.ForwardEconomicStep
            or stored_step.sequence != self._validated_protocol().start_sequence + before_count
        ):
            raise _evidence.ForwardEconomicEvidenceError(
                "recorded step append identity drift"
            )
        stored_digest = canonical_digest(step_to_payload(stored_step))
        published_digest = canonical_digest(step_to_payload(published_step))
        if stored_digest != published_digest:
            raise _evidence.ForwardEconomicEvidenceError(
                "recorded step publication identity drift"
            )
        record = registries.get(id(self))
        if (
            record is None
            or record[0]() is not self
            or record[1] != before_digests
            or record[2] != before_evidence_sha256
        ):
            raise _evidence.ForwardEconomicEvidenceError(
                "internal recorded step identity registry changed during append"
            )
        committed_steps = tuple(self._steps)
        expected_evidence_sha256 = _evidence_digest(self, committed_steps)
        registries[id(self)] = (
            record[0],
            (*before_digests, stored_digest),
            expected_evidence_sha256,
        )
        return published_step

    accumulator_type.__init__ = guarded_init
    accumulator_type.steps = property(guarded_steps)
    accumulator_type.next_sequence = property(guarded_next_sequence)
    accumulator_type._validated_aggregate_state = guarded_validate_aggregate
    accumulator_type.record = guarded_record
    accumulator_type.summary = guarded_summary
    accumulator_type._recorded_step_identity_guard_installed = True


_install_forward_economic_step_identity_guard()
del _install_forward_economic_step_identity_guard