
    @property
    def stopped(self) -> bool:
        return self._control.state is LiveControlState.STOPPED

    def register_input(
        self,
        input_id: str,
        *,
        source_ids: str | tuple[str, ...] | None = None,
        sports: str | tuple[str, ...] | None = None,
        event_ids: str | tuple[str, ...] | None = None,
        market_ids: str | tuple[str, ...] | None = None,
        selection_ids: str | tuple[str, ...] | None = None,
    ) -> None:
        normalized_id = FocusedMirrorDependencyIndex._input_id(input_id)
        candidate = _InputSpec(
            input_id=normalized_id,
            source_ids=_selector_tuple(source_ids, name="source_ids"),
            sports=_selector_tuple(sports, name="sports"),
            event_ids=_selector_tuple(event_ids, name="event_ids"),
            market_ids=_selector_tuple(market_ids, name="market_ids"),
            selection_ids=_selector_tuple(selection_ids, name="selection_ids"),
        )
        existing = self._input_specs.get(normalized_id)
        if existing is not None:
            if existing != candidate:
                raise ValueError(
                    f"input_id {normalized_id!r} conflicts with durable registration"
                )
            return
        if len(self._input_specs) >= self.bounds.max_registered_inputs:
            raise ValueError("max_registered_inputs would be exceeded")

        previous_specs = tuple(self._input_specs.values())
        dependency = self.dependencies.register(
            candidate.input_id,
            source_ids=candidate.source_ids,
            sports=candidate.sports,
            event_ids=candidate.event_ids,
            market_ids=candidate.market_ids,
            selection_ids=candidate.selection_ids,
        )
        self._input_specs[candidate.input_id] = candidate
        try:
            self._persist_input_registry(expected_previous=previous_specs)
        except BaseException:
            self._input_specs.pop(candidate.input_id, None)
            self.dependencies.unregister(candidate.input_id)
            raise
        self._pending_affected[dependency.input_id] = None
        self._needs_cache_rebuild = True

    def unregister_input(self, input_id: str) -> bool:
        normalized_id = FocusedMirrorDependencyIndex._input_id(input_id)
        existing = self._input_specs.get(normalized_id)
        if existing is None:
            return False
        previous_specs = tuple(self._input_specs.values())
        if not self.dependencies.unregister(normalized_id):
            raise LiveDecisionProgressError("live dependency registry is inconsistent during retirement")
        self._input_specs.pop(normalized_id)
        try:
            self._persist_input_registry(expected_previous=previous_specs)
        except BaseException:
            self._input_specs[normalized_id] = existing
            self.dependencies.register(
                existing.input_id,
                source_ids=existing.source_ids,
                sports=existing.sports,
                event_ids=existing.event_ids,
                market_ids=existing.market_ids,
                selection_ids=existing.selection_ids,
            )
            raise
        self._pending_affected.pop(normalized_id, None)
        self._intent_cache.pop(normalized_id, None)
        self._input_market_sha256.pop(normalized_id, None)
        return True

    def pause(self) -> None:
        if self.stopped:
            raise RuntimeError("cannot pause a stopped live loop")
        self._persist_control(LiveControlState.PAUSED)

    def resume(self) -> None:
        if self.stopped:
            raise RuntimeError("cannot resume a stopped live loop")
        self._persist_control(LiveControlState.RUNNING)
