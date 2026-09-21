import pytest

from autosport.accessibility_announcements import (
    AnnouncementDecision,
    AnnouncementPriority,
)


@pytest.mark.parametrize(
    "priority",
    (AnnouncementPriority.POLITE, AnnouncementPriority.ASSERTIVE),
)
def test_caller_cannot_mint_emit_decision_without_announcement_gate(priority):
    """Only the policy gate may issue an emit=True announcement decision.

    The downstream Windows/UIA emitter must be able to trust that an emitted
    decision already passed the canonical #944 classification and dedupe policy.
    A public direct constructor would let a caller bypass both.
    """

    with pytest.raises(TypeError, match="product-issued|AnnouncementGate"):
        AnnouncementDecision(
            emit=True,
            priority=priority,
            text="Критичне повідомлення",
            reason="EMIT",
            move_focus=False,
        )
