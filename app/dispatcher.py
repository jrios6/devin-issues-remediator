import logging
import threading

from .config import Settings
from .db import Store
from .devin_client import DevinClient
from .github_client import GitHubClient
from .sync_health import SyncHealth

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
    labels = ",".join(label["name"] for label in issue.get("labels", []))
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
        self.sync_health = SyncHealth()
        self.poller_enabled = settings.poll_interval_seconds > 0
        self.poller_interval = settings.poll_interval_seconds or 60
        # Runtime-tunable dispatch config (editable via /api/config)
        self.cfg = {"devin_mode": settings.devin_mode,
                    "devin_max_acu_limit": settings.devin_max_acu_limit}

    # ---------- event intake ----------

    def consider_issue(self, issue: dict, source: str) -> bool:
        """Dispatch a Devin session for a labeled issue we haven't seen."""
        labels = {label["name"] for label in issue.get("labels", [])}
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
                max_acu_limit=self.cfg["devin_max_acu_limit"] or None,
                devin_mode=self.cfg["devin_mode"] or None,
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
        with self.sync_health.observe("github", "issues"):
            issues = self.gh.list_labeled_issues(self.s.trigger_label)
        return sum(self.consider_issue(i, "poller") for i in issues)

    def integration_health(self) -> dict:
        max_age = max(90, self.s.track_interval_seconds * 3)
        expected: dict[str, dict[str, float]] = {"github": {}, "devin": {}}
        if self.poller_enabled:
            expected["github"]["issues"] = max(90, self.poller_interval * 3)
        for row in self.store.active():
            number = row["issue_number"]
            if row["session_id"]:
                expected["devin"][f"session:{number}"] = max_age
            if row["state"] == "pr_opened":
                expected["github"][f"pr:{number}"] = max_age
                expected["github"][f"checks:{number}"] = max_age
        return self.sync_health.snapshot(expected)

    def track_once(self):
        for r in self.store.active():
            if r["state"] == "pr_opened":
                self._track_pr(r)
                continue
            if r["state"] != "running" or not r["session_id"]:
                continue
            n = r["issue_number"]
            try:
                with self.sync_health.observe("devin", f"session:{n}"):
                    sess = self.devin.get_session(r["session_id"])
            except Exception as e:  # noqa: BLE001
                log.warning("status poll failed for issue #%s: %s", n, e)
                continue
            status, detail = sess.get("status"), sess.get("status_detail") or ""
            self.store.update_metrics(n, acus=sess.get("acus_consumed"),
                                      devin_mode=sess.get("devin_mode"))
            prs = [p["pr_url"] for p in sess.get("pull_requests") or []]
            if prs:
                self.store.mark_completed(n, "pr_opened", prs[0], "awaiting merge")
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
            with self.sync_health.observe("github", f"pr:{n}"):
                pr = self.gh.get_pull(r["pr_url"])
        except Exception as e:  # noqa: BLE001
            log.warning("PR state poll failed for issue #%s: %s", n, e)
            return
        self._refresh_metrics(r, pr)
        pr_state = pr["state"]
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

    def _refresh_metrics(self, r: dict, pr: dict):
        """Pull cost (ACUs), mode, PR size and CI status for the dashboard."""
        n = r["issue_number"]
        fields = {"pr_additions": pr["additions"], "pr_deletions": pr["deletions"],
                  "pr_files": pr["changed_files"], "pr_comments": pr["comments"],
                  "pr_opened_at": pr["created_at_ts"]}
        try:
            with self.sync_health.observe("github", f"checks:{n}"):
                fields["pr_checks"] = self.gh.check_summary(pr["head_sha"])
        except Exception as e:  # noqa: BLE001
            log.warning("check-runs poll failed for issue #%s: %s", n, e)
        if r.get("session_id"):
            try:
                with self.sync_health.observe("devin", f"session:{n}"):
                    sess = self.devin.get_session(r["session_id"])
                fields["acus"] = sess.get("acus_consumed")
                fields["devin_mode"] = sess.get("devin_mode")
            except Exception as e:  # noqa: BLE001
                log.warning("session poll failed for issue #%s: %s", n, e)
        self.store.update_metrics(n, **fields)

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

    MODES = ("normal", "fast", "lite", "ultra", "fusion")

    def get_config(self) -> dict:
        return {"devin_mode": self.cfg["devin_mode"] or "",
                "devin_max_acu_limit": self.cfg["devin_max_acu_limit"],
                "poll_interval_seconds": self.poller_interval,
                "modes": list(self.MODES)}

    def set_config(self, body: dict) -> dict:
        if "devin_mode" in body:
            mode = str(body["devin_mode"] or "")
            if mode and mode not in self.MODES:
                raise ValueError(f"devin_mode must be one of {self.MODES} or empty")
            self.cfg["devin_mode"] = mode
        if "devin_max_acu_limit" in body:
            v = int(body["devin_max_acu_limit"] or 0)
            if v < 0:
                raise ValueError("devin_max_acu_limit must be >= 0")
            self.cfg["devin_max_acu_limit"] = v
        if "poll_interval_seconds" in body:
            v = int(body["poll_interval_seconds"])
            if v < 5:
                raise ValueError("poll_interval_seconds must be >= 5")
            self.poller_interval = v
            self.poller_enabled = True
        return self.get_config()

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
