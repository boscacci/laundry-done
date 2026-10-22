import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from laundry_done_relay.app import create_app
from laundry_done_relay import delivery
from test_app import _post


def setup(tmp_path):
    path = tmp_path / "events.sqlite3"
    app = create_app(
        database_path=path, device_secret="test-secret", alert_backend="simulation"
    )
    return path, TestClient(app)


def done(client, event_id="completion", cycle="boot"):
    return _post(
        client,
        "test-secret",
        dict(
            device_id="laundry-stack-1",
            event_id=event_id,
            cycle_id=cycle,
            state="done_sent",
            cycle_label="stack",
            motion_rms_mg=1,
            last_motion_ms=240000,
            firmware_version="legacy",
        ),
    )


def test_completion_and_outbox_commit_once_without_network(tmp_path):
    path, client = setup(tmp_path)
    assert done(client).status_code == 202
    assert done(client).json()["duplicate"]
    assert done(client, "other").status_code == 202
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from notification_jobs").fetchone()[0] == 1
        assert db.execute("select count(*) from events").fetchone()[0] == 2
    assert delivery.jobs(path)[0]["status"] == "queued"


def test_migration_never_calls_historical_completions(tmp_path):
    path, client = setup(tmp_path)
    done(client)
    with sqlite3.connect(path) as db:
        db.execute("delete from notification_jobs")
    delivery.init_db(path)
    assert delivery.jobs(path) == []


def test_failed_submission_survives_restart_with_same_id_and_clip(tmp_path):
    path, client = setup(tmp_path)
    done(client)
    before = delivery.jobs(path)[0]

    def fail(job):
        raise TimeoutError("sensitive destination must never reach logs")

    delivery.work_once(path, fail, now=before["created_at"])
    delivery.init_db(path)
    seen = []
    delivery.work_once(
        path,
        lambda job: seen.append(job) or {"status": "accepted", "job_id": job["id"]},
        now=before["created_at"] + 31,
    )
    assert seen[0]["id"] == before["id"]
    assert seen[0]["message_key"] == before["message_key"]
    assert delivery.jobs(path)[0]["error"] is None


def test_concurrent_workers_only_claim_one_job(tmp_path):
    path, client = setup(tmp_path)
    done(client)
    calls = []

    def run(_):
        delivery.work_once(
            path,
            lambda j: calls.append(j["id"]) or {"status": "accepted"},
            now=delivery.jobs(path)[0]["created_at"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))
    assert len(calls) == 1


def test_expiry_and_cancel_prevent_submission(tmp_path):
    path, client = setup(tmp_path)
    done(client)
    job = delivery.jobs(path)[0]
    delivery.work_once(
        path, lambda j: pytest.fail("expired call"), now=job["expires_at"] + 1
    )
    assert delivery.jobs(path)[0]["status"] == "expired"


def test_controls_reject_cross_origin_and_deduplicate(tmp_path):
    path, client = setup(tmp_path)
    headers = {
        "x-laundry-admin-secret": "test-secret",
        "Idempotency-Key": "start-1",
        "Origin": "https://evil.example",
    }
    assert (
        client.post(
            "/api/v1/control", json={"action": "start"}, headers=headers
        ).status_code
        == 403
    )
    headers.pop("Origin")
    first = client.post("/api/v1/control", json={"action": "start"}, headers=headers)
    assert first.status_code == 200
    assert (
        client.post("/api/v1/control", json={"action": "start"}, headers=headers).json()
        == first.json()
    )
    assert (
        client.post(
            "/api/v1/control", json={"action": "cancel"}, headers=headers
        ).status_code
        == 409
    )
    assert client.post("/api/v1/control", json={"action": "start"}).status_code == 401


def test_test_call_requires_explicit_control_and_pause_blocks_it(tmp_path):
    path, client = setup(tmp_path)
    headers = {"x-laundry-admin-secret": "test-secret", "Idempotency-Key": "pause"}
    client.post("/api/v1/control", json={"action": "pause"}, headers=headers)
    headers["Idempotency-Key"] = "test"
    assert (
        client.post(
            "/api/v1/control", json={"action": "test"}, headers=headers
        ).status_code
        == 409
    )
    assert delivery.jobs(path) == []


def test_device_events_cannot_override_destination_or_message(tmp_path):
    path, client = setup(tmp_path)
    done(client)
    job = delivery.jobs(path)[0]
    assert job["recipient_id"] == "owner"
    assert job["message_key"].startswith("laundry-")
    assert "destination" not in job


def test_untrusted_proxy_cannot_forge_monitor_identity(monkeypatch):
    from types import SimpleNamespace
    from laundry_done_relay.app import _has_admin_access

    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
    request = SimpleNamespace(
        client=SimpleNamespace(host="192.168.1.90"),
        headers={
            "tailscale-user-login": "owner@example.com",
            "x-forwarded-for": "100.70.1.1",
        },
    )
    assert not _has_admin_access(
        "secret", "", request, {"owner@example.com"}, {"100.64.0.0/10"}
    )


def test_command_hmac_is_bound_to_packet_session_and_bounded(tmp_path):
    import hashlib
    import hmac
    import json
    from laundry_done_relay.app import _monitoring_command

    path, client = setup(tmp_path)
    delivery.control(path, "start", "laundry-stack-1", "start")
    raw = {"device_id": "laundry-stack-1", "cycle_id": "boot", "event_id": "packet-1"}
    result = _monitoring_command(path, "secret", raw)
    assert hmac.compare_digest(
        result["signature"],
        hmac.new(
            b"secret", b"monitoring-v1\n" + result["payload"].encode(), hashlib.sha256
        ).hexdigest(),
    )
    command = json.loads(result["payload"])
    assert command["event_id"] == "packet-1" and command["session_id"] == "boot"
    assert 0 < command["lease_seconds"] <= 14400


def test_cancelled_monitor_does_not_rearm_on_same_boot(tmp_path):
    path, client = setup(tmp_path)
    raw = {"device_id": "laundry-stack-1", "cycle_id": "boot", "event_id": "sample-1"}
    delivery.observe(path, raw, True, now=100)
    delivery.control(path, "cancel", "laundry-stack-1", "cancel", now=101)
    delivery.observe(path, {**raw, "event_id": "sample-2"}, True, now=102)
    assert (
        delivery.status(path, "laundry-stack-1", now=102)["session"]["status"]
        == "canceled"
    )


def test_expired_monitor_does_not_extend_four_hour_limit(tmp_path):
    path, client = setup(tmp_path)
    raw = {"device_id": "laundry-stack-1", "cycle_id": "boot", "event_id": "sample-1"}
    delivery.observe(path, raw, True, now=100)
    delivery.observe(path, {**raw, "event_id": "sample-2"}, True, now=14501)
    assert (
        delivery.status(path, "laundry-stack-1", now=14501)["session"]["status"]
        == "expired"
    )


def test_https_origin_is_valid_behind_tls_proxy_without_trusting_forwarded_headers(
    tmp_path,
):
    path, client = setup(tmp_path)
    response = client.post(
        "/api/v1/control",
        json={"action": "start"},
        headers={
            "x-laundry-admin-secret": "test-secret",
            "Idempotency-Key": "tls-start",
            "Host": "laundry.example",
            "Origin": "https://laundry.example",
        },
    )
    assert response.status_code == 200


def test_outbox_insert_failure_rolls_back_completion_event(tmp_path, monkeypatch):
    path, client = setup(tmp_path)

    def fail(*args):
        raise sqlite3.IntegrityError("outbox failure")

    monkeypatch.setattr(delivery, "enqueue_completion", fail)
    with pytest.raises(sqlite3.IntegrityError, match="outbox failure"):
        done(client)
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from events").fetchone()[0] == 0
