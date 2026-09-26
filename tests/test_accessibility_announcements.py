import inspect

import pytest

import autosport.accessibility_announcements as announcements

from autosport.accessibility_announcements import (
    AnnouncementDecision,
    AnnouncementEvent,
    AnnouncementGate,
    AnnouncementKind,
    AnnouncementPriority,
    priority_for_kind,
)


SILENT_KINDS = {
    AnnouncementKind.PRICE_TICK,
    AnnouncementKind.MARKET_REFRESH,
    AnnouncementKind.PROGRESS_TICK,
}
POLITE_KINDS = {
    AnnouncementKind.OPERATION_STARTED,
    AnnouncementKind.OPERATION_COMPLETED,
    AnnouncementKind.STOP_REQUESTED,
    AnnouncementKind.STOP_COMPLETED,
}
ASSERTIVE_KINDS = {
    AnnouncementKind.RECOVERY_REQUIRED,
    AnnouncementKind.CRITICAL_ERROR,
    AnnouncementKind.AUTHORITY_BLOCKED,
}


def _valid_activity_id(priority: AnnouncementPriority) -> str:
    return (
        f"autosport:announcement:v1:{priority.value.lower()}:sha256:"
        + ("0" * 64)
    )


def _decision_for_invariant_test(**values):
    """Bypass the public constructor only to exercise internal invariants."""

    decision = object.__new__(AnnouncementDecision)
    defaults = dict(
        emit=True,
        priority=AnnouncementPriority.POLITE,
        text="Оновлений стан",
        reason="EMIT",
        activity_id=_valid_activity_id(AnnouncementPriority.POLITE),
        move_focus=False,
    )
    defaults.update(values)
    if "activity_id" not in values:
        if defaults["emit"] is False:
            defaults["activity_id"] = None
        elif isinstance(defaults["priority"], AnnouncementPriority) and (
            defaults["priority"] is not AnnouncementPriority.SILENT
        ):
            defaults["activity_id"] = _valid_activity_id(defaults["priority"])
    for name, value in defaults.items():
        object.__setattr__(decision, name, value)
    decision.__post_init__()
    return decision


def _event(
    kind: AnnouncementKind,
    *,
    token: str = "state-1",
    text: str = "Оновлений стан",
    episode: str | None = None,
) -> AnnouncementEvent:
    return AnnouncementEvent(
        kind=kind,
        text=text,
        state_token=token,
        episode_id=episode,
    )


def test_every_kind_has_exact_product_owned_priority():
    assert set(AnnouncementKind) == SILENT_KINDS | POLITE_KINDS | ASSERTIVE_KINDS
    assert not (SILENT_KINDS & POLITE_KINDS)
    assert not (SILENT_KINDS & ASSERTIVE_KINDS)
    assert not (POLITE_KINDS & ASSERTIVE_KINDS)

    for kind in SILENT_KINDS:
        assert priority_for_kind(kind) is AnnouncementPriority.SILENT
    for kind in POLITE_KINDS:
        assert priority_for_kind(kind) is AnnouncementPriority.POLITE
    for kind in ASSERTIVE_KINDS:
        assert priority_for_kind(kind) is AnnouncementPriority.ASSERTIVE


@pytest.mark.parametrize("kind", sorted(SILENT_KINDS, key=lambda item: item.value))
def test_high_frequency_churn_is_always_silent(kind):
    gate = AnnouncementGate()

    first = gate.decide(_event(kind, token="tick-1"))
    second = gate.decide(_event(kind, token="tick-2", text="Ще один tick"))

    for decision in (first, second):
        assert decision.emit is False
        assert decision.priority is AnnouncementPriority.SILENT
        assert decision.text is None
        assert decision.reason == "HIGH_FREQUENCY_CHURN"
        assert decision.activity_id is None
        assert decision.move_focus is False
    assert gate.history_size == 0


@pytest.mark.parametrize("kind", sorted(POLITE_KINDS, key=lambda item: item.value))
def test_polite_transition_emits_once_per_state_token(kind):
    gate = AnnouncementGate()

    first = gate.decide(_event(kind, token="transition-1"))
    duplicate = gate.decide(_event(kind, token="transition-1", text="Changed projection text"))
    next_transition = gate.decide(_event(kind, token="transition-2"))

    assert first.emit is True
    assert first.priority is AnnouncementPriority.POLITE
    assert first.text == "Оновлений стан"
    assert first.reason == "EMIT"
    assert first.activity_id is not None
    assert first.activity_id.isascii()
    assert first.move_focus is False
    assert duplicate.emit is False
    assert duplicate.priority is AnnouncementPriority.SILENT
    assert duplicate.text is None
    assert duplicate.reason == "DUPLICATE_STATE_TRANSITION"
    assert duplicate.activity_id is None
    assert next_transition.emit is True
    assert next_transition.priority is AnnouncementPriority.POLITE
    assert next_transition.activity_id != first.activity_id


@pytest.mark.parametrize("kind", sorted(ASSERTIVE_KINDS, key=lambda item: item.value))
def test_assertive_event_emits_once_per_critical_episode(kind):
    gate = AnnouncementGate()

    first = gate.decide(_event(kind, token="projection-1", episode="episode-a"))
    same_episode_new_projection = gate.decide(
        _event(kind, token="projection-2", episode="episode-a", text="Detailed update")
    )
    new_episode = gate.decide(_event(kind, token="projection-1", episode="episode-b"))

    assert first.emit is True
    assert first.priority is AnnouncementPriority.ASSERTIVE
    assert first.activity_id is not None
    assert first.activity_id.isascii()
    assert same_episode_new_projection.emit is False
    assert same_episode_new_projection.priority is AnnouncementPriority.SILENT
    assert same_episode_new_projection.reason == "DUPLICATE_CRITICAL_EPISODE"
    assert same_episode_new_projection.activity_id is None
    assert new_episode.emit is True
    assert new_episode.priority is AnnouncementPriority.ASSERTIVE
    assert new_episode.activity_id != first.activity_id


def test_caller_cannot_supply_or_escalate_priority():
    parameters = inspect.signature(AnnouncementEvent).parameters
    assert "priority" not in parameters
    assert "urgency" not in parameters
    assert "activity_id" not in parameters

    gate = AnnouncementGate()
    churn = gate.decide(_event(AnnouncementKind.PRICE_TICK, token="attacker-minted-token"))
    assert churn.emit is False
    assert churn.priority is AnnouncementPriority.SILENT


@pytest.mark.parametrize(
    "priority",
    (AnnouncementPriority.POLITE, AnnouncementPriority.ASSERTIVE),
)
def test_caller_cannot_construct_emit_decision_without_gate(priority):
    with pytest.raises(TypeError, match="product-issued"):
        AnnouncementDecision(
            emit=True,
            priority=priority,
            text="Критичне повідомлення",
            reason="EMIT",
            move_focus=False,
        )


def test_announcement_policy_never_moves_focus():
    gate = AnnouncementGate()
    decisions = [
        gate.decide(_event(AnnouncementKind.PRICE_TICK)),
        gate.decide(_event(AnnouncementKind.STOP_REQUESTED)),
        gate.decide(_event(AnnouncementKind.RECOVERY_REQUIRED, episode="recovery-1")),
    ]
    assert all(decision.move_focus is False for decision in decisions)

    with pytest.raises(ValueError, match="must never request focus movement"):
        _decision_for_invariant_test(
            priority=AnnouncementPriority.ASSERTIVE,
            text="Критична помилка",
            move_focus=True,
        )


def test_history_is_strictly_bounded_and_eviction_is_deterministic():
    gate = AnnouncementGate(max_history=3)

    for index in range(10):
        decision = gate.decide(
            _event(
                AnnouncementKind.OPERATION_COMPLETED,
                token=f"transition-{index}",
                text=f"Операцію {index} завершено",
            )
        )
        assert decision.emit is True
        assert gate.history_size <= 3

    assert gate.history_size == 3

    replay = gate.decide(
        _event(
            AnnouncementKind.OPERATION_COMPLETED,
            token="transition-0",
            text="Операцію 0 завершено",
        )
    )
    assert replay.emit is True
    assert gate.history_size == 3


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "4", None])
def test_invalid_history_bound_fails_closed(value):
    with pytest.raises(ValueError, match="max_history must be a positive integer"):
        AnnouncementGate(max_history=value)


@pytest.mark.parametrize("field,value", [("text", ""), ("text", " x "), ("state_token", ""), ("state_token", " x ")])
def test_event_text_and_state_token_must_be_nonempty_trimmed(field, value):
    kwargs = {"kind": AnnouncementKind.STOP_COMPLETED, "text": "Готово", "state_token": "done-1"}
    kwargs[field] = value
    with pytest.raises(ValueError, match=field):
        AnnouncementEvent(**kwargs)


def test_assertive_episode_identity_is_required_and_trimmed():
    for episode in (None, "", " bad "):
        with pytest.raises(ValueError, match="episode_id"):
            _event(AnnouncementKind.CRITICAL_ERROR, episode=episode)

    with pytest.raises(ValueError, match="valid only for assertive"):
        _event(AnnouncementKind.OPERATION_STARTED, episode="unexpected")


def test_runtime_types_fail_closed():
    with pytest.raises(TypeError, match="kind must be AnnouncementKind"):
        AnnouncementEvent(kind="PRICE_TICK", text="x", state_token="y")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="event must be AnnouncementEvent"):
        AnnouncementGate().decide(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="kind must be AnnouncementKind"):
        priority_for_kind("STOP_COMPLETED")  # type: ignore[arg-type]


def test_suppressed_decision_cannot_accidentally_carry_announceable_payload():
    with pytest.raises(ValueError, match="must be SILENT"):
        _decision_for_invariant_test(
            emit=False,
            priority=AnnouncementPriority.POLITE,
            text=None,
            reason="DUPLICATE_STATE_TRANSITION",
        )
    with pytest.raises(ValueError, match="must not carry announcement text"):
        _decision_for_invariant_test(
            emit=False,
            priority=AnnouncementPriority.SILENT,
            text="Do not announce",
            reason="HIGH_FREQUENCY_CHURN",
        )


def test_emitted_decision_cannot_be_silent():
    with pytest.raises(ValueError, match="cannot be SILENT"):
        _decision_for_invariant_test(
            emit=True,
            priority=AnnouncementPriority.SILENT,
            text="Impossible",
            reason="EMIT",
        )


@pytest.mark.parametrize(
    "kwargs,message",
    [
        (
            dict(
                emit=1,
                priority=AnnouncementPriority.POLITE,
                text="x",
                reason="EMIT",
            ),
            "emit must be bool",
        ),
        (
            dict(
                emit=True,
                priority="POLITE",
                text="x",
                reason="EMIT",
            ),
            "priority must be AnnouncementPriority",
        ),
        (
            dict(
                emit=True,
                priority=AnnouncementPriority.POLITE,
                text="x",
                reason="EMIT",
                move_focus=1,
            ),
            "move_focus must be bool",
        ),
    ],
)
def test_malformed_decision_runtime_types_fail_closed(kwargs, message):
    with pytest.raises(TypeError, match=message):
        _decision_for_invariant_test(**kwargs)


def test_cross_kind_assertive_events_share_episode_dedupe_identity():
    gate = AnnouncementGate()

    first = gate.decide(
        _event(
            AnnouncementKind.AUTHORITY_BLOCKED,
            token="authority-projection",
            episode="critical-episode-1",
            text="Дію заблоковано",
        )
    )
    same_episode_different_kind = gate.decide(
        _event(
            AnnouncementKind.CRITICAL_ERROR,
            token="error-projection",
            episode="critical-episode-1",
            text="Критична помилка",
        )
    )

    assert first.emit is True
    assert first.priority is AnnouncementPriority.ASSERTIVE
    assert same_episode_different_kind.emit is False
    assert same_episode_different_kind.priority is AnnouncementPriority.SILENT
    assert same_episode_different_kind.reason == "DUPLICATE_CRITICAL_EPISODE"


def test_cross_kind_polite_events_share_state_transition_dedupe_identity():
    gate = AnnouncementGate()

    first = gate.decide(
        _event(
            AnnouncementKind.OPERATION_STARTED,
            token="operation-transition-1",
            text="Операцію розпочато",
        )
    )
    same_transition_different_kind = gate.decide(
        _event(
            AnnouncementKind.STOP_REQUESTED,
            token="operation-transition-1",
            text="Запитано зупинку",
        )
    )

    assert first.emit is True
    assert first.priority is AnnouncementPriority.POLITE
    assert same_transition_different_kind.emit is False
    assert same_transition_different_kind.priority is AnnouncementPriority.SILENT
    assert same_transition_different_kind.reason == "DUPLICATE_STATE_TRANSITION"


def test_polite_activity_identity_is_stable_nonlocalized_and_text_independent():
    first = AnnouncementGate().decide(
        _event(
            AnnouncementKind.OPERATION_STARTED,
            token="перехід-42",
            text="Операцію розпочато",
        )
    )
    reprojection = AnnouncementGate().decide(
        _event(
            AnnouncementKind.STOP_REQUESTED,
            token="перехід-42",
            text="Інший локалізований текст",
        )
    )
    unrelated = AnnouncementGate().decide(
        _event(
            AnnouncementKind.OPERATION_STARTED,
            token="перехід-43",
            text="Операцію розпочато",
        )
    )

    assert first.emit is True
    assert reprojection.emit is True
    assert unrelated.emit is True
    assert first.activity_id == reprojection.activity_id
    assert unrelated.activity_id != first.activity_id
    assert first.activity_id is not None
    assert first.activity_id.isascii()
    assert "перехід" not in first.activity_id
    assert "Операцію" not in first.activity_id


def test_assertive_activity_identity_follows_episode_across_projection_kind_and_text():
    first = AnnouncementGate().decide(
        _event(
            AnnouncementKind.CRITICAL_ERROR,
            token="projection-a",
            episode="критичний-епізод-7",
            text="Критична помилка",
        )
    )
    reprojection = AnnouncementGate().decide(
        _event(
            AnnouncementKind.AUTHORITY_BLOCKED,
            token="projection-b",
            episode="критичний-епізод-7",
            text="Дію заблоковано",
        )
    )
    unrelated = AnnouncementGate().decide(
        _event(
            AnnouncementKind.CRITICAL_ERROR,
            token="projection-a",
            episode="критичний-епізод-8",
            text="Критична помилка",
        )
    )

    assert first.emit is True
    assert reprojection.emit is True
    assert unrelated.emit is True
    assert first.activity_id == reprojection.activity_id
    assert unrelated.activity_id != first.activity_id
    assert first.activity_id is not None
    assert first.activity_id.isascii()
    assert "епізод" not in first.activity_id
    assert "Критична" not in first.activity_id


def test_activity_identity_namespace_must_match_policy_priority():
    with pytest.raises(ValueError, match="invalid product-issued format"):
        _decision_for_invariant_test(
            priority=AnnouncementPriority.ASSERTIVE,
            activity_id=_valid_activity_id(AnnouncementPriority.POLITE),
        )


@pytest.mark.parametrize(
    "activity_id",
    [
        None,
        "",
        " autosport:announcement:v1:polite:sha256:" + ("0" * 64),
        "autosport:announcement:v1:polite:sha256:" + ("g" * 64),
        "autosport:announcement:v1:polite:sha256:" + ("0" * 63),
        "локалізований-id",
    ],
)
def test_emitted_activity_identity_must_be_valid_product_format(activity_id):
    with pytest.raises(ValueError, match="activity_id"):
        _decision_for_invariant_test(activity_id=activity_id)


def test_suppressed_decision_cannot_carry_activity_identity():
    with pytest.raises(ValueError, match="must not carry activity_id"):
        _decision_for_invariant_test(
            emit=False,
            priority=AnnouncementPriority.SILENT,
            text=None,
            reason="DUPLICATE_STATE_TRANSITION",
            activity_id=_valid_activity_id(AnnouncementPriority.POLITE),
        )


class _HostileHistoryInt(int):
    def __le__(self, other):
        raise AssertionError("hostile int comparison must not run")


class _HostileString(str):
    def strip(self, *args, **kwargs):
        raise AssertionError("hostile string strip must not run")

    def encode(self, *args, **kwargs):
        raise AssertionError("hostile string encode must not run")


class _HostileEventSubclass(AnnouncementEvent):
    def __getattribute__(self, name):
        if name in {"kind", "text", "state_token", "episode_id"}:
            raise AssertionError("subclass event fields must not be read")
        return super().__getattribute__(name)


def test_history_bound_requires_exact_builtin_int_before_comparison():
    with pytest.raises(ValueError, match="max_history must be a positive integer"):
        AnnouncementGate(max_history=_HostileHistoryInt(-1))


@pytest.mark.parametrize("field", ["text", "state_token"])
def test_event_strings_require_exact_builtin_str_before_dispatch(field):
    kwargs = {
        "kind": AnnouncementKind.OPERATION_STARTED,
        "text": "Операцію розпочато",
        "state_token": "transition-1",
    }
    kwargs[field] = _HostileString(kwargs[field])
    with pytest.raises(ValueError, match=field):
        AnnouncementEvent(**kwargs)


def test_assertive_episode_requires_exact_builtin_str_before_dispatch():
    with pytest.raises(ValueError, match="episode_id"):
        AnnouncementEvent(
            kind=AnnouncementKind.CRITICAL_ERROR,
            text="Критична помилка",
            state_token="projection-1",
            episode_id=_HostileString("episode-1"),
        )


def test_decide_rejects_event_subclass_before_reading_polymorphic_fields():
    hostile = object.__new__(_HostileEventSubclass)
    with pytest.raises(TypeError, match="event must be AnnouncementEvent"):
        AnnouncementGate().decide(hostile)


def test_gate_revalidates_exact_event_fields_after_construction():
    event = _event(AnnouncementKind.OPERATION_STARTED, token="transition-1")
    object.__setattr__(event, "state_token", _HostileString("transition-1"))
    with pytest.raises(ValueError, match="event.state_token"):
        AnnouncementGate().decide(event)


def test_generic_decision_mint_helper_is_not_exposed_through_module_or_decide_metadata():
    assert not hasattr(announcements, "_issue_announcement_decision")
    assert AnnouncementGate.decide.__closure__ is None
    assert AnnouncementGate.decide.__defaults__ is None


def test_exact_gate_issued_decision_is_one_shot_emitter_authority():
    gate = AnnouncementGate()
    decision = gate.decide(
        _event(
            AnnouncementKind.CRITICAL_ERROR,
            token="projection-1",
            episode="episode-1",
            text="Критична помилка",
        )
    )

    assert gate.consume_for_emission(decision) is decision
    with pytest.raises(ValueError, match="not issued for emission"):
        gate.consume_for_emission(decision)


def test_same_shaped_forged_decision_is_not_emitter_eligible():
    gate = AnnouncementGate()
    issued = gate.decide(
        _event(
            AnnouncementKind.STOP_COMPLETED,
            token="stop-1",
            text="Зупинено",
        )
    )
    forged = _decision_for_invariant_test(
        emit=issued.emit,
        priority=issued.priority,
        text=issued.text,
        reason=issued.reason,
        activity_id=issued.activity_id,
        move_focus=issued.move_focus,
    )

    assert type(forged) is AnnouncementDecision
    assert forged == issued
    assert forged is not issued
    with pytest.raises(ValueError, match="not issued for emission"):
        gate.consume_for_emission(forged)
    assert gate.consume_for_emission(issued) is issued


def test_decision_from_another_gate_is_not_emitter_eligible():
    first_gate = AnnouncementGate()
    second_gate = AnnouncementGate()
    decision = first_gate.decide(
        _event(
            AnnouncementKind.STOP_COMPLETED,
            token="stop-1",
            text="Зупинено",
        )
    )

    with pytest.raises(ValueError, match="not issued for emission"):
        second_gate.consume_for_emission(decision)
    assert first_gate.consume_for_emission(decision) is decision


def test_suppressed_decision_never_receives_emission_authority():
    gate = AnnouncementGate()
    decision = gate.decide(_event(AnnouncementKind.PRICE_TICK, token="tick-1"))
    assert decision.emit is False
    with pytest.raises(ValueError, match="not issued for emission"):
        gate.consume_for_emission(decision)


def test_emission_authority_registry_is_bounded_by_history_limit():
    gate = AnnouncementGate(max_history=2)
    first = gate.decide(_event(AnnouncementKind.STOP_COMPLETED, token="stop-1"))
    second = gate.decide(_event(AnnouncementKind.STOP_COMPLETED, token="stop-2"))
    third = gate.decide(_event(AnnouncementKind.STOP_COMPLETED, token="stop-3"))

    with pytest.raises(ValueError, match="not issued for emission"):
        gate.consume_for_emission(first)
    assert gate.consume_for_emission(second) is second
    assert gate.consume_for_emission(third) is third


def test_gate_internal_state_rejects_ordinary_rebinding():
    gate = AnnouncementGate()
    with pytest.raises(AttributeError, match="product-owned and immutable"):
        gate._max_history = 999  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match="product-owned and immutable"):
        gate._history = ()  # type: ignore[attr-defined]
