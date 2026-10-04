# SaaS Sentry

**A lightweight, local CLI for finding unused SaaS licenses and estimating subscription waste.**

SaaS Sentry combines an HR roster, SaaS activity export, and license billing export to identify licenses that may be reclaimed or optimized. It turns those records into an explainable, application-level savings report using configurable business rules. No AI service, SaaS account access, or third-party runtime package is required.

> **Review before acting:** Findings are recommendations for a person to investigate. SaaS Sentry does not disable accounts, change subscriptions, or cancel contracts.

## Contents

- [Why SaaS Sentry](#why-saas-sentry)
- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Input CSV formats](#input-csv-formats)
- [Rules and configuration](#rules-and-configuration)
- [Understanding the report](#understanding-the-report)
- [Data handling and validation](#data-handling-and-validation)
- [Project structure](#project-structure)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

## Why SaaS Sentry

As SaaS portfolios grow, it becomes harder to reconcile who works at the company, who uses each product, which accounts still hold licenses, and what those licenses cost. SaaS Sentry demonstrates that a useful first pass can come from ordinary CSV exports and transparent rules:

- **ETL:** read, validate, and normalize HR, usage, and billing records.
- **Identity resolution:** match records by normalized email.
- **Business rules:** flag terminated accounts, stale activity, and expensive low-usage licenses.
- **Cost modeling:** estimate annual savings from per-license annual costs.
- **Reporting:** produce a readable CLI summary and machine-readable findings CSV.

## Features

- Analyze three local CSV exports with one command.
- Match email addresses without regard to case or surrounding whitespace.
- Use HR status as the authoritative employee status.
- Prioritize terminated-account reclaim, then inactivity, then costly low usage.
- Avoid counting one license in multiple finding categories.
- Configure stale-login, annual-cost, and usage-count thresholds.
- Set an analysis date to make stale-login results reproducible.
- Export individual findings, explanations, and annual savings to CSV.
- Run offline with Python's standard library.

## How it works

```text
HR CSV ─────────────┐
Usage CSV ──────────┼─> Validate and normalize ─> Match identities ─> Apply rules ─> Savings report
Billing CSV ────────┘
```

The exact join key is email, stripped of surrounding whitespace and case-folded. Each billing row represents one user's license for one application. Annual costs should be supplied in INR, and usage counts should use a consistent period across the usage export.

## Requirements

- Python 3.11 or newer
- `pip` and `venv` for the editable installation instructions below
- No third-party runtime dependencies

## Install

```bash
git clone https://github.com/<OWNER>/saas-sentry.git
cd saas-sentry
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

On Windows PowerShell, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

The editable install provides the `saas-sentry` command. To run directly from a checkout without installing the package:

```bash
PYTHONPATH=src python3 -m saas_sentry.cli --help
```

## Quick start

Use the included example exports:

```bash
saas-sentry analyze \
  --hr examples/hr.csv \
  --usage examples/usage.csv \
  --billing examples/billing.csv \
  --as-of 2026-10-04 \
  --output-csv findings.csv
```

Example terminal output:

```text
Potential savings

Google Workspace
1 terminated accounts
₹9,000/year

Notion
1 optimization candidates
₹12,000/year

Slack
1 inactive users
₹12,000/year

Total potential annual savings: ₹33,000
Active licenses analyzed: 4
```

Use `saas-sentry analyze --help` to see all command-line options. The command exits with status `0` when analysis succeeds and `2` when an input file cannot be read or validated.

## Input CSV formats

Column names are case-sensitive and must match the names shown below. Extra columns are allowed. Save files as UTF-8; a UTF-8 BOM is also accepted.

### HR roster (`--hr`)

Required columns: `employee,email,status`.

`status` must be `active` or `terminated` (case-insensitive). Email must not be blank and must be unique in the roster.

```csv
employee,email,status
Alice,alice@company.com,active
Bob,bob@company.com,active
John,john@company.com,terminated
Cara,cara@company.com,active
```

### SaaS usage (`--usage`)

Required columns: `employee,email,application,last_login`. `last_login` must be an ISO calendar date (`YYYY-MM-DD`) or blank. `usage_count` is optional and must be a non-negative whole number when provided. It should represent the same observation period for each row, such as the last 30 days.

Each normalized email/application pair must be unique.

```csv
employee,email,application,last_login,usage_count
Alice,alice@company.com,Slack,2026-09-28,42
Bob,bob@company.com,Slack,2025-11-02,1
John,john@company.com,Google Workspace,2026-08-01,12
Cara,cara@company.com,Notion,2026-10-01,2
```

### License billing (`--billing`)

Required columns: `email,application,license_status,annual_cost`.

- `license_status` must be `active` or `inactive` (case-insensitive).
- `annual_cost` is the annual cost for one user's license, as a non-negative number in INR.
- Each normalized email/application pair must be unique.

```csv
email,application,license_status,annual_cost
alice@company.com,Slack,active,12000
bob@company.com,Slack,active,12000
john@company.com,Google Workspace,active,9000
cara@company.com,Notion,active,12000
```

The billing export supplies license status and cost. Inactive licenses are ignored by the rules. An active billing identity absent from HR is reported as unmatched and excluded from savings rather than being assigned an assumed employment status.

## Rules and configuration

Rules are evaluated in this order. An active license is assigned to the first matching rule only.

| Priority | Condition | Finding | Default annual savings estimate |
| --- | --- | --- | --- |
| 1 | HR status is `terminated` and billing license status is `active` | `terminated` — recommend reclaim | Full annual license cost |
| 2 | Active license has no usage record, a blank last login, or a last login more than 90 days ago | `inactive` — investigate or reclaim | Full annual license cost |
| 3 | Active license costs more than ₹10,000/year and usage count is below 5 | `optimization` — review the plan or seat | Full annual license cost |

Set thresholds to match your organization's policy:

```bash
saas-sentry analyze \
  --hr hr.csv \
  --usage usage.csv \
  --billing billing.csv \
  --stale-days 60 \
  --cost-threshold 15000 \
  --low-usage-threshold 3
```

| Option | Default | Meaning |
| --- | ---: | --- |
| `--stale-days` | `90` | Flag a login only when it is more than this many days old. |
| `--cost-threshold` | `10000` | Cost must be greater than this annual INR amount for the low-usage rule. |
| `--low-usage-threshold` | `5` | Usage count must be less than this number for the low-usage rule. |
| `--as-of` | Local current date | Analysis date in `YYYY-MM-DD` format. Set this to reproduce stale-login results. |
| `--output-csv` | Not written | Optional path for the detailed candidate CSV. |

A login exactly N days before the analysis date is not stale when `--stale-days N`. A usage count exactly equal to `--low-usage-threshold` is not low. The annual cost threshold is also strict: a license costing exactly the threshold is not flagged by the low-usage rule.

## Understanding the report

The terminal report groups candidate counts and estimated savings by application and finding category. The optional findings CSV has these columns:

```text
employee,email,application,category,reason,annual_savings_inr
```

Savings are estimated by summing the full annual cost for each candidate license. They are not a prediction of realized cash savings: contract terms, seat minimums, billing periods, taxes, and negotiated discounts can change the amount actually recovered. Confirm a finding with the application owner and billing team before acting.

## Data handling and validation

- The tool reads the supplied local CSV files and writes only the requested findings CSV.
- It makes no network requests and does not connect to SaaS services.
- It does not store data in a database or change account state.
- Invalid dates, costs, statuses, usage counts, missing required columns, and duplicate identities stop analysis with a file and row-oriented error where available.
- Duplicate identities are rejected to prevent ambiguous joins and accidental double-counting.
- Billing identities without an HR match are omitted from savings and surfaced as an unmatched count.

## Project structure

```text
.
├── examples/                 # Small HR, usage, and billing CSV exports
├── src/saas_sentry/
│   ├── cli.py                 # Argument parsing and command entry point
│   └── engine.py              # CSV validation, identity matching, and rules
├── LICENSE
├── README.md
└── pyproject.toml
```

## Roadmap

Potential follow-on work includes provider-specific exports or API connectors for Google Workspace, Microsoft 365, Slack, GitHub, Zoom, and Atlassian; support for additional currencies; configurable usage windows; and a review workflow for approving recommendations. These are future directions, not capabilities included in the current CLI.

## Contributing

Contributions and bug reports are welcome. For a code change:

1. Open an issue or discussion describing the problem or proposed behavior.
2. Create a focused branch and keep changes aligned with the documented CSV schemas and rule behavior.
3. Include or update example data and documentation when input or output behavior changes.
4. Run the CLI against the example CSVs and inspect the generated report before opening a pull request.

Please do not include real employee, usage, or billing data in issues, example files, or pull requests. Use synthetic records instead.

## License

SaaS Sentry is available under the [MIT License](LICENSE).
