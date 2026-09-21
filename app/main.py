import hashlib
import hmac
import html
import logging
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
def events(limit: int = 50):
    return {"events": store.recent_events(limit)}


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
        sess = f'<a href="{r["session_url"]}">session</a>' if r["session_url"] else "—"
        pr = f'<a href="{r["pr_url"]}">PR</a>' if r["pr_url"] else "—"
        pr_state = f" ({r['pr_state']})" if r.get("pr_state") else ""
        rows += (f"<tr><td>#{r['issue_number']}</td><td>{html.escape(r['issue_title'])}</td>"
                 f"<td class='{r['state']}'>{r['state']}{pr_state}</td>"
                 f"<td>{sess}</td><td>{pr}</td><td>{html.escape(str(r.get('detail') or ''))}</td>"
                 f"<td>{age}</td></tr>")
    evs = "".join(
        f"<tr><td>{_ago(e['ts'])}</td><td>{e['kind']}</td>"
        f"<td>#{e['issue_number'] or ''}</td><td>{html.escape(str(e['message']))}</td></tr>"
        for e in store.recent_events(20))
    return f"""<!doctype html><meta http-equiv="refresh" content="15">
<title>Devin Issue Remediator</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;background:#0f1117;color:#e6e6e6}}
table{{border-collapse:collapse;width:100%;margin-bottom:2rem}}
td,th{{border:1px solid #333;padding:.4rem .6rem;text-align:left;font-size:.9rem}}
a{{color:#7aa2ff}} .running{{color:#fbbf24}} .succeeded{{color:#34d399}}
.merged{{color:#34d399}} .pr_opened{{color:#a78bfa}}
.failed{{color:#f87171}} .queued{{color:#9ca3af}} h1,h2{{font-weight:600}}
</style>
<h1>Devin Issue Remediator — {settings.github_repo}</h1>
<p>Trigger label: <code>{settings.trigger_label}</code> ·
Poller: {"every " + str(settings.poll_interval_seconds) + "s" if settings.poll_interval_seconds else "off"} ·
<a href="/report">report</a> · <a href="/metrics">metrics</a> · <a href="/api/tasks">api</a></p>
<h2>Remediations</h2>
<table><tr><th>Issue</th><th>Title</th><th>State</th><th>Session</th><th>PR</th>
<th>Detail</th><th>Seen</th></tr>{rows or '<tr><td colspan=7>No issues yet</td></tr>'}</table>
<h2>Recent events</h2>
<table><tr><th>When</th><th>Kind</th><th>Issue</th><th>Message</th></tr>
{evs or '<tr><td colspan=4>No events yet</td></tr>'}</table>"""


def _ago(ts: float) -> str:
    s = int(time.time() - ts)
    return f"{s // 60}m{s % 60}s ago" if s >= 60 else f"{s}s ago"
