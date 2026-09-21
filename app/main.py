import hashlib
import hmac
import html
import logging
import re
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse

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


@app.post("/scan")
def scan_now():
    """Manual trigger — useful for demos without a public webhook."""
    return {"dispatched": dispatcher.poll_issues_once()}


@app.post("/issues/{number}/dispatch")
def dispatch_one(number: int):
    issue = dispatcher.gh.get_issue(number)
    return {"dispatched": dispatcher.consider_issue(issue, source="manual")}


# ---------- observability ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/tasks")
def tasks():
    return {"tasks": store.all(), "counts": store.counts()}


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


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    c = store.counts()
    lines = [
        "# HELP issues_detected_total Issues seen carrying the trigger label",
        "# TYPE issues_detected_total counter",
        f"issues_detected_total {c['by_kind'].get('detected', 0)}",
        "# HELP devin_sessions_dispatched_total Devin sessions created",
        "# TYPE devin_sessions_dispatched_total counter",
        f"devin_sessions_dispatched_total {c['by_kind'].get('dispatched', 0)}",
        "# HELP prs_opened_total Remediations that produced a PR",
        "# TYPE prs_opened_total counter",
        f"prs_opened_total {c['by_kind'].get('pr_opened', 0)}",
        "# HELP remediations_failed_total Remediations that failed",
        "# TYPE remediations_failed_total counter",
        f"remediations_failed_total {c['by_state'].get('failed', 0)}",
        "# HELP remediations_merged_total Remediations merged into the target repo",
        "# TYPE remediations_merged_total counter",
        f"remediations_merged_total {c['by_state'].get('merged', 0)}",
        "# HELP remediations_inflight Currently queued, running, or awaiting PR merge",
        "# TYPE remediations_inflight gauge",
        f"remediations_inflight {c['by_state'].get('queued', 0) + c['by_state'].get('running', 0) + c['by_state'].get('pr_opened', 0)}",
        "# HELP remediation_duration_seconds Time from dispatch to completion",
        "# TYPE remediation_duration_seconds summary",
    ]
    d = c["durations"]
    for k in ("avg_s", "min_s", "max_s"):
        v = d.get(k)
        lines.append(f"remediation_duration_seconds{{stat=\"{k}\"}} {v or 0}")
    return "\n".join(lines) + "\n"


@app.get("/report", response_class=PlainTextResponse)
def report():
    c = store.counts()
    t = store.all()
    merged = c["by_state"].get("merged", 0)
    failed = c["by_state"].get("failed", 0)
    inflight = (c["by_state"].get("queued", 0) + c["by_state"].get("running", 0)
                + c["by_state"].get("pr_opened", 0))
    d = c["durations"]
    lines = [
        "# Remediation report",
        f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}_", "",
        f"- Issues detected: **{len(t)}**",
        f"- Merged: **{merged}** | Failed: **{failed}** | In flight (queued/running/awaiting merge): **{inflight}**",
        f"- PRs opened: **{c['by_kind'].get('pr_opened', 0)}**",
        f"- Avg time to remediate: **{_fmt_s(d.get('avg_s'))}** "
        f"(min {_fmt_s(d.get('min_s'))}, max {_fmt_s(d.get('max_s'))})",
        f"- Merge rate (PRs merged / opened): **{(merged / c['by_kind'].get('pr_opened', 0) * 100) if c['by_kind'].get('pr_opened') else 0:.0f}%**",
        "", "| Issue | State | Session | PR |", "|---|---|---|---|",
    ]
    for r in t:
        lines.append(
            f"| #{r['issue_number']} {r['issue_title'][:50]} | {r['state']} | "
            f"[session]({r['session_url']}) | {r['pr_url'] or '—'} |")
    return "\n".join(lines) + "\n"


def _fmt_s(v):
    return f"{v / 60:.1f}m" if v else "—"


@app.get("/", response_class=HTMLResponse)
def dashboard():
    rows = ""
    for r in store.all():
        age = _ago(r["created_at"])
        issue = (f'<a href="https://github.com/{settings.github_repo}/issues/{r["issue_number"]}"'
                 f' target="_blank" rel="noopener">#{r["issue_number"]}</a>')
        sess = (f'<a href="{r["session_url"]}" target="_blank" rel="noopener">session</a>'
                if r["session_url"] else "—")
        pr = (f'<a href="{r["pr_url"]}" target="_blank" rel="noopener">PR</a>'
              if r["pr_url"] else "—")
        pr_state = f" ({r['pr_state']})" if r.get("pr_state") else ""
        detail = _progress(r)
        rows += (f"<tr><td>{issue}</td><td>{html.escape(r['issue_title'])}</td>"
                 f"<td class='{r['state']}'>{r['state']}{pr_state}</td>"
                 f"<td>{sess}</td><td>{pr}</td><td>{html.escape(detail)}</td>"
                 f"<td>{age}</td></tr>")
    evs = ""
    for e in store.recent_events(15):
        issue = ""
        if e["issue_number"]:
            issue = (f'<a href="https://github.com/{settings.github_repo}'
                     f'/issues/{e["issue_number"]}" target="_blank" rel="noopener">'
                     f'#{e["issue_number"]}</a>')
        evs += (f"<tr><td>{_ago(e['ts'])}</td><td>{e['kind']}</td>"
                f"<td>{issue}</td><td>{_linkify(str(e['message']))}</td></tr>")
    return f"""<!doctype html>
<title>Devin Issue Remediator</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:ui-sans-serif,system-ui,sans-serif;margin:0;background:#0b0d12;color:#dde1e8;font-size:15px}}
main{{padding:1.2rem 1.4rem 2rem}}
header{{display:flex;align-items:baseline;gap:.8rem;flex-wrap:wrap;border-bottom:1px solid #232733;padding-bottom:.7rem;margin-bottom:1rem}}
h1{{font-size:1.05rem;font-weight:650;margin:0}}
h2{{font-size:.8rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#8b93a5;margin:1.4rem 0 .5rem}}
.meta{{color:#8b93a5;font-size:.82rem}}
.label{{background:#1f6feb33;color:#7aa2ff;border:1px solid #1f6feb66;padding:.05rem .45rem;border-radius:999px;font-size:.75rem;font-weight:600;font-family:ui-monospace,SFMono-Regular,monospace}}
a{{color:#7aa2ff;text-decoration:none}} a:hover{{text-decoration:underline}}
header .links{{margin-left:auto;font-size:.82rem;display:flex;gap:.9rem;align-items:center}}
.cards{{display:flex;gap:.6rem;flex-wrap:wrap}}
.card{{background:#12151d;border:1px solid #232733;border-radius:8px;padding:.5rem .9rem;min-width:100px}}
.card .n{{font-size:1.25rem;font-weight:650;line-height:1.1}}
.card .l{{color:#8b93a5;font-size:.72rem;letter-spacing:.04em;text-transform:uppercase}}
table{{border-collapse:collapse;width:100%;background:#12151d;border:1px solid #232733;border-radius:8px;overflow:hidden}}
th{{background:#161a24;color:#8b93a5;font-size:.72rem;letter-spacing:.05em;text-transform:uppercase;padding:.45rem .7rem;text-align:left}}
td{{border-top:1px solid #232733;padding:.42rem .7rem;font-size:.86rem;vertical-align:top}}
tbody tr:hover{{background:#181d29}}
.pill{{display:inline-block;padding:.1rem .55rem;border-radius:999px;font-size:.75rem;font-weight:600}}
.pill.queued{{background:#2a2f3c;color:#b8bfcd}}
.pill.running{{background:#3a2e14;color:#fbbf24}}
.pill.pr_opened{{background:#2a2140;color:#c4b5fd}}
.pill.merged{{background:#123528;color:#4ade80}}
.pill.failed{{background:#3b1a1a;color:#f87171}}
.sub{{color:#8b93a5;font-size:.75rem}}
button{{background:#1a1e29;color:#dde1e8;border:1px solid #2b3242;border-radius:6px;padding:.2rem .6rem;font-size:.8rem;cursor:pointer}}
button:hover{{background:#232938}}
#pollerbtn{{display:inline-flex;align-items:center;gap:.4rem;background:#1c2230;border:1px solid #39455c;box-shadow:0 1px 0 #0006;padding:.22rem .7rem;font-weight:600}}
#pollerbtn:hover{{background:#263042;border-color:#4d5b76}}
#pollerbtn:active{{transform:translateY(1px)}}
#pollerbtn .dot{{width:7px;height:7px;border-radius:50%;background:#4ade80;box-shadow:0 0 6px #4ade8099}}
#pollerbtn.off .dot{{background:#6b7280;box-shadow:none}}
#pollerbtn .hint{{color:#8b93a5;font-weight:400}}
#updated{{color:#6b7280;font-size:.78rem}}
.ev-msg{{font-family:ui-monospace,SFMono-Regular,monospace;font-size:.8rem;color:#b8bfcd}}
.pager{{display:flex;align-items:center;gap:.7rem;margin-top:.6rem}}
tr.filler td{{height:1.72rem}} tr.filler:hover{{background:none}}
button:disabled{{opacity:.4;cursor:default}}
</style>
<main>
<header>
<h1>Devin Issue Remediator</h1>
<span class="meta">{settings.github_repo} · trigger <span class="label">{settings.trigger_label}</span></span>
<button id="pollerbtn" onclick="togglePoller()" title="Toggle issue polling"></button>
<span id="updated"></span>
<nav class="links"><a href="/report">report</a><a href="/metrics">metrics</a><a href="/api/tasks">api</a></nav>
</header>
<div class="cards" id="stats"></div>
<h2>Remediations</h2>
<table><thead><tr><th>Issue</th><th>Title</th><th>State</th><th>Session</th><th>PR</th>
<th>Progress</th><th>Seen</th></tr></thead><tbody id="taskrows">{rows}</tbody></table>
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
const pill = r => {{
  const ps = r.pr_state ? ` <span class="sub">· ${{esc(r.pr_state)}}</span>` : '';
  return `<span class="pill ${{r.state}}">${{esc(r.state)}}</span>${{ps}}`;
}};
async function refresh() {{
  const [t, e, p] = await Promise.all([
    fetch('/api/tasks').then(r => r.json()),
    fetch(`/api/events?limit=${{PAGE}}&offset=${{offset}}`).then(r => r.json()),
    fetch('/api/poller').then(r => r.json()),
  ]);
  const bs = t.counts.by_state, bk = t.counts.by_kind;
  const cards = [
    ['issues', t.tasks.length],
    ['prs opened', bk.pr_opened || 0],
    ['merged', bs.merged || 0],
    ['failed', bs.failed || 0],
  ];
  document.getElementById('stats').innerHTML = cards.map(([l, n]) =>
    `<div class="card"><div class="n">${{n}}</div><div class="l">${{l}}</div></div>`).join('');
  const st = r => {{
    if (r.state === 'pr_opened') {{
      return r.pr_state === 'closed' ? 'PR closed' : 'awaiting merge';
    }}
    if (r.state === 'merged') return 'PR merged';
    return {{waiting_for_user: 'working', working: 'working'}}[r.detail] || (r.detail || '');
  }};
  document.getElementById('taskrows').innerHTML = t.tasks.map(r => {{
    const iss = `<a href="${{ISSUE_BASE + r.issue_number}}" target="_blank" rel="noopener">#${{r.issue_number}}</a>`;
    const sess = r.session_url
      ? `<a href="${{r.session_url}}" target="_blank" rel="noopener">session</a>` : '—';
    const pr = r.pr_url
      ? `<a href="${{r.pr_url}}" target="_blank" rel="noopener">PR</a>` : '—';
    return `<tr><td>${{iss}}</td><td>${{esc(r.issue_title)}}</td>`
      + `<td>${{pill(r)}}</td><td>${{sess}}</td><td>${{pr}}</td>`
      + `<td class="sub">${{esc(st(r))}}</td><td class="sub">${{ago(r.created_at)}}</td></tr>`;
  }}).join('') || '<tr><td colspan=7>No issues yet</td></tr>';
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
}}
let lastUpdate = Date.now() / 1000;
function tickUpdated() {{
  document.getElementById('updated').textContent = 'updated ' + ago(lastUpdate);
}}
async function page(d) {{
  offset = Math.max(0, offset + d * PAGE);
  await refresh();
  window.scrollTo({{top: document.body.scrollHeight, behavior: 'smooth'}});
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
refresh();
setInterval(refresh, 10000);
setInterval(tickUpdated, 1000);
</script>"""


def _progress(r: dict) -> str:
    if r["state"] == "pr_opened":
        return "PR closed" if r.get("pr_state") == "closed" else "awaiting merge"
    if r["state"] == "merged":
        return "PR merged"
    detail = str(r.get("detail") or "")
    return "working" if detail in ("working", "waiting_for_user") else detail


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
