import hashlib
import hmac
import importlib
import json
import os
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Store
from app.dispatcher import Dispatcher


ISSUE = {
    "number": 7, "title": "Correct timezone handling",
    "html_url": "https://github.com/jrios6/superset/issues/7",
    "body": "Add a regression test under a non-UTC timezone.",
    "state": "open", "labels": [{"name": "devin-fix"}],
}
PR_URL = "https://github.com/jrios6/superset/pull/8"


@pytest.fixture
def workflow(tmp_path):
    settings = Settings(
        devin_api_key="test", devin_org_id="org-test", github_token="test",
        webhook_secret="test-secret", poll_interval_seconds=0,
        db_path=str(tmp_path / "lifecycle.db"),
    )
    store = Store(settings.db_path)
    dispatcher = Dispatcher(settings, store)
    requests = []
    session = {
        "session_id": "devin-test", "url": "https://app.devin.ai/sessions/test",
        "status": "running", "status_detail": "working", "pull_requests": [],
        "acus_consumed": 1.5, "devin_mode": "normal",
    }
    pr = {
        "state": "open", "merged_at": None, "created_at": "2026-09-01T12:00:00Z",
        "additions": 10, "deletions": 2, "changed_files": 2, "comments": 1,
        "review_comments": 1, "head": {"sha": "head-sha"},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if request.url.host == "api.devin.ai":
            if request.method == "POST" and path == "/v3/organizations/org-test/sessions":
                return httpx.Response(200, json=session)
            if request.method == "GET" and path == "/v3/organizations/org-test/sessions/devin-test":
                return httpx.Response(200, json=session)
        prefix = "/repos/jrios6/superset"
        if request.method == "GET" and path == f"{prefix}/issues":
            assert request.url.params["state"] == "open"
            assert request.url.params["labels"] == "devin-fix"
            return httpx.Response(200, json=[ISSUE])
        if request.method == "GET" and path == f"{prefix}/issues/7":
            return httpx.Response(200, json=ISSUE)
        if request.method == "GET" and path == f"{prefix}/pulls/8":
            return httpx.Response(200, json=pr)
        if request.method == "GET" and path == f"{prefix}/commits/head-sha/check-runs":
            return httpx.Response(200, json={"check_runs": [{
                "status": "completed", "conclusion": "success",
            }]})
        if request.method == "POST" and path in (
            f"{prefix}/issues/7/labels", f"{prefix}/issues/7/comments",
        ):
            return httpx.Response(201, json={})
        if request.method == "DELETE" and path.startswith(f"{prefix}/issues/7/labels/"):
            return httpx.Response(204)
        raise AssertionError(f"Unexpected provider request: {request.method} {request.url}")

    dispatcher.gh._c.close()
    dispatcher.devin._c.close()
    dispatcher.gh._c = httpx.Client(
        base_url="https://api.github.com/repos/jrios6/superset",
        transport=httpx.MockTransport(respond),
    )
    dispatcher.devin._c = httpx.Client(
        base_url="https://api.devin.ai/v3/organizations/org-test",
        transport=httpx.MockTransport(respond),
    )
    with patch.dict(os.environ, {
        "DEVIN_API_KEY": "test", "DEVIN_ORG_ID": "org-test", "DB_PATH": settings.db_path,
        "POLL_INTERVAL_SECONDS": "0",
    }):
        module = importlib.import_module("app.main")
    with patch.object(module, "dispatcher", dispatcher), \
            patch.object(module, "store", store), patch.object(module, "settings", settings):
        yield TestClient(module.app), dispatcher, requests, session, pr
    dispatcher.gh._c.close()
    dispatcher.devin._c.close()


def signed_headers(body: bytes) -> dict[str, str]:
    signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json", "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": f"sha256={signature}",
    }


def trigger(client, source: str):
    if source == "webhook":
        body = json.dumps({"action": "labeled", "issue": ISSUE}).encode()
        return client.post("/webhooks/github", content=body, headers=signed_headers(body))
    if source == "poller":
        return client.post("/scan")
    return client.post("/issues/7/dispatch")


@pytest.mark.parametrize("source", ["webhook", "poller", "manual"])
@pytest.mark.parametrize("outcome", ["merged", "closed"])
def test_event_to_session_to_pr_to_terminal_outcome(workflow, source, outcome):
    client, dispatcher, requests, session, pr = workflow
    response = trigger(client, source)
    assert response.status_code == 200
    assert response.json()["dispatched"]
    for duplicate in ("webhook", "poller", "manual"):
        assert not trigger(client, duplicate).json()["dispatched"]
    creates = [r for r in requests if r.method == "POST" and r.url.host == "api.devin.ai"]
    assert len(creates) == 1
    body = json.loads(creates[0].content)
    assert body["repos"] == ["jrios6/superset"]
    assert "Closes #7" in body["prompt"]
    assert ISSUE["body"] in body["prompt"]
    assert body["max_acu_limit"] == 10
    assert body["structured_output_required"] is True
    assert body["structured_output_schema"]["required"] == ["summary", "outcome"]
    dispatcher.track_once()
    assert dispatcher.store.all()[0]["state"] == "running"

    session["pull_requests"] = [{"pr_url": PR_URL}]
    session["status_detail"] = "waiting_for_user"
    dispatcher.track_once()
    row = dispatcher.store.all()[0]
    assert row["state"] == "pr_opened"
    assert row["pr_url"] == PR_URL
    dispatcher.track_once()
    row = dispatcher.store.all()[0]
    assert row["pr_checks"] == "passing"
    assert row["pr_additions"] == 10
    assert row["pr_comments"] == 2
    assert row["acus"] == 1.5
    assert dispatcher.integration_health()["github"]["status"] == "healthy"

    pr["state"] = "closed"
    pr["merged_at"] = "2026-09-02T12:00:00Z" if outcome == "merged" else None
    dispatcher.track_once()
    assert dispatcher.store.active() == []
    state = "merged" if outcome == "merged" else "failed"
    row = client.get("/api/tasks").json()["tasks"][0]
    assert row["state"] == state
    assert row["pr_state"] == outcome
    assert row["completed_at"] is not None
    events = client.get("/api/events").json()["events"]
    assert [event["kind"] for event in reversed(events)] == [
        "detected", "dispatched", "status", "pr_opened", state,
    ]
    labels = [
        json.loads(r.content)["labels"][0]
        for r in requests if r.method == "POST" and r.url.path.endswith("/labels")
    ]
    assert labels == [
        "devin-in-progress", "devin-pr-opened",
        "devin-done" if outcome == "merged" else "devin-failed",
    ]
    comments = [
        json.loads(r.content)["body"]
        for r in requests if r.method == "POST" and r.url.path.endswith("/comments")
    ]
    assert len(comments) == 3
    assert session["url"] in comments[0]
    assert PR_URL in comments[1]
    before = len(requests)
    dispatcher.track_once()
    assert len(requests) == before


@pytest.mark.parametrize("status,detail", [
    ("exit", ""), ("running", "finished"), ("error", "error"),
])
def test_terminal_session_without_pr_records_failure(workflow, status, detail):
    client, dispatcher, requests, session, _ = workflow
    trigger(client, "webhook")
    session.update(status=status, status_detail=detail)
    dispatcher.track_once()
    row = dispatcher.store.all()[0]
    assert row["state"] == "failed"
    assert row["pr_url"] is None
    assert dispatcher.store.active() == []
    assert any(
        r.method == "POST" and r.url.path.endswith("/labels")
        and json.loads(r.content) == {"labels": ["devin-failed"]} for r in requests
    )


@pytest.mark.parametrize("signature", ["", "sha256=incorrect", "sha1=incorrect"])
def test_invalid_webhook_signature_has_no_side_effects(workflow, signature):
    client, dispatcher, requests, _, _ = workflow
    body = json.dumps({"action": "labeled", "issue": ISSUE}).encode()
    headers = signed_headers(body)
    headers["X-Hub-Signature-256"] = signature
    assert client.post("/webhooks/github", content=body, headers=headers).status_code == 401
    assert requests == []
    assert dispatcher.store.all() == []


def test_tampered_signed_payload_is_rejected(workflow):
    client, dispatcher, requests, _, _ = workflow
    body = json.dumps({"action": "labeled", "issue": ISSUE}).encode()
    response = client.post(
        "/webhooks/github", content=body + b" ", headers=signed_headers(body))
    assert response.status_code == 401
    assert requests == []
    assert dispatcher.store.event_count() == 0


@pytest.mark.parametrize("event,action", [("ping", None), ("issues", "edited")])
def test_irrelevant_events_do_not_dispatch(workflow, event, action):
    client, dispatcher, requests, _, _ = workflow
    body = json.dumps({"action": action, "issue": ISSUE}).encode()
    headers = {**signed_headers(body), "X-GitHub-Event": event}
    response = client.post("/webhooks/github", content=body, headers=headers)
    assert response.status_code == 200
    assert "ignored" in response.json()
    assert requests == []
    assert dispatcher.store.all() == []
