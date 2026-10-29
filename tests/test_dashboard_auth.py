from __future__ import annotations

import unittest

from saas_sentry.dashboard_auth import (
    AuthorizationError,
    authorize,
    map_identity,
    parse_authorization_rules,
    scope_report,
)


class DashboardAuthorizationTests(unittest.TestCase):
    def test_group_mapping_creates_app_and_team_scoped_principal(self) -> None:
        rules = parse_authorization_rules(
            '[{"group":"Slack-Owners","role":"reviewer","applications":["Slack"],"teams":["Engineering"]}]'
        )
        user = map_identity({"email": "owner@example.com", "email_verified": True, "groups": ["Slack-Owners"]}, rules)
        self.assertEqual(user.role, "reviewer")
        self.assertTrue(authorize(user, "review:write", application="Slack"))
        with self.assertRaises(AuthorizationError):
            authorize(user, "review:write", application="Notion")
        self.assertTrue(authorize(user, "review:write", team="Engineering"))
        with self.assertRaises(AuthorizationError):
            authorize(user, "review:write", team="Finance")

    def test_unmapped_identity_is_denied(self) -> None:
        rules = parse_authorization_rules('[{"email":"known@example.com","role":"viewer"}]')
        with self.assertRaises(AuthorizationError):
            map_identity({"email": "unknown@example.com", "email_verified": True}, rules)

    def test_report_projection_filters_findings_and_aggregates_to_scope(self) -> None:
        rules = parse_authorization_rules(
            '[{"email":"owner@example.com","role":"reviewer","applications":["Slack"]}]'
        )
        user = map_identity({"email": "owner@example.com", "email_verified": True}, rules)
        report = {
            "findings": [
                {"finding_id": "f1", "application": "Slack", "department": "Engineering", "opportunity_savings_inr": "100", "realizable_12m_inr": "40", "annualized_cost_inr": "120"},
                {"finding_id": "f2", "application": "Notion", "department": "Finance", "opportunity_savings_inr": "900", "realizable_12m_inr": "300", "annualized_cost_inr": "1000"},
            ],
            "renewal_calendar": [], "cost_center_showback": [], "data_quality_issues": [],
            "totals_inr": {"contract_adjusted_annual_opportunity": "1000", "estimated_realizable_12m": "340", "annualized_candidate_cost": "1120"},
            "active_license_count": 22,
        }
        scoped = scope_report(report, user)
        self.assertEqual([row["finding_id"] for row in scoped["findings"]], ["f1"])
        self.assertEqual(scoped["totals_inr"]["contract_adjusted_annual_opportunity"], "100")
        self.assertIsNone(scoped["active_license_count"])


if __name__ == "__main__":
    unittest.main()
