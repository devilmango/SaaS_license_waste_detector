"""Command-line interface for SaaS Sentry."""

from __future__ import annotations

import argparse
import csv
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import sys
import time

from .actions import ActionError, list_actions, propose_action, transition_action
from .connectors import (
    ConnectorError,
    GitHubCopilotConnector,
    GoogleWorkspaceConnector,
    Microsoft365Connector,
    SlackConnector,
    ZoomConnector,
)
from .alerts import deliver_alerts
from .entitlements import analyze_entitlements, format_entitlements
from .import_health import build_import_health, format_import_health
from .realization import build_realization_report, format_realization_report
from .engine import (
    InputError,
    analyze,
    format_findings_csv,
    format_quality_csv,
    format_report,
    load_config,
    load_inputs,
    load_reviews,
    format_renewals_csv,
    format_showback_csv,
    update_review,
)
from .reports import compare_snapshots, list_snapshots, read_snapshot, render_html, report_payload, write_snapshot
from .focus import format_focus_spend, load_focus_spend
from .execution import build_execution_plan, ensure_plan_available, execute_plan, resolve_execution
from .monitoring import detect_anomalies, format_anomalies
from .portfolio import (
    discover_portfolio,
    format_discovery,
    format_forecast,
    format_reconciliation,
    forecast_renewals,
    reconcile_focus,
)


def _money(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not amount.is_finite() or amount < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number")
    return amount


def _nonnegative(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a whole number") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def _ratio(value: str) -> Decimal:
    try:
        ratio = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a decimal ratio from 0 to 1") from exc
    if not ratio.is_finite() or not Decimal("0") <= ratio <= Decimal("1"):
        raise argparse.ArgumentTypeError("must be a decimal ratio from 0 to 1")
    return ratio


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="saas-sentry",
        description="Detect unused SaaS licenses from HR, usage, and billing CSV exports.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    report = subparsers.add_parser("analyze", help="analyze CSV exports and report candidates")
    report.add_argument("--hr", type=Path, required=True, help="HR roster CSV")
    report.add_argument("--usage", type=Path, required=True, help="SaaS usage CSV")
    report.add_argument("--billing", type=Path, required=True, help="license billing CSV")
    report.add_argument("--contract-pricing", type=Path, help="optional per-contract seat pricing tiers CSV")
    report.add_argument("--config", type=Path, help="TOML rules and dated currency rates")
    report.add_argument("--stale-days", type=_nonnegative, help="override configured stale-login days")
    report.add_argument("--cost-threshold", type=_money, help="override annual INR cost threshold")
    report.add_argument("--low-usage-threshold", type=_nonnegative, help="override usage-count threshold")
    report.add_argument("--low-active-day-ratio", type=_ratio, help="override minimum active-day share of the usage window")
    report.add_argument("--low-features-used-threshold", type=_nonnegative, help="override low feature-count threshold")
    report.add_argument("--usage-window-days", type=_nonnegative, help="default window for rows without usage_window_days")
    report.add_argument("--as-of", type=date.fromisoformat, help="analysis date (YYYY-MM-DD); defaults to today")
    report.add_argument("--output-csv", type=Path, help="write detailed candidate findings to this CSV")
    report.add_argument("--quality-csv", type=Path, help="write unmatched/conflicting/contract issues to this CSV")
    report.add_argument("--output-json", type=Path, help="write a machine-readable report JSON")
    report.add_argument("--output-html", type=Path, help="write a standalone HTML report")
    report.add_argument("--showback-csv", type=Path, help="write annual license spend and opportunity by cost center")
    report.add_argument("--renewals-csv", type=Path, help="write contract renewal and notice planning calendar")
    report.add_argument("--snapshot-dir", type=Path, help="save an analysis snapshot in this directory")
    report.add_argument("--review-file", type=Path, help="include decisions from a review ledger CSV")

    review = subparsers.add_parser("review", help="record and inspect analyst decisions")
    review_actions = review.add_subparsers(dest="review_command", required=True)
    list_reviews = review_actions.add_parser("list", help="list saved review decisions")
    list_reviews.add_argument("--file", type=Path, default=Path("reviews.csv"), help="review ledger (default: reviews.csv)")
    set_review = review_actions.add_parser("set", help="create or update a finding decision")
    set_review.add_argument("--file", type=Path, default=Path("reviews.csv"), help="review ledger (default: reviews.csv)")
    set_review.add_argument("--finding-id", required=True, help="finding_id from the findings CSV")
    set_review.add_argument("--status", required=True, choices=("confirmed", "dismissed", "deferred"))
    set_review.add_argument("--owner", default="", help="person responsible for follow-up")
    set_review.add_argument("--note", default="", help="decision rationale or follow-up note")
    set_review.add_argument("--reviewed-on", type=date.fromisoformat, help="review date (YYYY-MM-DD); defaults to today")

    connect = subparsers.add_parser("connect", help="import provider data using read-only access")
    connectors = connect.add_subparsers(dest="provider", required=True)
    microsoft = connectors.add_parser("microsoft365", help="import M365 users, SKUs, and sign-in dates")
    microsoft.add_argument("--pricing", type=Path, required=True, help="CSV mapping sku_part_number to annual_cost and contract terms")
    microsoft.add_argument("--output-dir", type=Path, required=True, help="directory for canonical usage.csv and billing.csv")

    google = connectors.add_parser("google-workspace", help="import Workspace users, last logins, and assigned SKUs")
    google.add_argument("--pricing", type=Path, required=True, help="CSV mapping product_id/sku_id to application and annual_cost")
    google.add_argument("--output-dir", type=Path, required=True)
    google.add_argument("--customer", default="my_customer", help="Google Workspace customer ID (default: my_customer)")
    slack = connectors.add_parser("slack", help="import Slack billable seats and daily member analytics")
    slack.add_argument("--pricing", type=Path, required=True, help="single-row workspace annual seat pricing CSV")
    slack.add_argument("--output-dir", type=Path, required=True)
    slack.add_argument("--lookback-days", type=_nonnegative, default=90)
    slack.add_argument("--as-of", type=date.fromisoformat, help="latest analytics date (YYYY-MM-DD); defaults to today")
    slack.add_argument("--team-id", default="", help="workspace ID when using an organization token")
    github = connectors.add_parser("github-copilot", help="import Copilot seats and recent activity for an organization")
    github.add_argument("--organization", required=True)
    github.add_argument("--pricing", type=Path, required=True, help="single-row annual Copilot seat pricing CSV")
    github.add_argument("--identity-map", type=Path, required=True, help="map GitHub login to HR email and/or employee ID")
    github.add_argument("--output-dir", type=Path, required=True)
    zoom = connectors.add_parser("zoom", help="import active licensed Zoom users and last login times")
    zoom.add_argument("--pricing", type=Path, required=True, help="CSV mapping Zoom user_type to application and annual_cost")
    zoom.add_argument("--output-dir", type=Path, required=True)

    focus = subparsers.add_parser("focus", help="normalize FOCUS billing exports")
    focus_actions = focus.add_subparsers(dest="focus_command", required=True)
    focus_import = focus_actions.add_parser("import", help="aggregate a FOCUS CSV into cost-center spend")
    focus_import.add_argument("--input", type=Path, required=True, help="FOCUS cost and usage CSV")
    focus_import.add_argument("--output", type=Path, required=True, help="normalized spend CSV destination")
    focus_import.add_argument("--config", type=Path, help="currency conversion rates and effective date")
    focus_import.add_argument("--cost-center-tag", default="cost_center", help="FOCUS Tags key for cost center")

    reconciliation = subparsers.add_parser("reconcile", help="compare FOCUS actual spend to assigned-license cost")
    reconciliation.add_argument("--hr", type=Path, required=True)
    reconciliation.add_argument("--usage", type=Path, required=True)
    reconciliation.add_argument("--billing", type=Path, required=True)
    reconciliation.add_argument("--focus", type=Path, required=True, help="normalized CSV from focus import")
    reconciliation.add_argument("--contract-pricing", type=Path)
    reconciliation.add_argument("--config", type=Path)
    reconciliation.add_argument("--tolerance-percent", type=_money, default=Decimal("5"))
    reconciliation.add_argument("--output", type=Path, required=True)

    discovery = subparsers.add_parser("discover", help="identify unmanaged apps and overlapping app categories")
    discovery.add_argument("--hr", type=Path, required=True)
    discovery.add_argument("--usage", type=Path, required=True)
    discovery.add_argument("--billing", type=Path, required=True)
    discovery.add_argument("--inventory", type=Path, required=True, help="app inventory CSV from SSO/procurement/expense exports")
    discovery.add_argument("--contract-pricing", type=Path)
    discovery.add_argument("--config", type=Path)
    discovery.add_argument("--output", type=Path, required=True)

    forecast = subparsers.add_parser("forecast", help="model renewal spend with seat and price scenarios")
    forecast.add_argument("--hr", type=Path, required=True)
    forecast.add_argument("--usage", type=Path, required=True)
    forecast.add_argument("--billing", type=Path, required=True)
    forecast.add_argument("--contract-pricing", type=Path)
    forecast.add_argument("--config", type=Path)
    forecast.add_argument("--price-increase-percent", type=_money, default=Decimal("0"))
    forecast.add_argument("--seat-growth-percent", type=_money, default=Decimal("0"))
    forecast.add_argument("--seat-reduction-percent", type=_money, default=Decimal("0"), help="planned seat reduction before the renewal")
    forecast.add_argument("--output", type=Path, required=True)

    actions = subparsers.add_parser("actions", help="propose, approve, and record completed license reclaims")
    action_commands = actions.add_subparsers(dest="action_command", required=True)
    action_list = action_commands.add_parser("list", help="show latest action states from the append-only ledger")
    action_list.add_argument("--file", type=Path, default=Path("actions.csv"))
    action_propose = action_commands.add_parser("propose", help="propose a reclaim for a confirmed finding")
    action_propose.add_argument("--findings", type=Path, required=True)
    action_propose.add_argument("--file", type=Path, default=Path("actions.csv"))
    action_propose.add_argument("--finding-id", required=True)
    action_propose.add_argument("--owner", required=True)
    action_propose.add_argument("--proposed-by", required=True)
    action_propose.add_argument("--note", default="")
    action_propose.add_argument("--proposed-on", type=date.fromisoformat)
    for event_name, actor_flag in (("approve", "--approved-by"), ("reject", "--rejected-by")):
        action = action_commands.add_parser(event_name, help=f"{event_name} a proposed reclaim")
        action.add_argument("--file", type=Path, default=Path("actions.csv"))
        action.add_argument("--action-id", required=True)
        action.add_argument(actor_flag, dest="actor", required=True)
        action.add_argument("--note", default="")
        action.add_argument("--on", dest="occurred_on", type=date.fromisoformat)
    action_reclaimed = action_commands.add_parser("reclaimed", help="record an approved reclaim and actual savings")
    action_reclaimed.add_argument("--file", type=Path, default=Path("actions.csv"))
    action_reclaimed.add_argument("--action-id", required=True)
    action_reclaimed.add_argument("--reclaimed-by", dest="actor", required=True)
    action_reclaimed.add_argument("--actual-annual-savings", type=_money, required=True)
    action_reclaimed.add_argument("--note", default="")
    action_reclaimed.add_argument("--on", dest="occurred_on", type=date.fromisoformat)
    action_execute = action_commands.add_parser(
        "execute", help="preview or explicitly execute an approved Microsoft 365 license reclaim",
    )
    action_execute.add_argument("--findings", type=Path, required=True)
    action_execute.add_argument("--billing", type=Path, required=True, help="latest provider billing export")
    action_execute.add_argument("--file", type=Path, default=Path("actions.csv"), help="approval action ledger")
    action_execute.add_argument("--execution-ledger", type=Path, default=Path("execution-events.csv"))
    action_execute.add_argument("--action-id", required=True)
    action_execute.add_argument(
        "--provider", choices=("microsoft365", "google_workspace", "github_copilot"),
        default="microsoft365",
    )
    action_execute.add_argument("--executed-by", required=True)
    action_execute.add_argument("--execute", action="store_true", help="perform the provider change; default is preview only")
    action_resolve = action_commands.add_parser(
        "resolve-execution", help="record an independent human reconciliation of an ambiguous provider result",
    )
    action_resolve.add_argument("--file", type=Path, default=Path("actions.csv"))
    action_resolve.add_argument("--execution-ledger", type=Path, default=Path("execution-events.csv"))
    action_resolve.add_argument("--action-id", required=True)
    action_resolve.add_argument(
        "--provider", choices=("microsoft365", "google_workspace", "github_copilot"),
        default="microsoft365",
    )
    action_resolve.add_argument("--resolved-as", choices=("completed", "not-applied"), required=True)
    action_resolve.add_argument("--resolved-by", required=True)
    action_resolve.add_argument("--note", required=True)
    history = subparsers.add_parser("history", help="list or compare saved analysis snapshots")
    history_actions = history.add_subparsers(dest="history_command", required=True)
    history_list = history_actions.add_parser("list", help="list snapshots in a directory")
    history_list.add_argument("--dir", type=Path, default=Path("snapshots"))
    history_compare = history_actions.add_parser("compare", help="compare two snapshots or use latest")
    history_compare.add_argument("--dir", type=Path, default=Path("snapshots"))
    history_compare.add_argument("--baseline", required=True, help="snapshot filename, run prefix, or latest")
    history_compare.add_argument("--current", required=True, help="snapshot filename, run prefix, or latest")
    history_compare.add_argument("--output-json", type=Path, help="optional path for comparison JSON")

    monitor = subparsers.add_parser("monitor", help="detect spend, seat, renewal, contract, and invoice anomalies")
    monitor.add_argument("--current", type=Path, required=True, help="current analysis JSON report or snapshot")
    monitor.add_argument("--baseline", type=Path, help="optional prior analysis report or snapshot")
    monitor.add_argument("--reconciliation", type=Path, help="optional reconciliation CSV from the reconcile command")
    monitor.add_argument("--spend-change-threshold-percent", type=_money, default=Decimal("20"))
    monitor.add_argument("--seat-change-threshold", type=_nonnegative, default=2)
    monitor.add_argument("--renewal-window-days", type=_nonnegative, default=60)
    monitor.add_argument("--output", type=Path, required=True)

    notify = subparsers.add_parser("notify", help="preview or deliver new monitor alerts to an HTTPS JSON webhook")
    notify.add_argument("--alerts", type=Path, required=True, help="anomaly CSV generated by monitor")
    notify.add_argument("--ledger", type=Path, default=Path("alert-deliveries.csv"))
    notify.add_argument("--webhook-url", default=os.environ.get("SAAS_SENTRY_WEBHOOK_URL", ""))
    notify.add_argument("--send", action="store_true", help="deliver alerts; default is preview only")

    entitlements = subparsers.add_parser(
        "entitlements", help="compare committed contract seats with active assigned licenses",
    )
    entitlements.add_argument("--billing", type=Path, required=True)
    entitlements.add_argument("--contracts", type=Path, required=True, help="contract entitlements CSV")
    entitlements.add_argument("--output", type=Path, required=True)

    import_health = subparsers.add_parser(
        "import-health", help="report source freshness, identity coverage, and import metadata consistency",
    )
    import_health.add_argument("--hr", type=Path, required=True)
    import_health.add_argument("--usage", type=Path, required=True)
    import_health.add_argument("--billing", type=Path, required=True)
    import_health.add_argument("--metadata", type=Path, help="optional provider import.json metadata")
    import_health.add_argument("--stale-after-days", type=_nonnegative, default=7)
    import_health.add_argument("--as-of", type=date.fromisoformat, help="report date (YYYY-MM-DD)")
    import_health.add_argument("--output", type=Path, required=True)

    realization = subparsers.add_parser(
        "realization", help="compare modeled and actual annual savings from reclaim actions",
    )
    realization.add_argument("--actions", type=Path, required=True, help="append-only actions CSV ledger")
    realization.add_argument("--findings", type=Path, required=True, help="findings CSV used to propose actions")
    realization.add_argument("--output", type=Path, required=True)

    watch = subparsers.add_parser("watch", help="run recurring reports in the foreground")
    watch.add_argument("--hr", type=Path, required=True)
    watch.add_argument("--usage", type=Path, required=True)
    watch.add_argument("--billing", type=Path, required=True)
    watch.add_argument("--contract-pricing", type=Path)
    watch.add_argument("--config", type=Path)
    watch.add_argument("--stale-days", type=_nonnegative)
    watch.add_argument("--cost-threshold", type=_money)
    watch.add_argument("--low-usage-threshold", type=_nonnegative)
    watch.add_argument("--low-active-day-ratio", type=_ratio)
    watch.add_argument("--low-features-used-threshold", type=_nonnegative)
    watch.add_argument("--usage-window-days", type=_nonnegative)
    watch.add_argument("--review-file", type=Path)
    watch.add_argument("--output-dir", type=Path, required=True)
    watch.add_argument("--every-hours", type=float, default=24.0)
    watch.add_argument("--once", action="store_true", help="write one scheduled report and exit")
    return parser


def _analyze(args: argparse.Namespace) -> int:
    rules, currency = load_config(args.config)
    overrides = {
        "stale_days": args.stale_days,
        "cost_threshold_inr": args.cost_threshold,
        "low_usage_threshold": args.low_usage_threshold,
        "low_active_day_ratio": args.low_active_day_ratio,
        "low_features_used_threshold": args.low_features_used_threshold,
    }
    rules = replace(rules, **{key: value for key, value in overrides.items() if value is not None})
    window = args.usage_window_days if args.usage_window_days is not None else rules.default_usage_window_days
    data = load_inputs(
        args.hr, args.usage, args.billing, currency=currency,
        default_usage_window_days=window, contract_pricing_path=args.contract_pricing,
    )
    as_of = args.as_of or date.today()
    result = analyze(data, as_of=as_of, rules=rules, currency=currency)
    reviews = load_reviews(args.review_file)
    print(format_report(result, reviews))
    inputs = {path.resolve() for path in (args.hr, args.usage, args.billing, args.contract_pricing, args.config) if path}
    output_paths = [path for path in (
        args.output_csv, args.quality_csv, args.output_json, args.output_html,
        args.showback_csv, args.renewals_csv,
    ) if path]
    resolved_outputs = [path.resolve() for path in output_paths]
    if len(set(resolved_outputs)) != len(resolved_outputs):
        raise InputError("report output paths must be different")
    if inputs.intersection(resolved_outputs):
        raise InputError("report outputs cannot overwrite input CSV or configuration files")
    payload = report_payload(
        result, as_of=as_of,
        source_paths=[
            path for path in (args.hr, args.usage, args.billing, args.contract_pricing, args.review_file)
            if path and path.exists()
        ],
        config_path=args.config,
        reviews=reviews,
    )
    if args.output_csv:
        _write_text(args.output_csv, format_findings_csv(result, reviews))
    if args.quality_csv:
        _write_text(args.quality_csv, format_quality_csv(result))
    if args.output_json:
        _write_text(args.output_json, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    if args.output_html:
        _write_text(args.output_html, render_html(payload))
    if args.showback_csv:
        _write_text(args.showback_csv, format_showback_csv(result))
    if args.renewals_csv:
        _write_text(args.renewals_csv, format_renewals_csv(result))
    if args.snapshot_dir:
        print(f"Saved analysis snapshot to {write_snapshot(args.snapshot_dir, payload)}")
    return 0


def _review(args: argparse.Namespace) -> int:
    if args.review_command == "set":
        update_review(
            args.file, args.finding_id, args.status, owner=args.owner, note=args.note,
            reviewed_on=args.reviewed_on,
        )
        print(f"Saved {args.status} decision for {args.finding_id} in {args.file}.")
        return 0
    records = load_reviews(args.file)
    if not records:
        print(f"No review decisions found in {args.file}.")
        return 0
    writer = csv.writer(sys.stdout)
    writer.writerow(["finding_id", "status", "owner", "reviewed_on", "note"])
    for key, record in sorted(records.items()):
        writer.writerow([key, record["status"], record["owner"], record["reviewed_on"], record["note"]])
    return 0


def _connect(args: argparse.Namespace) -> int:
    if args.provider == "microsoft365":
        users, licenses = Microsoft365Connector.from_environment().import_exports(args.pricing, args.output_dir)
        print(f"Imported {users} Microsoft 365 users and {licenses} license assignments into {args.output_dir}.")
        print("The import is read-only; use the supplied HR CSV and run analyze on the generated exports.")
        return 0
    if args.provider == "google-workspace":
        users, licenses = GoogleWorkspaceConnector.from_environment().import_exports(
            args.pricing, args.output_dir, customer=args.customer,
        )
    elif args.provider == "slack":
        users, licenses = SlackConnector.from_environment().import_exports(
            args.pricing, args.output_dir, lookback_days=args.lookback_days,
            as_of=args.as_of, team_id=args.team_id,
        )
    elif args.provider == "github-copilot":
        users, licenses = GitHubCopilotConnector.from_environment().import_exports(
            args.organization, args.pricing, args.identity_map, args.output_dir,
        )
    elif args.provider == "zoom":
        users, licenses = ZoomConnector.from_environment().import_exports(args.pricing, args.output_dir)
    else:
        return 2
    print(f"Imported {users} provider users and {licenses} license assignments into {args.output_dir}.")
    print("Provider import requests are read-only; analyze the generated usage.csv and billing.csv with your HR roster.")
    return 0


def _actions(args: argparse.Namespace) -> int:
    if args.action_command == "list":
        writer = csv.writer(sys.stdout)
        writer.writerow([
            "action_id", "finding_id", "status", "owner", "proposed_by", "approver",
            "proposed_on", "updated_on", "estimated_annual_savings_inr",
            "actual_annual_savings_inr", "variance_inr", "note",
        ])
        for action in list_actions(args.file):
            writer.writerow([
                action.action_id, action.finding_id, action.status, action.owner,
                action.proposed_by, action.approver, action.proposed_on, action.updated_on,
                f"{action.estimated_annual_savings_inr:.2f}",
                f"{action.actual_annual_savings_inr:.2f}" if action.actual_annual_savings_inr is not None else "",
                f"{action.variance_inr:.2f}" if action.variance_inr is not None else "", action.note,
            ])
        return 0
    if args.action_command == "propose":
        action_id = propose_action(
            args.findings, args.file, args.finding_id, owner=args.owner,
            proposed_by=args.proposed_by, note=args.note, proposed_on=args.proposed_on,
        )
        print(f"Proposed reclaim action {action_id}; approval is required before completion.")
        return 0
    if args.action_command == "execute":
        inputs = (args.findings, args.billing, args.file, args.execution_ledger)
        if args.execution_ledger.resolve() in {path.resolve() for path in inputs[:-1]}:
            raise ActionError("execution ledger cannot overwrite an action or input file")
        plan = build_execution_plan(
            args.file, args.findings, args.billing, args.action_id, provider=args.provider,
        )
        ensure_plan_available(plan, args.execution_ledger)
        operation_label = {
            "microsoft365": "remove",
            "google_workspace": "revoke",
            "github_copilot": "request cancellation of",
        }[plan.provider]
        print(
            f"Action {plan.action_id}: {plan.provider} would {operation_label} application {plan.application!r} "
            f"from {plan.email}. Idempotency key: {plan.idempotency_key}."
        )
        if not args.execute:
            print("Preview only; no provider request was sent. Pass --execute to perform the approved change.")
            return 0
        connectors = {
            "microsoft365": Microsoft365Connector.from_environment,
            "google_workspace": GoogleWorkspaceConnector.from_environment,
            "github_copilot": GitHubCopilotConnector.from_environment,
        }
        connector = connectors[plan.provider]()
        result = execute_plan(
            plan, args.execution_ledger, actor=args.executed_by,
            operation=lambda target: connector.remove_user_license(
                target.provider_user_id, target.provider_license_id,
            ),
        )
        print(f"Provider execution recorded as {result} in {args.execution_ledger}.")
        print("Verify refreshed billing data before recording actual savings with actions reclaimed.")
        return 0
    if args.action_command == "resolve-execution":
        resolve_execution(
            args.file, args.execution_ledger, args.action_id, provider=args.provider,
            resolved_as=args.resolved_as.replace("-", "_"), actor=args.resolved_by, note=args.note,
        )
        print(f"Recorded independent {args.resolved_as} reconciliation for action {args.action_id}.")
        return 0
    event = {"approve": "approved", "reject": "rejected", "reclaimed": "reclaimed"}[args.action_command]
    transition_action(
        args.file, args.action_id, event, actor=args.actor, note=args.note,
        actual_annual_savings_inr=getattr(args, "actual_annual_savings", None),
        occurred_on=args.occurred_on,
    )
    print(f"Recorded {event} for action {args.action_id}.")
    return 0


def _focus_import(args: argparse.Namespace) -> int:
    if args.output.resolve() == args.input.resolve():
        raise InputError("FOCUS import output cannot overwrite its input")
    if args.config and args.output.resolve() == args.config.resolve():
        raise InputError("FOCUS import output cannot overwrite its configuration")
    _, currency = load_config(args.config)
    rows = load_focus_spend(args.input, currency=currency, cost_center_tag=args.cost_center_tag)
    _write_text(args.output, format_focus_spend(rows, currency.rate_date))
    print(f"Normalized {len(rows)} FOCUS spend groups into {args.output}.")
    return 0


def _portfolio_inputs(args: argparse.Namespace):
    _, currency = load_config(args.config)
    return load_inputs(
        args.hr, args.usage, args.billing, currency=currency,
        contract_pricing_path=args.contract_pricing,
    )


def _portfolio_output(output: Path, inputs: tuple[Path | None, ...], content: str) -> None:
    resolved = output.resolve()
    if resolved in {path.resolve() for path in inputs if path is not None}:
        raise InputError("report output cannot overwrite an input file")
    _write_text(output, content)


def _reconcile(args: argparse.Namespace) -> int:
    inputs = _portfolio_inputs(args)
    rows = reconcile_focus(args.focus, inputs, tolerance_percent=args.tolerance_percent)
    source_paths = (args.hr, args.usage, args.billing, args.focus, args.contract_pricing, args.config)
    _portfolio_output(args.output, source_paths, format_reconciliation(rows))
    print(f"Reconciled {len(rows)} application/cost-center billing groups into {args.output}.")
    return 0


def _discover(args: argparse.Namespace) -> int:
    inputs = _portfolio_inputs(args)
    rows = discover_portfolio(args.inventory, inputs)
    source_paths = (args.hr, args.usage, args.billing, args.inventory, args.contract_pricing, args.config)
    _portfolio_output(args.output, source_paths, format_discovery(rows))
    unmanaged = sum("shadow_saas" in row.portfolio_status for row in rows)
    overlaps = sum("overlap_candidate" in row.portfolio_status for row in rows)
    print(f"Discovered {len(rows)} apps ({unmanaged} unmanaged, {overlaps} overlap candidates) in {args.output}.")
    return 0


def _forecast(args: argparse.Namespace) -> int:
    inputs = _portfolio_inputs(args)
    rows = forecast_renewals(
        inputs, price_increase_percent=args.price_increase_percent,
        seat_growth_percent=args.seat_growth_percent,
        seat_reduction_percent=args.seat_reduction_percent,
    )
    source_paths = (args.hr, args.usage, args.billing, args.contract_pricing, args.config)
    _portfolio_output(args.output, source_paths, format_forecast(rows))
    print(f"Forecast {len(rows)} active contracts into {args.output}.")
    return 0


def _history(args: argparse.Namespace) -> int:
    snapshots = list_snapshots(args.dir)
    if args.history_command == "list":
        if not snapshots:
            print(f"No analysis snapshots found in {args.dir}.")
            return 0
        for path, payload in snapshots:
            totals = payload["totals_inr"]
            print(
                f"{path.name}  as of {payload['as_of']}  findings={len(payload['findings'])}  "
                f"annual opportunity=₹{Decimal(totals['contract_adjusted_annual_opportunity']):,.2f}"
            )
        return 0

    def resolve(selector: str) -> dict:
        if selector == "latest":
            if not snapshots:
                raise InputError(f"No analysis snapshots found in {args.dir}")
            return snapshots[-1][1]
        path = Path(selector)
        if not path.is_absolute() and not path.exists():
            matches = [(p, payload) for p, payload in snapshots if p.name == selector or p.stem.startswith(selector)]
            if len(matches) != 1:
                raise InputError(f"Snapshot selector {selector!r} matched {len(matches)} files")
            return matches[0][1]
        return read_snapshot(path)

    comparison = compare_snapshots(resolve(args.baseline), resolve(args.current))
    if args.output_json:
        _write_text(args.output_json, json.dumps(comparison, indent=2) + "\n")
    print(f"Snapshot comparison: {comparison['baseline_as_of']} → {comparison['current_as_of']}")
    print(f"New findings: {len(comparison['added_finding_ids'])}")
    print(f"Resolved findings: {len(comparison['resolved_finding_ids'])}")
    print(f"Changed findings: {len(comparison['changed_findings'])}")
    print(f"Annual opportunity change: ₹{Decimal(comparison['contract_adjusted_annual_opportunity_delta_inr']):,.2f}")
    return 0


def _monitor(args: argparse.Namespace) -> int:
    current = read_snapshot(args.current)
    baseline = read_snapshot(args.baseline) if args.baseline else None
    anomalies = detect_anomalies(
        current, baseline=baseline, reconciliation_path=args.reconciliation,
        spend_change_threshold_percent=args.spend_change_threshold_percent,
        seat_change_threshold=args.seat_change_threshold,
        renewal_window_days=args.renewal_window_days,
    )
    inputs = {args.current.resolve()}
    if args.baseline:
        inputs.add(args.baseline.resolve())
    if args.reconciliation:
        inputs.add(args.reconciliation.resolve())
    if args.output.resolve() in inputs:
        raise InputError("monitor output cannot overwrite an input file")
    _write_text(args.output, format_anomalies(anomalies))
    counts = {severity: sum(row.severity == severity for row in anomalies) for severity in ("high", "medium", "low")}
    print(
        f"Detected {len(anomalies)} anomalies ({counts['high']} high, {counts['medium']} medium, "
        f"{counts['low']} low) in {args.output}."
    )
    return 0


def _notify(args: argparse.Namespace) -> int:
    if not args.webhook_url:
        raise InputError("provide --webhook-url or set SAAS_SENTRY_WEBHOOK_URL")
    if args.ledger.resolve() == args.alerts.resolve():
        raise InputError("alert delivery ledger cannot overwrite the alerts CSV")
    pending, delivered = deliver_alerts(
        args.alerts, args.ledger, webhook_url=args.webhook_url, send=args.send,
    )
    if args.send:
        print(f"Delivered {delivered} new alerts; {pending - delivered} duplicate alerts skipped.")
    else:
        print(f"Preview: {pending} new alerts would be sent. Pass --send to deliver them.")
    return 0


def _entitlements(args: argparse.Namespace) -> int:
    if args.output.resolve() in {args.billing.resolve(), args.contracts.resolve()}:
        raise InputError("entitlement report cannot overwrite an input file")
    rows = analyze_entitlements(args.contracts, args.billing)
    _write_text(args.output, format_entitlements(rows))
    overages = sum(row.overage_seats for row in rows)
    exposure = sum((row.estimated_annual_overage_inr for row in rows), Decimal("0"))
    print(f"Wrote {len(rows)} contract entitlement rows ({overages} overage seats; ₹{exposure:.2f}/year estimated).")
    return 0


def _import_health(args: argparse.Namespace) -> int:
    inputs = {args.hr.resolve(), args.usage.resolve(), args.billing.resolve()}
    if args.metadata:
        inputs.add(args.metadata.resolve())
    if args.output.resolve() in inputs:
        raise InputError("import health output cannot overwrite an input file")
    rows = build_import_health(
        args.hr, args.usage, args.billing, metadata_path=args.metadata,
        as_of=args.as_of, stale_after_days=args.stale_after_days,
    )
    _write_text(args.output, format_import_health(rows))
    statuses = {status: sum(row.status == status for row in rows) for status in ("healthy", "issues", "stale", "empty", "future_dated")}
    print(
        f"Wrote import health for {len(rows)} sources "
        f"({statuses['healthy']} healthy, {statuses['issues']} with issues, {statuses['stale']} stale)."
    )
    return 0


def _realization(args: argparse.Namespace) -> int:
    if args.output.resolve() in {args.actions.resolve(), args.findings.resolve()}:
        raise InputError("savings realization output cannot overwrite an input file")
    rows = build_realization_report(args.actions, args.findings)
    _write_text(args.output, format_realization_report(rows))
    overall = next((row for row in rows if row.scope_type == "overall"), None)
    if not overall:
        print(f"Wrote an empty savings realization report to {args.output}.")
    else:
        rate = f"{overall.realization_percent:.2f}%" if overall.realization_percent is not None else "n/a"
        print(
            f"Wrote savings realization: {overall.reclaimed_count} reclaimed actions, "
            f"₹{overall.actual_annual_inr:.2f}/year actual vs ₹{overall.estimated_reclaimed_annual_inr:.2f}/year "
            f"estimated ({rate} realization)."
        )
    return 0


def _watch(args: argparse.Namespace) -> int:
    if not math.isfinite(args.every_hours) or args.every_hours <= 0:
        raise InputError("--every-hours must be greater than zero")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        args.output_csv = args.output_dir / f"findings-{stamp}.csv"
        args.quality_csv = args.output_dir / f"quality-{stamp}.csv"
        args.output_json = args.output_dir / f"report-{stamp}.json"
        args.output_html = args.output_dir / f"report-{stamp}.html"
        args.showback_csv = args.output_dir / f"showback-{stamp}.csv"
        args.renewals_csv = args.output_dir / f"renewals-{stamp}.csv"
        args.snapshot_dir = args.output_dir / "snapshots"
        args.as_of = None
        _analyze(args)
        if args.once:
            return 0
        print(f"Next scheduled analysis in {args.every_hours:g} hours. Stop with Ctrl-C.")
        time.sleep(args.every_hours * 3600)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "analyze":
            return _analyze(args)
        if args.command == "review":
            return _review(args)
        if args.command == "connect":
            return _connect(args)
        if args.command == "focus":
            return _focus_import(args)
        if args.command == "reconcile":
            return _reconcile(args)
        if args.command == "discover":
            return _discover(args)
        if args.command == "forecast":
            return _forecast(args)
        if args.command == "actions":
            return _actions(args)
        if args.command == "history":
            return _history(args)
        if args.command == "monitor":
            return _monitor(args)
        if args.command == "notify":
            return _notify(args)
        if args.command == "entitlements":
            return _entitlements(args)
        if args.command == "import-health":
            return _import_health(args)
        if args.command == "realization":
            return _realization(args)
        return _watch(args)
    except (InputError, ConnectorError, ActionError, OSError, ValueError) as exc:
        print(f"saas-sentry: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped scheduled analysis.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
