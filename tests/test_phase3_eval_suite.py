"""Unit test wrapper that runs all 22 evaluation scenarios as part of `unittest discover`."""
from __future__ import annotations

import unittest
from eval.run_eval import run_all_evals


class Phase3EvalSuiteTest(unittest.TestCase):
    def test_all_22_automated_eval_scenarios_pass(self) -> None:
        results = run_all_evals(write_markdown=True)
        self.assertGreaterEqual(len(results), 20)
        failed = [r for r in results if not r.passed]
        self.assertEqual(
            failed,
            [],
            f"Some eval scenarios failed: {[(r.scenario_id, r.name, r.actual) for r in failed]}",
        )


if __name__ == "__main__":
    unittest.main()
