import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    devin_api_key: str
    devin_org_id: str
    github_token: str
    github_repo: str = "jrios6/superset"
    trigger_label: str = "devin-fix"
    in_progress_label: str = "devin-in-progress"
    done_label: str = "devin-done"
    failed_label: str = "devin-failed"
    webhook_secret: str = ""
    poll_interval_seconds: int = 60  # 0 disables the poller
    track_interval_seconds: int = 30
    db_path: str = "/data/remediator.db"
    devin_api_base: str = "https://api.devin.ai"
    github_api_base: str = "https://api.github.com"
    devin_max_acu_limit: int = 10
    port: int = 8000


def load() -> Settings:
    api_key = os.environ.get("DEVIN_API_KEY", "")
    org_id = os.environ.get("DEVIN_ORG_ID", "")
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    if not api_key:
        raise RuntimeError("DEVIN_API_KEY is required")
    if not org_id:
        raise RuntimeError("DEVIN_ORG_ID is required (e.g. org-xxxx)")
    return Settings(
        devin_api_key=api_key,
        devin_org_id=org_id,
        github_token=gh_token,
        github_repo=os.environ.get("GITHUB_REPO", "jrios6/superset"),
        trigger_label=os.environ.get("TRIGGER_LABEL", "devin-fix"),
        webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET", ""),
        poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "60")),
        track_interval_seconds=int(os.environ.get("TRACK_INTERVAL_SECONDS", "30")),
        db_path=os.environ.get("DB_PATH", "/data/remediator.db"),
        devin_api_base=os.environ.get("DEVIN_API_BASE", "https://api.devin.ai"),
        devin_max_acu_limit=int(os.environ.get("DEVIN_MAX_ACU_LIMIT", "10")),
        port=int(os.environ.get("PORT", "8000")),
    )
