from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from saas_sentry.engine import InputError
from saas_sentry.tier_optimization import (
    list_tier_action_history, list_tier_actions, recommend_downgrades, stage_recommendations,
    transition_tier_action,
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

    def test_missing_usage_telemetry_is_flagged_and_not_staged(self) -> None:
        self.usage.write_text(
            "employee,email,application,features_used\nAlice,alice@example.com,Zoom,\n", encoding="utf-8",
        )
        rows = recommend_downgrades(self.usage, self.billing, self.tiers)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "needs_telemetry")
        self.assertIn("features_used", rows[0].compatibility_reason)
        ledger = self.root / "actions.csv"
        self.assertEqual(stage_recommendations(rows, ledger, actor="analyst"), 1)
        self.assertEqual(list_tier_actions(ledger)[0]["status"], "needs_telemetry")
        with self.assertRaisesRegex(InputError, "only proposed"):
            transition_tier_action(ledger, rows[0].action_id, "approved", actor="reviewer")

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
        self.assertEqual(state["status"], "verified")
        self.assertEqual(calls, ["zoom"])
        with self.assertRaisesRegex(InputError, "approved"):
            transition_tier_action(ledger, action_id, "execute", actor="operator",
                                   operation=lambda _state: calls.append("duplicate"))
        self.assertEqual(len(list_tier_actions(ledger)), 1)
        self.assertEqual([row["event"] for row in list_tier_action_history(ledger)[action_id]],
                         ["proposed", "approved", "execution_started", "executed", "verified"])

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
        with self.assertRaisesRegex(InputError, "supports Zoom 2-to-1"):
            transition_tier_action(ledger, slack_action, "execute", actor="operator",
                                   operation=lambda _state: "changed")

    def test_feature_activity_and_window_constraints_are_explained(self) -> None:
        self.usage.write_text(
            "employee,email,application,features_used,observed_features,active_days,usage_count,usage_window_days\n"
            "Alice,alice@example.com,Zoom,2,chat;calendar,8,20,30\n", encoding="utf-8",
        )
        self.tiers.write_text(
            "application,tier,feature_capacity,annual_cost_inr,provider_tier_id,supported_features,"
            "max_active_days,max_usage_count,minimum_usage_window_days\n"
            "Zoom,1,5,6000,1,chat;calendar,10,25,30\n"
            "Zoom,2,20,12000,2,chat;calendar,30,100,30\n", encoding="utf-8",
        )
        row = recommend_downgrades(self.usage, self.billing, self.tiers)[0]
        self.assertEqual(row.status, "proposed")
        self.assertIn("observed feature set is supported", row.compatibility_reason)
        self.assertEqual((row.active_days, row.usage_count, row.usage_window_days), (8, 20, 30))
        self.usage.write_text(self.usage.read_text().replace(",8,20,30", ",11,20,30"), encoding="utf-8")
        flagged = recommend_downgrades(self.usage, self.billing, self.tiers)[0]
        self.assertEqual(flagged.status, "incompatible")
        self.assertIn("active days exceed", flagged.compatibility_reason)

    def test_missing_window_and_feature_names_are_flagged(self) -> None:
        self.usage.write_text(
            "employee,email,application,features_used,observed_features,active_days,usage_count,usage_window_days\n"
            "Alice,alice@example.com,Zoom,2,,,20,\n", encoding="utf-8",
        )
        self.tiers.write_text(
            "application,tier,feature_capacity,annual_cost_inr,provider_tier_id,supported_features,"
            "minimum_usage_window_days,minimum_active_days\n"
            "Zoom,1,5,6000,1,chat;calendar,30,3\nZoom,2,20,12000,2,chat;calendar,30,3\n", encoding="utf-8",
        )
        row = recommend_downgrades(self.usage, self.billing, self.tiers)[0]
        self.assertEqual(row.status, "needs_telemetry")
        self.assertIn("observed feature names", row.compatibility_reason)
        self.assertIn("usage_window_days", row.compatibility_reason)
        self.assertIn("active_days", row.compatibility_reason)

    def test_contract_block_and_commitment_date_are_applied(self) -> None:
        self.billing.write_text(
            "email,application,license_status,annual_cost,license_tier,provider,provider_user_id,"
            "tier_change_allowed,commitment_end_date,contract_id\n"
            "alice@example.com,Zoom,active,12000,2,zoom,user-1,false,,C-1\n", encoding="utf-8",
        )
        blocked = recommend_downgrades(self.usage, self.billing, self.tiers)[0]
        self.assertEqual(blocked.status, "contract_blocked")
        self.assertEqual(blocked.contract_id, "C-1")
        self.billing.write_text(
            "email,application,license_status,annual_cost,license_tier,provider,provider_user_id,"
            "commitment_end_date,contract_id\n"
            "alice@example.com,Zoom,active,12000,2,zoom,user-1,2026-12-15,C-2\n", encoding="utf-8",
        )
        scheduled = recommend_downgrades(self.usage, self.billing, self.tiers, as_of=date(2026, 11, 10))[0]
        self.assertEqual(scheduled.status, "scheduled")
        self.assertEqual(scheduled.eligible_on, "2026-12-15")

    def test_older_action_ledger_is_read_and_upgraded_before_appending(self) -> None:
        ledger = self.root / "legacy-actions.csv"
        ledger.write_text(
            "action_id,event,status,application,email,provider,provider_user_id,current_tier,"
            "recommended_tier,current_provider_tier_id,recommended_provider_tier_id,annual_savings_inr,"
            "actor,proposed_by,approved_by,occurred_at,result\n"
            "older,proposed,proposed,Zoom,old@example.com,zoom,user-old,2,1,2,1,100,analyst,analyst,,,\n",
            encoding="utf-8",
        )
        self.assertEqual(list_tier_actions(ledger)[0]["provider_license_id"], "")
        stage_recommendations(recommend_downgrades(self.usage, self.billing, self.tiers), ledger, actor="analyst")
        with ledger.open(encoding="utf-8", newline="") as handle:
            import csv
            reader = csv.DictReader(handle)
            self.assertIn("compatibility_reason", reader.fieldnames)
            self.assertEqual(len(list(reader)), 2)


if __name__ == "__main__":
    unittest.main()
