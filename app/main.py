import hashlib
import hmac
import html
import logging
import re
import time
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from .config import load
from .db import Store
from .dispatcher import Dispatcher

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

settings = load()
store = Store(settings.db_path)
dispatcher = Dispatcher(settings, store)
app = FastAPI(title="devin-issue-remediator")


@app.on_event("startup")
def _startup():
    if not settings.webhook_secret:
        logging.getLogger(__name__).warning(
            "GITHUB_WEBHOOK_SECRET unset: /webhooks/github is unauthenticated. "
            "Set it for any deployment reachable beyond localhost; "
            "the manual /scan and /issues/{n}/dispatch endpoints carry no auth."
        )
    dispatcher.start()


@app.on_event("shutdown")
def _shutdown():
    dispatcher.stop()


# ---------- event intake ----------

@app.post("/webhooks/github")
async def github_webhook(request: Request):
    body = await request.body()
    if settings.webhook_secret:
        sig = request.headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(
            settings.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return Response(status_code=401)
    event = request.headers.get("x-github-event", "")
    payload = await request.json()
    if event == "issues" and payload.get("action") in ("opened", "labeled", "reopened"):
        issue = payload.get("issue", {})
        dispatched = dispatcher.consider_issue(issue, source="webhook")
        return {"ok": True, "dispatched": dispatched}
    return {"ok": True, "ignored": f"{event}:{payload.get('action')}"}


# ---------- manual triggers ----------
# Webhook and poller paths call Dispatcher directly; neither uses these routes.

@app.post("/scan")
def scan_now():
    """Scan open labeled issues on demand."""
    return {"dispatched": dispatcher.poll_issues_once()}


@app.post("/issues/{number}/dispatch")
def dispatch_one(number: int):
    """Check one issue for dispatch on demand."""
    issue = dispatcher.gh.get_issue(number)
    return {"dispatched": dispatcher.consider_issue(issue, source="manual")}


# ---------- observability ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/tasks")
def tasks():
    return {"tasks": store.all(), "counts": store.counts()}


@app.get("/api/integrations")
def integrations():
    return dispatcher.integration_health()


@app.get("/api/events")
def events(limit: int = 50, offset: int = 0):
    return {"events": store.recent_events(limit, offset),
            "total": store.event_count(),
            "limit": limit, "offset": offset}


@app.get("/api/poller")
def poller_state():
    return {"enabled": dispatcher.poller_enabled,
            "interval_seconds": dispatcher.poller_interval}


@app.post("/api/poller")
async def set_poller(request: Request):
    body = await request.json()
    enabled = bool(body.get("enabled"))
    dispatcher.set_poller(enabled)
    store.event("config", None, f"poller {'enabled' if enabled else 'disabled'}")
    return poller_state()


@app.get("/api/config")
def get_config():
    return dispatcher.get_config()


@app.post("/api/config")
async def set_config(request: Request):
    body = await request.json()
    try:
        cfg = dispatcher.set_config(body)
    except (ValueError, TypeError) as e:
        return Response(str(e), status_code=422)
    store.event("config", None,
                "config updated: " + ", ".join(f"{k}={v}" for k, v in body.items()))
    return cfg


@app.get("/", response_class=HTMLResponse)
def dashboard():
    evs = ""
    for e in store.recent_events(15):
        issue = ""
        if e["issue_number"]:
            issue = (f'<a href="https://github.com/{settings.github_repo}'
                     f'/issues/{e["issue_number"]}" target="_blank" rel="noopener">'
                     f'#{e["issue_number"]}</a>')
        evs += (f"<tr><td>{_ago(e['ts'])}</td><td>{e['kind']}</td>"
                f"<td>{issue}</td><td>{_linkify(str(e['message']))}</td></tr>")
    return rf"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Devin Issue Remediator</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:ui-sans-serif,system-ui,sans-serif;margin:0;background:#0b0d12;color:#dde1e8;font-size:15px}}
main{{padding:1.5rem 1.6rem 2rem;max-width:1440px;margin:auto}}
header{{display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap;border-bottom:1px solid #232733;padding-bottom:.7rem;margin-bottom:1rem}}
h1{{font-size:1.05rem;font-weight:650;margin:0}}
h2{{font-size:.85rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#b1bacb;margin:1.4rem 0 .7rem}}
.meta{{color:#a5aec0;font-size:.82rem}}
.label{{background:#1f6feb33;color:#7aa2ff;border:1px solid #1f6feb66;padding:.05rem .45rem;border-radius:999px;font-size:.75rem;font-weight:600;font-family:ui-monospace,SFMono-Regular,monospace}}
a{{color:#7aa2ff;text-decoration:none}} a:hover{{text-decoration:underline}}
header .links{{margin-left:auto;font-size:.82rem;display:flex;gap:.9rem;align-items:center}}
.cards{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.8rem}}
.card{{background:#12151d;border:1px solid #2b3242;border-radius:10px;padding:1rem 1.1rem}}
.card .n{{font-size:1.9rem;font-weight:650;line-height:1.2;margin:.4rem 0}}
.card .l{{color:#bcc5d5;font-size:.8rem}}
.secondary{{display:flex;gap:.7rem 1.5rem;flex-wrap:wrap;padding:.9rem 0;font-size:.82rem;color:#a5aec0}}
.secondary strong{{color:#dde1e8;font-weight:550}}
.attention{{display:flex;gap:.6rem;flex-wrap:wrap;margin-bottom:.7rem}}
.attention button{{padding:.65rem .85rem;font-size:.85rem}}
.attention button[aria-pressed="true"]{{border-color:#7aa2ff;background:#1b2d4d}}
.attention strong{{margin-right:.35rem;font-size:1rem}}
.health{{display:grid;grid-template-columns:1fr 1fr;gap:.8rem;margin:.7rem 0 1.2rem}}
.health-item{{padding:.8rem 1rem;border:1px solid #2b3242;border-radius:8px;background:#12151d}}
.health-item .sub{{display:block;margin-top:.5rem;line-height:1.6;overflow-wrap:anywhere}}
.health-item .pill{{margin-left:.5rem}}
.banner{{border:1px solid #8b692b;background:#342a18;color:#ffdf98;border-radius:8px;padding:.8rem 1rem;margin-bottom:1rem}}
.hidden{{display:none}}
.table-wrap{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;background:#12151d;border:1px solid #232733;border-radius:8px;overflow:hidden}}
th{{background:#161a24;color:#b1bacb;font-size:.72rem;letter-spacing:.05em;text-transform:uppercase;padding:.8rem 1rem;text-align:left}}
td{{border-top:1px solid #232733;padding:.85rem 1rem;font-size:.9rem;vertical-align:top;line-height:1.5}}
#remediations{{min-width:740px}}
.issue-cell{{width:46%;min-width:260px}}
.issue-title{{font-weight:550;display:block}}
.issue-number{{color:#a5aec0;margin-right:.35rem}}
details{{margin-top:.5rem;color:#b1bacb;font-size:.8rem}}
summary{{cursor:pointer;width:fit-content}}
.metrics{{display:flex;gap:.4rem 1.2rem;flex-wrap:wrap;margin:.5rem 0}}
.detail-text{{white-space:pre-wrap;overflow-wrap:anywhere}}
.actions a{{display:block;white-space:nowrap;margin-bottom:.35rem;font-size:.82rem}}
.detected{{white-space:nowrap}}
.ci-note{{display:block;max-width:150px;margin-top:.3rem}}
tbody tr:hover{{background:#181d29}}
.pill{{display:inline-block;padding:.1rem .55rem;border-radius:999px;font-size:.75rem;font-weight:600}}
.pill.queued{{background:#2a2f3c;color:#b8bfcd}}
.pill.running{{background:#3a2e14;color:#fbbf24}}
.pill.pr_opened{{background:#2a2140;color:#c4b5fd}}
.pill.merged{{background:#123528;color:#4ade80}}
.pill.failed{{background:#3b1a1a;color:#f87171}}
.sub{{color:#a5aec0;font-size:.8rem}}
.ci{{display:inline-block;padding:.05rem .45rem;border-radius:999px;font-size:.72rem;font-weight:600}}
.ci.passing{{background:#123528;color:#4ade80}} .ci.failing{{background:#3b1a1a;color:#f87171}} .ci.pending{{background:#3a2e14;color:#fbbf24}}
.ci.unknown,.pill.idle{{background:#2a2f3c;color:#c6cddd}}
.pill.healthy{{background:#123528;color:#4ade80}}
.pill.error{{background:#3b1a1a;color:#f87171}}
.pill.stale,.pill.pending,.pill.waiting{{background:#3a2e14;color:#fbbf24}}
.add{{color:#4ade80}} .del{{color:#f87171}}
.num{{font-variant-numeric:tabular-nums}}
button{{background:#1a1e29;color:#dde1e8;border:1px solid #2b3242;border-radius:6px;padding:.2rem .6rem;font-size:.8rem;cursor:pointer}}
button:hover{{background:#232938}}
.tblhead{{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap}}
.tblhead h2{{margin-bottom:0}}
.filters{{display:flex;gap:.4rem;align-items:center;margin:.4rem 0 .5rem}}
.filters select{{background:#1a1e29;color:#dde1e8;border:1px solid #2b3242;border-radius:6px;padding:.15rem .45rem;font-size:.78rem}}
.sort-button{{border:0;background:none;padding:0;font:inherit;letter-spacing:inherit;text-transform:inherit}}
#pollerbtn{{display:inline-flex;align-items:center;gap:.4rem;background:#1c2230;border:1px solid #39455c;box-shadow:0 1px 0 #0006;padding:.22rem .7rem;font-weight:600}}
#pollerbtn:hover{{background:#263042;border-color:#4d5b76}}
#pollerbtn:active{{transform:translateY(1px)}}
#pollerbtn .dot{{width:7px;height:7px;border-radius:50%;background:#4ade80;box-shadow:0 0 6px #4ade8099}}
#pollerbtn.off .dot{{background:#6b7280;box-shadow:none}}
#pollerbtn .hint{{color:#8b93a5;font-weight:400}}
#updated{{color:#a5aec0;font-size:.78rem}}
.ev-msg{{font-family:ui-monospace,SFMono-Regular,monospace;font-size:.8rem;color:#b8bfcd}}
.pager{{display:flex;align-items:center;gap:.7rem;margin-top:.6rem}}
tr.filler td{{height:1.72rem}} tr.filler:hover{{background:none}}
button:disabled{{opacity:.4;cursor:default}}
button:focus,select:focus,input:focus{{outline:none}}
button:focus-visible,select:focus-visible,input:focus-visible,a:focus-visible,summary:focus-visible{{outline:2px solid #7aa2ff;outline-offset:3px}}
#cfgbtn{{font-size:.8rem}}
.modal{{position:fixed;inset:0;background:#07090dcc;display:flex;align-items:flex-start;justify-content:center;padding-top:12vh;z-index:10;backdrop-filter:blur(2px)}}
.modal.hidden{{display:none}}
#cfgpanel{{background:#141824;border:1px solid #2f3748;border-radius:14px;padding:1.4rem 1.6rem 1.2rem;display:flex;flex-direction:column;gap:1.05rem;min-width:420px;font-size:.9rem;color:#8b93a5;box-shadow:0 16px 48px #000c;border-top:2px solid #1f6feb;animation:pop .12s ease-out}}
@keyframes pop{{from{{transform:scale(.97);opacity:0}}to{{transform:scale(1);opacity:1}}}}
#cfgpanel .cfghead{{display:flex;justify-content:space-between;align-items:flex-start}}
#cfgpanel .cfgtitle{{color:#dde1e8;font-weight:650;font-size:1.05rem}}
#cfgpanel .cfgsub{{color:#6b7280;font-size:.78rem;margin-top:.25rem}}
#cfgpanel .x{{background:none;border:none;color:#6b7280;font-size:1.15rem;padding:0 .2rem;line-height:1;cursor:pointer}}
#cfgpanel .x:hover{{color:#dde1e8}}
#cfgpanel .cfgrow{{display:flex;align-items:center;justify-content:space-between;gap:1.4rem}}
#cfgpanel .cfgrow>span{{font-size:.88rem;color:#b8bfcd}}
#cfgpanel select,#cfgpanel input{{background:#1a1e29;color:#dde1e8;border:1px solid #2b3242;border-radius:8px;padding:.45rem .6rem;font-size:.88rem;width:11rem;outline:none;transition:border-color .1s}}
#cfgpanel select:focus,#cfgpanel input:focus{{border-color:#1f6feb}}
#cfgpanel .cfgfoot{{display:flex;align-items:center;gap:.7rem;justify-content:flex-end;border-top:1px solid #232733;padding-top:1rem;margin-top:.2rem}}
#cfgpanel .primary{{background:#1f6feb;border-color:#1f6feb;color:#fff;font-weight:600;padding:.42rem 1.3rem;font-size:.88rem}}
#cfgpanel .primary:hover{{background:#3b82f6;border-color:#3b82f6}}
@media(max-width:800px){{main{{padding:1rem}}.cards{{grid-template-columns:repeat(2,minmax(0,1fr))}}.health{{grid-template-columns:1fr}}}}
</style>
<main>
<header>
<h1>Devin Issue Remediator</h1>
<span class="meta">{settings.github_repo} · trigger <span class="label">{settings.trigger_label}</span></span>
<button id="pollerbtn" onclick="togglePoller()" title="Toggle issue polling"></button>
<span id="updated"></span>
<nav class="links"><button id="cfgbtn" onclick="toggleCfg()" title="Runtime settings">config</button><a href="/api/tasks" target="_blank" rel="noopener">api</a></nav>
</header>
<div id="cfgmodal" class="modal hidden" onclick="if(event.target===this)toggleCfg()">
<div id="cfgpanel">
  <div class="cfghead">
    <div><div class="cfgtitle">Runtime settings</div>
    <div class="cfgsub">apply to the next dispatched session</div></div>
    <button class="x" onclick="toggleCfg()" title="Close">×</button>
  </div>
  <div class="cfgrow"><span>agent mode</span><select id="cfg-mode"></select></div>
  <div class="cfgrow"><span>max ACUs / session</span><input id="cfg-acu" type="number" min="0" step="1"></div>
  <div class="cfgrow"><span>poll interval (s)</span><input id="cfg-poll" type="number" min="5" step="5"></div>
  <div class="cfgfoot"><span id="cfgmsg" class="sub"></span><button class="primary" onclick="saveCfg()">save</button></div>
</div>
</div>
<div id="refresh-error" class="banner hidden" role="alert"></div>
<noscript><p>This dashboard requires JavaScript. <a href="/api/tasks">View remediation data</a>.</p></noscript>
<h2>Overview <span class="sub">· all time</span></h2>
<div class="cards" id="stats" aria-label="Remediation outcomes"></div>
<div class="secondary" id="secondary-stats"></div>
<h2>Needs attention</h2>
<div class="attention" id="attention" aria-label="Attention filters"></div>
<span class="sub">Open PRs may need review or merge. Categories can overlap.</span>
<h2>Integration health</h2>
<div class="health" id="integrations"><div class="sub">Waiting for sync status…</div></div>
<div class="tblhead"><h2>Remediations</h2>
<div class="filters"><span id="active-filter" class="sub"></span><button id="clear-filter" class="hidden" onclick="setFilter('all')">Clear filter</button><label class="sub" for="statefilter">State</label>
<select id="statefilter" onchange="setFilter(this.value)">
<option value="all">All states</option><option value="queued">Queued</option><option value="running">Running</option>
<option value="pr_opened">PR open</option><option value="merged">Merged</option><option value="failed">Failed</option>
</select></div></div>
<div class="table-wrap"><table id="remediations"><thead><tr><th id="issueth" aria-sort="descending"><button class="sort-button" onclick="toggleSort()">Issue <span id="sortarrow">↓</span></button></th>
<th>Status</th><th>CI</th><th>Actions</th><th>Detected</th></tr></thead>
<tbody id="taskrows"><tr><td colspan="5">Loading remediations…</td></tr></tbody></table></div>
<div class="pager">
<button id="tprev" onclick="tpage(-1)">‹ prev</button>
<span id="tpageinfo" class="sub"></span>
<button id="tnext" onclick="tpage(1)">next ›</button>
</div>
<h2>Recent events</h2>
<table><thead><tr><th>When</th><th>Kind</th><th>Issue</th><th>Message</th></tr></thead>
<tbody id="eventrows">{evs}</tbody></table>
<div class="pager">
<button id="prev" onclick="page(-1)">‹ newer</button>
<span id="pageinfo" class="sub"></span>
<button id="next" onclick="page(1)">older ›</button>
</div>
</main>
<script>
const ago = ts => {{
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s >= 86400) return `${{Math.floor(s / 86400)}}d ${{Math.floor(s % 86400 / 3600)}}h ago`;
  if (s >= 3600) return `${{Math.floor(s / 3600)}}h ${{Math.floor(s % 3600 / 60)}}m ago`;
  return s >= 60 ? `${{Math.floor(s / 60)}}m ago` : `${{s}}s ago`;
}};
const PAGE = 15;
let offset = 0;
const ISSUE_BASE = 'https://github.com/{settings.github_repo}/issues/';
const esc = s => String(s).replace(/[&<>"]/g, c => ({{'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}})[c]);
const linkify = s => esc(s).replace(/https?:\/\/\S+/g,
  u => `<a href="${{u}}" target="_blank" rel="noopener">${{u}}</a>`);
const waitingForInput = r => r.state === 'running' && !r.pr_url && r.detail === 'waiting_for_user';
const openPR = r => r.state === 'pr_opened' && (!r.pr_state || r.pr_state === 'open');
const stateLabels = {{queued: 'Queued', running: 'Working', pr_opened: 'PR open', merged: 'Merged', failed: 'Failed'}};
const attentionLabels = {{review: 'PRs to review', ci: 'Failing CI', failed: 'Failed remediations', input: 'Waiting for input'}};
let cached = null, integrationHealth = {{}}, pageFresh = false, refreshId = 0;
const checksFresh = r => pageFresh && ['pr', 'checks'].every(kind =>
  integrationHealth.github?.resources?.[`${{kind}}:${{r.issue_number}}`]?.status === 'healthy');
function needsAttention(r, filter) {{
  return filter === 'review' ? openPR(r)
    : filter === 'ci' ? openPR(r) && r.pr_checks === 'failing' && checksFresh(r)
    : filter === 'failed' ? r.state === 'failed'
    : filter === 'input' ? waitingForInput(r) : true;
}}
const pill = r => {{
  const waiting = waitingForInput(r);
  const label = waiting ? 'Waiting for input' : stateLabels[r.state] || r.state;
  return `<span class="pill ${{waiting ? 'waiting' : esc(r.state)}}">${{esc(label)}}</span>`;
}};
function ciBadge(r) {{
  if (!r.pr_url) return '<span class="sub">—</span>';
  const label = {{passing: 'Passing', failing: 'Failing', pending: 'Pending'}}[r.pr_checks] || 'No checks';
  if (openPR(r) && !checksFresh(r)) {{
    return `<span class="ci unknown">Unverified</span><span class="sub ci-note">Last reported: ${{esc(label)}}</span>`;
  }}
  const note = openPR(r) ? '' : '<span class="sub ci-note">Last recorded</span>';
  return `<span class="ci ${{r.pr_checks ? esc(r.pr_checks) : 'unknown'}}">${{esc(label)}}</span>${{note}}`;
}}
function renderHealth() {{
  const labels = {{healthy: 'Healthy', error: 'Sync error', stale: 'Stale', pending: 'Awaiting sync', idle: 'Idle'}};
  document.getElementById('integrations').innerHTML = ['github', 'devin'].map(provider => {{
    const reading = integrationHealth[provider] || {{status: 'pending', resources: {{}}}};
    const status = pageFresh ? reading.status : 'pending';
    const last = reading.last_success ? ago(reading.last_success) : 'not yet confirmed';
    let description = `Last successful sync: ${{last}}`;
    if (status === 'idle') description = provider === 'github'
      ? 'Issue polling paused; no open PRs to track.' : 'No active sessions to track.';
    if (status === 'stale') description += ' · Tracking is overdue.';
    const failures = Object.entries(reading.resources).filter(([, r]) => r.error).map(([key, r]) => {{
      const [kind, number] = key.split(':');
      const name = {{issues: 'Issue polling', pr: 'PR', checks: 'CI', session: 'Session'}}[kind] || kind;
      return `${{name}}${{number ? ` #${{number}}` : ''}}: ${{r.error}}`;
    }});
    if (failures.length) description += ` · ${{failures.join('; ')}}`;
    if (!pageFresh) description = 'Dashboard connection unavailable; sync status cannot be verified.';
    return `<div class="health-item"><strong>${{provider === 'github' ? 'GitHub' : 'Devin'}}</strong>`
      + `<span class="pill ${{status}}">${{pageFresh ? labels[status] : 'Unknown'}}</span>`
      + `<span class="sub">${{esc(description)}}</span></div>`;
  }}).join('');
}}
function renderTasks() {{
  if (!cached) return;
  const t = cached, bs = t.counts.by_state, tot = t.counts.totals || {{}};
  const durations = t.counts.durations || {{}};
  const dur = s => s == null ? '—' : (s >= 3600 ? `${{(s / 3600).toFixed(1)}}h` : `${{(s / 60).toFixed(1)}}m`);
  const cards = [
    ['Awaiting review', t.tasks.filter(openPR).length, 'Open PRs · review or merge'],
    ['Merged', bs.merged || 0, 'Remediations delivered'],
    ['Failed', bs.failed || 0, 'Needs investigation'],
    ['Agent turnaround', dur(durations.avg_s), `Average dispatch → first PR · ${{durations.sample_count || 0}} runs`],
  ];
  document.getElementById('stats').innerHTML = cards.map(([l, n, detail]) =>
    `<div class="card"><div class="l">${{l}}</div><div class="n num">${{n}}</div><div class="sub">${{detail}}</div></div>`).join('');
  const metered = t.tasks.filter(r => r.acus != null);
  const usage = metered.length ? metered.reduce((sum, r) => sum + r.acus, 0).toFixed(1) : 'Unavailable';
  document.getElementById('secondary-stats').innerHTML =
    `<span>Issues <strong>${{t.tasks.length}}</strong></span>`
    + `<span>PRs opened <strong>${{t.tasks.filter(r => r.pr_url).length}}</strong></span>`
    + `<span title="Sum of reported Agent Compute Units; missing usage is excluded">Reported ACUs <strong>${{usage}}</strong> · ${{metered.length}}/${{t.tasks.length}} metered</span>`
    + `<span>Change size <strong>+${{tot.additions || 0}} / −${{tot.deletions || 0}}</strong> · ${{tot.files || 0}} files</span>`;
  const unverified = t.tasks.some(r => openPR(r) && !checksFresh(r));
  document.getElementById('attention').innerHTML = Object.entries(attentionLabels).map(([key, label]) => {{
    const count = t.tasks.filter(r => needsAttention(r, key)).length;
    const suffix = key === 'ci' && unverified ? ' · incomplete' : '';
    return `<button onclick="setAttention('${{key}}')" aria-pressed="${{attentionFilter === key}}" `
      + `title="${{key === 'ci' ? 'Only freshly verified CI failures are counted. Check integration health for missing results.' : label}}">`
      + `<strong class="num">${{count}}</strong> ${{label}}${{suffix}}</button>`;
  }}).join('');
  document.getElementById('statefilter').value = stateFilter;
  document.getElementById('active-filter').textContent = attentionLabels[attentionFilter] || '';
  document.getElementById('clear-filter').classList.toggle('hidden', !attentionFilter && stateFilter === 'all');
  const rows = t.tasks
    .filter(r => (stateFilter === 'all' || r.state === stateFilter) && needsAttention(r, attentionFilter))
    .sort((a, b) => sortAsc ? a.issue_number - b.issue_number : b.issue_number - a.issue_number);
  const ttotal = rows.length;
  taskOffset = Math.min(taskOffset, Math.max(0, Math.ceil(ttotal / TPAGE) - 1) * TPAGE);
  document.getElementById('sortarrow').textContent = sortAsc ? '↑' : '↓';
  document.getElementById('issueth').setAttribute('aria-sort', sortAsc ? 'ascending' : 'descending');
  const expanded = new Set(Array.from(document.querySelectorAll('#taskrows details[open]'), el => el.dataset.issue));
  document.getElementById('taskrows').innerHTML = rows.slice(taskOffset, taskOffset + TPAGE).map(r => {{
    const iss = `<a class="issue-title" href="${{ISSUE_BASE + r.issue_number}}" target="_blank" rel="noopener"><span class="issue-number">#${{r.issue_number}}</span> ${{esc(r.issue_title)}}</a>`;
    const sess = r.session_url
      ? `<a href="${{esc(r.session_url)}}" target="_blank" rel="noopener">Open session ↗</a>` : '';
    const pr = r.pr_url
      ? `<a href="${{esc(r.pr_url)}}" target="_blank" rel="noopener">${{openPR(r) ? 'Review PR' : 'View PR'}} ↗</a>` : '';
    const size = r.pr_additions == null ? 'Not reported'
      : `<span class="add">+${{r.pr_additions}}</span> <span class="del">−${{r.pr_deletions}}</span>`
        + ` <span class="sub">· ${{r.pr_files}} file${{r.pr_files === 1 ? '' : 's'}}</span>`;
    const acus = r.acus == null ? 'Not reported' : r.acus.toFixed(2);
    const detail = r.state === 'failed' && r.detail
      ? `<div class="detail-text">Failure: ${{esc(r.detail)}}</div>` : '';
    return `<tr><td class="issue-cell">${{iss}}<details data-issue="${{r.issue_number}}" ${{expanded.has(String(r.issue_number)) ? 'open' : ''}}>`
      + `<summary>Details <span class="sub">· size, usage, mode</span></summary><div class="metrics">`
      + `<span>Change size: ${{size}}</span><span>ACUs: ${{acus}}</span><span>Mode: ${{esc(r.devin_mode || 'Not reported')}}</span></div>${{detail}}</details></td>`
      + `<td>${{pill(r)}}</td><td>${{ciBadge(r)}}</td><td class="actions">${{pr}}${{sess}}</td>`
      + `<td class="sub detected"><time title="${{esc(new Date(r.created_at * 1000).toLocaleString())}}" datetime="${{new Date(r.created_at * 1000).toISOString()}}">${{ago(r.created_at)}}</time></td></tr>`;
  }}).join('') || `<tr><td colspan="5">${{t.tasks.length ? 'No matching remediations' : 'No issues yet'}}</td></tr>`;
  const tfrom = ttotal ? taskOffset + 1 : 0;
  document.getElementById('tpageinfo').textContent =
    `${{tfrom}}–${{Math.min(taskOffset + TPAGE, ttotal)}} of ${{ttotal}}`;
  document.getElementById('tprev').disabled = taskOffset === 0;
  document.getElementById('tnext').disabled = taskOffset + TPAGE >= ttotal;
}}
async function fetchJSON(url) {{
  const response = await fetch(url, {{signal: AbortSignal.timeout(10000)}});
  if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
  return response.json();
}}
async function refresh() {{
  const requestId = ++refreshId;
  try {{
    const [t, e, p, health] = await Promise.all([
      fetchJSON('/api/tasks'),
      fetchJSON(`/api/events?limit=${{PAGE}}&offset=${{offset}}`),
      fetchJSON('/api/poller'),
      fetchJSON('/api/integrations'),
    ]);
    if (requestId !== refreshId) return;
    cached = t;
    integrationHealth = health;
    pageFresh = true;
    document.getElementById('refresh-error').classList.add('hidden');
    renderTasks();
    renderHealth();
    document.getElementById('eventrows').innerHTML = e.events.map(ev =>
      `<tr><td class="sub">${{ago(ev.ts)}}</td><td>${{esc(ev.kind)}}</td>`
      + `<td>${{ev.issue_number ? `<a href="${{ISSUE_BASE + ev.issue_number}}" target="_blank" rel="noopener">#${{ev.issue_number}}</a>` : ''}}</td>`
      + `<td class="ev-msg">${{linkify(ev.message)}}</td></tr>`
    ).join('') + '<tr class="filler"><td colspan=4></td></tr>'.repeat(
      Math.max(0, PAGE - e.events.length));
    const from = e.total ? offset + 1 : 0;
    document.getElementById('pageinfo').textContent =
      `${{from}}–${{offset + e.events.length}} of ${{e.total}}`;
    document.getElementById('prev').disabled = offset === 0;
    document.getElementById('next').disabled = offset + e.events.length >= e.total;
    const pb = document.getElementById('pollerbtn');
    pb.className = p.enabled ? '' : 'off';
    pb.innerHTML = '<span class="dot"></span>' + (p.enabled
      ? `polling every ${{p.interval_seconds}}s <span class="hint">· pause</span>`
      : 'polling paused <span class="hint">· start</span>');
    lastUpdate = Date.now() / 1000;
    tickUpdated();
  }} catch (error) {{
    if (requestId !== refreshId) return;
    showRefreshError();
  }}
}}
function showRefreshError() {{
  pageFresh = false;
  const banner = document.getElementById('refresh-error');
  banner.textContent = cached ? 'Dashboard refresh failed. Showing cached data; live status is unverified. Retrying automatically.'
    : 'Unable to load dashboard data. Retrying automatically.';
  banner.classList.remove('hidden');
  renderTasks();
  renderHealth();
}}
const TPAGE = 10;
let taskOffset = 0, sortAsc = false, stateFilter = 'all', attentionFilter = '';
function setFilter(s) {{ stateFilter = s; attentionFilter = ''; taskOffset = 0; renderTasks(); }}
function setAttention(s) {{ attentionFilter = attentionFilter === s ? '' : s; stateFilter = 'all'; taskOffset = 0; renderTasks(); }}
function toggleSort() {{ sortAsc = !sortAsc; taskOffset = 0; renderTasks(); }}
function tpage(d) {{
  taskOffset = Math.max(0, taskOffset + d * TPAGE);
  renderTasks();
}}
let lastUpdate = null;
function tickUpdated() {{
  document.getElementById('updated').textContent = lastUpdate
    ? 'Dashboard fetched ' + ago(lastUpdate) : 'Dashboard not yet loaded';
  if (pageFresh && lastUpdate && Date.now() / 1000 - lastUpdate > 30) showRefreshError();
}}
async function page(d) {{
  offset = Math.max(0, offset + d * PAGE);
  await refresh();
  window.scrollTo({{top: document.body.scrollHeight, behavior: 'smooth'}});
}}
function toggleCfg() {{
  document.getElementById('cfgmodal').classList.toggle('hidden');
}}
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') document.getElementById('cfgmodal').classList.add('hidden');
}});
let cfgLoaded = false;
async function loadCfg() {{
  if (cfgLoaded) return;
  cfgLoaded = true;
  const c = await fetch('/api/config').then(r => r.json());
  const sel = document.getElementById('cfg-mode');
  sel.innerHTML = '<option value="">org default</option>'
    + c.modes.map(m => `<option${{m === c.devin_mode ? ' selected' : ''}}>${{m}}</option>`).join('');
  document.getElementById('cfg-acu').value = c.devin_max_acu_limit;
  document.getElementById('cfg-poll').value = c.poll_interval_seconds;
}}
async function saveCfg() {{
  const body = {{
    devin_mode: document.getElementById('cfg-mode').value,
    devin_max_acu_limit: +document.getElementById('cfg-acu').value,
    poll_interval_seconds: +document.getElementById('cfg-poll').value,
  }};
  const r = await fetch('/api/config', {{method: 'POST',
    headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(body)}});
  document.getElementById('cfgmsg').textContent = r.ok ? 'saved' : await r.text();
  setTimeout(() => document.getElementById('cfgmsg').textContent = '', 3000);
  refresh();
}}
async function togglePoller() {{
  const cur = await fetch('/api/poller').then(r => r.json());
  await fetch('/api/poller', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{enabled: !cur.enabled}}),
  }});
  refresh();
}}
loadCfg();
refresh();
setInterval(refresh, 10000);
setInterval(tickUpdated, 1000);
</script>
</html>"""


def _linkify(message: str) -> str:
    return re.sub(
        r"https?://\S+",
        lambda m: f'<a href="{m.group(0)}" target="_blank" rel="noopener">{m.group(0)}</a>',
        html.escape(message))


def _ago(ts: float) -> str:
    s = int(time.time() - ts)
    if s >= 86400:
        return f"{s // 86400}d {s % 86400 // 3600}h ago"
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60}m ago"
    return f"{s // 60}m ago" if s >= 60 else f"{s}s ago"
