"""Command-line interface for SaaS Sentry."""

from __future__ import annotations

import argparse
import csv
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys

from .engine import (
    InputError,
    Rules,
    analyze,
    format_findings_csv,
    format_quality_csv,
    format_report,
    load_config,
    load_inputs,
    load_reviews,
    update_review,
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
    data = load_inputs(args.hr, args.usage, args.billing, currency=currency, default_usage_window_days=window)
    result = analyze(data, as_of=args.as_of, rules=rules, currency=currency)
    reviews = load_reviews(args.review_file)
    print(format_report(result, reviews))
    if args.output_csv:
        args.output_csv.write_text(format_findings_csv(result, reviews), encoding="utf-8")
    if args.quality_csv:
        args.quality_csv.write_text(format_quality_csv(result), encoding="utf-8")
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


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "analyze":
            return _analyze(args)
        return _review(args)
    except (InputError, OSError) as exc:
        print(f"saas-sentry: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
