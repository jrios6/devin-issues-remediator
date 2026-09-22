import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db import Store
from app.dispatcher import build_prompt


def test_build_prompt_contains_issue_context():
    issue = {
        "number": 7,
        "title": "Fix the thing",
        "html_url": "https://github.com/x/y/issues/7",
        "body": "## Scope\nDo the thing",
        "labels": [{"name": "devin-fix"}],
    }
    p = build_prompt(issue, "x/y")
    assert "issue #7" in p
    assert "x/y" in p
    assert "pull request" in p
    assert "Closes #7" in p


def test_store_lifecycle(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    assert s.upsert_queued(1, "t", "u") is True
    assert s.upsert_queued(1, "t", "u") is False  # dedupe
    s.mark_dispatched(1, "sess-1", "http://x")
    s.mark_completed(1, "succeeded", "http://pr", "done")
    all_rows = s.all()
    assert all_rows[0]["state"] == "succeeded"
    assert all_rows[0]["pr_url"] == "http://pr"
    counts = s.counts()
    assert counts["by_state"]["succeeded"] == 1
