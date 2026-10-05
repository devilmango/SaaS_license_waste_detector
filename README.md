# SaaS Sentry

**A local CLI for detecting unused SaaS licenses, reviewing recommendations, and estimating contract-aware savings.**

SaaS Sentry combines HR, usage, and billing data to flag terminated accounts, stale logins, and expensive low-use licenses. It produces transparent findings, data-quality diagnostics, historical comparisons, cost-center showback, and reports. CSV analysis stays local; provider connectors import data without changing provider state. Reclaim actions require separate approval and are recorded locally.

## Features

- Resolve identities using employee IDs, primary email, and email aliases.
- Report unmatched/conflicting identities for source-data cleanup.
- Apply configurable rules using last login, usage counts, active-day ratio, and feature counts.
- Convert costs into INR with explicit, dated exchange rates.
- Model contract seat minimums, price tiers, renewal dates, notice periods, multi-year commitments, and bundled products.
- Keep review decisions in a local CSV ledger.
- Save analysis snapshots and compare findings across runs.
- Generate CSV, JSON, and standalone HTML reports.
- Allocate active license spend and modeled savings to departments and cost centers.
- Build a contract renewal calendar with notice deadlines and upcoming actions.
- Normalize FinOps Open Cost and Usage Specification (FOCUS) invoice data by service and cost-center tag.
- Reconcile FOCUS invoice spend to assigned-license costs, flagging variances and unlicensed spend.
- Discover unmanaged SaaS applications and overlapping apps from a combined procurement/SSO/expense inventory.
- Forecast renewal costs with seat-growth, reduction, contract-floor, and vendor price-increase scenarios.
- Import Microsoft 365 directory users, assigned SKUs, and successful sign-in dates through read-only Graph access.
- Import Google Workspace, Slack, GitHub Copilot, and Zoom license/activity data through provider APIs.
- Run recurring reports in a foreground scheduler.
- Track proposed license reclaims through a separate-approver gate and compare realized savings with the estimate.

## Architecture

```text
HR CSV ─────────────────┐
Usage CSV or provider import ┼─> Validate + resolve identity ─> Rules + contract model
Billing CSV + price map ┘                                  ├─> Findings / quality CSV
                                                           ├─> JSON / HTML report
                                                           ├─> Cost-center showback + renewal calendar
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
  --showback-csv showback.csv \
  --renewals-csv renewals.csv \
  --snapshot-dir snapshots
```

`saas-sentry analyze --help` shows every option. Successful analysis exits `0`; invalid input or configuration exits `2`.

## Input formats

CSV column names are case-sensitive. Extra columns are accepted. Files may be UTF-8 or UTF-8 with BOM.

### HR roster (`--hr`)

Required headers: `employee,status`. Each row needs at least one of `email` or `employee_id`. Status is `active` or `terminated`. Optional `email_aliases` contains semicolon-separated addresses. Optional `department` and `cost_center` assign license spend and savings to an organizational owner; blank values appear as `Unassigned`.

```csv
employee,employee_id,email,email_aliases,department,cost_center,status
Alice,E001,alice@company.com,,Engineering,ENG-100,active
Bob,E002,bob@company.com,robert@company.com,Engineering,ENG-100,active
John,E003,john@company.com,,People,PEO-200,terminated
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

## Additional provider connectors

Each connector writes canonical `usage.csv`, `billing.csv`, and `import.json` files under the requested output directory. It only reads provider business data. Pricing is supplied locally because provider APIs do not expose negotiated annual seat costs. Use least-privilege credentials and store them in a secret manager or environment variables.

### Google Workspace

The connector reads Directory users and license assignments for product/SKU rows in the pricing map. It requires a bearer token in `GOOGLE_WORKSPACE_ACCESS_TOKEN`, authorized for Directory users read access and Enterprise License Manager. Note: Google's `apps.licensing` OAuth scope grants read/write capability, even though SaaS Sentry makes only GET requests. Protect this token as a privileged credential. Assignments are matched to Directory users by primary email. See the [Directory user resource](https://developers.google.com/workspace/admin/directory/reference/rest/v1/users) and [license assignment API](https://developers.google.com/workspace/admin/licensing/reference/rest/v1/licenseAssignments).

Pricing columns: `product_id,sku_id,application,annual_cost` plus optional contract fields from the billing schema.

```bash
export GOOGLE_WORKSPACE_ACCESS_TOKEN="<short-lived-token>"
saas-sentry connect google-workspace --pricing examples/google-workspace-pricing.csv \
  --output-dir imports/google-workspace
```

### Slack

Slack usage requires `users:read`, `users:read.email`, and `admin.analytics:read`, plus administrator access to daily member analytics. The connector retrieves one member-analytics file per day (90 days by default), derives the latest observed activity date, and counts activity days and events. This is an activity signal, not a login timestamp. Slack documents that `is_billable_seat` may be inaccurate for some self-serve payment arrangements; validate it against the invoice. Daily member analytics are available only on supported Business+/Enterprise plans, with history depending on plan age. See [Slack member analytics](https://api.slack.com/methods/admin.analytics.getFile).

```bash
export SLACK_ACCESS_TOKEN="<user-token>"
saas-sentry connect slack --pricing examples/slack-pricing.csv \
  --output-dir imports/slack --lookback-days 90 --as-of 2026-10-04
```

Use `--team-id` with an organization token. Pick an `--as-of` date for which analytics are available; the default is yesterday. Pricing is a single row with annual seat cost and optional contract fields.

### GitHub Copilot

This connector covers Copilot Business seats, not general GitHub organization membership. Create a token with read access to the organization's Copilot seats and set `GITHUB_TOKEN`. GitHub's seat response does not provide a dependable HR email, so the required identity map associates GitHub login to `email` and/or `employee_id`. Unmapped seats stop the import. The latest activity comes from `last_activity_at`; IDE activity appears only when telemetry is enabled, and GitHub documents this endpoint as public preview. See [Copilot seat management](https://docs.github.com/en/rest/copilot/copilot-user-management).

```bash
export GITHUB_TOKEN="<read-scoped-token>"
saas-sentry connect github-copilot --organization example-org \
  --pricing examples/github-copilot-pricing.csv \
  --identity-map examples/github-copilot-identities.csv \
  --output-dir imports/github-copilot
```

Identity map columns: `login,email,employee_id` and optional `employee`. Pricing uses one row with `annual_cost`, optional `application`, and contract fields.

### Zoom

Create a Server-to-Server OAuth app with the granular admin user-list read scope (`user:read:list_users:admin`) and set `ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`, and `ZOOM_CLIENT_SECRET`. The connector obtains an access token, then reads active users. It imports only user type `2` (licensed); basic and unassigned types are skipped. Zoom's `last_login_time` may have a three-day buffer. See the [Zoom users API](https://developers.zoom.us/docs/api/users/).

Pricing columns: `user_type,application,annual_cost` plus optional contract fields. The example maps type `2`.

```bash
export ZOOM_ACCOUNT_ID="<account-id>"
export ZOOM_CLIENT_ID="<client-id>"
export ZOOM_CLIENT_SECRET="<client-secret>"
saas-sentry connect zoom --pricing examples/zoom-pricing.csv --output-dir imports/zoom
```

Connectors are independent. Analyze provider exports with the same HR roster. Review product labels and contract IDs before combining datasets to avoid accidental collisions.

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

### Cost-center showback

`--showback-csv` writes one row per department and cost center with active seat count, annualized license spend, contract-adjusted annual opportunity, and estimated savings realizable in 12 months. The JSON and HTML reports include the same showback table. Spend is assigned from each active license to the employee's HR cost center; missing mappings are grouped under `Unassigned`. This is modeled assigned-license spend and should be reconciled to provider invoices.

### Renewal calendar

`--renewals-csv` writes one row per active contract with applications, renewal and commitment dates, the notice deadline, effective change date, current annualized spend, candidate count, and contract-adjusted opportunity. Status values are `scheduled`, `action_due_soon` (within 90 days), `missed_notice_next_cycle`, or `date_unknown`. The CSV is also embedded in JSON and HTML reports. If a notice date has passed, the planning estimate assumes the next annual cycle; this simplifying assumption is called out because vendor terms vary.

### FOCUS invoice spend

FOCUS exports represent billed charges and do not necessarily identify individual license holders. Import them as a complementary cost-center/service spend view rather than treating invoice rows as per-seat assignments. The importer uses standard FOCUS fields `ServiceName`, `BilledCost`, `BillingCurrency`, `BillingPeriodStart`, `BillingPeriodEnd`, and optional `ProviderName` and `Tags`. It reads the configured cost-center key from the JSON `Tags` column (default: `cost_center`), converts billed amounts to INR using the configured dated rates, then aggregates duplicates by provider, service, cost center, billing period, and source currency.

```bash
saas-sentry focus import \
  --input focus-billing.csv \
  --output focus-spend.csv \
  --config config.toml \
  --cost-center-tag cost_center
```

The output includes actual billed cost in INR, original billing currency, billing period, and exchange-rate date. It preserves negative adjustments/credits and groups charges with different currencies separately. See the [FOCUS specification](https://focus.finops.org/) for the interoperable cost and usage format.

### Invoice-to-license reconciliation

Normalize the FOCUS bill first, then compare actual spend with expected costs from active license assignments. The report is grouped by application, cost center, and billing period. It identifies above-tolerance variances, spend with no matching active licenses, and active-license spend with no corresponding invoice row. Expected annual seat cost is prorated to the billing-window length. The normalized FOCUS amounts and license costs must use the same currency configuration (INR in the normalized export).

```bash
saas-sentry focus import --input examples/focus-billing.csv \
  --output focus-spend.csv --config config.example.toml
saas-sentry reconcile --hr examples/hr.csv --usage examples/usage.csv \
  --billing examples/billing.csv --contract-pricing examples/contract-pricing.csv \
  --focus focus-spend.csv --config config.example.toml \
  --tolerance-percent 5 --output reconciliation.csv
```

The `status` column is `matched`, `variance`, `unlicensed_spend`, or `missing_invoice_spend`. The default tolerance is five percent of expected spend. Negative invoice adjustments such as credits are retained in the actual amount and variance.

### Shadow SaaS and overlapping apps

Combine application rows exported from sources such as SSO, procurement, and expense systems into one inventory CSV. Required columns are `application`, `vendor`, `category`, and `source`; `owner` is optional. Repeated app rows merge their vendors, owners, and sources. Apps absent from active license billing are marked `shadow_saas`; applications sharing a category are flagged as overlap candidates for human review. Category matching is exact after case and whitespace normalization, so use a consistent app taxonomy.

```csv
application,vendor,category,owner,source
Slack,Slack Technologies,Collaboration,Engineering,SSO
Mattermost,Mattermost Inc,Collaboration,Engineering,Expense
Unknown AI Tool,Example Inc,AI assistant,Product,Procurement
```

```bash
saas-sentry discover --hr examples/hr.csv --usage examples/usage.csv \
  --billing examples/billing.csv --inventory examples/application-inventory.csv \
  --config config.example.toml --output portfolio-discovery.csv
```

This report is a discovery aid: an inventory row alone does not establish that an app is unauthorized, redundant, or safe to retire.

### Renewal forecast scenarios

Project active contract costs at renewal with percentage assumptions for seat growth, planned seat reduction, and supplier price increases. The forecast applies contract seat minimums after growth and reduction. When contract price tiers are provided, it selects the tier covering the projected seat count; otherwise it uses the current average annual cost per active seat. Forecast percentages are entered as percent values (for example, `8` means 8%).

```bash
saas-sentry forecast --hr examples/hr.csv --usage examples/usage.csv \
  --billing examples/billing.csv --contract-pricing examples/contract-pricing.csv \
  --config config.example.toml --price-increase-percent 8 \
  --seat-growth-percent 12 --seat-reduction-percent 10 \
  --output renewal-forecast.csv
```

The CSV includes current spend, projected seats and spend before reductions, and the reduced-seat scenario. These are planning estimates based on supplied assumptions, not vendor quotes or guaranteed savings.

## Approval-based reclaim tracking

The action ledger is an append-only local CSV event log. A proposal requires a finding with `review_status=confirmed`. A different person must approve the proposed reclaim before it can be marked reclaimed. Completion records actual annual savings so the variance from the estimate can be reviewed. Rejections are recorded too. The workflow does not revoke provider licenses; all connectors remain import-only.

```bash
# Confirm the candidate in the review ledger, then export reviewed findings.
saas-sentry review set --file reviews.csv --finding-id 0123456789abcdef \
  --status confirmed --owner FinOps --note "Owner approved reclaim at renewal"
saas-sentry analyze --hr examples/hr.csv --usage examples/usage.csv \
  --billing examples/billing.csv --review-file reviews.csv --output-csv findings.csv

# Propose, approve as a different person, then record completion and actual savings.
saas-sentry actions propose --findings findings.csv --file actions.csv \
  --finding-id 0123456789abcdef --owner FinOps --proposed-by analyst@example.com
saas-sentry actions approve --file actions.csv --action-id <ACTION_ID> \
  --approved-by manager@example.com --note "Approved"
saas-sentry actions reclaimed --file actions.csv --action-id <ACTION_ID> \
  --reclaimed-by operator@example.com --actual-annual-savings 12000
saas-sentry actions list --file actions.csv
```

The action list reports status, owner, proposer, approver, estimated savings, actual savings, and variance. Keep the ledger access-controlled because it contains employee references and approver identities.

## Data quality and privacy

`--quality-csv` includes source, row, identity, issue, and remediation detail for unmatched/conflicting records, contract seat floors, and bundles. Conflicting identities are excluded; a unique employee ID or matching email may still resolve with a warning when the other identifier is unknown.

Local CSV analysis makes no network calls. Provider connectors require network access and do not mutate provider data. Google Workspace's license scope includes write permission even though this connector makes GET requests only. Review ledgers, action logs, snapshots, and reports may contain employee information; store them with appropriate access controls.

## CI and development

```bash
python -m compileall -q src tests
python -m unittest discover -s tests -v
saas-sentry analyze --hr examples/hr.csv --usage examples/usage.csv \
  --billing examples/billing.csv --contract-pricing examples/contract-pricing.csv \
  --config config.example.toml --as-of 2026-10-04 \
  --output-csv findings.csv --quality-csv data-quality.csv \
  --showback-csv showback.csv --renewals-csv renewals.csv
saas-sentry focus import --input examples/focus-billing.csv \
  --output focus-spend.csv --config config.example.toml
saas-sentry reconcile --hr examples/hr.csv --usage examples/usage.csv \
  --billing examples/billing.csv --focus focus-spend.csv --output reconciliation.csv
saas-sentry discover --hr examples/hr.csv --usage examples/usage.csv \
  --billing examples/billing.csv --inventory examples/application-inventory.csv \
  --output portfolio-discovery.csv
saas-sentry forecast --hr examples/hr.csv --usage examples/usage.csv \
  --billing examples/billing.csv --output renewal-forecast.csv
```

GitHub Actions runs package install, compilation, unit tests, and an example analysis on Python 3.11, 3.12, and 3.13.

## License

MIT. See [LICENSE](LICENSE).
