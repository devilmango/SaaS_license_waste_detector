"""Command-line interface for SaaS Sentry."""

from __future__ import annotations

import argparse
import csv
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
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

    history = subparsers.add_parser("history", help="list or compare saved analysis snapshots")
    history_actions = history.add_subparsers(dest="history_command", required=True)
    history_list = history_actions.add_parser("list", help="list snapshots in a directory")
    history_list.add_argument("--dir", type=Path, default=Path("snapshots"))
    history_compare = history_actions.add_parser("compare", help="compare two snapshots or use latest")
    history_compare.add_argument("--dir", type=Path, default=Path("snapshots"))
    history_compare.add_argument("--baseline", required=True, help="snapshot filename, run prefix, or latest")
    history_compare.add_argument("--current", required=True, help="snapshot filename, run prefix, or latest")
    history_compare.add_argument("--output-json", type=Path, help="optional path for comparison JSON")

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
        print("The connector is read-only; use the supplied HR CSV and run analyze on the generated exports.")
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
    print("Provider data requests are read-only; analyze the generated usage.csv and billing.csv with your HR roster.")
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
        if args.command == "actions":
            return _actions(args)
        if args.command == "history":
            return _history(args)
        return _watch(args)
    except (InputError, ConnectorError, ActionError, OSError, ValueError) as exc:
        print(f"saas-sentry: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped scheduled analysis.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
