from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from autosport.shadow_strategy_memory import ShadowMemoryPlan, ShadowStrategyMemory

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
ACTIONS = frozenset({"A", "B", "WAIT"})


def stamp(hour):
    return (START + timedelta(hours=hour)).isoformat()


def feedback(memory, index, reward, **changes):
    args = dict(event_id=f"match-{index}", decision_at=stamp(index), result_available_at=stamp(index + 1),
                now=stamp(index + 1), net_returns={"A": Decimal(reward), "B": Decimal(-1)},
                source_sha256="a" * 64, availability_basis="recorded_source_timestamp_unverified")
    args.update(changes)
    return memory.observe(**args)


def memory():
    return ShadowStrategyMemory(ShadowMemoryPlan(("A", "B"), window=3, minimum_observations=3))


def test_declined_actions_can_recover_without_placing_training_bets():
    m = memory()
    for i in range(3):
        feedback(m, i, "-1")
    assert m.choose(as_of=stamp(3), admissible_actions=ACTIONS) == "WAIT"
    for i in range(3, 6):
        feedback(m, i, "1")
    assert m.choose(as_of=stamp(6), admissible_actions=ACTIONS) == "A"
    assert m.choose(as_of=stamp(6), admissible_actions=frozenset({"B", "WAIT"})) == "WAIT"
    assert m.payload()["execution_authorized"] is False
    assert m.payload()["promotion_authorized"] is False


def test_future_feedback_and_backdated_decisions_are_rejected_without_mutation():
    m = memory()
    before = m.payload()
    with pytest.raises(ValueError, match="by now"):
        feedback(m, 0, "1", now=stamp(0))
    assert m.payload() == before
    feedback(m, 0, "1")
    with pytest.raises(ValueError, match="future memory"):
        m.choose(as_of=stamp(0), admissible_actions=ACTIONS)


def test_duplicate_retry_never_increases_support_and_conflict_fails():
    m = memory()
    assert feedback(m, 0, "1")
    assert feedback(m, 0, "1", now=stamp(2)) is False
    assert len(m.payload()["records"]) == 1
    with pytest.raises(ValueError, match="conflicting"):
        feedback(m, 0, "2")


def test_recovery_utf8_path_and_stale_writer(tmp_path):
    p = tmp_path / "Пам’ять агентів" / "shadow.json"
    m = memory()
    feedback(m, 0, "1")
    m.save(p)
    left, stale = ShadowStrategyMemory.load(p), ShadowStrategyMemory.load(p)
    feedback(left, 1, "1")
    left.save(p)
    feedback(stale, 2, "2")
    with pytest.raises(ValueError, match="stale"):
        stale.save(p)
    restored = ShadowStrategyMemory.load(p)
    feedback(restored, 2, "1")
    restored.save(p)
    assert restored.choose(as_of=stamp(3), admissible_actions=ACTIONS) == "A"
    assert ShadowStrategyMemory.load(p).payload() == restored.payload()


def test_checkpoint_tamper_is_rejected(tmp_path):
    p = tmp_path / "memory.json"
    m = memory()
    feedback(m, 0, "1")
    m.save(p)
    raw = json.loads(p.read_text())
    raw["payload"]["records"][0]["net_returns"]["A"] = "1000"
    p.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="integrity"):
        ShadowStrategyMemory.load(p)


def test_no_action_expansion_or_forced_risk():
    m = memory()
    with pytest.raises(ValueError, match="admissible"):
        m.choose(as_of=stamp(0), admissible_actions=frozenset({"C", "WAIT"}))
    with pytest.raises(ValueError, match="WAIT"):
        m.choose(as_of=stamp(0), admissible_actions=frozenset({"A"}))


@pytest.mark.parametrize("values", [{"A": Decimal("NaN"), "B": Decimal(0)},
                                    {"A": 1.0, "B": Decimal(0)}, {"A": Decimal(0)}])
def test_invalid_or_partial_feedback_never_updates(values):
    m = memory()
    with pytest.raises(ValueError):
        feedback(m, 0, "1", net_returns=values)
    assert m.payload()["records"] == []
