from unittest.mock import patch

import httpx
import pytest

from app.config import Settings
from app.db import Store
from app.dispatcher import Dispatcher


@pytest.fixture
def dispatcher(tmp_path):
    dispatcher = Dispatcher(
        Settings(devin_api_key="test", devin_org_id="test", github_token="test"),
        Store(str(tmp_path / "eligibility.db")),
    )
    yield dispatcher
    dispatcher.gh._c.close()
    dispatcher.devin._c.close()


@pytest.fixture
def issue():
    return {
        "number": 1, "title": "Fix timezone handling", "body": "Add a regression test",
        "html_url": "https://github.com/jrios6/superset/issues/1",
        "state": "open", "labels": [{"name": "devin-fix"}],
    }


@pytest.mark.parametrize("change", [
    {"state": "closed"},
    {"labels": []},
    {"state": "closed", "labels": []},
    {"pull_request": {"url": "https://api.github.com/repos/jrios6/superset/pulls/1"}},
    {"state": None},
])
def test_stale_payload_cannot_dispatch_ineligible_canonical_issue(dispatcher, issue, change):
    with patch.object(dispatcher.gh, "get_issue", return_value={**issue, **change}), \
            patch.object(dispatcher.devin, "create_session") as create:
        assert dispatcher.consider_issue(issue, "webhook") is False
    create.assert_not_called()
    assert dispatcher.store.all() == []


def test_refresh_failure_leaves_issue_eligible_for_a_later_attempt(dispatcher, issue):
    with patch.object(dispatcher.gh, "get_issue", side_effect=httpx.ReadTimeout("private")), \
            patch.object(dispatcher.devin, "create_session") as create:
        assert dispatcher.consider_issue(issue, "webhook") is False
    create.assert_not_called()
    assert dispatcher.store.all() == []
    with patch.object(dispatcher.gh, "get_issue", return_value=issue), \
            patch.object(dispatcher, "_dispatch") as dispatch:
        assert dispatcher.consider_issue(issue, "poller") is True
        assert dispatcher.consider_issue(issue, "webhook") is False
    dispatch.assert_called_once_with(issue)


def test_dispatch_uses_canonical_prompt_and_creates_one_session(dispatcher, issue):
    canonical = {**issue, "title": "Canonical title", "body": "Canonical instructions"}
    with patch.object(dispatcher.gh, "get_issue", return_value=canonical), \
            patch.object(dispatcher.gh, "add_label"), \
            patch.object(dispatcher.gh, "remove_label"), \
            patch.object(dispatcher.gh, "comment"), \
            patch.object(dispatcher.devin, "create_session", return_value={
                "session_id": "devin-test", "url": "https://app.devin.ai/sessions/test",
            }) as create:
        assert dispatcher.consider_issue(issue, "manual") is True
        assert dispatcher.consider_issue(issue, "poller") is False
    create.assert_called_once()
    assert "Canonical title" in create.call_args.kwargs["prompt"]
    assert "Canonical instructions" in create.call_args.kwargs["prompt"]
    assert dispatcher.store.all()[0]["state"] == "running"
