# SaaS Sentry

**A local CLI for detecting unused SaaS licenses, reviewing recommendations, and estimating contract-aware savings.**

SaaS Sentry combines HR, usage, contract, and billing data to flag terminated accounts, stale logins, expensive low-use licenses, and contract entitlement exposure. It produces transparent findings, data-quality diagnostics, historical comparisons, cost-center showback, anomaly alerts, and reports. Provider imports are read-only. License reclaim execution is a separate, explicitly enabled workflow requiring an approved action.

## Features

- Resolve identities using employee IDs, primary email, and email aliases.
- Report unmatched/conflicting identities for source-data cleanup.
- Apply configurable rules using last login, usage counts, active-day ratio, and feature counts.
- Convert costs into INR with explicit, dated exchange rates.
- Model contract seat minimums, price tiers, renewal dates, notice periods, multi-year commitments, and bundled products.
- Keep review decisions in a local CSV ledger.
- Store action and access-review event ledgers in a shared SQLite database with transactional writes, or migrate existing CSV ledgers.
- Save analysis snapshots and compare findings across runs.
- Generate CSV, JSON, and standalone HTML reports.
- Allocate active license spend and modeled savings to departments and cost centers.
- Build a contract renewal calendar with notice deadlines and upcoming actions.
- Normalize FinOps Open Cost and Usage Specification (FOCUS) invoice data by service and cost-center tag.
- Reconcile FOCUS invoice spend to assigned-license costs, flagging variances and unlicensed spend.
- Discover unmanaged SaaS applications and overlapping apps from a combined procurement/SSO/expense inventory.
- Forecast renewal costs with seat-growth, reduction, contract-floor, and vendor price-increase scenarios.
- Monitor report snapshots for material spend/seat shifts, renewal deadlines, seat-floor exposure, and invoice anomalies.
- Execute approved Microsoft 365, Google Workspace, Slack, and Zoom license reclaims and GitHub Copilot seat cancellations with preview-by-default and an idempotent audit ledger.
- Compare assigned licenses with purchased contract entitlements to identify seat overages, unused seats, and missing entitlement records.
- Run owner-assigned access-review campaigns with due dates, cooldown-aware reminder queues, owner-grouped webhook delivery, and decisions linked to the review ledger.
- Model shared seat pools, volume price tiers, and prorated charges from effective-dated mid-term seat, entitlement, and price changes.
- Deliver monitor alerts to an HTTPS JSON webhook with preview-by-default and a local deduplication ledger.
- Report source freshness, identity-match coverage, import quality issues, and provider metadata consistency.
- Import Entra ID and SCIM directory records, including active state and app/group access, then compare provisioning with HR status.
- Keep versioned contract terms linked to agreement/quote/invoice evidence, require separate approval, and reconcile terms against FOCUS invoices.
- Compare realized annual savings with estimates and break results down by owner, app, department, cost center, and month.
- Compare reclaimed-license estimates with actual invoice spend for the matching app and cost center.
- Import Microsoft 365 directory users, assigned SKUs, and successful sign-in dates through read-only Graph access.
- Import Google Workspace, Slack, GitHub Copilot, and Zoom license/activity data through provider APIs.
- Run recurring reports in a foreground scheduler.
- Schedule recurring provider imports followed by analysis, timestamped reports, and optional reclaim verification.
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

For import, register an Entra application with admin consent for application permissions `User.Read.All`, `LicenseAssignment.Read.All`, and `AuditLog.Read.All`. Microsoft documents that `signInActivity` requires `AuditLog.Read.All` and an Entra ID P1/P2 license; the user list is limited to a maximum page size of 500 when selecting sign-in activity. See [list users](https://learn.microsoft.com/en-us/graph/api/user-list?view=graph-rest-1.0), [sign-in activity](https://learn.microsoft.com/en-us/graph/api/resources/signinactivity?view=graph-rest-1.0), [list subscribed SKUs](https://learn.microsoft.com/en-us/graph/api/subscribedsku-list?view=graph-rest-1.0), and [client credentials](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow).

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

FOCUS exports represent billed charges and do not necessarily identify individual license holders. Import them as a complementary cost-center/service spend view rather than treating invoice rows as per-seat assignments. The importer uses standard FOCUS fields `ServiceName`, `BilledCost`, `BillingCurrency`, `BillingPeriodStart`, `BillingPeriodEnd`, and optional `ProviderName` and `Tags`. It reads cost-center and contract ID keys from the JSON `Tags` column (defaults: `cost_center` and `contract_id`), converts billed amounts to INR using the configured dated rates, then aggregates duplicates by provider, service, cost center, contract ID, billing period, and source currency.

```bash
saas-sentry focus import \
  --input focus-billing.csv \
  --output focus-spend.csv \
  --config config.toml \
  --cost-center-tag cost_center --contract-id-tag contract_id
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

The action ledger is an append-only local CSV event log. A proposal requires a finding with `review_status=confirmed`. A different person must approve the proposed reclaim before it can be marked reclaimed. Completion records actual annual savings so the variance from the estimate can be reviewed. Rejections are recorded too. Provider execution is a separate opt-in command with its own execution ledger.

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

### Shared SQLite event ledgers

Action and access-review event ledgers accept `.db`, `.sqlite`, or `.sqlite3` paths. Their events live in separate tables, so the same database file can be passed to both workflows. SQLite transactions serialize concurrent writes and validate a state change against the current event history before appending it. CSV remains supported for simple local workflows.

```bash
# Use one database for action and campaign events.
saas-sentry actions propose --findings findings.csv --file saas-sentry.sqlite \
  --finding-id 0123456789abcdef --owner FinOps --proposed-by analyst@example.com
saas-sentry access-review create --findings findings.csv --ledger saas-sentry.sqlite \
  --due-on 2026-11-01 --created-by finops@example.com --default-owner app-owner@example.com

# Migrate an existing, validated CSV event ledger once.
saas-sentry ledger migrate --kind actions --source actions.csv --database saas-sentry.sqlite
```

The migration refuses to overwrite an existing table of the same ledger type. Review access to the database file as it contains employee and approver data.

### Savings realization report

The action ledger records estimated and actual annual savings when a reclaim is completed. Generate a CSV report to compare those amounts, see the realization percentage and variance, track estimated savings still in the proposed/approved pipeline, and group results by owner, application, department, cost center, and completion month:

```bash
saas-sentry realization --actions actions.csv --findings findings.csv \
  --output savings-realization.csv
```

Only reclaimed actions contribute to actual-versus-estimated realization. Pending actions appear separately as pipeline estimates; rejected actions remain visible in counts. Completion month uses the reclaimed event date recorded in the action ledger. Actual savings are user-entered annualized estimates and should be reconciled against post-change invoices or renewal charges.

### Import health and identity coverage

Check HR, usage, and billing source row counts, match coverage, quality issues, and freshness. With provider `import.json` metadata, the report also checks that the imported license-assignment count agrees with the billing CSV. The default stale threshold is seven days; override it for the cadence used by your exports.

```bash
saas-sentry import-health --hr examples/hr.csv --usage examples/usage.csv \
  --billing examples/billing.csv --output import-health.csv

# Provider export folders also contain import.json:
saas-sentry import-health --hr examples/hr.csv --usage imports/slack/usage.csv \
  --billing imports/slack/billing.csv --metadata imports/slack/import.json \
  --stale-after-days 3 --output slack-import-health.csv
```

Freshness uses provider `imported_at` for usage/billing when metadata is supplied; otherwise it uses each CSV file's modification date. HR freshness always uses the HR CSV modification date. Identity coverage is the percentage of rows that resolve uniquely through primary email, aliases, or employee ID. The report surfaces missing/unmatched identities, collisions, malformed source values, and duplicate app/user rows as issues; it does not modify source files.

### Identity directory and SCIM reconciliation

Import Entra ID directory users through the existing Microsoft Graph app credentials or pull users from a SCIM 2.0 service. Entra app access is represented by assigned SKU IDs; SCIM access is represented by group memberships. SCIM requests require HTTPS and use `SAAS_SENTRY_SCIM_TOKEN`; set `SAAS_SENTRY_SCIM_BASE_URL` or pass the service root explicitly.

```bash
saas-sentry identity import-entra --output entra-identities.csv
saas-sentry identity import-scim --base-url https://id.example.com/scim/v2 \
  --output scim-identities.csv
saas-sentry identity reconcile --hr examples/hr.csv --identities scim-identities.csv \
  --output identity-review.csv
```

The reconciliation flags terminated employees with active app access, active HR records that are deprovisioned, ambiguous identity matches, and directory users missing from HR. It writes a review report and does not change accounts or access.

### Monitoring anomalies and contract limits

Compare a current JSON analysis report with a baseline to flag material spend or seat-count changes by cost center and contract. The monitor also flags contracts below their committed seat floor, reclaim candidates blocked by contract minimums, approaching notice deadlines, and any non-matching status in a reconciliation CSV.

```bash
saas-sentry monitor --current report.json --baseline previous-report.json \
  --reconciliation reconciliation.csv --spend-change-threshold-percent 20 \
  --seat-change-threshold 2 --renewal-window-days 60 --output anomalies.csv
```

Baseline comparison is optional. `--current` and `--baseline` accept JSON report files or saved snapshots. The output is an alert CSV; findings are signals for review and do not trigger provider changes.

### Guarded provider reclaim execution

Provider imports preserve target identifiers needed to resolve a provider action. The action must already be approved by someone other than its proposer, and its finding must still be confirmed. `actions execute` is preview-only unless `--execute` is explicitly supplied. Execution uses an idempotency ledger; if a request has an ambiguous outcome, a different operator must reconcile provider state before marking it completed or enabling a retry:

```bash
# Preview first: no credentials or provider request are needed for this step.
saas-sentry actions execute --findings findings.csv --billing imports/m365/billing.csv \
  --file actions.csv --execution-ledger execution-events.csv \
  --action-id <ACTION_ID> --provider microsoft365 --executed-by operator@example.com

# Only after reviewing the preview, explicitly perform the provider change.
saas-sentry actions execute --findings findings.csv --billing imports/m365/billing.csv \
  --file actions.csv --execution-ledger execution-events.csv \
  --action-id <ACTION_ID> --provider microsoft365 --executed-by operator@example.com --execute
```

Microsoft 365 execution requires Graph application permission `LicenseAssignment.ReadWrite.All` with admin consent. It re-reads the assignment, checks `licenseAssignmentStates`, and refuses group-assigned or unverifiable licenses. Google Workspace execution uses the [License Manager delete endpoint](https://developers.google.com/workspace/admin/licensing/reference/rest/v1/licenseAssignments/delete) to verify and revoke the exact product/SKU assignment; its credential needs the appropriate licensing write scope. GitHub Copilot execution calls the [selected-users cancellation endpoint](https://docs.github.com/en/rest/copilot/copilot-user-management) for one organization username; GitHub marks the seat for cancellation at the end of the current billing cycle, and team-assigned seats may be refused by the API. Grant execution credentials only to a narrowly scoped execution app and keep them in a secret manager. Refresh provider exports and verify the resulting billing state before recording realized savings.

```bash
saas-sentry actions resolve-execution --file actions.csv \
  --execution-ledger execution-events.csv --action-id <ACTION_ID> \
  --provider microsoft365 --resolved-as not-applied \
  --resolved-by reviewer@example.com --note "Refreshed tenant state confirms the SKU remains assigned"
```

Use `--resolved-as completed` when the tenant confirms the license was removed; then refresh exports and record actual savings. `not-applied` permits another attempt with the same action idempotency key. A reconciliation must be recorded by someone other than the last execution actor.

Supported providers are selected with `--provider microsoft365`, `--provider google_workspace`, `--provider github_copilot`, `--provider slack`, or `--provider zoom`. Previewing does not require provider credentials. Provider mutations require `--execute`.

Slack actions remove a standard member from a Slack Enterprise Grid workspace through [`admin.users.remove`](https://api.slack.com/methods/admin.users.remove), which requires the `admin.users:write` scope and a workspace/team ID. The importer records the workspace ID when available; otherwise pass `--team-id` during import. Zoom actions re-read the user and change the account type from Licensed (`2`) to Basic (`1`) through the [Zoom Update User API](https://developers.zoom.us/docs/api/users/). Both adapters verify the live target before mutation and skip or refuse changed account states.

After a provider sync, verify executed actions against its complete `billing.csv` snapshot. The command uses the sibling `import.json` metadata to check the provider and assignment count, then appends each result to a verification event ledger and writes a CSV for follow-up:

```bash
saas-sentry actions verify-provider --file actions.csv \
  --execution-ledger execution-events.csv --findings findings.csv \
  --billing imports/microsoft365/billing.csv --provider microsoft365 \
  --verification-ledger reclaim-verification-events.csv \
  --output reclaim-verification.csv
```

`verified_reclaimed` means the exact provider user/license assignment is absent from the complete refreshed snapshot. `still_assigned`, `assignment_changed`, and `identity_mismatch` remain open for operator review and include a follow-up instruction. This records provider state without automatically marking the approval action reclaimed; refresh billing and enter realized savings separately after invoice review.

### Verify savings against invoices

`verify-savings` compares normalized FOCUS invoice exports from two non-overlapping billing periods. It joins reclaimed action records to findings by `finding_id`, then groups billed cost by application and cost center. The current invoice file must be a complete export for its period; if an application/cost-center pair is absent from it, the command treats its spend as zero. The observed reduction is the difference in daily spend rates annualized to 365 days.

```bash
saas-sentry focus import --input examples/focus-billing.csv \
  --output focus-before.csv --config config.example.toml
# Normalize the next period's vendor invoice export to the same FOCUS schema as focus-before.csv.
saas-sentry verify-savings --actions actions.csv --findings findings.csv \
  --baseline-invoice focus-before.csv --current-invoice focus-after.csv \
  --minimum-realization-percent 80 --output invoice-verification.csv
```

The report includes baseline/current spend, modeled annual savings, annualized invoice reduction, variance, realization percentage, and a status. `verified` means the observed reduction meets the configured percentage; `partial`, `spend_increased`, and `no_reduction_observed` identify other outcomes. This is app/cost-center level evidence, not proof that a particular reclaim caused the entire invoice change: price changes, seat changes, credits, and unrelated usage can affect the same spend group. Review billing details before recording realized savings.

### Scheduled provider syncs

Run provider import and analysis once with `--once`, or keep the process running with `--every-hours`. Each cycle writes refreshed `usage.csv`, `billing.csv`, and provider metadata under the output directory, then creates a timestamped analysis/report directory and snapshot history. Scheduling runs in the foreground; use a process supervisor or scheduler for persistent operation. Add both action and execution ledgers to verify completed reclaims after each fresh import:

```bash
saas-sentry sync --provider microsoft365 --hr hr.csv --pricing m365-pricing.csv \
  --output-dir imports/microsoft365 --action-ledger actions.csv \
  --execution-ledger execution-events.csv --once
```

Each cycle writes `reports/<run-id>/reclaim-verification.csv` and appends outcomes to `reclaim-verification-events.csv` under the sync output directory by default. Use `--verification-ledger` to choose a different event-log path. Both ledgers are optional, but must be supplied together. A changed assignment stays in the report as a follow-up; it is never automatically retried or marked reclaimed.

```bash
saas-sentry sync --provider microsoft365 --hr examples/hr.csv \
  --pricing examples/provider-pricing.csv --contract-pricing examples/contract-pricing.csv \
  --config config.example.toml --output-dir scheduled/microsoft365 \
  --every-hours 24 --once
```

Available providers are `microsoft365`, `google-workspace`, `slack`, `github-copilot`, and `zoom`. Provider credentials come from the same environment variables as their `connect` commands. GitHub Copilot sync also requires `--organization` and `--identity-map`; Slack accepts `--team-id`. Each cycle performs a fresh provider import, so API permissions, rate limits, and source-data freshness apply. For an operator-managed scheduled analysis that uses already-imported CSVs without calling a provider, use `watch`.

### Contract entitlement and overage tracking

Keep purchased seat counts in a contract CSV with `contract_id,application,entitled_seats`. The command compares that entitlement with active assignments in the billing export. Overages are priced using the mean annual cost per active assignment for the contract/application pair; missing entitlement mappings are surfaced as `missing_entitlement` rather than omitted.

```bash
saas-sentry entitlements --billing examples/billing.csv \
  --contracts examples/contract-entitlements.csv --output entitlements.csv
```

The report includes active and entitled seats, overage seats, unused entitlement seats, and estimated annual overage exposure. It is a reconciliation signal: reconcile contract line items and billing terms with procurement before changing a subscription.

For contracts where multiple applications draw from one purchased seat pool, add an optional `pool_id` column to the entitlement CSV and repeat the same `entitled_seats` value for each application in that pool. Active identities are counted once per pool. Billing rows can include an optional `seat_pool_id` when the application-to-pool mapping is not represented in the entitlement file. Optional `min_seats,max_seats` columns define entitlement tiers by the pool's active seat count; repeat the same tier schedule for every application in a shared pool. `contract-pricing.csv` continues to define the separate price-per-seat tiers.

### Owner access-review campaigns

Create an assignment campaign from unreviewed findings. Existing `review_owner` values are used; an optional `application,owner` CSV maps apps to owners, and `--default-owner` fills any remaining blanks. Decisions are restricted to the assigned owner and are written to both the campaign event ledger and the existing `reviews.csv` ledger, so analysis and reclaim approval continue to use the same review state.

```bash
saas-sentry access-review create --findings findings.csv \
  --ledger access-review-events.csv --due-on 2026-11-01 \
  --created-by finops@example.com --owner-map application-owners.csv \
  --default-owner app-owner@example.com
saas-sentry access-review list --ledger access-review-events.csv
saas-sentry access-review reminders --ledger access-review-events.csv \
  --as-of 2026-10-25 --within-days 7 --minimum-gap-days 7 \
  --output owner-reminders.csv
saas-sentry access-review decide --ledger access-review-events.csv \
  --reviews reviews.csv --campaign-id <CAMPAIGN_ID> --finding-id <FINDING_ID> \
  --decision confirmed --reviewed-by app-owner@example.com --note "Still required"
```

The reminders command writes a local queue and records when each queue entry was generated. Deliver the queue to an owner-grouped HTTPS JSON webhook with a preview-first command:

```bash
saas-sentry access-review deliver-reminders --reminders owner-reminders.csv \
  --ledger access-review-deliveries.csv \
  --webhook-url https://hooks.example.com/saas-sentry/reviews
# Review the preview, then opt in to network delivery:
saas-sentry access-review deliver-reminders --reminders owner-reminders.csv \
  --ledger access-review-deliveries.csv \
  --webhook-url https://hooks.example.com/saas-sentry/reviews --send
```

Set `SAAS_SENTRY_REVIEW_WEBHOOK_URL` instead of passing `--webhook-url`; optionally set `SAAS_SENTRY_REVIEW_WEBHOOK_TOKEN` for bearer authentication. Each owner receives one JSON payload containing that owner's reminders. The delivery ledger records per-finding success or failure, skips successful reminders on later runs, and retries failed deliveries. Reminder generation still respects the configured minimum gap; overdue items remain eligible until an owner decides them.

### Versioned contract evidence and renewal checks

Record each agreement, order form, quote, invoice, or amendment as a numbered contract version. Terms include the covered applications, effective and renewal dates, notice period, annual commitment entered in INR, responsible owner, and a reference to the source document. A different operator must approve the latest version before it is considered approved. The ledger is append-only; later terms create a new version.

```bash
saas-sentry contracts record --ledger contract-events.csv \
  --contract-id slack-enterprise --vendor "Slack Inc" --owner finops@example.com \
  --application Slack --effective-date 2026-01-01 --renewal-date 2027-01-01 \
  --notice-days 60 --annual-commitment-inr 120000 --currency USD \
  --evidence-type order_form --evidence-ref evidence/slack-order-form.pdf \
  --recorded-by procurement@example.com
saas-sentry contracts approve --ledger contract-events.csv \
  --contract-id slack-enterprise --approved-by finance@example.com --note "Terms reviewed"
saas-sentry contracts list --ledger contract-events.csv
saas-sentry focus import --input invoice-focus.csv --output invoice-normalized.csv \
  --config config.example.toml
saas-sentry contracts reconcile --ledger contract-events.csv \
  --invoice invoice-normalized.csv --tolerance-percent 5 --output contract-invoice-check.csv
```

Invoice checks compare the latest contract version's annual commitment, prorated across the invoice period, with actual normalized FOCUS spend. Pending versions remain marked `pending_approval`. When the FOCUS `Tags` data includes a contract identifier, pass `--contract-id-tag` during import to keep spend distinct even when multiple contracts cover the same service. Without contract tags, application names must map to only one contract. The report includes over/under commitment status, variance, renewal date, notice deadline, and evidence reference. This is a reconciliation signal; review tax, credits, discounts, and the source document before changing a subscription.

### Seat reassignment planning

Match a confirmed reclaim finding to an employee request for the same application and contract. The planner requires `review_status=confirmed`, verifies that the source finding still points to an active billed seat, and checks that the requester is active in HR and does not already hold that seat. It prioritizes high, then normal, then low requests and proposes each source seat at most once.

Create a request export with `employee_id,email,application,contract_id,priority,requested_on,reason` columns. Use the exact contract ID from billing so a seat is not moved across contract pools. Generate findings with the normal `analyze --output-csv` command after recording a review decision as confirmed.

```csv
employee_id,email,application,contract_id,priority,requested_on,reason
E010,new.hire@company.com,Slack,slack-enterprise,high,2026-10-01,New hire needs a collaboration seat
```

```bash
saas-sentry reassign --hr hr.csv --usage usage.csv --billing billing.csv \
  --findings findings.csv --demand license-demand.csv \
  --config config.example.toml --output reassignment-plan.csv
```

The output includes matched proposals, reclaim supply without a matching request, and requests without a matching confirmed seat. It is a planning report; it does not change provider assignments. Review contract terms and then execute an approved change through the existing action workflow.

### Procurement and AP invoice imports

Normalize exported CSV invoice lines from Coupa, NetSuite, Ariba, or the canonical schema into the same invoice columns used by contract reconciliation. Export headings vary by tenant; the importer recognizes common heading aliases and reports missing required fields. Required values are vendor, application/service, contract ID, invoice ID, billing period start and end, amount, and currency. Optional owner, cost center, renewal date, notice period, and evidence link are carried through for finance follow-up.

```bash
saas-sentry procurement import --system coupa --input coupa-invoices.csv \
  --config config.example.toml --output normalized-invoices.csv
saas-sentry contracts reconcile --ledger contract-events.csv \
  --invoice normalized-invoices.csv --output contract-invoice-check.csv
```

Amounts are converted to INR using the dated rates in the TOML configuration; non-INR inputs fail when a rate is missing. The original currency remains in `source_currency`. These adapters consume system exports and make no network calls. For a custom export, select `--system canonical` and use the normalized field names, or adjust the export headings to one of the recognized aliases.

### Renewal owner and budget approvals

Create an append-only renewal campaign from the latest version of each contract when its notice deadline falls within the selected horizon or is already overdue. The assigned contract owner records a recommendation with evidence; a different budget approver then approves or rejects it with a separate evidence reference.

```bash
saas-sentry renewals create --contracts contract-events.csv \
  --ledger renewal-events.csv --as-of 2026-10-01 --horizon-days 90 \
  --created-by finops@example.com
saas-sentry renewals list --ledger renewal-events.csv --as-of 2026-10-01 \
  --output renewal-audit.csv
saas-sentry renewals decide --ledger renewal-events.csv \
  --contract-id slack-enterprise --version 1 --decision renegotiate \
  --decided-by app-owner@example.com --evidence-ref evidence/renewal-review.pdf
saas-sentry renewals approve --ledger renewal-events.csv \
  --contract-id slack-enterprise --version 1 --status approved \
  --approved-by finance@example.com --evidence-ref evidence/budget-approval.pdf
```

The exported audit report identifies pending owner decisions, overdue notice deadlines, pending budget approvals, and completed or rejected decisions. Campaign events retain the contract version, owner, dates, decision actors, notes, and evidence references. Creating a campaign is idempotent for a given contract version.

### Shared pools and prorated contract changes

`contract-model` projects charges for an invoice period from active billing assignments, fixed or tiered seat entitlements, the existing `contract-pricing.csv` volume tiers, and a dated change log. Add `pool_id` to entitlement rows to share entitlements across applications. Active identities are deduplicated within each pool. Change events support `seat_add`, `seat_remove`, `entitlement_change`, and `price_change`; seat and entitlement events target a `pool_id` when the contract has multiple pools. Price changes apply contract-wide from their effective date for the rest of the modeled period. Changes outside the selected invoice period are ignored.

```csv
contract_id,pool_id,effective_date,event_type,seat_delta,entitlement_delta,annual_cost_per_seat,currency
shared-contract,collaboration,2026-10-15,seat_add,1,,,
shared-contract,collaboration,2026-10-15,entitlement_change,,1,,
shared-contract,,2026-10-20,price_change,,,,9500,INR
```

```bash
saas-sentry contract-model --billing billing.csv --contracts contract-entitlements.csv \
  --contract-pricing contract-pricing.csv --changes contract-changes.csv \
  --period-start 2026-10-01 --period-end 2026-11-01 \
  --config config.toml --output contract-period.csv
```

The end date is exclusive. Charges are calculated per effective-date segment using the matching tier's annual per-seat rate, the contractual seat minimum, and a 365-day proration basis. The report shows opening/ending active and entitled seats, ending overage, start/end annualized run rates, and the modeled period charge. This is a planning estimate; reconcile the schedule and proration convention with negotiated contract language and the vendor invoice.

### Webhook alert delivery

Generate an anomaly CSV with `monitor`, then preview the newly eligible alerts before delivery. The generic HTTPS webhook receives one JSON object containing `source` and an `alerts` array. Successful deliveries are recorded in the local ledger and duplicate alert rows are skipped on subsequent runs. A changed alert (for example, changed current value or summary) has a different key and can be delivered again.

```bash
saas-sentry notify --alerts anomalies.csv --ledger alert-deliveries.csv \
  --webhook-url https://hooks.example.com/saas-sentry
# After reviewing the preview, explicitly deliver:
SAAS_SENTRY_WEBHOOK_TOKEN="<secret>" saas-sentry notify \
  --alerts anomalies.csv --ledger alert-deliveries.csv \
  --webhook-url https://hooks.example.com/saas-sentry --send
```

The URL must use HTTPS and cannot embed credentials. The optional bearer token is read from `SAAS_SENTRY_WEBHOOK_TOKEN`. A delivery is marked only after a successful 2xx response; keep the ledger durable to preserve deduplication across runs.

## Data quality and privacy

`--quality-csv` includes source, row, identity, issue, and remediation detail for unmatched/conflicting records, contract seat floors, and bundles. Conflicting identities are excluded; a unique employee ID or matching email may still resolve with a warning when the other identifier is unknown.

Local CSV analysis, alert previews, and execution previews make no network calls. Provider imports are read-only; explicitly executed reclaim actions and webhook deliveries send network requests only after the corresponding opt-in flags. Review ledgers, execution logs, snapshots, reports, and webhook payloads may contain employee information; store them with appropriate access controls.

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
