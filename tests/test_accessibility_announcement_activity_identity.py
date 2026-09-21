import inspect

from autosport.accessibility_announcements import (
    AnnouncementEvent,
    AnnouncementGate,
    AnnouncementKind,
)


def _activity_id(decision) -> str:
    assert decision.emit is True
    assert hasattr(decision, "activity_id"), (
        "an emitted product-issued AnnouncementDecision must carry the "
        "non-localized activity identity required by the UIA notification emitter"
    )
    activity_id = decision.activity_id
    assert isinstance(activity_id, str)
    assert activity_id
    assert activity_id == activity_id.strip()
    assert activity_id.isascii(), "UIA activity identity must be non-localized/stable"
    return activity_id


def test_polite_activity_identity_is_policy_issued_stable_and_text_independent():
    assert "activity_id" not in inspect.signature(AnnouncementEvent).parameters

    first = AnnouncementGate().decide(
        AnnouncementEvent(
            kind=AnnouncementKind.OPERATION_STARTED,
            text="Операцію розпочато",
            state_token="runtime-transition-42",
        )
    )
    reprojection = AnnouncementGate().decide(
        AnnouncementEvent(
            kind=AnnouncementKind.OPERATION_STARTED,
            text="Інший локалізований текст",
            state_token="runtime-transition-42",
        )
    )
    unrelated = AnnouncementGate().decide(
        AnnouncementEvent(
            kind=AnnouncementKind.OPERATION_STARTED,
            text="Операцію розпочато",
            state_token="runtime-transition-43",
        )
    )

    first_id = _activity_id(first)
    assert _activity_id(reprojection) == first_id
    assert _activity_id(unrelated) != first_id
    assert "Операцію" not in first_id


def test_assertive_activity_identity_tracks_episode_not_projection_text_or_state_token():
    first = AnnouncementGate().decide(
        AnnouncementEvent(
            kind=AnnouncementKind.CRITICAL_ERROR,
            text="Критична помилка",
            state_token="projection-a",
            episode_id="critical-episode-7",
        )
    )
    same_episode_new_projection = AnnouncementGate().decide(
        AnnouncementEvent(
            kind=AnnouncementKind.AUTHORITY_BLOCKED,
            text="Дію заблоковано",
            state_token="projection-b",
            episode_id="critical-episode-7",
        )
    )
    unrelated_episode = AnnouncementGate().decide(
        AnnouncementEvent(
            kind=AnnouncementKind.CRITICAL_ERROR,
            text="Критична помилка",
            state_token="projection-a",
            episode_id="critical-episode-8",
        )
    )

    first_id = _activity_id(first)
    assert _activity_id(same_episode_new_projection) == first_id
    assert _activity_id(unrelated_episode) != first_id
    assert "Критична" not in first_id
    assert "Дію" not in first_id


def test_suppressed_decision_never_requests_a_uia_activity():
    suppressed = AnnouncementGate().decide(
        AnnouncementEvent(
            kind=AnnouncementKind.PRICE_TICK,
            text="Ціна оновилась",
            state_token="price-tick-1",
        )
    )

    assert suppressed.emit is False
    assert getattr(suppressed, "activity_id", None) is None
