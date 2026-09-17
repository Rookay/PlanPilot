from unittest import TestCase

from app.services import evaluation_service
from tests.helpers import make_session


class EvaluationTests(TestCase):
    def setUp(self):
        self.db = make_session()

    def tearDown(self):
        self.db.close()

    def test_deterministic_agent_evaluation_reports_all_metrics(self):
        result = evaluation_service.run_evaluation(self.db)

        self.assertEqual(result["scenario_count"], 5)
        self.assertEqual(result["passed_count"], 5)
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["metrics"]["tool_selection_accuracy"], 100.0)
        self.assertEqual(result["metrics"]["constraint_pass_rate"], 100.0)
        self.assertEqual(
            evaluation_service.latest_run(self.db)["id"],
            result["id"],
        )
