import httpx


class DevinClient:
    """Thin client over the Devin v3 organization API."""

    def __init__(self, api_key: str, org_id: str, api_base: str = "https://api.devin.ai"):
        self._org = org_id
        self._c = httpx.Client(
            base_url=f"{api_base}/v3/organizations/{org_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )

    def create_session(self, prompt: str, repos: list[str], tags: list[str],
                       max_acu_limit: int | None = None,
                       structured_output_schema: dict | None = None,
                       title: str | None = None) -> dict:
        body: dict = {"prompt": prompt, "repos": repos, "tags": tags,
                      "unlisted": False, "bypass_approval": True}
        if max_acu_limit:
            body["max_acu_limit"] = max_acu_limit
        if structured_output_schema:
            body["structured_output_schema"] = structured_output_schema
            body["structured_output_required"] = True
        if title:
            body["title"] = title
        resp = self._c.post("/sessions", json=body)
        resp.raise_for_status()
        return resp.json()  # {session_id, url, is_new_session}

    def get_session(self, session_id: str) -> dict:
        resp = self._c.get(f"/sessions/{session_id}")
        resp.raise_for_status()
        return resp.json()  # {status, status_detail, pull_requests[], url, structured_output, ...}
