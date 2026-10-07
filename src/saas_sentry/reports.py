"""Portable reports and saved analysis snapshots."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import html
import json
from pathlib import Path
from typing import Any

from .engine import Analysis


def _file_hash(path: Path | None) -> str | None:
    if path is None:
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError(f"Cannot fingerprint input {path}: {exc}") from exc
    return digest.hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def report_payload(
    analysis: Analysis,
    *,
    as_of: date,
    source_paths: list[Path],
    config_path: Path | None = None,
    reviews: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    reviews = reviews or {}
    findings = []
    for finding in analysis.findings:
        item = _jsonable(finding.__dict__)
        item["review"] = reviews.get(finding.finding_id, {"status": "unreviewed"})
        findings.append(item)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of.isoformat(),
        "active_license_count": analysis.active_license_count,
        "currency_rate_date": analysis.currency_rate_date.isoformat() if analysis.currency_rate_date else None,
        "totals_inr": {
            "annualized_candidate_cost": format(sum((f.annualized_cost_inr for f in analysis.findings), Decimal("0")), "f"),
            "contract_adjusted_annual_opportunity": format(sum((f.opportunity_savings_inr for f in analysis.findings), Decimal("0")), "f"),
            "estimated_realizable_12m": format(sum((f.realizable_12m_inr for f in analysis.findings), Decimal("0")), "f"),
        },
        "source_fingerprints": {str(path): _file_hash(path) for path in source_paths},
        "config_fingerprint": _file_hash(config_path),
        "findings": findings,
        "cost_center_showback": [_jsonable(row.__dict__) for row in analysis.showback],
        "renewal_calendar": [_jsonable(row.__dict__) for row in analysis.renewals],
        "data_quality_issues": [_jsonable(issue.__dict__) for issue in analysis.issues],
    }


def write_snapshot(directory: Path, payload: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    suffix = hashlib.sha256(canonical).hexdigest()[:10]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{timestamp}-{suffix}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def render_html(payload: dict[str, Any]) -> str:
    """Render a standalone, escaped HTML report without remote assets."""
    totals = payload["totals_inr"]
    rows = []
    for finding in payload["findings"]:
        fields = (
            finding["application"], finding["employee"], finding["email"],
            finding["category"], finding["reason"], finding["evidence"],
            f"₹{Decimal(finding['opportunity_savings_inr']):,.2f}",
            f"₹{Decimal(finding['realizable_12m_inr']):,.2f}",
            finding.get("review", {}).get("status", "unreviewed"),
        )
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in fields) + "</tr>")
    if not rows:
        rows.append('<tr><td colspan="9">No candidates found.</td></tr>')
    quality_items = "".join(
        "<li>" + html.escape(f"{item['source']} row {item['row']}: {item['issue']} — {item['details']}") + "</li>"
        for item in payload["data_quality_issues"]
    ) or "<li>No data quality or contract issues.</li>"
    showback_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in (
            row["department"], row["cost_center"], row["active_license_count"],
            f"₹{Decimal(row['annualized_spend_inr']):,.2f}",
            f"₹{Decimal(row['annual_opportunity_inr']):,.2f}",
            f"₹{Decimal(row['estimated_realizable_12m_inr']):,.2f}",
        )) + "</tr>"
        for row in payload.get("cost_center_showback", [])
    ) or '<tr><td colspan="6">No cost-center data available.</td></tr>'
    renewal_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value or ''))}</td>" for value in (
            row["contract_id"], row["applications"], row.get("renewal_date"),
            row.get("commitment_end_date"), row.get("notice_deadline"),
            row.get("days_until_notice_deadline"), row["active_seats"], row.get("seat_minimum", 0),
            f"₹{Decimal(row['annualized_spend_inr']):,.2f}",
            f"₹{Decimal(row['annual_opportunity_inr']):,.2f}", row["status"],
        )) + "</tr>"
        for row in payload.get("renewal_calendar", [])
    ) or '<tr><td colspan="11">No active contracts to display.</td></tr>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SaaS Sentry report</title><style>
body{{font:15px/1.5 system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#17212b}}
h1{{margin-bottom:.25rem}}.muted{{color:#526272}}.cards{{display:flex;gap:1rem;flex-wrap:wrap;margin:1.5rem 0}}
.card{{border:1px solid #d6dee6;border-radius:8px;padding:1rem;min-width:210px}}.value{{font-size:1.3rem;font-weight:700}}
table{{border-collapse:collapse;width:100%;font-size:.9rem}}th,td{{border:1px solid #d6dee6;padding:.6rem;text-align:left;vertical-align:top}}
th{{background:#f2f5f8}}tr:nth-child(even){{background:#fafbfd}}code{{overflow-wrap:anywhere}}
</style></head><body>
<h1>SaaS Sentry</h1><p class="muted">Analysis as of {html.escape(payload['as_of'])} · Generated {html.escape(payload['generated_at'])} · INR</p>
<section class="cards">
<div class="card"><div>Annualized candidate cost</div><div class="value">₹{Decimal(totals['annualized_candidate_cost']):,.2f}</div></div>
<div class="card"><div>Contract-adjusted annual opportunity</div><div class="value">₹{Decimal(totals['contract_adjusted_annual_opportunity']):,.2f}</div></div>
<div class="card"><div>Estimated realizable in 12 months</div><div class="value">₹{Decimal(totals['estimated_realizable_12m']):,.2f}</div></div>
</section>
<p>Active licenses analyzed: {int(payload['active_license_count'])}. Findings are recommendations for review; estimated savings are not guaranteed.</p>
<h2>Cost-center showback</h2><table><thead><tr><th>Department</th><th>Cost center</th><th>Active licenses</th><th>Annualized spend</th><th>Annual opportunity</th><th>Estimated 12-month savings</th></tr></thead>
<tbody>{showback_rows}</tbody></table>
<h2>Renewal calendar</h2><table><thead><tr><th>Contract</th><th>Applications</th><th>Renewal</th><th>Commitment end</th><th>Notice deadline</th><th>Days to deadline</th><th>Seats</th><th>Seat minimum</th><th>Annual spend</th><th>Opportunity</th><th>Status</th></tr></thead>
<tbody>{renewal_rows}</tbody></table>
<h2>Findings</h2><table><thead><tr><th>Application</th><th>Employee</th><th>Email</th><th>Category</th><th>Reason</th><th>Evidence</th><th>Annual opportunity</th><th>Estimated 12-month savings</th><th>Review</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<h2>Data quality and contract issues</h2><ul>{quality_items}</ul>
</body></html>"""


def read_snapshot(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read snapshot {path}: {exc}") from exc
    if payload.get("schema_version") != 1 or not isinstance(payload.get("findings"), list):
        raise ValueError(f"Unsupported or invalid snapshot: {path}")
    return payload


def list_snapshots(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not directory.exists():
        return []
    items = [(path, read_snapshot(path)) for path in directory.glob("*.json")]
    return sorted(items, key=lambda item: item[1].get("generated_at", ""))


def compare_snapshots(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    old = {item["finding_id"]: item for item in baseline["findings"]}
    new = {item["finding_id"]: item for item in current["findings"]}
    added = sorted(set(new) - set(old))
    resolved = sorted(set(old) - set(new))
    changed = []
    for key in sorted(set(old) & set(new)):
        before, after = old[key], new[key]
        fields = ("category", "annualized_cost_inr", "opportunity_savings_inr", "realizable_12m_inr", "reason")
        diffs = {field: {"before": before.get(field), "after": after.get(field)}
                 for field in fields if before.get(field) != after.get(field)}
        if diffs:
            changed.append({"finding_id": key, "changes": diffs})
    before_total = Decimal(baseline["totals_inr"]["contract_adjusted_annual_opportunity"])
    after_total = Decimal(current["totals_inr"]["contract_adjusted_annual_opportunity"])
    return {
        "baseline_as_of": baseline["as_of"], "current_as_of": current["as_of"],
        "added_finding_ids": added, "resolved_finding_ids": resolved,
        "changed_findings": changed,
        "contract_adjusted_annual_opportunity_delta_inr": format(after_total - before_total, "f"),
    }
