import hashlib
import hmac
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from laundry_done_relay.app import _has_admin_access, _monitor_html, create_app


def _signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post(client: TestClient, secret: str, payload: dict):
    import json

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return client.post(
        "/api/v1/events",
        content=body,
        headers={
            "content-type": "application/json",
            "x-laundry-signature": _signature(secret, body),
        },
    )


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _calibration_payload(
    *,
    event_id: str,
    at: datetime,
    rms: float,
    peak: float,
    cycle_id: str = "server-classified-run",
) -> dict:
    return {
        "device_id": "laundry-stack-1",
        "event_id": event_id,
        "cycle_id": cycle_id,
        "state": "calibration_sample",
        "cycle_label": "unknown",
        "motion_rms_mg": rms,
        "last_motion_ms": 0,
        "firmware_version": "readings-only",
        "peak_mg": peak,
        "sample_window_ms": 4000,
        "sample_count": 100,
        "device_time_utc": _utc_text(at),
    }


def _js_function(script: str, name: str) -> str:
    start = script.index(f"function {name}")
    brace_start = script.index("{", start)
    depth = 0
    for index in range(brace_start, len(script)):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[start : index + 1]
    raise AssertionError(f"Could not extract JS function {name}")


def _classify_with_dashboard_js(tmp_path, samples: list[dict]) -> list[str]:
    html = _monitor_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    classifier = "\n".join(
        _js_function(script, name)
        for name in (
            "phase",
            "numeric",
            "phaseFromServerSample",
            "classifySample",
            "smoothedClassificationSamples",
            "classifySamples",
        )
    )
    runner = tmp_path / "classify-dashboard.js"
    runner.write_text(
        classifier
        + "\n"
        + f"console.log(JSON.stringify(classifySamples({json.dumps(samples)}).map(phase => phase.short)));\n"
    )
    result = subprocess.run(["node", runner], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _dashboard_js_results(tmp_path, function_names: tuple[str, ...], expression: str):
    html = _monitor_html()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    runner = tmp_path / "dashboard-function.js"
    runner.write_text(
        "\n".join(_js_function(script, name) for name in function_names)
        + "\n"
        + f"console.log(JSON.stringify({expression}));\n"
    )
    result = subprocess.run(["node", runner], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_healthz_reports_ok(tmp_path):
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
    )

    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_done_event_is_stored_and_pushed_once(tmp_path):
    sent = []
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
        push_message=lambda message: sent.append(message),
    )
    client = TestClient(app)
    payload = {
        "device_id": "laundry-stack-1",
        "event_id": "evt-1",
        "cycle_id": "cycle-1",
        "state": "done_sent",
        "cycle_label": "washer",
        "motion_rms_mg": 3.2,
        "last_motion_ms": 600001,
        "firmware_version": "0.1.0",
    }

    first = _post(client, "test-secret", payload)
    duplicate = _post(client, "test-secret", payload)

    assert first.status_code == 202
    assert first.json() == {"accepted": True, "duplicate": False}
    assert duplicate.status_code == 202
    assert duplicate.json() == {"accepted": True, "duplicate": True}
    assert sent == [
        {
            "title": "Washer done",
            "message": "No washer motion for 10 min.",
            "priority": 5,
        }
    ]


def test_second_done_event_for_same_cycle_is_stored_without_second_push(tmp_path):
    sent = []
    database_path = tmp_path / "events.sqlite3"
    app = create_app(
        database_path=database_path,
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
        push_message=lambda message: sent.append(message),
    )
    client = TestClient(app)
    payload = {
        "device_id": "laundry-stack-1",
        "event_id": "server-done-evt",
        "cycle_id": "cycle-1",
        "state": "done_sent",
        "cycle_label": "washer",
        "motion_rms_mg": 3.2,
        "last_motion_ms": 600001,
        "firmware_version": "server-classifier",
    }

    first = _post(client, "test-secret", payload)
    second = _post(client, "test-secret", {**payload, "event_id": "firmware-done-evt"})

    assert first.status_code == 202
    assert second.status_code == 202
    assert sent == [
        {
            "title": "Washer done",
            "message": "No washer motion for 10 min.",
            "priority": 5,
        }
    ]
    with sqlite3.connect(database_path) as conn:
        done_count = conn.execute(
            "SELECT count(*) FROM events WHERE state = 'done_sent' AND cycle_id = 'cycle-1'"
        ).fetchone()[0]
    assert done_count == 2


def test_rejects_bad_signature(tmp_path):
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
    )

    response = TestClient(app).post(
        "/api/v1/events",
        json={"event_id": "evt-1"},
        headers={"x-laundry-signature": "nope"},
    )

    assert response.status_code == 401


def test_rejects_invalid_negative_motion_reading(tmp_path):
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
    )

    response = _post(
        TestClient(app),
        "test-secret",
        {
            "device_id": "laundry-stack-1",
            "event_id": "bad-reading",
            "cycle_id": "cycle-1",
            "state": "calibration_sample",
            "cycle_label": "unknown",
            "motion_rms_mg": -1.0,
            "last_motion_ms": 0,
            "firmware_version": "calibration-capture",
        },
    )

    assert response.status_code == 422


def test_stack_label_sends_generic_alert(tmp_path):
    sent = []
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
        push_message=lambda message: sent.append(message),
    )

    response = _post(
        TestClient(app),
        "test-secret",
        {
            "device_id": "laundry-stack-1",
            "event_id": "evt-stack",
            "cycle_id": "cycle-stack",
            "state": "done_sent",
            "cycle_label": "stack",
            "motion_rms_mg": 4.0,
            "last_motion_ms": 720000,
            "firmware_version": "0.1.0",
        },
    )

    assert response.status_code == 202
    assert sent[0]["title"] == "Laundry stack stopped"


def test_button_pressed_event_sends_phone_test_alert(tmp_path):
    sent = []
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
        push_message=lambda message: sent.append(message),
    )

    response = _post(
        TestClient(app),
        "test-secret",
        {
            "device_id": "button-board",
            "event_id": "button-evt-1",
            "cycle_id": "manual-button-test",
            "state": "button_pressed",
            "cycle_label": "unknown",
            "motion_rms_mg": 0.0,
            "last_motion_ms": 0,
            "firmware_version": "button-test",
        },
    )

    assert response.status_code == 202
    assert sent == [
        {
            "title": "Laundry button pressed",
            "message": "ESP32 button test reached the relay.",
            "priority": 5,
        }
    ]


def test_motion_started_event_sends_phone_test_alert(tmp_path):
    sent = []
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
        push_message=lambda message: sent.append(message),
    )

    response = _post(
        TestClient(app),
        "test-secret",
        {
            "device_id": "laundry-stack-1",
            "event_id": "motion-evt-1",
            "cycle_id": "motion-request-test",
            "state": "motion_started",
            "cycle_label": "unknown",
            "motion_rms_mg": 51.0,
            "last_motion_ms": 0,
            "firmware_version": "motion-request-test",
        },
    )

    assert response.status_code == 202
    assert sent == [
        {
            "title": "Laundry motion detected",
            "message": "Fast motion trigger from laundry-stack-1: 51.0 mg (motion-evt-1).",
            "priority": 5,
        }
    ]


def test_calibration_samples_are_stored_without_phone_push_and_can_be_listed(tmp_path):
    sent = []
    database_path = tmp_path / "events.sqlite3"
    app = create_app(
        database_path=database_path,
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
        push_message=lambda message: sent.append(message),
    )

    response = _post(
        TestClient(app),
        "test-secret",
        {
            "device_id": "laundry-stack-1",
            "event_id": "cal-evt-1",
            "cycle_id": "calibration-run-1",
            "state": "calibration_sample",
            "cycle_label": "unknown",
            "motion_rms_mg": 18.4,
            "last_motion_ms": 0,
            "firmware_version": "calibration-capture",
            "peak_mg": 73.2,
            "sample_window_ms": 4000,
            "sample_count": 100,
        },
    )

    assert response.status_code == 202
    assert sent == []
    with sqlite3.connect(database_path) as conn:
        stored = conn.execute(
            "SELECT state, motion_rms_mg, raw_json FROM events WHERE event_id = ?",
            ("cal-evt-1",),
        ).fetchone()
    assert stored[0] == "calibration_sample"
    assert stored[1] == 18.4
    assert '"peak_mg":73.2' in stored[2]

    list_response = TestClient(app).get(
        "/api/v1/calibration/events",
        headers={"x-laundry-admin-secret": "test-secret"},
    )

    assert list_response.status_code == 200
    assert list_response.json()["events"][0]["event_id"] == "cal-evt-1"
    assert list_response.json()["events"][0]["peak_mg"] == 73.2
    assert list_response.json()["events"][0]["server_phase"] == "washer"
    assert list_response.json()["notifications"] == []


def test_calibration_listing_includes_gotify_notification_moments(tmp_path):
    sent = []
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
        push_message=lambda message: sent.append(message),
    )
    client = TestClient(app)

    response = _post(
        client,
        "test-secret",
        {
            "device_id": "laundry-stack-1",
            "event_id": "done-evt-1",
            "cycle_id": "cycle-1",
            "state": "done_sent",
            "cycle_label": "dryer",
            "motion_rms_mg": 1.2,
            "last_motion_ms": 600001,
            "firmware_version": "0.1.0",
        },
    )
    assert response.status_code == 202

    list_response = client.get(
        "/api/v1/calibration/events",
        headers={"x-laundry-admin-secret": "test-secret"},
    )

    assert list_response.status_code == 200
    notifications = list_response.json()["notifications"]
    assert notifications == [
        {
            "event_id": "done-evt-1",
            "device_id": "laundry-stack-1",
            "cycle_id": "cycle-1",
            "state": "done_sent",
            "cycle_label": "dryer",
            "title": "Dryer done",
            "message": "No dryer motion for 10 min.",
            "received_at": notifications[0]["received_at"],
        }
    ]
    assert sent == [
        {
            "title": "Dryer done",
            "message": "No dryer motion for 10 min.",
            "priority": 5,
        }
    ]


def test_server_classifies_readings_and_sends_done_notification(tmp_path):
    sent = []
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
        push_message=lambda message: sent.append(message),
    )
    client = TestClient(app)
    start = datetime(2026, 5, 24, 20, 0, tzinfo=timezone.utc)

    for index in range(8):
        response = _post(
            client,
            "test-secret",
            _calibration_payload(
                event_id=f"active-{index}",
                at=start + timedelta(seconds=index * 10),
                rms=32.0,
                peak=75.0,
            ),
        )
        assert response.status_code == 202
    assert sent == []

    for index in range(66):
        response = _post(
            client,
            "test-secret",
            _calibration_payload(
                event_id=f"quiet-{index}",
                at=start + timedelta(seconds=80 + index * 10),
                rms=0.8,
                peak=2.0,
            ),
        )
        assert response.status_code == 202
    assert sent == []

    final_response = _post(
        client,
            "test-secret",
            _calibration_payload(
                event_id="quiet-final",
                at=start + timedelta(seconds=80 + 66 * 10),
                rms=0.8,
                peak=2.0,
            ),
        )

    assert final_response.status_code == 202
    assert sent == [
        {
            "title": "Washer done",
            "message": "No washer motion for 10 min.",
            "priority": 5,
        }
    ]

    duplicate_quiet = _post(
        client,
            "test-secret",
            _calibration_payload(
                event_id="quiet-duplicate",
                at=start + timedelta(seconds=80 + 67 * 10),
                rms=0.8,
                peak=2.0,
            ),
    )
    assert duplicate_quiet.status_code == 202
    assert len(sent) == 1

    list_response = client.get(
        "/api/v1/calibration/events?limit=20",
        headers={"x-laundry-admin-secret": "test-secret"},
    )

    assert list_response.status_code == 200
    assert list_response.json()["notifications"][0]["state"] == "done_sent"
    assert list_response.json()["notifications"][0]["title"] == "Washer done"


def test_server_done_label_uses_current_cycle_after_previous_notification(tmp_path):
    sent = []
    database_path = tmp_path / "events.sqlite3"
    app = create_app(
        database_path=database_path,
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
        push_message=lambda message: sent.append(message),
    )
    client = TestClient(app)
    washer_start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=4)

    for index in range(8):
        response = _post(
            client,
            "test-secret",
            _calibration_payload(
                event_id=f"washer-active-{index}",
                at=washer_start + timedelta(seconds=index * 10),
                rms=32.0,
                peak=75.0,
            ),
        )
        assert response.status_code == 202

    for index in range(67):
        response = _post(
            client,
            "test-secret",
            _calibration_payload(
                event_id=f"washer-quiet-{index}",
                at=washer_start + timedelta(seconds=80 + index * 10),
                rms=0.8,
                peak=2.0,
            ),
        )
        assert response.status_code == 202

    assert sent == [
        {
            "title": "Washer done",
            "message": "No washer motion for 10 min.",
            "priority": 5,
        }
    ]
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            UPDATE events
            SET received_at = ?
            WHERE event_id LIKE 'washer-active-%'
               OR event_id LIKE 'washer-quiet-%'
            """,
            (_utc_text(washer_start),),
        )
        conn.execute(
            "UPDATE events SET received_at = ? WHERE state = 'done_sent'",
            (_utc_text(washer_start + timedelta(minutes=13)),),
        )
    sent.clear()

    dryer_start = washer_start + timedelta(hours=2)
    dryer_readings = [(27.9, 69.6)] + [(24.0, 50.0)] * 15
    for index, (rms, peak) in enumerate(dryer_readings):
        response = _post(
            client,
            "test-secret",
            _calibration_payload(
                event_id=f"dryer-active-{index}",
                at=dryer_start + timedelta(seconds=index * 10),
                rms=rms,
                peak=peak,
            ),
        )
        assert response.status_code == 202

    for index in range(67):
        response = _post(
            client,
            "test-secret",
            _calibration_payload(
                event_id=f"dryer-quiet-{index}",
                at=dryer_start + timedelta(seconds=len(dryer_readings) * 10 + index * 10),
                rms=0.8,
                peak=2.0,
            ),
        )
        assert response.status_code == 202

    assert sent == [
        {
            "title": "Dryer done",
            "message": "No dryer motion for 10 min.",
            "priority": 5,
        }
    ]


def test_calibration_listing_can_filter_handling_noise_by_peak(tmp_path):
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
    )
    client = TestClient(app)
    base_payload = {
        "device_id": "laundry-stack-1",
        "cycle_id": "calibration-run-1",
        "state": "calibration_sample",
        "cycle_label": "unknown",
        "motion_rms_mg": 4.0,
        "last_motion_ms": 0,
        "firmware_version": "calibration-capture",
        "sample_window_ms": 4000,
        "sample_count": 100,
    }
    _post(client, "test-secret", {**base_payload, "event_id": "real-motion", "peak_mg": 12.0})
    _post(client, "test-secret", {**base_payload, "event_id": "handling-noise", "peak_mg": 73.2})

    response = client.get(
        "/api/v1/calibration/events?max_peak_mg=40",
        headers={"x-laundry-admin-secret": "test-secret"},
    )

    assert response.status_code == 200
    assert [event["event_id"] for event in response.json()["events"]] == ["real-motion"]


def test_calibration_listing_filters_noise_before_applying_limit(tmp_path):
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
    )
    client = TestClient(app)
    base_payload = {
        "device_id": "laundry-stack-1",
        "cycle_id": "calibration-run-1",
        "state": "calibration_sample",
        "cycle_label": "unknown",
        "motion_rms_mg": 4.0,
        "last_motion_ms": 0,
        "firmware_version": "calibration-capture",
        "sample_window_ms": 4000,
        "sample_count": 100,
    }
    _post(client, "test-secret", {**base_payload, "event_id": "good-1", "peak_mg": 12.0})
    _post(client, "test-secret", {**base_payload, "event_id": "good-2", "peak_mg": 13.0})
    _post(client, "test-secret", {**base_payload, "event_id": "noise-1", "peak_mg": 500.0})
    _post(client, "test-secret", {**base_payload, "event_id": "noise-2", "peak_mg": 600.0})

    response = client.get(
        "/api/v1/calibration/events?limit=2&max_peak_mg=300",
        headers={"x-laundry-admin-secret": "test-secret"},
    )

    assert response.status_code == 200
    assert [event["event_id"] for event in response.json()["events"]] == ["good-2", "good-1"]


def test_calibration_listing_can_return_two_week_history_above_previous_ui_cap(tmp_path):
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
    )
    client = TestClient(app)
    base_payload = {
        "device_id": "laundry-stack-1",
        "cycle_id": "calibration-run-1",
        "state": "calibration_sample",
        "cycle_label": "unknown",
        "motion_rms_mg": 4.0,
        "last_motion_ms": 0,
        "firmware_version": "calibration-capture",
        "sample_window_ms": 4000,
        "sample_count": 100,
        "peak_mg": 12.0,
    }
    for index in range(1002):
        _post(client, "test-secret", {**base_payload, "event_id": f"sample-{index:04d}"})

    response = client.get(
        "/api/v1/calibration/events?limit=1002&days=14&max_peak_mg=300",
        headers={"x-laundry-admin-secret": "test-secret"},
    )

    assert response.status_code == 200
    event_ids = [event["event_id"] for event in response.json()["events"]]
    assert len(event_ids) == 1002
    assert "sample-0000" in event_ids


def test_calibration_listing_expires_events_after_14_days(tmp_path):
    database_path = tmp_path / "events.sqlite3"
    app = create_app(
        database_path=database_path,
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
    )
    client = TestClient(app)
    base_payload = {
        "device_id": "laundry-stack-1",
        "cycle_id": "calibration-run-1",
        "state": "calibration_sample",
        "cycle_label": "unknown",
        "motion_rms_mg": 4.0,
        "last_motion_ms": 0,
        "firmware_version": "calibration-capture",
        "sample_window_ms": 4000,
        "sample_count": 100,
    }
    _post(client, "test-secret", {**base_payload, "event_id": "expired", "peak_mg": 12.0})
    _post(client, "test-secret", {**base_payload, "event_id": "fresh", "peak_mg": 13.0})
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE events SET received_at = datetime('now', '-15 days') WHERE event_id = ?",
            ("expired",),
        )

    response = client.get(
        "/api/v1/calibration/events?days=14",
        headers={"x-laundry-admin-secret": "test-secret"},
    )

    assert response.status_code == 200
    assert [event["event_id"] for event in response.json()["events"]] == ["fresh"]
    with sqlite3.connect(database_path) as conn:
        remaining_event_ids = [
            row[0] for row in conn.execute("SELECT event_id FROM events ORDER BY event_id")
        ]
    assert remaining_event_ids == ["fresh"]


def test_calibration_listing_requires_admin_secret(tmp_path):
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
    )

    response = TestClient(app).get("/api/v1/calibration/events")

    assert response.status_code == 401


def test_local_monitor_request_can_list_calibration_without_secret():
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})

    assert _has_admin_access("test-secret", "", request)


def test_forwarded_public_monitor_request_does_not_inherit_local_proxy_access():
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"x-forwarded-for": "203.0.113.10"},
    )

    assert not _has_admin_access("test-secret", "", request)


def test_forwarded_tailscale_monitor_request_can_use_configured_cidr():
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"x-forwarded-for": "100.79.119.18, 203.0.113.10"},
    )

    assert _has_admin_access(
        "test-secret",
        "",
        request,
        monitor_tailscale_ips={"100.64.0.0/10"},
    )


def test_remote_monitor_request_still_requires_admin_secret():
    request = SimpleNamespace(client=SimpleNamespace(host="192.168.1.50"), headers={})

    assert not _has_admin_access("test-secret", "", request)
    assert _has_admin_access("test-secret", "test-secret", request)


def test_tailscale_identity_can_list_calibration_without_device_secret():
    request = SimpleNamespace(
        client=SimpleNamespace(host="172.20.0.1"),
        headers={"tailscale-user-login": "user@example.com"},
    )

    assert _has_admin_access("test-secret", "", request, {"user@example.com"})


def test_configured_tailscale_ip_can_list_calibration_without_device_secret():
    request = SimpleNamespace(client=SimpleNamespace(host="100.79.119.18"), headers={})

    assert _has_admin_access(
        "test-secret",
        "",
        request,
        monitor_tailscale_ips={"100.79.119.18"},
    )


def test_configured_tailscale_cidr_can_list_calibration_without_device_secret():
    request = SimpleNamespace(client=SimpleNamespace(host="100.79.119.18"), headers={})

    assert _has_admin_access(
        "test-secret",
        "",
        request,
        monitor_tailscale_ips={"100.64.0.0/10"},
    )


def test_configured_tailscale_cidr_does_not_allow_lan_clients():
    request = SimpleNamespace(client=SimpleNamespace(host="192.168.1.219"), headers={})

    assert not _has_admin_access(
        "test-secret",
        "",
        request,
        monitor_tailscale_ips={"100.64.0.0/10"},
    )


def test_unapproved_tailscale_ip_cannot_list_calibration():
    request = SimpleNamespace(client=SimpleNamespace(host="100.79.119.19"), headers={})

    assert not _has_admin_access(
        "test-secret",
        "",
        request,
        monitor_tailscale_ips={"100.79.119.18"},
    )


def test_unapproved_tailscale_identity_cannot_list_calibration():
    request = SimpleNamespace(
        client=SimpleNamespace(host="172.20.0.1"),
        headers={"tailscale-user-login": "other@example.com"},
    )

    assert not _has_admin_access("test-secret", "", request, {"user@example.com"})


def test_monitor_page_serves_realtime_dashboard(tmp_path):
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
    )

    response = TestClient(app).get("/monitor")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.headers["cache-control"] == "no-store"
    assert "Laundry Activity Monitor" in response.text
    assert "/api/v1/calibration/events" in response.text
    assert "max_peak_mg=300" in response.text
    assert "Filtering jolts &gt; 300 mg" in response.text
    assert "0.3 g" in response.text
    assert "mg = 1/1000 g" in response.text
    assert "RMS = typical shake" in response.text
    assert "milli-g" in response.text
    assert "Root mean square" not in response.text
    assert "Phase backgrounds" in response.text
    assert "Best guess" in response.text
    assert "Vibration strength" in response.text
    assert "Washer running" in response.text
    assert "Washer-like value pattern" in response.text
    assert "Dryer running" in response.text
    assert "Sustained mid-level tumble pattern" in response.text
    assert "classifySamples" in response.text
    assert "Arduino Wi-Fi" in response.text
    assert "Wake state" in response.text
    assert "connection-card" in response.text
    assert "updateConnectionIndicators" in response.text
    assert "escapeHtml" in response.text
    assert "connection-offline" in response.text
    assert "wake-node" in response.text
    assert "wake-state" in response.text
    assert 'max="12" value="4"' in response.text
    assert "about 2 min" in response.text
    assert "sampleTime" in response.text
    assert "sampleTimestamp" in response.text
    assert 'value="900000">Live 15 min' in response.text
    assert 'value="1920000" selected>Last 32 min' in response.text
    assert 'value="1209600000">Last 14 days' in response.text
    assert "latestTimestamp - windowMs" in response.text
    assert "drawTimeAxis" in response.text
    assert "niceTimeTickMs" in response.text
    assert "formatAxisTime" in response.text
    assert "classifySample" in response.text
    assert "phaseFromServerSample" in response.text
    assert "smoothedClassificationSamples(samples, 8)" in response.text
    assert "drawClassificationBands" in response.text
    assert "drawNotificationMarkers" in response.text
    assert "state.notifications" in response.text
    assert "latestActivityTimestamp" in response.text
    assert "visibleNotifications" in response.text
    assert "Notification moments" in response.text
    assert "Gotify notification" in response.text
    assert "x-laundry-admin-secret" not in response.text
    assert "limit=200000&days=14&max_peak_mg=300" in response.text
    assert "startLiveUpdates" in response.text
    assert "startLiveUpdates();" in response.text
    assert "if (maxValue <= 5) return 5" in response.text
    assert "rms <= 5 && peak <= 8" in response.text
    assert "washerValueCandidate = (rms >= 2.3 && rms <= 8 && peak >= 8 && peak <= 35) || rms >= 28 || peak >= 65" in response.text
    assert "dryerValueCandidate = rms >= 12 && rms <= 28 && peak >= 25 && peak <= 65" in response.text
    assert "recentWasherAge < 6" in response.text
    assert "ping-countdown" in response.text
    assert "conic-gradient" in response.text
    assert "estimateSampleCadenceMs" in response.text
    assert "updatePingCountdown" in response.text
    assert "setInterval(updatePingCountdown, 250)" in response.text
    assert "rangeMs >= 2 * 24 * 60 * 60 * 1000" in response.text
    assert "auth-panel" not in response.text
    assert "Direct LAN login" not in response.text
    assert "openAuthPanel" not in response.text
    assert "Tailnet access required" in response.text
    assert "Optional secret" not in response.text
    assert 'id="connect"' not in response.text
    assert "Sensor sample" in response.text
    assert "Wi-Fi signal" in response.text
    assert "Relay received" in response.text
    assert "Basecamp" not in response.text
    assert "latest.event_id" not in response.text
    assert "latest.cycle_id" not in response.text
    assert "<canvas" in response.text
    assert 'id="zoom"' in response.text
    assert 'id="scale-mode"' in response.text
    assert 'id="smooth"' in response.text
    assert 'id="tooltip"' in response.text
    assert "mousemove" in response.text


def test_monitor_root_redirects_to_dashboard(tmp_path):
    app = create_app(
        database_path=tmp_path / "events.sqlite3",
        device_secret="test-secret",
        gotify_url="http://gotify.local",
        gotify_app_token="token",
    )

    response = TestClient(app, follow_redirects=False).get("/")

    assert response.status_code == 307
    assert response.headers["location"] == "/monitor"

    head_response = TestClient(app, follow_redirects=False).head("/")

    assert head_response.status_code == 307
    assert head_response.headers["location"] == "/monitor"


def test_dashboard_classifier_uses_accelerometer_values_without_handoff(tmp_path):
    washer_samples = [{"motion_rms_mg": 2.6, "peak_mg": 15.2} for _ in range(8)]
    dryer_samples = [{"motion_rms_mg": 22.0, "peak_mg": 50.0} for _ in range(8)]

    assert _classify_with_dashboard_js(tmp_path, washer_samples)[-1] == "washer"
    assert _classify_with_dashboard_js(tmp_path, dryer_samples)[-1] == "dryer"


def test_dashboard_formats_stale_check_in_age_as_days_and_hours(tmp_path):
    assert _dashboard_js_results(
        tmp_path,
        ("formatAge",),
        "[formatAge(45), formatAge(5 * 60), formatAge(65 * 60), formatAge(60 * 60 * 60)]",
    ) == ["45s", "5 min", "1 hr 5 min", "2 days 12 hr"]


def test_dashboard_classifier_uses_eight_sample_smoothed_values(tmp_path):
    samples = [
        {"motion_rms_mg": 0.9, "peak_mg": 2.0},
        {"motion_rms_mg": 0.9, "peak_mg": 2.0},
        {"motion_rms_mg": 0.9, "peak_mg": 2.0},
        {"motion_rms_mg": 0.9, "peak_mg": 2.0},
        {"motion_rms_mg": 0.9, "peak_mg": 2.0},
        {"motion_rms_mg": 0.9, "peak_mg": 2.0},
        {"motion_rms_mg": 0.9, "peak_mg": 2.0},
        {"motion_rms_mg": 22.0, "peak_mg": 45.0},
    ]

    assert _classify_with_dashboard_js(tmp_path, samples)[-1] == "quiet"


def test_dashboard_prefers_server_classification_fields(tmp_path):
    samples = [{"motion_rms_mg": 40.0, "peak_mg": 80.0, "server_phase": "quiet"}]

    assert _classify_with_dashboard_js(tmp_path, samples)[-1] == "quiet"
