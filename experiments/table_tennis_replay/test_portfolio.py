import unittest
from decimal import Decimal as D
from research_portfolio import basket

class CoverageTests(unittest.TestCase):
    def test_four_outcomes_and_budget_reconcile(self):
        r=basket([[D('1.5'),D('2.5')],[D('1.8'),D('1.9')]],D(100))
        self.assertEqual(len(r['scenario_net']),4)
        self.assertAlmostEqual(sum(D(s) for s in r['stakes']),D(100),places=20)
        nets=[D(n) for n in r['scenario_net']]
        self.assertLess(max(nets),0)
        self.assertLess(max(nets)-min(nets),D('1e-20'))
    def test_fair_and_arbitrage_inputs(self):
        self.assertEqual(D(basket([[D(2),D(2)],[D(2),D(2)]])['best_worst_case_net']),0)
        self.assertGreater(D(basket([[D('2.1'),D('2.1')],[D('2.1'),D('2.1')]])['best_worst_case_net']),0)
        with self.assertRaises(ValueError):basket([[D(1),D(2)],[D(2),D(2)]])

if __name__=='__main__':unittest.main()
