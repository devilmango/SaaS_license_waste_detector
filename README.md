# SaaS Sentry

**A local CLI for detecting unused SaaS licenses, reviewing recommendations, and estimating contract-aware savings.**

SaaS Sentry combines HR, usage, and billing data to flag terminated accounts, stale logins, and expensive low-use licenses. It produces transparent findings, data-quality diagnostics, historical comparisons, and reports. CSV analysis stays local; the optional Microsoft 365 connector makes read-only Microsoft Graph requests. SaaS Sentry never assigns or removes licenses.

## Features

- Resolve identities using employee IDs, primary email, and email aliases.
- Report unmatched/conflicting identities for source-data cleanup.
- Apply configurable rules using last login, usage counts, active-day ratio, and feature counts.
- Convert costs into INR with explicit, dated exchange rates.
- Model contract seat minimums, price tiers, renewal dates, notice periods, multi-year commitments, and bundled products.
- Keep review decisions in a local CSV ledger.
- Save analysis snapshots and compare findings across runs.
- Generate CSV, JSON, and standalone HTML reports.
- Import Microsoft 365 directory users, assigned SKUs, and successful sign-in dates through read-only Graph access.
- Run recurring reports in a foreground scheduler.

## Architecture

```text
HR CSV ─────────────────┐
Usage CSV or M365 import ┼─> Validate + resolve identity ─> Rules + contract model
Billing CSV + price map ┘                                  ├─> Findings / quality CSV
                                                           ├─> JSON / HTML report
                                                           ├─> Analysis snapshots
                                                           └─> Review ledger
```

## Requirements and installation

- Python 3.11 or newer
- No third-party runtime dependencies

```bash
git clone https://github.com/<OWNER>/saas-sentry.git
cd saas-sentry
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On PowerShell, activate with `.venv\Scripts\Activate.ps1`.

## Quick start

```bash
saas-sentry analyze \
  --hr examples/hr.csv \
  --usage examples/usage.csv \
  --billing examples/billing.csv \
  --contract-pricing examples/contract-pricing.csv \
  --config config.example.toml \
  --as-of 2026-10-04 \
  --output-csv findings.csv \
  --quality-csv data-quality.csv \
  --output-json report.json \
  --output-html report.html \
  --snapshot-dir snapshots
```

`saas-sentry analyze --help` shows every option. Successful analysis exits `0`; invalid input or configuration exits `2`.

## Input formats

CSV column names are case-sensitive. Extra columns are accepted. Files may be UTF-8 or UTF-8 with BOM.

### HR roster (`--hr`)

Required headers: `employee,status`. Each row needs at least one of `email` or `employee_id`. Status is `active` or `terminated`. Optional `email_aliases` contains semicolon-separated addresses.

```csv
employee,employee_id,email,email_aliases,status
Alice,E001,alice@company.com,,active
Bob,E002,bob@company.com,robert@company.com,active
John,E003,john@company.com,,terminated
```

### SaaS usage (`--usage`)

Required headers: `application,last_login`; each row needs `email` or `employee_id`. Login dates use `YYYY-MM-DD` or are blank.

| Optional column | Meaning |
| --- | --- |
| `usage_count` | Activity count during the observation window. |
| `active_days` | Days with activity during the observation window. |
| `usage_window_days` | Row-specific window; defaults to `usage.window_days`. |
| `features_used` | Count of distinct features used. |
| `employee` | Display name supplied by the source. |

```csv
employee_id,email,application,last_login,usage_count,active_days,usage_window_days,features_used
E001,alice@company.com,Slack,2026-09-28,42,25,30,5
E002,robert@company.com,Slack,2025-11-02,1,1,30,1
```

### Billing and contract details (`--billing`)

Required headers: `application,license_status,annual_cost`; each row needs `email` or `employee_id`. `annual_cost` is the annual per-user amount in its row currency or the configured default currency.

| Optional column | Meaning |
| --- | --- |
| `currency` | ISO currency code. |
| `contract_id` | Contract grouping key; defaults to the application. |
| `renewal_date` | Next contractual date for changing seats. |
| `seat_minimum` | Minimum active paid seats; repeat consistently for a contract. Defaults to zero. |
| `commitment_end_date` | End of a fixed or multi-year commitment. |
| `notice_days` | Advance notice required before the effective date. |
| `bundle_group` | Optional per-person group of bundled products. |

```csv
employee_id,email,application,license_status,annual_cost,currency,contract_id,renewal_date,seat_minimum,commitment_end_date,notice_days,bundle_group
E001,alice@company.com,Slack,active,12000,INR,slack-annual,2026-11-15,1,2026-11-15,30,
E002,robert@company.com,Slack,active,12000,INR,slack-annual,2026-11-15,1,2026-11-15,30,
```

`license_status` is `active` or `inactive`. Each person/application pair must be unique.

### Contract price tiers (`--contract-pricing`)

This optional CSV has one price row per seat-count tier. `max_seats` can be blank for the final, open-ended tier. Rates use the same currency conversion config as billing.

```csv
contract_id,min_seats,max_seats,annual_cost_per_seat,currency
slack-annual,1,10,12000,INR
slack-annual,11,,10000,INR
```

Tier ranges may not overlap and must cover current and projected seat counts. A tier schedule calculates the contract's current and projected total spend; without one, per-seat billing amounts are summed.

## Rules and configuration

Copy [config.example.toml](config.example.toml) and set your organization's policies:

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

`rates_to_inr` is INR per one unit of each currency; a `rate_date` is required when non-INR rates are configured. A billing row may specify a currency that overrides the default.

Rules are applied in priority order:

1. Terminated employee + active license → `terminated`.
2. Active license with no usage record, blank login, or login older than `stale_days` → `inactive`.
3. Annual cost above `cost_threshold_inr` and low active-day ratio, low feature count, or low usage count → `optimization`.

Thresholds can be overridden per run with `--stale-days`, `--cost-threshold`, `--low-usage-threshold`, `--low-active-day-ratio`, `--low-features-used-threshold`, and `--usage-window-days`. Active-day ratio is `active_days / usage_window_days`.

## Savings estimates

The report distinguishes:

- **Annualized candidate cost:** face value of all flagged licenses.
- **Contract-adjusted annual opportunity:** modeled reduction after seat minimums, price tiers, and bundle constraints.
- **Estimated realizable in 12 months:** prorated from the next date on which changes can take effect.

The effective date is the later of renewal and commitment end. If the notice deadline has passed, the estimate assumes the following annual renewal date. Missing dates assume changes can start on the analysis date. A bundle group only contributes savings when all active products for a person in that group are reclaimable.

These are planning estimates, not guaranteed cash savings. The model assumes annual renewal cadence when a notice deadline is missed and does not account for taxes, refunds, all discounts, or every negotiated contract clause. Confirm findings with Finance and product owners before action.

## Review workflow

Findings have stable IDs. Set a review state, owner, note, and date in a local CSV ledger:

```bash
saas-sentry review set --file reviews.csv \
  --finding-id 0123456789abcdef --status confirmed \
  --owner FinOps --note "Reclaim at renewal" --reviewed-on 2026-10-04
saas-sentry review list --file reviews.csv
```

Statuses are `confirmed`, `dismissed`, and `deferred`. Add `--review-file reviews.csv` to analysis to include saved decisions in the findings CSV and report. The ledger retains the latest decision per finding; updates replace that row.

## Microsoft 365 connector

`connect microsoft365` reads directory users, assigned license SKUs, and last successful sign-in through Microsoft Graph, then writes canonical `usage.csv` and `billing.csv`. Sign-in is a user-level signal repeated for each assigned SKU, not per-workload activity. Negotiated prices and contract terms are supplied in a local price map because Graph does not provide those values.

Register an Entra application with admin consent for application permissions `User.Read.All`, `LicenseAssignment.Read.All`, and `AuditLog.Read.All`. Microsoft documents that `signInActivity` requires `AuditLog.Read.All` and an Entra ID P1/P2 license; the user list is limited to a maximum page size of 500 when selecting sign-in activity. See [list users](https://learn.microsoft.com/en-us/graph/api/user-list?view=graph-rest-1.0), [sign-in activity](https://learn.microsoft.com/en-us/graph/api/resources/signinactivity?view=graph-rest-1.0), [list subscribed SKUs](https://learn.microsoft.com/en-us/graph/api/subscribedsku-list?view=graph-rest-1.0), and [client credentials](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow).

Put credentials in environment variables or a secret manager, never in repository files:

```bash
export M365_TENANT_ID="<tenant-id>"
export M365_CLIENT_ID="<application-id>"
export M365_CLIENT_SECRET="<secret>"
saas-sentry connect microsoft365 --pricing m365-pricing.csv --output-dir imports/m365
```

The pricing CSV requires `sku_part_number,annual_cost`; optional fields are `currency,contract_id,renewal_date,seat_minimum,commitment_end_date,notice_days,bundle_group`. Every assigned SKU must have a price mapping. The connector stops without writing outputs if sign-in activity is unavailable or any assigned SKU has no price.

Analyze the imported files with your HR roster:

```bash
saas-sentry analyze --hr hr.csv --usage imports/m365/usage.csv \
  --billing imports/m365/billing.csv --config config.toml \
  --output-csv findings.csv --quality-csv data-quality.csv
```

## Snapshots, exports, and scheduling

`--snapshot-dir` saves findings, analysis date, and source/config fingerprints. Snapshots contain employee details; keep them private. List and compare runs by filename/prefix or `latest`:

```bash
saas-sentry history list --dir snapshots
saas-sentry history compare --dir snapshots --baseline latest --current 20261004
```

`--output-json` writes structured data and `--output-html` creates a standalone report with no remote scripts or assets. For recurring analysis, use the foreground scheduler, which runs immediately and repeats until stopped:

```bash
saas-sentry watch --hr hr.csv --usage usage.csv --billing billing.csv \
  --contract-pricing contract-pricing.csv --config config.toml \
  --output-dir reports --every-hours 24
```

The scheduler expects input exports to be refreshed separately and can run under a process supervisor or host scheduler.

## Data quality and privacy

`--quality-csv` includes source, row, identity, issue, and remediation detail for unmatched/conflicting records, contract seat floors, and bundles. Conflicting identities are excluded; a unique employee ID or matching email may still resolve with a warning when the other identifier is unknown.

Local CSV analysis makes no network calls. The Microsoft 365 connector requires network access and performs token and Graph GET requests only. Review ledgers, snapshots, and reports may contain employee information; store them with appropriate access controls.

## CI and development

```bash
python -m compileall -q src tests
python -m unittest discover -s tests -v
saas-sentry analyze --hr examples/hr.csv --usage examples/usage.csv \
  --billing examples/billing.csv --contract-pricing examples/contract-pricing.csv \
  --config config.example.toml --as-of 2026-10-04 \
  --output-csv findings.csv --quality-csv data-quality.csv
```

GitHub Actions runs package install, compilation, unit tests, and an example analysis on Python 3.11, 3.12, and 3.13.

## License

MIT. See [LICENSE](LICENSE).
