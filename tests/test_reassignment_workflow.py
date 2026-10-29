from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

from saas_sentry.engine import InputError
from saas_sentry.reassignment_workflow import list_states, stage_plan, transition


class ReassignmentWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plan = self.root / "plan.csv"
        self.plan.write_text(
            "finding_id,application,contract_id,from_email,to_email,status\n"
            "f-1,Slack,c-1,former@example.com,new@example.com,proposed\n",
            encoding="utf-8",
        )
        self.ledger = self.root / "events.csv"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_transfer_requires_independent_approval_and_completion(self) -> None:
        self.assertEqual(stage_plan(self.plan, self.ledger, actor="finops", on=date(2026, 10, 1)), 1)
        with self.assertRaises(InputError):
            transition(self.ledger, "f-1", "approved", actor="finops", evidence_ref="approval/1")
        transition(self.ledger, "f-1", "approved", actor="owner", evidence_ref="approval/1")
        transition(self.ledger, "f-1", "completed", actor="it-operator", evidence_ref="ticket/22")
        current = list_states(self.ledger)
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["status"], "completed")
        self.assertEqual(current[0]["to_email"], "new@example.com")

    def test_transfer_cannot_complete_before_approval(self) -> None:
        stage_plan(self.plan, self.ledger, actor="finops")
        with self.assertRaises(InputError):
            transition(self.ledger, "f-1", "completed", actor="it-operator", evidence_ref="ticket/22")


if __name__ == "__main__":
    unittest.main()
