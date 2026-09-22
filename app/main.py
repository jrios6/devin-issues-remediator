import hashlib
import hmac
import logging
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from .config import load
from .dashboard import render_dashboard
from .db import Store
from .dispatcher import Dispatcher

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

# Import must not raise: deploy tooling imports this module to locate `app`.
try:
    settings = load()
    store = Store(settings.db_path)
    dispatcher = Dispatcher(settings, store)
except RuntimeError:
    settings = store = dispatcher = None
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
    return render_dashboard(settings, store)
