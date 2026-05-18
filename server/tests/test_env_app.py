import importlib
import sys


def test_env_app_imports_after_required_environment_is_set(monkeypatch, tmp_path):
    monkeypatch.setenv("LAUNDRY_RELAY_FROM_ENV", "1")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "events.sqlite3"))
    monkeypatch.setenv("DEVICE_SECRET", "test-secret")
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.local")
    monkeypatch.setenv("GOTIFY_APP_TOKEN", "token")

    sys.modules.pop("laundry_done_relay.app", None)

    app_module = importlib.import_module("laundry_done_relay.app")

    assert app_module.app.title == "Laundry Done Relay"
