import importlib
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Store
from app.dispatcher import Dispatcher


ISSUE = {
    "number": 1, "title": "Fix", "html_url": "https://github.com/jrios6/superset/issues/1",
    "state": "open", "labels": [{"name": "devin-fix"}],
}
SESSION = {
    "session_id": "devin-test", "url": "https://app.devin.ai/sessions/test",
    "org_id": "org-test", "tags": ["issue-remediation", "issue-1"],
    "created_at": 100, "status": "running", "status_detail": "working",
}


@pytest.fixture
def dispatcher(tmp_path):
    dispatcher = Dispatcher(
        Settings(devin_api_key="test", devin_org_id="org-test", github_token="test"),
        Store(str(tmp_path / "recovery.db")),
    )
    with patch.object(dispatcher.gh, "add_label"), \
            patch.object(dispatcher.gh, "remove_label"), \
            patch.object(dispatcher.gh, "comment"):
        yield dispatcher
    dispatcher.gh._c.close()
    dispatcher.devin._c.close()


def queue(dispatcher):
    assert dispatcher.store.upsert_queued(1, ISSUE["title"], ISSUE["html_url"])


def require_recovery(dispatcher):
    queue(dispatcher)
    assert dispatcher.store.require_recovery(1, "Unknown outcome")


def test_creation_timeout_does_not_count_as_dispatch_or_retry_automatically(dispatcher):
    with patch.object(dispatcher.gh, "get_issue", return_value=ISSUE), \
            patch.object(dispatcher.devin, "create_session",
                         side_effect=httpx.ReadTimeout("private-key")) as create:
        assert dispatcher.consider_issue(ISSUE, "webhook") is False
        assert dispatcher.consider_issue(ISSUE, "poller") is False
        dispatcher.track_once()
    create.assert_called_once()
    row = dispatcher.store.all()[0]
    assert row["state"] == "recovery_required"
    assert row["completed_at"] is None
    assert "ReadTimeout" in row["detail"]
    assert "private-key" not in str(dispatcher.store.recent_events())
    assert "private-key" not in row["detail"]


def test_created_session_can_be_recovered_after_local_persistence_failure(dispatcher):
    queue(dispatcher)
    with patch.object(dispatcher.devin, "create_session", return_value=SESSION), \
            patch.object(dispatcher.store, "mark_dispatched",
                         side_effect=sqlite3.OperationalError("write failed")):
        assert dispatcher._dispatch(ISSUE) is False
    with patch.object(dispatcher.devin, "get_session", return_value=SESSION), \
            patch.object(dispatcher.devin, "create_session") as create:
        row = dispatcher.recover_issue(1, "devin-test", confirm_session_matches_issue=True)
    create.assert_not_called()
    assert row["session_id"] == SESSION["session_id"]


def test_startup_flags_interrupted_and_legacy_failed_dispatches(tmp_path, dispatcher):
    path = str(tmp_path / "persisted.db")
    store = Store(path)
    store.upsert_queued(1, "Interrupted before response", "url")
    store.upsert_queued(2, "Legacy timeout", "url")
    store.mark_completed(2, "failed", None, "dispatch error: timeout")
    store.upsert_queued(3, "Already dispatched", "url")
    store.mark_dispatched(3, "devin-existing", "url")
    store.upsert_queued(4, "Real terminal failure", "url")
    store.mark_completed(4, "failed", None, "finished without PR")
    dispatcher.store = Store(path)
    with patch("app.dispatcher.threading.Thread"), \
            patch.object(dispatcher.devin, "create_session") as create:
        dispatcher.start()
        dispatcher.start()
    create.assert_not_called()
    rows = dispatcher.store.all()
    assert [row["state"] for row in rows] == [
        "recovery_required", "recovery_required", "running", "failed",
    ]
    assert len(dispatcher.store.recent_events()) == 2
    assert rows[1]["completed_at"] is None
    assert rows[2]["session_id"] == "devin-existing"


def test_attach_reconciles_existing_session_and_preserves_real_creation_time(dispatcher):
    require_recovery(dispatcher)
    with patch.object(dispatcher.devin, "get_session", return_value=SESSION), \
            patch.object(dispatcher.devin, "create_session") as create:
        row = dispatcher.recover_issue(
            1, "devin-test", confirm_session_matches_issue=True)
        dispatcher.track_once()
        with pytest.raises(ValueError, match="not awaiting"):
            dispatcher.recover_issue(1, "devin-test", confirm_session_matches_issue=True)
    create.assert_not_called()
    assert row["state"] == "running"
    assert row["session_id"] == "devin-test"
    assert row["dispatched_at"] == 100
    assert row["completed_at"] is None
    assert dispatcher.store.all()[0]["detail"] == "working"


@pytest.mark.parametrize("change", [
    {"session_id": "devin-other"}, {"org_id": "org-other"},
    {"tags": ["issue-remediation", "issue-2"]}, {"tags": []},
])
def test_attach_rejects_mismatched_session(dispatcher, change):
    require_recovery(dispatcher)
    with patch.object(dispatcher.devin, "get_session", return_value={**SESSION, **change}):
        with pytest.raises(ValueError, match="do not match"):
            dispatcher.recover_issue(1, "devin-test", confirm_session_matches_issue=True)
    assert dispatcher.store.all()[0]["state"] == "recovery_required"


def test_attach_rejects_session_already_tracked_elsewhere(dispatcher):
    require_recovery(dispatcher)
    dispatcher.store.upsert_queued(2, "Other", "url")
    dispatcher.store.mark_dispatched(2, "devin-test", SESSION["url"])
    with patch.object(dispatcher.devin, "get_session", return_value=SESSION):
        with pytest.raises(ValueError, match="already tracked"):
            dispatcher.recover_issue(1, "devin-test", confirm_session_matches_issue=True)
    assert dispatcher.store.all()[0]["state"] == "recovery_required"


@pytest.mark.parametrize("kwargs", [
    {},
    {"session_id": "devin-test"},
    {"session_id": "devin-test", "confirm_no_session": True},
    {"session_id": "devin-test", "confirm_no_session": True,
     "confirm_session_matches_issue": True},
    {"confirm_no_session": True, "confirm_session_matches_issue": True},
])
def test_recovery_requires_unambiguous_confirmation(dispatcher, kwargs):
    require_recovery(dispatcher)
    with patch.object(dispatcher.devin, "create_session") as create, \
            patch.object(dispatcher.devin, "get_session") as get:
        with pytest.raises(ValueError, match="Confirm"):
            dispatcher.recover_issue(1, **kwargs)
    create.assert_not_called()
    get.assert_not_called()


def test_confirmed_retry_makes_one_attempt_and_resumes_tracking(dispatcher):
    require_recovery(dispatcher)
    with patch.object(dispatcher.gh, "get_issue", return_value=ISSUE), \
            patch.object(dispatcher.devin, "create_session", return_value=SESSION) as create:
        row = dispatcher.recover_issue(1, confirm_no_session=True)
        with pytest.raises(ValueError, match="not awaiting"):
            dispatcher.recover_issue(1, confirm_no_session=True)
    create.assert_called_once()
    assert row["state"] == "running"
    assert row["detail"] is None
    assert row["completed_at"] is None


@pytest.mark.parametrize("change", [{"state": "closed"}, {"labels": []}, {"pull_request": {}}])
def test_confirmed_retry_rechecks_canonical_issue(dispatcher, change):
    require_recovery(dispatcher)
    with patch.object(dispatcher.gh, "get_issue", return_value={**ISSUE, **change}), \
            patch.object(dispatcher.devin, "create_session") as create:
        with pytest.raises(ValueError, match="must still be open"):
            dispatcher.recover_issue(1, confirm_no_session=True)
    create.assert_not_called()
    assert dispatcher.store.all()[0]["state"] == "recovery_required"


def test_retry_timeout_requires_reconciliation_again(dispatcher):
    require_recovery(dispatcher)
    with patch.object(dispatcher.gh, "get_issue", return_value=ISSUE), \
            patch.object(dispatcher.devin, "create_session",
                         side_effect=httpx.ReadTimeout("timeout")):
        row = dispatcher.recover_issue(1, confirm_no_session=True)
    assert row["state"] == "recovery_required"


def test_recovery_claim_is_atomic_across_store_instances(tmp_path):
    path = str(tmp_path / "concurrent.db")
    store = Store(path)
    store.upsert_queued(1, "Fix", "url")
    store.require_recovery(1, "Unknown")
    stores = [Store(path), Store(path)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda s: s.claim_recovery(1), stores))
    assert sorted(results) == [False, True]


@pytest.fixture
def client(dispatcher, tmp_path):
    with patch.dict(os.environ, {
        "DEVIN_API_KEY": "test", "DEVIN_ORG_ID": "org-test",
        "DB_PATH": str(tmp_path / "api.db"), "POLL_INTERVAL_SECONDS": "0",
    }):
        module = importlib.import_module("app.main")
    with patch.object(module, "dispatcher", dispatcher):
        yield TestClient(module.app)


@pytest.mark.parametrize("body", [
    {"confirm_no_session": "false"},
    {"confirm_no_session": 1},
    {"session_id": "../other"},
    {"session_id": "devin-test", "unexpected": True},
])
def test_api_rejects_invalid_recovery_input(client, dispatcher, body):
    require_recovery(dispatcher)
    assert client.post("/issues/1/recover", json=body).status_code == 422
    assert dispatcher.store.all()[0]["state"] == "recovery_required"


def test_api_verification_failure_does_not_claim_recovery(client, dispatcher):
    require_recovery(dispatcher)
    with patch.object(dispatcher.gh, "get_issue", side_effect=httpx.ReadTimeout("private-key")):
        response = client.post("/issues/1/recover", json={"confirm_no_session": True})
    assert response.status_code == 502
    assert "private-key" not in response.text
    assert dispatcher.store.all()[0]["state"] == "recovery_required"


def test_api_attaches_existing_session(client, dispatcher):
    require_recovery(dispatcher)
    with patch.object(dispatcher.devin, "get_session", return_value=SESSION):
        response = client.post("/issues/1/recover", json={
            "session_id": "devin-test", "confirm_session_matches_issue": True,
        })
    assert response.status_code == 200
    assert response.json()["task"]["state"] == "running"
