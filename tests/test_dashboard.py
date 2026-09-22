import importlib
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def dashboard_app(tmp_path_factory):
    path = tmp_path_factory.mktemp("dashboard") / "test.db"
    with patch.dict(os.environ, {
        "DEVIN_API_KEY": "test", "DEVIN_ORG_ID": "test", "GITHUB_TOKEN": "test",
        "DB_PATH": str(path), "POLL_INTERVAL_SECONDS": "0",
    }):
        module = importlib.import_module("app.main")
    yield module
    module.dispatcher.gh._c.close()
    module.dispatcher.devin._c.close()


def test_dashboard_api_and_initial_state(dashboard_app):
    client = TestClient(dashboard_app.app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Needs attention" in response.text
    assert "Detected" in response.text
    assert client.get("/api/tasks").json()["counts"]["durations"]["sample_count"] == 0
    health = client.get("/api/integrations").json()
    assert health["github"]["status"] == "idle"
    assert health["devin"]["status"] == "idle"


def test_dashboard_javascript(dashboard_app):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for dashboard JavaScript regression checks")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("dashboard.test.cjs"))],
        input=dashboard_app.dashboard(), text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
