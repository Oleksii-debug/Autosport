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
    raw_steps_property = accumulator_type.steps
    raw_next_sequence_property = accumulator_type.next_sequence
    step_type = _evidence.ForwardEconomicStep
    step_to_payload = step_type.to_payload
    step_to_payload_code = getattr(step_to_payload, "__code__", None)
    canonical_digest = _evidence._canonical_digest
    canonical_digest_code = getattr(canonical_digest, "__code__", None)

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
        tuple[ReferenceType[_evidence.ForwardEconomicEvidenceAccumulator], tuple[str, ...]],
    ] = {}

    def _require_executable_identity() -> None:
        if (
            _evidence.ForwardEconomicStep is not step_type
            or getattr(step_type, "to_payload", None) is not step_to_payload
            or getattr(step_to_payload, "__code__", None) is not step_to_payload_code
            or getattr(_evidence, "_canonical_digest", None) is not canonical_digest
            or getattr(canonical_digest, "__code__", None) is not canonical_digest_code
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
        raw_init(self, protocol)
        key = id(self)

        def forget(_weakref: object, *, registry_key: int = key) -> None:
            registries.pop(registry_key, None)

        registries[key] = (ref(self, forget), ())

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

    def guarded_record(
        self: _evidence.ForwardEconomicEvidenceAccumulator,
        observation: _evidence.ForwardDecisionObservation,
        resolver: _evidence.EconomicAuthorityResolver,
    ) -> _evidence.ForwardEconomicStep:
        before_digests = _registry_for(self)
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
        if record is None or record[0]() is not self or record[1] != before_digests:
            raise _evidence.ForwardEconomicEvidenceError(
                "internal recorded step identity registry changed during append"
            )
        registries[id(self)] = (record[0], (*before_digests, stored_digest))
        return published_step

    accumulator_type.__init__ = guarded_init
    accumulator_type.steps = property(guarded_steps)
    accumulator_type.next_sequence = property(guarded_next_sequence)
    accumulator_type._validated_aggregate_state = guarded_validate_aggregate
    accumulator_type.record = guarded_record
    accumulator_type._recorded_step_identity_guard_installed = True


_install_forward_economic_step_identity_guard()
del _install_forward_economic_step_identity_guard
