import hashlib
import hmac

from fastapi.testclient import TestClient

from laundry_done_relay.app import create_app


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
