import ast
from pathlib import Path
import unittest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_COLLECTOR_SERVICE = _REPO_ROOT / "src" / "autosport" / "collector_service.py"


def _attr_chain(node: ast.AST) -> tuple[str, ...] | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return tuple(reversed(parts))
    return None


def _is_self_call(node: ast.AST, method: str) -> bool:
    return isinstance(node, ast.Call) and _attr_chain(node.func) == ("self", method)


def _is_poll_interval_sleep(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and _attr_chain(node.func) == ("self", "_sleep")
        and len(node.args) == 1
        and _attr_chain(node.args[0])
        == ("self", "_config", "poll_interval_seconds")
    )


def _statement_contains_call(statement: ast.stmt, predicate) -> bool:
    return any(
        isinstance(node, ast.Call) and predicate(node)
        for node in ast.walk(statement)
    )


def _loop_has_completion_relative_poll_delay(loop: ast.stmt) -> bool:
    body = getattr(loop, "body", ())
    cycle_positions = [
        index
        for index, statement in enumerate(body)
        if _statement_contains_call(
            statement, lambda call: _is_self_call(call, "run_cycle")
        )
    ]
    sleep_positions = [
        index
        for index, statement in enumerate(body)
        if _statement_contains_call(statement, _is_poll_interval_sleep)
    ]
    return any(
        cycle_index < sleep_index
        for cycle_index in cycle_positions
        for sleep_index in sleep_positions
    )


class CollectorProspectiveScheduleGapTests(unittest.TestCase):
    def test_run_does_not_model_prospective_schedule_as_post_cycle_delay(self):
        tree = ast.parse(_COLLECTOR_SERVICE.read_text(encoding="utf-8"))

        service = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef)
                and node.name == "HeadlessCollectorService"
            ),
            None,
        )
        self.assertIsNotNone(
            service,
            "canonical HeadlessCollectorService disappeared; requalify #1250",
        )

        run_method = next(
            (
                node
                for node in service.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "run"
            ),
            None,
        )
        self.assertIsNotNone(
            run_method,
            "canonical HeadlessCollectorService.run disappeared; requalify #1250",
        )

        offending_loops = [
            node
            for node in ast.walk(run_method)
            if isinstance(node, (ast.For, ast.AsyncFor, ast.While))
            and _loop_has_completion_relative_poll_delay(node)
        ]

        self.assertFalse(
            offending_loops,
            (
                "HeadlessCollectorService.run still executes run_cycle() and then "
                "sleeps exactly poll_interval_seconds in the same loop. That is a "
                "completion-relative delay, not a prospectively frozen acquisition "
                "schedule: a long cycle or process downtime shifts later starts and "
                "can erase missed due slots from the scientific denominator. #1250 "
                "requires the canonical #1180 lineage to own durable prospective "
                "schedule/slot identity and bind each due slot to canonical START "
                "evidence; do not satisfy this test by adding a second scheduler or "
                "caller-authored due-time list."
            ),
        )


if __name__ == "__main__":
    unittest.main()
