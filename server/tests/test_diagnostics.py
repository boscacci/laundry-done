import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from laundry_done_relay.app import LaundryEvent, _init_db, _store_event, create_app
from laundry_done_relay.diagnostics import read_packets
from test_app import _post


def test_diagnostics_reads_legacy_and_new_packets_without_changing_database(tmp_path):
    path = tmp_path / "events.sqlite3"
    _init_db(path)
    for index in range(3):
        raw = dict(
            device_id="sensor",
            event_id=f"sample-{index}",
            cycle_id="boot",
            state="calibration_sample",
            cycle_label="unknown",
            motion_rms_mg=1.0,
            last_motion_ms=0,
            firmware_version="test",
            uptime_ms=index * 10000,
            peak_mg=2.0,
            wifi_rssi=-60,
        )
        if index == 2:
            raw["diagnostics"] = {
                "version": 1,
                "reset_reason": 9,
                "private_extra": "not-for-output",
            }
        _store_event(path, LaundryEvent(**raw), json.dumps(raw).encode())
    before = path.read_bytes()
    packets = read_packets(path, device_id="sensor", limit=2)
    assert len(packets) == 2
    assert packets[0]["uptime_ms"] == 10000
    assert packets[0]["diagnostics"] == {}
    assert packets[1]["diagnostics"] == {"version": 1, "reset_reason": 9}
    assert packets[1]["reset_reason_name"] == "brownout"
    assert "not-for-output" not in json.dumps(packets)
    assert read_packets(path, device_id="other", limit=2) == []
    assert path.read_bytes() == before


def test_missing_diagnostics_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        read_packets(path, device_id="sensor", limit=2)
    assert not path.exists()


def test_wireless_diagnostics_round_trip_through_signed_telemetry_without_alert(
    tmp_path,
):
    sent = []
    client = TestClient(
        create_app(
            database_path=tmp_path / "events.sqlite3",
            device_secret="test-secret",
            gotify_url="http://unused.invalid",
            gotify_app_token="test-token",
            push_message=sent.append,
        )
    )
    extra = {"version": 1, "reset_reason": 9, "detector_state": "idle"}
    raw = dict(
        device_id="sensor",
        event_id="sample",
        cycle_id="boot",
        state="calibration_sample",
        cycle_label="unknown",
        motion_rms_mg=1.0,
        last_motion_ms=0,
        firmware_version="test",
        diagnostics=extra,
    )
    assert _post(client, "test-secret", raw).status_code == 202
    assert client.get("/api/v1/calibration/events").status_code == 401
    result = client.get(
        "/api/v1/calibration/events", headers={"x-laundry-admin-secret": "test-secret"}
    )
    assert result.status_code == 200
    assert result.json()["events"][0]["diagnostics"] == extra
    assert sent == []


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_diagnostics_requires_bounded_history(tmp_path, limit):
    with pytest.raises(ValueError):
        read_packets(tmp_path / "missing", device_id="sensor", limit=limit)
