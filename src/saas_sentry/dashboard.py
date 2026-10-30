"""Local read-only dashboard and JSON API for SaaS license analysis."""

from __future__ import annotations

from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import csv
import tempfile
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit
from http.cookies import SimpleCookie
from threading import Event, RLock, Thread

from .engine import InputError, analyze, load_config, load_inputs, load_reviews, update_review
from .access_reviews import create_campaign, decide_campaign_item, list_campaign_items
from .dashboard_auth import AuthorizationError, Principal, authorize, principal_from_user_record, scope_report
from .dashboard_auth import SessionStore, parse_authorization_rules
from .dashboard_sso import configured_mode, identity_principal, oidc_client_from_environment, saml_auth, transaction
from .policy import active_exception_keys
from .reports import report_payload
from .schedules import create_schedule, list_schedules, run_due_schedules, run_schedule, set_schedule_enabled, update_schedule
from .storage import _connect


_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>SaaS Sentry dashboard</title><style>
:root{color-scheme:light;--ink:#14231f;--muted:#53665f;--line:#d9e3de;--surface:#fff;--canvas:#f3f7f4;--green:#16734a;--red:#a83939}
*{box-sizing:border-box}body{margin:0;background:var(--canvas);color:var(--ink);font:16px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{background:#12372c;color:#fff;padding:24px max(24px,calc((100vw - 1320px)/2));display:flex;align-items:center;justify-content:space-between;gap:16px}
h1{font-size:1.55rem;line-height:1.2;margin:0}.eyebrow{margin:5px 0 0;color:#c6ddd1;font-size:.9rem}.local{border:1px solid #7fac95;border-radius:999px;padding:6px 12px;font-size:.82rem;white-space:nowrap}
main{max-width:1320px;margin:28px auto;padding:0 24px 48px}.toolbar{display:flex;gap:12px;align-items:center;justify-content:space-between;margin-bottom:18px;flex-wrap:wrap}.toolbar p{margin:0;color:var(--muted)}
button,input{font:inherit}button{min-height:44px;background:var(--green);color:#fff;border:0;border-radius:8px;padding:9px 16px;cursor:pointer}button:hover{background:#105c3a}button:focus-visible,input:focus-visible{outline:3px solid #d38a32;outline-offset:2px}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:18px 0}.card,.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:0 2px 8px #1e47340a}.card{padding:18px}.label{color:var(--muted);font-size:.9rem}.value{font-size:1.45rem;font-weight:700;margin-top:7px;letter-spacing:-.02em}.card:nth-child(2) .value{color:var(--green)}
.panel{padding:20px;margin-top:18px}.panel h2{font-size:1.12rem;margin:0 0 12px}.search{width:min(360px,100%);min-height:44px;border:1px solid #aebeb5;border-radius:8px;padding:8px 12px;background:#fff}
.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px}table{border-collapse:collapse;width:100%;min-width:780px;font-size:.92rem}th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top}th{background:#edf3ef;color:#334940;font-weight:650}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:#f8fbf9}.status{font-weight:650}.empty{color:var(--muted);padding:20px;text-align:center}.error{color:var(--red);padding:12px 0}.foot{color:var(--muted);font-size:.85rem;margin:16px 2px 0}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:540px){header{padding:18px 16px;align-items:flex-start}.local{font-size:.76rem}main{padding:0 14px 32px;margin-top:18px}.cards{gap:9px}.card{padding:14px}.value{font-size:1.2rem}.panel{padding:14px}}
</style></head><body>
<header><div><h1>SaaS Sentry</h1><p class="eyebrow">License utilization and subscription waste</p></div><div><a href="/auth/login" style="color:white;margin-right:14px">Sign in with SSO</a><label class="local">Access token <input id="token" type="password" autocomplete="current-password" aria-label="Dashboard access token"></label></div></header>
<main><section class="toolbar"><p id="updated" role="status" aria-live="polite">Loading the latest analysis…</p><button id="refresh" type="button">Refresh report</button></section>
<div id="error" class="error" role="alert" hidden></div><section class="cards" aria-label="Portfolio summary">
<article class="card"><div class="label">Active licenses analyzed</div><div id="licenses" class="value">—</div></article>
<article class="card"><div class="label">Annual opportunity</div><div id="opportunity" class="value">—</div></article>
<article class="card"><div class="label">Estimated savings in 12 months</div><div id="realizable" class="value">—</div></article>
<article class="card"><div class="label">Findings to review</div><div id="finding-count" class="value">—</div></article>
</section><section class="panel"><h2>License findings</h2><label for="search" class="label">Filter findings by app, employee, or status</label><p><input id="search" class="search" type="search" autocomplete="off" placeholder="Search findings"></p>
<div class="table-wrap"><table><thead><tr><th>Application</th><th>Employee</th><th>Category</th><th>Activity evidence</th><th>Opportunity</th><th>Review</th><th>Campaign owner</th><th>Decision</th></tr></thead><tbody id="findings"><tr><td class="empty" colspan="8">Loading…</td></tr></tbody></table></div></section>
<section class="panel"><h2>Access review campaigns</h2><form id="campaign-form"><label>Due date <input id="campaign-due" type="date" required></label> <label>Default owner <input id="campaign-owner" type="email" placeholder="Optional when each row has an owner"></label> <button type="submit">Create campaign</button></form><div class="table-wrap" style="margin-top:14px"><table><thead><tr><th>Campaign</th><th>Application</th><th>Finding</th><th>Owner</th><th>Due</th><th>Status</th><th>Decision</th></tr></thead><tbody id="campaigns"><tr><td class="empty" colspan="7">Loading…</td></tr></tbody></table></div></section>
<section class="panel"><h2>Scheduled reports</h2><form id="schedule-form"><label>Name <input id="schedule-name" maxlength="100" required></label> <label>Local time <input id="schedule-time" type="time" required></label> <label>Timezone <input id="schedule-timezone" value="UTC" placeholder="Asia/Kolkata" required></label> <button type="submit">Create schedule</button></form><p class="foot">Schedules use the selected IANA timezone and run reports from the current dashboard data sources.</p><div class="table-wrap"><table><thead><tr><th>Name</th><th>Local schedule</th><th>Next run</th><th>Last result</th><th>Recent history</th><th>Actions</th></tr></thead><tbody id="schedules"><tr><td class="empty" colspan="6">Loading…</td></tr></tbody></table></div></section>
<section class="panel"><h2>Renewal calendar</h2><div class="table-wrap"><table><thead><tr><th>Contract</th><th>Applications</th><th>Renewal</th><th>Notice deadline</th><th>Days to notice</th><th>Status</th></tr></thead><tbody id="renewals"><tr><td class="empty" colspan="6">Loading…</td></tr></tbody></table></div></section>
<p class="foot">Analysis uses configured rules and active approved exceptions. Provider changes are not available here. Data is served by the local SaaS Sentry process.</p></main>
<script>
const money=value=>new Intl.NumberFormat(undefined,{style:'currency',currency:'INR',maximumFractionDigits:0}).format(Number(value||0));
const cell=(row,value,cls='')=>{const td=document.createElement('td');td.textContent=value??'';if(cls)td.className=cls;row.append(td);return td};let findingRows=[];
function drawFindings(){const body=document.querySelector('#findings');body.replaceChildren();const query=document.querySelector('#search').value.trim().toLowerCase();const visible=findingRows.filter(f=>[f.application,f.employee,f.category,f.review?.status].join(' ').toLowerCase().includes(query));if(!visible.length){const row=body.insertRow();const td=cell(row,query?'No findings match this filter.':'No candidates found.');td.colSpan=8;td.className='empty';return}for(const f of visible){const row=body.insertRow();cell(row,f.application);cell(row,`${f.employee} · ${f.email}`);cell(row,f.category);cell(row,f.evidence);cell(row,money(f.opportunity_savings_inr));cell(row,f.review?.status||'unreviewed','status');const ownerCell=cell(row,'');const owner=document.createElement('input');owner.type='email';owner.placeholder='Use campaign default';owner.value=f.review?.owner||'';owner.setAttribute('aria-label',`Campaign owner for ${f.employee} ${f.application}`);owner.dataset.findingId=f.finding_id;ownerCell.append(owner);const action=cell(row,'');const select=document.createElement('select');select.setAttribute('aria-label',`Decision for ${f.employee} ${f.application}`);for(const value of ['confirmed','dismissed','deferred']){const option=document.createElement('option');option.value=value;option.textContent=value;select.append(option)}const button=document.createElement('button');button.type='button';button.textContent='Save';button.addEventListener('click',async()=>{button.disabled=true;try{await api('/api/reviews',{method:'POST',body:JSON.stringify({finding_id:f.finding_id,status:select.value,note:'Reviewed from dashboard'})});await refresh()}catch(error){const panel=document.querySelector('#error');panel.textContent=error.message;panel.hidden=false}finally{button.disabled=false}});action.append(select,button)}}
function drawRenewals(rows){const body=document.querySelector('#renewals');body.replaceChildren();if(!rows.length){const row=body.insertRow();const td=cell(row,'No active contracts to display.');td.colSpan=6;td.className='empty';return}for(const r of rows){const row=body.insertRow();cell(row,r.contract_id);cell(row,r.applications);cell(row,r.renewal_date||'');cell(row,r.notice_deadline||'');cell(row,r.days_until_notice_deadline??'');cell(row,r.status,'status')}}
async function api(path,options={}){let session={};try{const auth=await fetch('/api/session',{cache:'no-store'});if(auth.ok)session=await auth.json()}catch{}const response=await fetch(path,{...options,headers:{'Content-Type':'application/json','Authorization':`Bearer ${document.querySelector('#token').value}`,'X-CSRF-Token':session.csrf_token||'',...options.headers}});const result=await response.json();if(!response.ok)throw new Error(result.error||`HTTP ${response.status}`);return result}
async function drawCampaigns(){const body=document.querySelector('#campaigns');body.replaceChildren();try{const rows=(await api('/api/campaigns')).campaigns;if(!rows.length){const tr=body.insertRow();const td=cell(tr,'No campaigns yet.');td.colSpan=7;td.className='empty';return}for(const item of rows){const tr=body.insertRow();cell(tr,item.campaign_id);cell(tr,item.application);cell(tr,item.finding_id);cell(tr,item.owner);cell(tr,item.due_on);cell(tr,item.status,'status');const td=cell(tr,'');if(item.status==='pending'||item.status==='overdue'){const select=document.createElement('select');for(const value of ['confirmed','dismissed','deferred']){const option=document.createElement('option');option.value=value;option.textContent=value;select.append(option)}const button=document.createElement('button');button.textContent='Decide';button.addEventListener('click',async()=>{button.disabled=true;try{await api('/api/campaigns/decision',{method:'POST',body:JSON.stringify({campaign_id:item.campaign_id,finding_id:item.finding_id,decision:select.value})});await refresh()}catch(error){const panel=document.querySelector('#error');panel.textContent=error.message;panel.hidden=false}finally{button.disabled=false}});td.append(select,button)}}}catch(error){body.innerHTML='';const tr=body.insertRow();const td=cell(tr,error.message);td.colSpan=7;td.className='empty'}}
async function drawSchedules(){const body=document.querySelector('#schedules');body.replaceChildren();try{const schedules=(await api('/api/schedules')).schedules;if(!schedules.length){const tr=body.insertRow();const td=cell(tr,'No schedules configured.');td.colSpan=6;td.className='empty';return}for(const schedule of schedules){const tr=body.insertRow();const name=document.createElement('input');name.value=schedule.name;name.maxLength=100;name.setAttribute('aria-label','Schedule name');cell(tr,'').append(name);const time=document.createElement('input');time.type='time';time.value=schedule.schedule_time;time.setAttribute('aria-label','Local schedule time');const timezone=document.createElement('input');timezone.value=schedule.timezone;timezone.setAttribute('aria-label','Schedule timezone');const localCell=cell(tr,'');localCell.append(time,timezone);cell(tr,schedule.enabled?(schedule.next_run||''):'Paused');cell(tr,`${schedule.last_result}${schedule.last_error?`: ${schedule.last_error}`:''}`,'status');const history=cell(tr,'');const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent=`${schedule.history.length} recent run(s)`;details.append(summary);for(const run of schedule.history){const item=document.createElement('div');item.textContent=`${run.started_at} · ${run.status} · ${run.attempts} attempt(s)${run.error?` · ${run.error}`:''}`;details.append(item)}history.append(details);const actions=cell(tr,'');const save=document.createElement('button');save.textContent='Save';save.addEventListener('click',async()=>{try{await api(`/api/schedules/${schedule.schedule_id}`,{method:'POST',body:JSON.stringify({name:name.value,schedule_time:time.value,timezone:timezone.value})});await drawSchedules()}catch(error){showError(error)}});const pause=document.createElement('button');pause.textContent=schedule.enabled?'Pause':'Resume';pause.addEventListener('click',async()=>{try{await api(`/api/schedules/${schedule.schedule_id}/pause`,{method:'POST',body:JSON.stringify({paused:schedule.enabled})});await drawSchedules()}catch(error){showError(error)}});const run=document.createElement('button');run.textContent='Run now';run.addEventListener('click',async()=>{run.disabled=true;try{await api(`/api/schedules/${schedule.schedule_id}/run`,{method:'POST'});await drawSchedules()}catch(error){showError(error)}finally{run.disabled=false}});actions.append(save,pause,run)}}catch(error){const tr=body.insertRow();const td=cell(tr,error.message);td.colSpan=6;td.className='empty'}}
function showError(error){const panel=document.querySelector('#error');panel.textContent=error.message;panel.hidden=false}
async function refresh(){const error=document.querySelector('#error');error.hidden=true;document.querySelector('#updated').textContent='Loading the latest analysis…';try{const response=await fetch('/api/report',{headers:{'Accept':'application/json','Authorization':`Bearer ${document.querySelector('#token').value}`},cache:'no-store'});if(!response.ok)throw new Error(response.status===401?'Enter a valid dashboard token':`Analysis returned HTTP ${response.status}`);const report=await response.json();document.querySelector('#licenses').textContent=report.scope_limited?report.findings.length:report.active_license_count;document.querySelector('.cards .card:first-child .label').textContent=report.scope_limited?'Visible findings':'Active licenses analyzed';document.querySelector('#opportunity').textContent=money(report.totals_inr.contract_adjusted_annual_opportunity);document.querySelector('#realizable').textContent=money(report.totals_inr.estimated_realizable_12m);findingRows=report.findings;document.querySelector('#finding-count').textContent=findingRows.length;drawFindings();drawRenewals(report.renewal_calendar);await drawCampaigns();await drawSchedules();document.querySelector('#updated').textContent=`As of ${report.as_of} · Updated ${new Date(report.generated_at).toLocaleString()}`}catch(err){document.querySelector('#updated').textContent='Could not load the report';error.textContent=err.message;error.hidden=false}}
document.querySelector('#campaign-form').addEventListener('submit',async event=>{event.preventDefault();try{const defaultOwner=document.querySelector('#campaign-owner').value;const assignments={};for(const input of document.querySelectorAll('#findings input[data-finding-id]'))assignments[input.dataset.findingId]=input.value.trim()||defaultOwner;await api('/api/campaigns',{method:'POST',body:JSON.stringify({due_on:document.querySelector('#campaign-due').value,default_owner:defaultOwner,assignments})});await refresh()}catch(error){const panel=document.querySelector('#error');panel.textContent=error.message;panel.hidden=false}});
document.querySelector('#schedule-form').addEventListener('submit',async event=>{event.preventDefault();try{await api('/api/schedules',{method:'POST',body:JSON.stringify({name:document.querySelector('#schedule-name').value,schedule_time:document.querySelector('#schedule-time').value,timezone:document.querySelector('#schedule-timezone').value})});event.target.reset();document.querySelector('#schedule-timezone').value='UTC';await drawSchedules()}catch(error){showError(error)}});
document.querySelector('#token').value=sessionStorage.getItem('saas-sentry-token')||'';document.querySelector('#token').addEventListener('change',e=>{sessionStorage.setItem('saas-sentry-token',e.target.value);refresh()});document.querySelector('#refresh').addEventListener('click',refresh);document.querySelector('#search').addEventListener('input',drawFindings);refresh();
</script></body></html>"""


def build_report(
    *, hr: Path, usage: Path, billing: Path, contract_pricing: Path | None = None,
    config: Path | None = None, review_file: Path | None = None,
    exceptions: Path | None = None, as_of: date | None = None,
) -> dict[str, Any]:
    rules, currency = load_config(config)
    data = load_inputs(hr, usage, billing, currency=currency, contract_pricing_path=contract_pricing)
    report_date = as_of or date.today()
    analysis = analyze(
        data, as_of=report_date, rules=rules, currency=currency,
        active_exceptions=active_exception_keys(exceptions, as_of=report_date),
    )
    reviews = load_reviews(review_file)
    sources = [path for path in (hr, usage, billing, contract_pricing, review_file, exceptions)
               if path is not None and path.exists()]
    return report_payload(analysis, as_of=report_date, source_paths=sources,
                          config_path=config, reviews=reviews)


def _build_dashboard_report(options: dict[str, Any]) -> dict[str, Any]:
    omitted = {"campaign_ledger", "schedule_db", "schedule_output_dir"}
    return build_report(**{key: value for key, value in options.items() if key not in omitted})


def _run_dashboard_schedule(report_options: dict[str, Any], schedule_id: str) -> dict[str, object] | None:
    database = report_options.get("schedule_db")
    if database is None:
        raise InputError("configure --schedule-db to manage dashboard schedules")
    output_dir = report_options.get("schedule_output_dir") or (database.parent / "scheduled-reports")

    return run_schedule(
        database, schedule_id,
        lambda _job, run_id: _write_scheduled_report(report_options, schedule_id, run_id, output_dir),
        retries=2, base_delay=1.0,
    )


def _write_scheduled_report(report_options: dict[str, Any], schedule_id: str, run_id: str,
                            output_dir: Path) -> str:
    report = _build_dashboard_report(report_options)
    target = output_dir / f"{schedule_id}-{run_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return str(target)


def _dashboard_schedule_worker(report_options: dict[str, Any], stop: Event) -> None:
    database = report_options.get("schedule_db")
    if database is None:
        return
    output_dir = report_options.get("schedule_output_dir") or (database.parent / "scheduled-reports")
    while not stop.wait(5):
        try:
            run_due_schedules(
                database,
                lambda job, run_id: _write_scheduled_report(
                    report_options, str(job["schedule_id"]), run_id, output_dir,
                ),
            )
        except Exception as exc:
            print(f"Dashboard schedule worker error: {type(exc).__name__}: {str(exc)[:300]}")


def route_request(method: str, target: str, report_options: dict[str, Any], *,
                  role: str = "anonymous", actor: str = "", body: bytes = b"",
                  principal: Principal | None = None) -> tuple[int, str, bytes]:
    """Resolve an HTTP request without opening a socket (also used by tests)."""
    route = urlsplit(target)
    principal = principal or Principal(actor, role)
    schedule_route = route.path.split("/")
    if route.path == "/api/schedules" and method == "GET":
        try:
            authorize(principal, "schedule:read", scope_required=False)
            database = report_options.get("schedule_db")
            if database is None:
                raise InputError("configure --schedule-db to manage dashboard schedules")
            payload = {"schedules": list_schedules(database)}
            return 200, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode()
        except AuthorizationError as exc:
            return 403, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode()
        except (InputError, OSError) as exc:
            return 400, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode()
    if method == "POST" and schedule_route[1:3] == ["api", "schedules"]:
        try:
            authorize(principal, "schedule:manage", scope_required=False)
            database = report_options.get("schedule_db")
            if database is None:
                raise InputError("configure --schedule-db to manage dashboard schedules")
            payload = json.loads(body or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("schedule request must be a JSON object")
            if len(schedule_route) == 3:
                schedule = create_schedule(
                    database, name=str(payload.get("name", "")),
                    schedule_time=str(payload.get("schedule_time", "")),
                    timezone_name=str(payload.get("timezone", "")), actor=principal.actor,
                )
                return 201, "application/json; charset=utf-8", json.dumps({"schedule": schedule}).encode()
            schedule_id = schedule_route[3]
            if len(schedule_route) == 4:
                schedule = update_schedule(
                    database, schedule_id, name=str(payload.get("name", "")),
                    schedule_time=str(payload.get("schedule_time", "")),
                    timezone_name=str(payload.get("timezone", "")),
                )
                return 200, "application/json; charset=utf-8", json.dumps({"schedule": schedule}).encode()
            if len(schedule_route) == 5 and schedule_route[4] == "pause":
                if not isinstance(payload.get("paused"), bool):
                    raise ValueError("paused must be a boolean")
                schedule = set_schedule_enabled(database, schedule_id, enabled=not payload["paused"])
                return 200, "application/json; charset=utf-8", json.dumps({"schedule": schedule}).encode()
            if len(schedule_route) == 5 and schedule_route[4] == "run":
                run = _run_dashboard_schedule(report_options, schedule_id)
                if run is None:
                    return 409, "application/json; charset=utf-8", b'{"error":"schedule run is already in progress"}'
                status = 200 if run["status"] == "succeeded" else 502
                return status, "application/json; charset=utf-8", json.dumps({"run": run}).encode()
            return 404, "application/json; charset=utf-8", b'{"error":"schedule route not found"}'
        except AuthorizationError as exc:
            return 403, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode()
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError, InputError, OSError) as exc:
            return 400, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode()
    if method == "POST" and route.path == "/api/reviews":
        try:
            authorize(principal, "review:write")
            payload = json.loads(body or b"{}")
            finding_id = str(payload.get("finding_id", "")).strip()
            status = str(payload.get("status", ""))
            owner = str(payload.get("owner", actor)).strip()
            note = str(payload.get("note", ""))
            if not finding_id or len(note) > 2000 or len(owner) > 200:
                raise ValueError("finding_id, a valid owner, and a note up to 2000 characters are required")
            report = _build_dashboard_report(report_options)
            finding = next((item for item in report["findings"] if item.get("finding_id") == finding_id), None)
            if finding is None:
                return 404, "application/json; charset=utf-8", b'{"error":"finding not found"}'
            authorize(principal, "review:write", application=finding.get("application", ""), team=finding.get("department", ""))
            review_path = report_options.get("review_file")
            if review_path is None:
                return 400, "application/json; charset=utf-8", b'{"error":"configure --review-file to enable review decisions"}'
            update_review(review_path, finding_id, status, owner=owner or actor, note=note)
            return 200, "application/json; charset=utf-8", json.dumps({"finding_id": finding_id, "status": status, "reviewed_by": actor}).encode()
        except AuthorizationError as exc:
            return 403, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode()
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            return 400, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode()
        except (InputError, OSError) as exc:
            return 400, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode()
    if method == "POST" and route.path in {"/api/campaigns", "/api/campaigns/decision"}:
        try:
            payload = json.loads(body or b"{}")
            ledger = report_options.get("campaign_ledger")
            if ledger is None:
                raise InputError("configure --campaign-ledger to enable access review campaigns")
            report = _build_dashboard_report(report_options)
            if route.path == "/api/campaigns":
                authorize(principal, "campaign:create", scope_required=False)
                due_on = date.fromisoformat(str(payload.get("due_on", "")))
                assignments = payload.get("assignments", {})
                if not isinstance(assignments, dict):
                    raise ValueError("assignments must map finding IDs to owners")
                all_ids = {row.get("finding_id") for row in report["findings"]}
                selected_ids = set(assignments) if assignments else all_ids
                rows = [row for row in report["findings"] if row.get("finding_id") in selected_ids]
                if selected_ids - all_ids:
                    raise ValueError("campaign includes unknown or out-of-scope findings")
                scoped_rows = []
                for row in rows:
                    try:
                        authorize(principal, "campaign:create", application=row.get("application", ""), team=row.get("department", ""))
                        scoped_rows.append(row)
                    except AuthorizationError:
                        if assignments:
                            raise
                rows = scoped_rows
                if not rows and report["findings"]:
                    raise AuthorizationError("No findings are inside your application or team scope")
                if not rows:
                    raise ValueError("campaign has no eligible findings")
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", suffix=".csv", delete=False) as handle:
                    temp_path = Path(handle.name)
                    fields = ["finding_id", "application", "review_status", "review_owner"]
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    for row in rows:
                        writer.writerow({"finding_id": row["finding_id"], "application": row["application"],
                                         "review_status": row.get("review", {}).get("status", "unreviewed"),
                                         "review_owner": assignments.get(row["finding_id"], "")})
                try:
                    campaign_id, assigned = create_campaign(
                        temp_path, ledger, due_on=due_on, created_by=principal.actor,
                        default_owner=str(payload.get("default_owner", "")),
                        campaign_id=str(payload.get("campaign_id", "") or "") or None,
                        owner_overrides={str(key): str(value) for key, value in assignments.items()},
                    )
                finally:
                    temp_path.unlink(missing_ok=True)
                result = {"campaign_id": campaign_id, "assigned": assigned}
            else:
                authorize(principal, "campaign:decide", scope_required=False)
                item = next((item for item in list_campaign_items(ledger, campaign_id=str(payload.get("campaign_id", "")))
                             if item.finding_id == str(payload.get("finding_id", ""))), None)
                if item is None:
                    return 404, "application/json; charset=utf-8", b'{"error":"campaign item not found"}'
                finding = next((row for row in report["findings"] if row.get("finding_id") == item.finding_id), {})
                authorize(principal, "campaign:decide", application=item.application,
                          team=str(finding.get("department", "")))
                if item.owner.casefold() != principal.actor.casefold():
                    raise AuthorizationError("Only the assigned owner can decide this campaign item")
                if report_options.get("review_file") is None:
                    raise InputError("configure --review-file to persist campaign decisions")
                decide_campaign_item(ledger, report_options.get("review_file"), item.campaign_id, item.finding_id,
                                    str(payload.get("decision", "")), reviewed_by=principal.actor,
                                    note=str(payload.get("note", "")))
                result = {"campaign_id": item.campaign_id, "finding_id": item.finding_id, "decision": payload.get("decision")}
            return 200, "application/json; charset=utf-8", json.dumps(result).encode()
        except AuthorizationError as exc:
            return 403, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode()
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError, InputError, OSError) as exc:
            return 400, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode()
    if method != "GET":
        return 405, "application/json; charset=utf-8", b'{"error":"unsupported method"}'
    if route.path == "/":
        return 200, "text/html; charset=utf-8", _HTML.encode("utf-8")
    if not route.path.startswith("/api/"):
        return 404, "application/json; charset=utf-8", b'{"error":"not found"}'
    try:
        report = _build_dashboard_report(report_options)
        if route.path == "/api/campaigns":
            authorize(principal, "campaign:read", scope_required=False)
            ledger = report_options.get("campaign_ledger")
            items = list_campaign_items(ledger) if ledger else ()
            visible = []
            finding_by_id = {row.get("finding_id"): row for row in report["findings"]}
            for item in items:
                try:
                    finding = finding_by_id.get(item.finding_id, {})
                    authorize(principal, "campaign:read", application=item.application,
                              team=str(finding.get("department", "")))
                    visible.append(item.__dict__ | {"due_on": item.due_on.isoformat(), "last_reminder_on": item.last_reminder_on.isoformat() if item.last_reminder_on else None})
                except AuthorizationError:
                    continue
            payload = {"campaigns": visible}
        elif route.path == "/api/report":
            payload = scope_report(report, principal)
        elif route.path == "/api/findings":
            report = scope_report(report, principal)
            query = parse_qs(route.query)
            offset = max(0, int(query.get("offset", ["0"])[0]))
            limit = min(500, max(1, int(query.get("limit", ["100"])[0])))
            payload = {"total": len(report["findings"]), "offset": offset,
                       "findings": report["findings"][offset:offset + limit]}
        elif route.path == "/api/renewals":
            payload = {"renewals": scope_report(report, principal)["renewal_calendar"]}
        elif route.path == "/api/health":
            payload = {"status": "ok", "as_of": report["as_of"],
                       "generated_at": report["generated_at"], "schema_version": report["schema_version"]}
        else:
            return 404, "application/json; charset=utf-8", b'{"error":"not found"}'
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return 200, "application/json; charset=utf-8", body
    except AuthorizationError as exc:
        return 403, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode()
    except (InputError, OSError, ValueError) as exc:
        body = json.dumps({"error": str(exc)}).encode("utf-8")
        return 400, "application/json; charset=utf-8", body


def load_dashboard_users(env_name: str = "SAAS_SENTRY_DASHBOARD_USERS") -> dict[str, dict[str, Any]]:
    """Read bearer credentials from a JSON environment variable, never a config file."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(f"{env_name} must contain a JSON array of actor, role, and token records") from exc
    if not isinstance(values, list):
        raise InputError(f"{env_name} must contain a JSON array")
    users: dict[str, dict[str, Any]] = {}
    for item in values:
        if not isinstance(item, dict):
            raise InputError(f"{env_name} entries must be objects")
        actor, role, token = (str(item.get(key, "")).strip() for key in ("actor", "role", "token"))
        if not actor or role not in {"viewer", "reviewer", "admin"} or len(token) < 24:
            raise InputError(f"{env_name} entries need actor, viewer/reviewer/admin role, and token of 24+ characters")
        for field in ("applications", "teams", "permissions"):
            value = item.get(field, [])
            if not isinstance(value, list) or any(not isinstance(entry, str) or not entry.strip() for entry in value):
                raise InputError(f"{env_name} field {field} must be a list of non-empty strings")
        if token in users:
            raise InputError(f"{env_name} contains a duplicate token")
        users[token] = {"actor": actor, "role": role,
                        "applications": item.get("applications", []), "teams": item.get("teams", []),
                        "permissions": item.get("permissions", [])}
    return users


def make_handler(report_options: dict[str, Any], users: dict[str, dict[str, str]] | None = None) -> type[BaseHTTPRequestHandler]:
    users = users or {}
    sso_mode = configured_mode()
    authentication_required = bool(users or sso_mode)
    sessions = SessionStore()
    transactions: dict[str, dict[str, Any]] = {}
    transaction_lock = RLock()
    rules = parse_authorization_rules(os.environ.get("SAAS_SENTRY_AUTHZ_RULES", ""))

    class DashboardHandler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _cookies(self) -> dict[str, str]:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                return {}
            return {key: morsel.value for key, morsel in cookie.items()}

        def _cookie_header(self, name: str, value: str, *, clear: bool = False, same_site: str = "Lax") -> str:
            secure = os.environ.get("SAAS_SENTRY_COOKIE_SECURE", "true").casefold() not in {"0", "false", "no"}
            parts = [f"{name}={value}", "Path=/", "HttpOnly", f"SameSite={same_site}"]
            if secure:
                parts.append("Secure")
            if clear:
                parts.append("Max-Age=0")
            return "; ".join(parts)

        def _sso_redirect(self, location: str, cookie: str = "") -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()

        def _sso_get(self) -> bool:
            route = urlsplit(self.path)
            mode = configured_mode()
            if route.path == "/auth/login":
                if mode == "oidc":
                    tx = transaction()
                    with transaction_lock:
                        transactions.update({key: value for key, value in transactions.items() if float(value.get("expires_at", "0")) >= time.time()})
                        transactions[tx["transaction"]] = tx
                    self._sso_redirect(oidc_client_from_environment().authorization_url(tx),
                                       self._cookie_header("saas_sentry_tx", tx["transaction"]))
                elif mode == "saml":
                    tx = transaction()
                    saml = saml_auth({"host": self.headers.get("Host", "localhost"), "port": "443", "https": True,
                                      "path": "/auth/saml/acs", "query": {}, "form": {}})
                    destination = saml.login(return_to=tx["state"])
                    tx["request_id"] = saml.get_last_request_id()
                    with transaction_lock:
                        transactions.update({key: value for key, value in transactions.items() if float(value.get("expires_at", "0")) >= time.time()})
                        transactions[tx["state"]] = tx
                    self._sso_redirect(destination, self._cookie_header("saas_sentry_tx", tx["state"], same_site="None"))
                else:
                    self._send(400, b'{"error":"configure OIDC or SAML SSO first"}', "application/json; charset=utf-8")
                return True
            if route.path == "/auth/oidc/callback":
                try:
                    values = parse_qs(route.query)
                    state = values.get("state", [""])[0]
                    tx_id = self._cookies().get("saas_sentry_tx", "")
                    with transaction_lock:
                        tx = transactions.pop(tx_id, None)
                    if mode != "oidc" or tx is None or tx["state"] != state or float(tx.get("expires_at", "0")) < time.time():
                        raise InputError("OIDC state validation failed")
                    claims = oidc_client_from_environment().identity(values.get("code", [""])[0], tx)
                    principal = identity_principal(claims, rules)
                    session_id, session = sessions.create(principal)
                    self._sso_redirect("/", self._cookie_header("saas_sentry_session", session_id))
                except (InputError, KeyError, ValueError) as exc:
                    self._send(401, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                return True
            if route.path == "/api/session":
                user = self._user()
                session = sessions.get(self._cookies().get("saas_sentry_session", ""))
                if user:
                    self._send(200, json.dumps({"actor": user.get("actor", ""), "csrf_token": session.csrf_token if session else ""}).encode(), "application/json; charset=utf-8")
                else:
                    self._send(401, b'{"error":"authentication required"}', "application/json; charset=utf-8")
                return True
            return False

        def do_GET(self) -> None:
            try:
                if self._sso_get():
                    return
            except InputError as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                return
            if self.path == "/healthz":
                self._send(200, b'{"status":"ok"}', "application/json; charset=utf-8")
                return
            if self.path == "/":
                status, content_type, body = route_request("GET", self.path, report_options)
                self._send(status, body, content_type)
                return
            user = self._user()
            if authentication_required and not user:
                status, content_type, body = 401, "application/json; charset=utf-8", b'{"error":"authentication required"}'
            else:
                principal = principal_from_user_record(user.get("actor", ""), user)
                status, content_type, body = route_request("GET", self.path, report_options, role=principal.role, actor=principal.actor, principal=principal)
            self._send(status, body, content_type)

        def do_POST(self) -> None:
            if urlsplit(self.path).path == "/auth/logout":
                session_id = self._cookies().get("saas_sentry_session", "")
                session = sessions.get(session_id)
                if session and self.headers.get("X-CSRF-Token", "") != session.csrf_token:
                    self._send(403, b'{"error":"CSRF token is required"}', "application/json; charset=utf-8")
                    return
                sessions.delete(session_id)
                self._send(200, b'{"logged_out":true}', "application/json; charset=utf-8",
                           {"Set-Cookie": self._cookie_header("saas_sentry_session", "", clear=True)})
                return
            if urlsplit(self.path).path == "/auth/saml/acs":
                try:
                    length = min(65536, int(self.headers.get("Content-Length", "0") or 0))
                    form = parse_qs(self.rfile.read(length).decode("utf-8", "strict"))
                    relay = form.get("RelayState", [""])[0]
                    with transaction_lock:
                        tx = transactions.pop(relay, None)
                    if configured_mode() != "saml" or not tx or relay != tx["state"] or float(tx.get("expires_at", "0")) < time.time() or self._cookies().get("saas_sentry_tx") != relay:
                        raise InputError("SAML RelayState validation failed")
                    saml = saml_auth({"host": self.headers.get("Host", "localhost"), "port": "443", "https": True,
                                      "path": "/auth/saml/acs", "query": {}, "form": {key: values[0] for key, values in form.items()}})
                    saml.process_response(tx.get("request_id"))
                    if saml.get_errors() or not saml.is_authenticated():
                        raise InputError("SAML response validation failed")
                    attrs = saml.get_attributes()
                    email = next((value[0] for key, value in attrs.items() if key.casefold() in {"email", "emailaddress", "mail"} and value), "")
                    group_values = next((value for key, value in attrs.items() if key.casefold() in {"groups", "group", "memberof"}), [])
                    principal = identity_principal({"email": email, "email_verified": True, "groups": group_values, "sub": saml.get_nameid()}, rules)
                    session_id, _ = sessions.create(principal)
                    self._sso_redirect("/", self._cookie_header("saas_sentry_session", session_id))
                except Exception as exc:
                    self._send(401, json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8")
                return
            user = self._user()
            session = sessions.get(self._cookies().get("saas_sentry_session", ""))
            if session and self.headers.get("X-CSRF-Token", "") != session.csrf_token:
                self._send(403, b'{"error":"CSRF token is required"}', "application/json; charset=utf-8")
                return
            if not user:
                status, content_type, body = 401, "application/json; charset=utf-8", b'{"error":"authentication required"}'
            else:
                length = min(1_048_576, int(self.headers.get("Content-Length", "0") or 0))
                principal = principal_from_user_record(user.get("actor", ""), user)
                status, content_type, body = route_request("POST", self.path, report_options, role=principal.role, actor=principal.actor, principal=principal, body=self.rfile.read(length))
            self._send(status, body, content_type)

        def do_PUT(self) -> None:
            status, content_type, body = route_request("PUT", self.path, report_options)
            self._send(status, body, content_type)

        def do_DELETE(self) -> None:
            status, content_type, body = route_request("DELETE", self.path, report_options)
            self._send(status, body, content_type)

        def _user(self) -> dict[str, str]:
            authorization = self.headers.get("Authorization", "")
            token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
            bearer = users.get(token, {})
            if bearer:
                return bearer
            session = sessions.get(self._cookies().get("saas_sentry_session", ""))
            if session:
                principal = session.principal
                return {"actor": principal.actor, "role": principal.role,
                        "applications": list(principal.applications), "teams": list(principal.teams),
                        "permissions": list(principal.permissions)}
            return {}

        def log_message(self, format: str, *args: Any) -> None:
            return

    return DashboardHandler


def create_server(host: str, port: int, report_options: dict[str, Any], *, users: dict[str, dict[str, str]] | None = None) -> ThreadingHTTPServer:
    if not 0 <= port <= 65535:
        raise InputError("dashboard port must be between 0 and 65535")
    sso_mode = configured_mode()
    if host not in {"127.0.0.1", "localhost", "::1"} and not users and not sso_mode:
        raise InputError("non-loopback dashboard binds require authenticated users in the configured environment variable")
    server = ThreadingHTTPServer((host, port), make_handler(report_options, users))
    schedule_db = report_options.get("schedule_db")
    if schedule_db is not None:
        # Initialize/migrate the schedule database before starting background work.
        connection = _connect(schedule_db)
        connection.close()
        stop = Event()
        worker = Thread(target=_dashboard_schedule_worker, args=(report_options, stop), daemon=True)
        worker.start()
        original_close = server.server_close

        def close() -> None:
            stop.set()
            original_close()

        server.server_close = close  # type: ignore[method-assign]
        server.schedule_worker = worker  # type: ignore[attr-defined]
    return server
