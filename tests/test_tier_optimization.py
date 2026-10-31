from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from saas_sentry.engine import InputError
from saas_sentry.tier_optimization import (
    list_tier_actions, recommend_downgrades, stage_recommendations, transition_tier_action,
)


class TierOptimizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.usage = self.root / "usage.csv"
        self.billing = self.root / "billing.csv"
        self.tiers = self.root / "tiers.csv"
        self.usage.write_text(
            "employee,email,application,features_used\nAlice,alice@example.com,Zoom,2\n",
            encoding="utf-8",
        )
        self.billing.write_text(
            "email,application,license_status,annual_cost,license_tier,provider,provider_user_id\n"
            "alice@example.com,Zoom,active,12000,2,zoom,user-1\n", encoding="utf-8",
        )
        self.tiers.write_text(
            "application,tier,feature_capacity,annual_cost_inr,provider_tier_id\n"
            "Zoom,1,5,6000,1\nZoom,2,20,12000,2\n", encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_low_usage_recommends_cheapest_compatible_tier_and_estimates_savings(self) -> None:
        rows = recommend_downgrades(self.usage, self.billing, self.tiers)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].current_tier, rows[0].recommended_tier), ("2", "1"))
        self.assertEqual((rows[0].current_provider_tier_id, rows[0].recommended_provider_tier_id), ("2", "1"))
        self.assertEqual(str(rows[0].annual_savings_inr), "6000.00")

    def test_does_not_recommend_a_tier_that_is_not_lower_than_the_current_tier(self) -> None:
        self.billing.write_text(
            "email,application,license_status,annual_cost,license_tier,provider,provider_user_id\n"
            "alice@example.com,Zoom,active,6000,1,zoom,user-1\n", encoding="utf-8",
        )
        self.assertEqual(recommend_downgrades(self.usage, self.billing, self.tiers), ())

    def test_missing_usage_telemetry_does_not_generate_a_downgrade(self) -> None:
        self.usage.write_text(
            "employee,email,application,features_used\nAlice,alice@example.com,Zoom,\n", encoding="utf-8",
        )
        self.assertEqual(recommend_downgrades(self.usage, self.billing, self.tiers), ())

    def test_approval_is_required_and_provider_execution_is_idempotent(self) -> None:
        rows = recommend_downgrades(self.usage, self.billing, self.tiers)
        ledger = self.root / "tier-actions.csv"
        self.assertEqual(stage_recommendations(rows, ledger, actor="analyst"), 1)
        action_id = rows[0].action_id
        calls = []
        with self.assertRaisesRegex(InputError, "approved"):
            transition_tier_action(ledger, action_id, "execute", actor="operator",
                                   operation=lambda state: calls.append(state))
        with self.assertRaisesRegex(InputError, "different person"):
            transition_tier_action(ledger, action_id, "approved", actor="analyst")
        transition_tier_action(ledger, action_id, "approved", actor="manager")
        state = transition_tier_action(ledger, action_id, "execute", actor="operator",
                                       operation=lambda _state: calls.append("zoom") or "license_changed_to_basic")
        self.assertEqual(state["status"], "executed")
        self.assertEqual(calls, ["zoom"])
        with self.assertRaisesRegex(InputError, "approved"):
            transition_tier_action(ledger, action_id, "execute", actor="operator",
                                   operation=lambda _state: calls.append("duplicate"))
        self.assertEqual(len(list_tier_actions(ledger)), 1)

    def test_execution_is_limited_to_known_supported_zoom_tier_mapping(self) -> None:
        rows = recommend_downgrades(self.usage, self.billing, self.tiers)
        ledger = self.root / "tier-actions.csv"
        stage_recommendations(rows, ledger, actor="analyst")
        transition_tier_action(ledger, rows[0].action_id, "approved", actor="manager")
        self.billing.write_text(
            "email,application,license_status,annual_cost,license_tier,provider,provider_user_id\n"
            "alice@example.com,Zoom,active,12000,2,slack,user-1\n", encoding="utf-8",
        )
        stage_recommendations(recommend_downgrades(self.usage, self.billing, self.tiers), ledger, actor="analyst")
        slack_action = recommend_downgrades(self.usage, self.billing, self.tiers)[0].action_id
        transition_tier_action(ledger, slack_action, "approved", actor="manager")
        with self.assertRaisesRegex(InputError, "currently supports only Zoom"):
            transition_tier_action(ledger, slack_action, "execute", actor="operator",
                                   operation=lambda _state: "changed")


if __name__ == "__main__":
    unittest.main()
