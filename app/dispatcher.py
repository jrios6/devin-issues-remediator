import logging
import threading
import time

from .config import Settings
from .db import Store
from .devin_client import DevinClient
from .github_client import GitHubClient

log = logging.getLogger("dispatcher")

STRUCTURED_SCHEMA = {
    "type": "object",
    "properties": {
        "pr_url": {"type": "string"},
        "summary": {"type": "string"},
        "tests_run": {"type": "string"},
        "outcome": {"type": "string", "enum": ["fixed", "partial", "blocked"]},
    },
    "required": ["summary", "outcome"],
}


def build_prompt(issue: dict, repo: str) -> str:
    labels = ",".join(l["name"] for l in issue.get("labels", []))
    return f"""You are remediating issue #{issue['number']} in the repository {repo}.

Issue title: {issue['title']}
Issue URL: {issue['html_url']}
Labels: {labels}

Issue body:
{issue.get('body') or '(no body)'}

Instructions:
- Work in {repo}; create a branch, implement the fix, and open a pull request against the default branch.
- Keep the change minimal and scoped to the issue. Follow the repo's own standards (AGENTS.md, pre-commit hooks).
- Run the repo's lint/typecheck and the tests named in the acceptance criteria before opening the PR.
- In the PR body, link this issue ("Closes #{issue['number']}").
- Report your final result via structured output: pr_url, summary, tests_run, outcome.
"""


class Dispatcher:
    """Event-driven core: labeled GitHub issue -> Devin session -> PR -> issue update."""

    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self.gh = GitHubClient(settings.github_token, settings.github_repo,
                               settings.github_api_base)
        self.devin = DevinClient(settings.devin_api_key, settings.devin_org_id,
                                 settings.devin_api_base)
        self._stop = threading.Event()
        self.poller_enabled = settings.poll_interval_seconds > 0
        self.poller_interval = settings.poll_interval_seconds or 60

    # ---------- event intake ----------

    def consider_issue(self, issue: dict, source: str) -> bool:
        """Dispatch a Devin session for a labeled issue we haven't seen."""
        labels = {l["name"] for l in issue.get("labels", [])}
        if self.s.trigger_label not in labels:
            return False
        # Refetch the canonical issue so dispatch never depends on webhook payload fidelity
        try:
            issue = self.gh.get_issue(issue["number"])
        except Exception as e:  # noqa: BLE001
            log.warning("refetch of issue #%s failed, using payload: %s", issue["number"], e)
        if not self.store.upsert_queued(issue["number"], issue["title"], issue["html_url"]):
            return False  # already tracked
        self.store.event("detected", issue["number"], f"via {source}")
        log.info("issue #%s detected via %s", issue["number"], source)
        self._dispatch(issue)
        return True

    def _dispatch(self, issue: dict):
        n = issue["number"]
        try:
            resp = self.devin.create_session(
                prompt=build_prompt(issue, self.s.github_repo),
                repos=[self.s.github_repo],
                tags=["issue-remediation", f"issue-{n}"],
                max_acu_limit=self.s.devin_max_acu_limit,
                structured_output_schema=STRUCTURED_SCHEMA,
                title=f"Fix issue #{n}: {issue['title'][:60]}",
            )
            self.store.mark_dispatched(n, resp["session_id"], resp["url"])
            self.store.event("dispatched", n, resp["url"])
            self._safe_label_swap(n, self.s.trigger_label, self.s.in_progress_label)
            self._safe_comment(
                n, f"Devin session dispatched: {resp['url']}\n\n"
                   "_Automated by the issue-remediation service._")
        except Exception as e:  # noqa: BLE001 - record and keep service alive
            log.exception("dispatch failed for issue #%s", n)
            self.store.mark_completed(n, "failed", None, f"dispatch error: {e}")
            self.store.event("failed", n, str(e))

    # ---------- background loops ----------

    def poll_issues_once(self) -> int:
        issues = self.gh.list_labeled_issues(self.s.trigger_label)
        return sum(self.consider_issue(i, "poller") for i in issues)

    def track_once(self):
        for r in self.store.active():
            if r["state"] == "pr_opened":
                self._track_pr(r)
                continue
            if r["state"] != "running" or not r["session_id"]:
                continue
            n = r["issue_number"]
            try:
                sess = self.devin.get_session(r["session_id"])
            except Exception as e:  # noqa: BLE001
                log.warning("status poll failed for issue #%s: %s", n, e)
                continue
            status, detail = sess.get("status"), sess.get("status_detail") or ""
            prs = [p["pr_url"] for p in sess.get("pull_requests") or []]
            if prs:
                self.store.mark_completed(n, "pr_opened", prs[0], detail or status)
                self.store.set_pr_state(n, "open")
                self.store.event("pr_opened", n, prs[0])
                self._safe_label_swap(n, self.s.in_progress_label, self.s.pr_opened_label)
                self._safe_comment(n, f"Devin opened a remediation PR: {prs[0]}\n\n"
                                      "_Marked `devin-done` only after the PR merges._")
            elif status in ("running", "claimed", "resuming", "new") and detail != "finished":
                if detail != r.get("detail"):
                    self.store.mark_running(n, detail)
                    self.store.event("status", n, detail)
            elif status == "exit" or detail == "finished":
                out = (sess.get("structured_output") or {})
                self.store.mark_completed(n, "failed", None,
                                          f"finished without PR: {out.get('summary', '')[:200]}")
                self.store.event("failed", n, "finished without PR")
                self._safe_label_swap(n, self.s.in_progress_label, self.s.failed_label)
                self._safe_comment(n, "Devin session finished without opening a PR. "
                                      f"Session: {sess.get('url')}")
            else:
                self.store.mark_completed(n, "failed", None, f"{status}: {detail}")
                self.store.event("failed", n, f"{status}: {detail}")
                self._safe_label_swap(n, self.s.in_progress_label, self.s.failed_label)
                self._safe_comment(n, f"Devin session ended in state `{status}` "
                                      f"({detail}). Session: {sess.get('url')}")

    def _track_pr(self, r: dict):
        """Watch an open remediation PR until it merges (done) or closes (failed)."""
        n = r["issue_number"]
        try:
            pr_state = self.gh.get_pull_state(r["pr_url"])
        except Exception as e:  # noqa: BLE001
            log.warning("PR state poll failed for issue #%s: %s", n, e)
            return
        if pr_state == "open":
            return
        if pr_state == "merged":
            self.store.set_pr_state(n, "merged")
            self.store.mark_completed(n, "merged", r["pr_url"], "PR merged")
            self.store.event("merged", n, r["pr_url"])
            self._safe_label_swap(n, self.s.pr_opened_label, self.s.done_label)
            self._safe_comment(n, f"Remediation PR merged: {r['pr_url']}")
        else:  # closed without merge
            self.store.set_pr_state(n, "closed")
            self.store.mark_completed(n, "failed", r["pr_url"], "PR closed unmerged")
            self.store.event("failed", n, "PR closed unmerged")
            self._safe_label_swap(n, self.s.pr_opened_label, self.s.failed_label)
            self._safe_comment(n, "Remediation PR was closed without merging — "
                                  "needs human triage.")

    def _loop(self, fn, interval: int, name: str):
        while not self._stop.is_set():
            try:
                fn()
            except Exception:  # noqa: BLE001
                log.exception("%s loop error", name)
            self._stop.wait(interval)

    def start(self):
        threading.Thread(target=self._poll_loop, daemon=True,
                         name="issue-poller").start()
        threading.Thread(target=self._loop, daemon=True, name="session-tracker",
                         args=(self.track_once,
                               self.s.track_interval_seconds, "tracker")).start()

    def set_poller(self, enabled: bool):
        self.poller_enabled = enabled

    def _poll_loop(self):
        while not self._stop.is_set():
            if self.poller_enabled:
                try:
                    self.poll_issues_once()
                except Exception:  # noqa: BLE001
                    log.exception("issue poll failed")
            self._stop.wait(self.poller_interval)

    def stop(self):
        self._stop.set()

    # ---------- helpers ----------

    def _safe_label_swap(self, n: int, old: str, new: str):
        try:
            self.gh.remove_label(n, old)
            self.gh.add_label(n, new)
        except Exception as e:  # noqa: BLE001
            log.warning("label swap failed for #%s: %s", n, e)

    def _safe_comment(self, n: int, body: str):
        try:
            self.gh.comment(n, body)
        except Exception as e:  # noqa: BLE001
            log.warning("comment failed for #%s: %s", n, e)
