import httpx


class GitHubClient:
    def __init__(self, token: str, repo: str, api_base: str = "https://api.github.com"):
        self.repo = repo
        self._c = httpx.Client(
            base_url=f"{api_base}/repos/{repo}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    def list_labeled_issues(self, label: str) -> list[dict]:
        resp = self._c.get("/issues", params={"labels": label, "state": "open", "per_page": 50})
        resp.raise_for_status()
        return [i for i in resp.json() if "pull_request" not in i]

    def get_issue(self, number: int) -> dict:
        resp = self._c.get(f"/issues/{number}")
        resp.raise_for_status()
        return resp.json()

    def comment(self, number: int, body: str):
        self._c.post(f"/issues/{number}/comments", json={"body": body}).raise_for_status()

    def add_label(self, number: int, label: str):
        self._c.post(f"/issues/{number}/labels", json={"labels": [label]}).raise_for_status()

    def remove_label(self, number: int, label: str):
        self._c.delete(f"/issues/{number}/labels/{label}").raise_for_status()
