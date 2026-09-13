import contextlib
import io
import unittest

from autosport.cli import build_parser, run_strategies
from autosport.strategies import available_strategies, build_strategy_agents, strategy_spec


class StrategyRuntimeTests(unittest.TestCase):
    def test_registry_identity_matches_fresh_runtime_agents(self):
        for spec in available_strategies():
            agents = build_strategy_agents(spec.strategy_id)
            self.assertEqual(tuple(agent.name for agent in agents), spec.agent_names)
            self.assertEqual(strategy_spec(spec.strategy_id), spec)

    def test_dataset_cli_rejects_noncanonical_strategy(self):
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(["dataset", "examples/tt_demo", "--strategy", "invented-v1"])
        self.assertEqual(raised.exception.code, 2)

    def test_strategy_listing_exposes_runtime_agents_and_truth(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_strategies(), 0)
        text = output.getvalue()
        self.assertIn("baseline-v1 | agents=market-mirror,paper-baseline", text)
        self.assertIn("observe-only-v1 | agents=market-mirror", text)
        self.assertIn("opens_paper_tickets=false", text)


if __name__ == "__main__":
    unittest.main()
