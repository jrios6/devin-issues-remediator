import pytest

from app.config import Settings
from app.db import Store
from app.dispatcher import Dispatcher


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("interval", [60, 120])
def test_settings_form_preserves_poller_state(tmp_path, enabled, interval):
    dispatcher = Dispatcher(
        Settings(devin_api_key="test", devin_org_id="test", github_token="test"),
        Store(str(tmp_path / "config.db")),
    )
    try:
        dispatcher.set_poller(enabled)
        config = dispatcher.set_config({
            "devin_mode": "lite",
            "devin_max_acu_limit": 5,
            "poll_interval_seconds": interval,
        })
        assert dispatcher.poller_enabled is enabled
        assert config["poll_interval_seconds"] == interval
        assert config["devin_mode"] == "lite"
        assert config["devin_max_acu_limit"] == 5
        dispatcher.set_poller(not enabled)
        assert dispatcher.poller_enabled is not enabled
    finally:
        dispatcher.gh._c.close()
        dispatcher.devin._c.close()
