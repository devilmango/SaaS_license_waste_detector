"""Local read-only dashboard and JSON API for SaaS license analysis."""

from __future__ import annotations

from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .engine import InputError, analyze, load_config, load_inputs, load_reviews
from .policy import active_exception_keys
from .reports import report_payload


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
<header><div><h1>SaaS Sentry</h1><p class="eyebrow">License utilization and subscription waste</p></div><span class="local">Local read-only view</span></header>
<main><section class="toolbar"><p id="updated" role="status" aria-live="polite">Loading the latest analysis…</p><button id="refresh" type="button">Refresh report</button></section>
<div id="error" class="error" role="alert" hidden></div><section class="cards" aria-label="Portfolio summary">
<article class="card"><div class="label">Active licenses analyzed</div><div id="licenses" class="value">—</div></article>
<article class="card"><div class="label">Annual opportunity</div><div id="opportunity" class="value">—</div></article>
<article class="card"><div class="label">Estimated savings in 12 months</div><div id="realizable" class="value">—</div></article>
<article class="card"><div class="label">Findings to review</div><div id="finding-count" class="value">—</div></article>
</section><section class="panel"><h2>License findings</h2><label for="search" class="label">Filter findings by app, employee, or status</label><p><input id="search" class="search" type="search" autocomplete="off" placeholder="Search findings"></p>
<div class="table-wrap"><table><thead><tr><th>Application</th><th>Employee</th><th>Category</th><th>Activity evidence</th><th>Opportunity</th><th>Review</th></tr></thead><tbody id="findings"><tr><td class="empty" colspan="6">Loading…</td></tr></tbody></table></div></section>
<section class="panel"><h2>Renewal calendar</h2><div class="table-wrap"><table><thead><tr><th>Contract</th><th>Applications</th><th>Renewal</th><th>Notice deadline</th><th>Days to notice</th><th>Status</th></tr></thead><tbody id="renewals"><tr><td class="empty" colspan="6">Loading…</td></tr></tbody></table></div></section>
<p class="foot">Analysis uses configured rules and active approved exceptions. Provider changes are not available here. Data is served by the local SaaS Sentry process.</p></main>
<script>
const money=value=>new Intl.NumberFormat(undefined,{style:'currency',currency:'INR',maximumFractionDigits:0}).format(Number(value||0));
const cell=(row,value,cls='')=>{const td=document.createElement('td');td.textContent=value??'';if(cls)td.className=cls;row.append(td);return td};let findingRows=[];
function drawFindings(){const body=document.querySelector('#findings');body.replaceChildren();const query=document.querySelector('#search').value.trim().toLowerCase();const visible=findingRows.filter(f=>[f.application,f.employee,f.category,f.review?.status].join(' ').toLowerCase().includes(query));if(!visible.length){const row=body.insertRow();const td=cell(row,query?'No findings match this filter.':'No candidates found.');td.colSpan=6;td.className='empty';return}for(const f of visible){const row=body.insertRow();cell(row,f.application);cell(row,`${f.employee} · ${f.email}`);cell(row,f.category);cell(row,f.evidence);cell(row,money(f.opportunity_savings_inr));cell(row,f.review?.status||'unreviewed','status')}}
function drawRenewals(rows){const body=document.querySelector('#renewals');body.replaceChildren();if(!rows.length){const row=body.insertRow();const td=cell(row,'No active contracts to display.');td.colSpan=6;td.className='empty';return}for(const r of rows){const row=body.insertRow();cell(row,r.contract_id);cell(row,r.applications);cell(row,r.renewal_date||'');cell(row,r.notice_deadline||'');cell(row,r.days_until_notice_deadline??'');cell(row,r.status,'status')}}
async function refresh(){const error=document.querySelector('#error');error.hidden=true;document.querySelector('#updated').textContent='Loading the latest analysis…';try{const response=await fetch('/api/report',{headers:{'Accept':'application/json'},cache:'no-store'});if(!response.ok)throw new Error(`Analysis returned HTTP ${response.status}`);const report=await response.json();document.querySelector('#licenses').textContent=report.active_license_count;document.querySelector('#opportunity').textContent=money(report.totals_inr.contract_adjusted_annual_opportunity);document.querySelector('#realizable').textContent=money(report.totals_inr.estimated_realizable_12m);findingRows=report.findings;document.querySelector('#finding-count').textContent=findingRows.length;drawFindings();drawRenewals(report.renewal_calendar);document.querySelector('#updated').textContent=`As of ${report.as_of} · Updated ${new Date(report.generated_at).toLocaleString()}`}catch(err){document.querySelector('#updated').textContent='Could not load the report';error.textContent=err.message;error.hidden=false}}
document.querySelector('#refresh').addEventListener('click',refresh);document.querySelector('#search').addEventListener('input',drawFindings);refresh();
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


def route_request(method: str, target: str, report_options: dict[str, Any]) -> tuple[int, str, bytes]:
    """Resolve an HTTP request without opening a socket (also used by tests)."""
    if method != "GET":
        return 405, "application/json; charset=utf-8", b'{"error":"read-only API"}'
    route = urlsplit(target)
    if route.path == "/":
        return 200, "text/html; charset=utf-8", _HTML.encode("utf-8")
    if not route.path.startswith("/api/"):
        return 404, "application/json; charset=utf-8", b'{"error":"not found"}'
    try:
        report = build_report(**report_options)
        if route.path == "/api/report":
            payload: Any = report
        elif route.path == "/api/findings":
            query = parse_qs(route.query)
            offset = max(0, int(query.get("offset", ["0"])[0]))
            limit = min(500, max(1, int(query.get("limit", ["100"])[0])))
            payload = {"total": len(report["findings"]), "offset": offset,
                       "findings": report["findings"][offset:offset + limit]}
        elif route.path == "/api/renewals":
            payload = {"renewals": report["renewal_calendar"]}
        elif route.path == "/api/health":
            payload = {"status": "ok", "as_of": report["as_of"],
                       "generated_at": report["generated_at"], "schema_version": report["schema_version"]}
        else:
            return 404, "application/json; charset=utf-8", b'{"error":"not found"}'
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return 200, "application/json; charset=utf-8", body
    except (InputError, OSError, ValueError) as exc:
        body = json.dumps({"error": str(exc)}).encode("utf-8")
        return 400, "application/json; charset=utf-8", body


def make_handler(report_options: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            status, content_type, body = route_request("GET", self.path, report_options)
            self._send(status, body, content_type)

        def do_POST(self) -> None:
            status, content_type, body = route_request("POST", self.path, report_options)
            self._send(status, body, content_type)

        def do_PUT(self) -> None:
            status, content_type, body = route_request("PUT", self.path, report_options)
            self._send(status, body, content_type)

        def do_DELETE(self) -> None:
            status, content_type, body = route_request("DELETE", self.path, report_options)
            self._send(status, body, content_type)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return DashboardHandler


def create_server(host: str, port: int, report_options: dict[str, Any]) -> ThreadingHTTPServer:
    if not 0 <= port <= 65535:
        raise InputError("dashboard port must be between 0 and 65535")
    return ThreadingHTTPServer((host, port), make_handler(report_options))
