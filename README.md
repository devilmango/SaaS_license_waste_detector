# SaaS Sentry

**A local CLI for finding unused SaaS licenses and estimating contract-aware subscription savings.**

SaaS Sentry joins HR, SaaS usage, and billing exports to flag terminated accounts, stale activity, and costly low-use licenses. It produces an explainable savings report, a data-quality file, and a persistent review ledger. It runs locally, makes no network requests, and never changes accounts or subscriptions.

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Install](#install)
- [Quick start](#quick-start)
- [CSV formats](#csv-formats)
- [Rules and configuration](#rules-and-configuration)
- [Review workflow](#review-workflow)
- [Savings estimates](#savings-estimates)
- [Data quality and privacy](#data-quality-and-privacy)
- [CI and development](#ci-and-development)
- [Roadmap](#roadmap)
- [License](#license)

## Features

- Load HR, SaaS usage, and billing CSVs with standard-library Python.
- Resolve identities by stable employee ID, primary email, or semicolon-separated email aliases.
- Report conflicting and unmatched identities in a separate CSV for remediation.
- Detect terminated accounts, stale or missing logins, and expensive licenses with low usage.
- Use active days and usage windows as comparable activity signals; retain usage counts and feature counts as evidence.
- Convert per-user annual costs from multiple currencies using configured, dated rates.
- Respect contract seat minimums and renewal dates when estimating recoverable savings.
- Record confirmed, dismissed, and deferred findings with owner, review date, and rationale.
- Export findings with stable IDs, decision evidence, savings scenarios, and review state.

## How it works

```text
HR CSV ──────────────┐
Usage CSV ───────────┼─> Validate and resolve identity ─> Apply rules ─> Savings scenarios
Billing + contracts ┘                                      │
                                                          ├─> Findings CSV
                                                          ├─> Data-quality CSV
                                                          └─> Review ledger
```

Each billing row represents one assigned user/application license. Billing costs are annual per-seat amounts in the row currency (or configured default currency). The engine joins by employee ID and email where supplied, and reports contradictory identifiers rather than guessing.

## Install

Requirements: Python 3.11 or newer. There are no third-party runtime dependencies.

```bash
git clone https://github.com/<OWNER>/saas-sentry.git
cd saas-sentry
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

For PowerShell activation, use `.venv\Scripts\Activate.ps1`. From an uninstalled checkout, use `PYTHONPATH=src python3 -m saas_sentry.cli --help`.

## Quick start

Analyze the included synthetic exports with the example rules and dated currency table:

```bash
saas-sentry analyze \
  --hr examples/hr.csv \
  --usage examples/usage.csv \
  --billing examples/billing.csv \
  --config config.example.toml \
  --as-of 2026-10-04 \
  --output-csv findings.csv \
  --quality-csv data-quality.csv
```

The command prints annualized candidate cost, contract-adjusted annual opportunity, and estimated savings realizable in the next 12 months. Use `saas-sentry analyze --help` for all options. Exit status is `0` on successful analysis and `2` when an input or configuration cannot be read or validated.

## CSV formats

Column names are case-sensitive. Extra columns are allowed. Files may be UTF-8 or UTF-8 with BOM.

### HR roster (`--hr`)

Required headers: `employee,status`; each row must include at least one of `email` or `employee_id`. Optional `email_aliases` contains aliases separated by semicolons.

```csv
employee,employee_id,email,email_aliases,status
Alice,E001,alice@company.com,,active
Bob,E002,bob@company.com,robert@company.com,active
John,E003,john@company.com,,terminated
```

Status values are `active` or `terminated`, case-insensitive. Employee IDs are matched case-insensitively after trimming.

### SaaS usage (`--usage`)

Required headers: `application,last_login`; each row must include at least one of `email` or `employee_id`. `last_login` uses `YYYY-MM-DD` or is blank.

Optional columns:

| Column | Meaning |
| --- | --- |
| `usage_count` | Product activity count for the row's usage window. |
| `active_days` | Number of days with activity in the usage window. |
| `usage_window_days` | Window represented by this row; defaults to configuration. |
| `features_used` | Count of distinct features used; included as evidence in findings. |
| `employee` | Display name from the usage source. |

```csv
employee_id,email,application,last_login,usage_count,active_days,usage_window_days,features_used
E001,alice@company.com,Slack,2026-09-28,42,25,30,5
E002,robert@company.com,Slack,2025-11-02,1,1,30,1
```

The employee/application pair must resolve uniquely and occur once in the usage export. Activity counts must be non-negative; active days cannot exceed the window.

### Billing and contract data (`--billing`)

Required headers: `application,license_status,annual_cost`; each row must include at least one of `email` or `employee_id`.

| Column | Meaning |
| --- | --- |
| `license_status` | `active` or `inactive`; inactive licenses do not enter rules or active seat counts. |
| `annual_cost` | Annual per-user license price in the row currency. |
| `currency` | ISO currency code; defaults to `[currency].default_currency`. |
| `contract_id` | Contract grouping key; defaults to application. Use a distinct ID for separate agreements. |
| `renewal_date` | Next date on which seat reductions can take effect (`YYYY-MM-DD`). |
| `seat_minimum` | Contract-wide minimum paid active seats; repeat the same value on each row in a contract. Defaults to zero. |

```csv
employee_id,email,application,license_status,annual_cost,currency,contract_id,renewal_date,seat_minimum
E001,alice@company.com,Slack,active,12000,INR,slack-annual,2026-11-15,1
E002,robert@company.com,Slack,active,12000,INR,slack-annual,2026-11-15,1
```

Employee/application pairs must be unique. A consistent `seat_minimum` is required for all rows sharing a contract ID.

## Rules and configuration

Copy `config.example.toml` and change the thresholds and exchange rates to your organization's settings:

```toml
[rules]
stale_days = 90
cost_threshold_inr = 10000
low_usage_threshold = 5
low_active_day_ratio = 0.10
low_features_used_threshold = 2

[usage]
window_days = 30

[currency]
default_currency = "INR"
rate_date = "2026-10-01"
rates_to_inr = { INR = 1, USD = 85, EUR = 92 }
```

`rates_to_inr` gives INR per one unit of the named currency. Include a dated rate whenever any non-INR currency is configured. Billing rows may override the default currency using their `currency` column. INR is always available at a rate of 1.

| Rule priority | Condition | Finding category |
| --- | --- | --- |
| 1 | Employee is terminated and license is active. | `terminated` |
| 2 | Active license has no usage row, blank login, or login older than `stale_days`. | `inactive` |
| 3 | Annual cost exceeds `cost_threshold_inr` and the active-day share is below `low_active_day_ratio`, distinct feature count is below `low_features_used_threshold`, or usage count is below `low_usage_threshold`. | `optimization` |

Active-day share is `active_days / usage_window_days`, so different observation windows can be compared. Rule settings may be overridden for one run with `--stale-days`, `--cost-threshold`, `--low-usage-threshold`, `--low-active-day-ratio`, `--low-features-used-threshold`, and `--usage-window-days`. `--as-of` makes stale checks and renewal calculations reproducible.

## Review workflow

First write findings to CSV so each candidate has a stable `finding_id`. Record or update a decision in the local ledger:

```bash
saas-sentry review set \
  --file reviews.csv \
  --finding-id 0123456789abcdef \
  --status confirmed \
  --owner "FinOps" \
  --note "Remove at renewal after owner sign-off" \
  --reviewed-on 2026-10-04
```

Valid statuses are `confirmed`, `dismissed`, and `deferred`. List saved decisions with `saas-sentry review list --file reviews.csv`. Include review state in a later analysis using `--review-file reviews.csv`; the findings CSV then carries status, owner, rationale, and review date. Each finding stores the latest decision; changing a decision replaces that finding's current ledger row.

## Savings estimates

The report separates three amounts:

- **Annualized candidate cost:** face value of all flagged seats, before contract limits.
- **Contract-adjusted annual opportunity:** highest-cost candidate seats up to the active-seat count above each contract's minimum.
- **Estimated realizable in next 12 months:** contract-adjusted opportunity prorated from each seat's next renewal date through the following year.

When a renewal date is absent, the estimate assumes a seat reduction can start on the analysis date. Costs are rounded to INR cents after conversion. Estimates do not model taxes, discounts, billing refunds, co-termination, or negotiated terms, and do not guarantee realized savings. Confirm findings with application owners and Finance before changing a subscription.

## Data quality and privacy

Use `--quality-csv data-quality.csv` to get row references and details for unmatched identities, conflicting IDs/emails, email collisions, and candidate seats blocked by contract minimums. Conflicting records are excluded from findings; an unknown ID paired with a matching email may still match by email and is reported as a warning. Correct upstream exports and rerun analysis.

SaaS Sentry reads the specified local CSVs and writes only requested outputs. It has no network/API integrations, database, or account-changing behavior. Do not use real employee data in public issues, examples, or pull requests.

## CI and development

Run the same core checks locally:

```bash
python -m compileall -q src tests
python -m unittest discover -s tests -v
saas-sentry analyze \
  --hr examples/hr.csv --usage examples/usage.csv --billing examples/billing.csv \
  --config config.example.toml --as-of 2026-10-04 \
  --output-csv findings.csv --quality-csv data-quality.csv
```

GitHub Actions runs these validations on Python 3.11, 3.12, and 3.13 for pushes and pull requests.

## Roadmap

Provider-specific Google Workspace, Microsoft 365, Slack, GitHub, Zoom, and Atlassian imports can be added on top of these canonical CSV contracts. Additional directions include richer contract proration and review history.

## License

MIT. See [LICENSE](LICENSE).
