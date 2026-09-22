from unittest.mock import patch

import pytest

from app.config import Settings
from app.db import Store
from app.dispatcher import Dispatcher


@pytest.fixture
def dispatcher(tmp_path):
    store = Store(str(tmp_path / "suspension.db"))
    store.upsert_queued(1, "Fix", "https://github.com/jrios6/superset/issues/1")
    store.mark_dispatched(1, "devin-test", "https://app.devin.ai/sessions/test")
    dispatcher = Dispatcher(
        Settings(devin_api_key="test", devin_org_id="test", github_token="test"), store)
    yield dispatcher
    dispatcher.gh._c.close()
    dispatcher.devin._c.close()


@pytest.mark.parametrize("detail", [
    "user_request", "inactivity", "usage_limit_exceeded", "out_of_credits",
    "waiting_for_user", "waiting_for_approval", "",
])
def test_suspended_session_remains_active_and_resumes(dispatcher, detail):
    with patch.object(dispatcher.devin, "get_session", return_value={
        "status": "suspended", "status_detail": detail,
    }), patch.object(dispatcher.gh, "remove_label") as remove, \
            patch.object(dispatcher.gh, "add_label") as add, \
            patch.object(dispatcher.gh, "comment") as comment:
        dispatcher.track_once()
        dispatcher.track_once()
    row = dispatcher.store.active()[0]
    assert row["state"] == "suspended"
    assert row["detail"] == detail
    assert row["completed_at"] is None
    assert dispatcher.store.event_count() == 1
    assert dispatcher.integration_health()["devin"]["status"] == "healthy"
    remove.assert_not_called()
    add.assert_not_called()
    comment.assert_not_called()

    with patch.object(dispatcher.devin, "get_session", return_value={
        "status": "resuming", "status_detail": detail,
    }):
        dispatcher.track_once()
    assert dispatcher.store.active()[0]["state"] == "running"


@pytest.mark.parametrize("status", ["suspended", "running"])
def test_pr_discovery_takes_precedence_over_suspension(dispatcher, status):
    dispatcher.store.mark_suspended(1, "waiting_for_user")
    pr_url = "https://github.com/jrios6/superset/pull/2"
    with patch.object(dispatcher.devin, "get_session", return_value={
        "status": status, "status_detail": "waiting_for_user",
        "pull_requests": [{"pr_url": pr_url}],
    }), patch.object(dispatcher.gh, "remove_label"), \
            patch.object(dispatcher.gh, "add_label"), \
            patch.object(dispatcher.gh, "comment"):
        dispatcher.track_once()
    row = dispatcher.store.active()[0]
    assert row["state"] == "pr_opened"
    assert row["pr_url"] == pr_url


def test_suspended_session_can_later_fail_terminally(dispatcher):
    dispatcher.store.mark_suspended(1, "user_request")
    with patch.object(dispatcher.devin, "get_session", return_value={
        "status": "error", "status_detail": "error",
    }), patch.object(dispatcher.gh, "remove_label"), \
            patch.object(dispatcher.gh, "add_label") as add, \
            patch.object(dispatcher.gh, "comment"):
        dispatcher.track_once()
    assert dispatcher.store.active() == []
    assert dispatcher.store.all()[0]["state"] == "failed"
    add.assert_called_once_with(1, "devin-failed")
