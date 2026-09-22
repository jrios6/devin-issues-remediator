from unittest.mock import patch

import httpx
import pytest

from app.config import Settings
from app.db import Store
from app.dispatcher import Dispatcher
from app.sync_health import SyncHealth


def test_health_tracks_failure_recovery_and_staleness():
    health = SyncHealth()
    expected = {"github": {"issues": 90}, "devin": {}}
    assert health.snapshot(expected)["github"]["status"] == "pending"
    assert health.snapshot(expected)["devin"]["status"] == "idle"
    with patch("app.sync_health.time.time", return_value=100):
        with health.observe("github", "issues"):
            pass
        assert health.snapshot(expected)["github"]["status"] == "healthy"
    with patch("app.sync_health.time.time", return_value=191):
        assert health.snapshot(expected)["github"]["status"] == "stale"
    with pytest.raises(httpx.ReadTimeout):
        with health.observe("github", "issues"):
            raise httpx.ReadTimeout("private request details")
    reading = health.snapshot(expected)["github"]
    assert reading["status"] == "error"
    assert reading["last_success"] == 100
    assert reading["resources"]["issues"]["error"] == "Request timed out"
    with health.observe("github", "issues"):
        pass
    assert health.snapshot(expected)["github"]["status"] == "healthy"


def test_successful_issue_poll_does_not_hide_failed_ci_sync():
    health = SyncHealth()
    expected = {"github": {"issues": 90, "checks:1": 90}}
    response = httpx.Response(
        401, request=httpx.Request("GET", "https://example.com/?token=private"))
    with pytest.raises(httpx.HTTPStatusError):
        with health.observe("github", "checks:1"):
            response.raise_for_status()
    with health.observe("github", "issues"):
        pass
    reading = health.snapshot(expected)["github"]
    assert reading["status"] == "error"
    assert reading["last_success"] is None
    assert reading["resources"]["checks:1"]["error"] == "HTTP 401"
    assert "private" not in str(reading)


@pytest.fixture
def dispatcher(tmp_path):
    store = Store(str(tmp_path / "health.db"))
    dispatcher = Dispatcher(Settings(
        devin_api_key="test", devin_org_id="test", github_token="test"), store)
    yield dispatcher
    dispatcher.gh._c.close()
    dispatcher.devin._c.close()


def test_paused_polling_is_idle_but_open_pr_tracking_still_counts(dispatcher):
    dispatcher.set_poller(False)
    assert dispatcher.integration_health()["github"]["status"] == "idle"
    dispatcher.store.upsert_queued(1, "Test", "https://example.com/issue")
    dispatcher.store.mark_dispatched(1, "session", "https://example.com/session")
    dispatcher.store.mark_completed(1, "pr_opened", "https://example.com/pr", "")
    reading = dispatcher.integration_health()
    assert reading["github"]["status"] == "pending"
    assert reading["devin"]["status"] == "pending"
    assert set(reading["github"]["resources"]) == {"pr:1", "checks:1"}


def test_tracker_reports_cached_ci_as_unverified_after_failure(dispatcher):
    dispatcher.set_poller(False)
    store = dispatcher.store
    store.upsert_queued(1, "Test", "https://example.com/issue")
    store.mark_dispatched(1, "session", "https://example.com/session")
    store.mark_completed(1, "pr_opened", "https://example.com/pr", "")
    store.update_metrics(1, pr_checks="passing")
    pr = {
        "state": "open", "head_sha": "sha", "additions": 2, "deletions": 1,
        "changed_files": 1, "comments": 0, "created_at_ts": 100,
    }
    with patch.object(dispatcher.gh, "get_pull", return_value=pr), \
            patch.object(dispatcher.gh, "check_summary",
                         side_effect=httpx.ReadTimeout("timeout")), \
            patch.object(dispatcher.devin, "get_session",
                         return_value={"acus_consumed": 0, "devin_mode": "normal"}):
        dispatcher.track_once()
    assert store.all()[0]["pr_checks"] == "passing"
    assert dispatcher.integration_health()["github"]["status"] == "error"
    assert dispatcher.integration_health()["devin"]["status"] == "healthy"
    with patch.object(dispatcher.gh, "get_pull", return_value=pr), \
            patch.object(dispatcher.gh, "check_summary", return_value="pending"), \
            patch.object(dispatcher.devin, "get_session", return_value={}):
        dispatcher.track_once()
    assert store.all()[0]["pr_checks"] == "pending"
    assert dispatcher.integration_health()["github"]["status"] == "healthy"
    with patch.object(dispatcher.gh, "get_pull", return_value=pr), \
            patch.object(dispatcher.gh, "check_summary", return_value=None), \
            patch.object(dispatcher.devin, "get_session", return_value={}):
        dispatcher.track_once()
    assert store.all()[0]["pr_checks"] is None


def test_turnaround_sample_excludes_rows_without_both_timestamps(tmp_path):
    store = Store(str(tmp_path / "counts.db"))
    store.upsert_queued(1, "Dispatched", "url")
    store.upsert_queued(2, "Queued", "url")
    with patch("app.db.time.time", return_value=100):
        store.mark_dispatched(1, "session", "url")
    store.update_metrics(1, pr_opened_at=220)
    store.update_metrics(2, pr_opened_at=250)
    assert store.counts()["durations"]["sample_count"] == 1
    assert store.counts()["durations"]["avg_s"] == 120
