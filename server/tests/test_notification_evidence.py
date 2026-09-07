"""Completion must be supported by a current, connected observation period."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from laundry_done_relay.app import _current_observation_segment, create_app
from test_app import _calibration_payload, _post


@pytest.fixture
def monitor(tmp_path):
    sent = []
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://unused.invalid",
        gotify_app_token="test-token",
        push_message=sent.append,
    )
    return TestClient(app), sent


START = datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)


def reading(monitor, seconds, *, active=False, session="boot-1", uptime=None):
    payload = _calibration_payload(
        event_id=f"{session}-{seconds}",
        cycle_id=session,
        at=START + timedelta(seconds=seconds),
        rms=32.0 if active else 0.8,
        peak=75.0 if active else 2.0,
    )
    if uptime is not None:
        payload["uptime_ms"] = uptime
    response = _post(monitor[0], "test-secret", payload)
    assert response.status_code == 202
    return response


@pytest.mark.parametrize("new_session", ["boot-1", "boot-2"])
def test_overnight_disconnect_is_not_evidence_of_quiet(monitor, new_session):
    for seconds in range(0, 610, 10):
        reading(monitor, seconds, active=True)
    for seconds in range(610, 710, 10):
        reading(monitor, seconds)
    # Yesterday's motion cannot arm this morning's quiet startup packet.
    for seconds in range(12 * 3600, 12 * 3600 + 120, 10):
        reading(monitor, seconds, session=new_session)
    assert monitor[1] == []


def test_reboot_discards_motion_even_without_a_long_gap(monitor):
    for seconds in range(0, 610, 10):
        reading(monitor, seconds, active=True)
    for seconds in range(610, 1400, 10):
        reading(monitor, seconds, session="boot-2")
    assert monitor[1] == []


def test_brief_setup_motion_does_not_arm_completion(monitor):
    for seconds in range(0, 70, 10):
        reading(monitor, seconds, active=True)
    for seconds in range(70, 1400, 10):
        reading(monitor, seconds)
    assert monitor[1] == []


def test_quiet_startup_never_sends_done(monitor):
    for seconds in range(0, 1400, 10):
        reading(monitor, seconds)
    assert monitor[1] == []


def test_current_observed_cycle_notifies_once_after_four_minutes_quiet(monitor):
    for seconds in range(0, 610, 10):
        reading(monitor, seconds, active=True)
    for seconds in range(610, 850, 10):
        reading(monitor, seconds)
    assert monitor[1] == []
    reading(monitor, 850)
    assert len(monitor[1]) == 1
    assert monitor[1][0]["title"] == "Laundry stack stopped"
    assert monitor[1][0]["message"] == "No washer/dryer stack motion for 4 min."
    reading(monitor, 860)
    assert len(monitor[1]) == 1


def test_current_cycle_can_arm_after_an_overnight_gap(monitor):
    for seconds in range(0, 610, 10):
        reading(monitor, seconds, active=True)
    morning = 12 * 3600
    for seconds in range(morning, morning + 610, 10):
        reading(monitor, seconds, active=True, session="boot-2")
    for seconds in range(morning + 610, morning + 870, 10):
        reading(monitor, seconds, session="boot-2")
    assert len(monitor[1]) == 1


@pytest.mark.parametrize(
    "gap,expected_count", [(120, 2), (180, 2), (181, 1), (0, 1), (-1, 1)]
)
def test_observation_gap_boundary(gap, expected_count):
    samples = [
        {"cycle_id": "boot", "device_time_utc": START.isoformat()},
        {
            "cycle_id": "boot",
            "device_time_utc": (START + timedelta(seconds=gap)).isoformat(),
        },
    ]
    assert len(_current_observation_segment(samples)) == expected_count


@pytest.mark.parametrize(
    "boundary", ["uptime", "receipt_gap", "invalid_timestamp", "missing_timestamp"]
)
def test_observation_evidence_resets_at_unreliable_boundaries(boundary):
    samples = [
        {"cycle_id": "boot", "device_time_utc": START.isoformat(), "uptime_ms": 10000},
        {
            "cycle_id": "boot",
            "device_time_utc": (START + timedelta(seconds=10)).isoformat(),
            "uptime_ms": 20000,
        },
    ]
    if boundary == "uptime":
        samples[1]["uptime_ms"] = 0
    elif boundary == "receipt_gap":
        samples[0]["received_at"] = START.isoformat()
        samples[1]["received_at"] = (START + timedelta(hours=1)).isoformat()
    elif boundary == "invalid_timestamp":
        samples[0]["device_time_utc"] = "not-a-date"
    else:
        del samples[0]["device_time_utc"]
    assert _current_observation_segment(samples) == samples[-1:]


def test_empty_observation_history_is_unarmed():
    assert _current_observation_segment([]) == []
