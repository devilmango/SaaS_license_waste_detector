"""Command-line interface for SaaS Sentry."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys

from .engine import InputError, analyze, format_findings_csv, format_report, load_inputs


def _money(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not amount.is_finite() or amount < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number")
    return amount


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="saas-sentry",
        description="Detect inactive and over-provisioned SaaS licenses from HR, usage, and billing CSVs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    report = subparsers.add_parser("analyze", help="analyze CSV exports and print potential savings")
    report.add_argument("--hr", type=Path, required=True, help="HR roster CSV")
    report.add_argument("--usage", type=Path, required=True, help="SaaS usage CSV")
    report.add_argument("--billing", type=Path, required=True, help="license billing CSV")
    report.add_argument("--stale-days", type=int, default=90, help="days without login to flag (default: 90)")
    report.add_argument("--cost-threshold", type=_money, default=Decimal("10000"), help="annual INR cost above which low usage is flagged")
    report.add_argument("--low-usage-threshold", type=int, default=5, help="usage_count below which a costly license is flagged")
    report.add_argument("--as-of", type=date.fromisoformat, help="analysis date (YYYY-MM-DD); defaults to today")
    report.add_argument("--output-csv", type=Path, help="write detailed findings to this CSV file")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.stale_days < 0 or args.low_usage_threshold < 0:
        parser.error("--stale-days and --low-usage-threshold must be non-negative")
    try:
        people, usage, licenses = load_inputs(args.hr, args.usage, args.billing)
        result = analyze(
            people, usage, licenses, as_of=args.as_of, stale_days=args.stale_days,
            cost_threshold=args.cost_threshold, low_usage_threshold=args.low_usage_threshold,
        )
        print(format_report(result))
        if args.output_csv:
            args.output_csv.write_text(format_findings_csv(result), encoding="utf-8")
    except (InputError, OSError) as exc:
        print(f"saas-sentry: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
