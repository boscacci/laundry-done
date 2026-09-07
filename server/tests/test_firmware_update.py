import copy
import hashlib
import hmac
import json
import sqlite3
import time

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient

from laundry_done_relay.app import create_app
from laundry_done_relay.firmware_update import make_package
from laundry_done_relay.ota import encryption_key, package_aad
from test_app import _post

SECRET = "isolated-test-secret"


@pytest.fixture
def rig(tmp_path):
    sent = []
    db = tmp_path / "events.sqlite3"
    client = TestClient(
        create_app(
            database_path=db,
            device_secret=SECRET,
            gotify_url="http://unused.invalid",
            gotify_app_token="unused",
            push_message=sent.append,
        )
    )
    return client, db, sent


def stage(client, package, secret=SECRET):
    body = json.dumps(package, separators=(",", ":")).encode()
    signature = hmac.new(
        secret.encode(), b"ota-stage-v1\n" + body, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/api/v1/firmware",
        content=body,
        headers={"x-laundry-signature": signature, "content-type": "application/json"},
    )


def packet(job="", result="", state="idle", event="sample-1"):
    return dict(
        device_id="sensor",
        event_id=event,
        cycle_id="boot-1",
        state="calibration_sample",
        cycle_label="unknown",
        motion_rms_mg=0.1,
        peak_mg=0.2,
        last_motion_ms=0,
        firmware_version="test",
        uptime_ms=60000,
        diagnostics=dict(
            ota_capable=True,
            detector_state=state,
            startup_settling=False,
            ota_job_id=job,
            ota_result=result,
        ),
    )


def test_encrypted_update_round_trip_is_authenticated_idempotent_and_acknowledged(rig):
    client, db, sent = rig
    image = b"\xe9" + b"test firmware content" * 100
    package = make_package(image, device_id="sensor", secret=SECRET)
    assert stage(client, package).status_code == 202
    assert stage(client, package).json()["duplicate"] is True
    response = _post(client, SECRET, packet()).json()
    offer = response["ota"]
    assert hmac.compare_digest(
        offer["signature"],
        hmac.new(
            SECRET.encode(),
            b"ota-offer-v1\n" + offer["manifest"].encode(),
            hashlib.sha256,
        ).hexdigest(),
    )
    manifest = json.loads(offer["manifest"])
    assert manifest["event_id"] == "sample-1"
    assert manifest["device_id"] == "sensor"
    job = package["job_id"]
    path = f"/api/v1/firmware/{job}"
    assert client.get(path).status_code == 401
    signature = hmac.new(
        SECRET.encode(), f"ota-download-v1\n{job}".encode(), hashlib.sha256
    ).hexdigest()
    download = client.get(path, headers={"x-laundry-signature": signature})
    assert download.status_code == 200
    assert image not in download.content
    plaintext = AESGCM(encryption_key(SECRET)).decrypt(
        bytes.fromhex(package["nonce"]),
        download.content + bytes.fromhex(package["tag"]),
        package_aad(package),
    )
    assert plaintext == image
    changed = bytearray(download.content)
    changed[50] ^= 1
    with pytest.raises(InvalidTag):
        AESGCM(encryption_key(SECRET)).decrypt(
            bytes.fromhex(package["nonce"]),
            bytes(changed) + bytes.fromhex(package["tag"]),
            package_aad(package),
        )
    ack = packet(job, "installed", event="after-reboot")
    ack["diagnostics"]["installed_sha256"] = package["sha256"]
    assert "ota" not in _post(client, SECRET, ack).json()
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute("SELECT status FROM firmware_jobs").fetchone()[0]
            == "installed"
        )
    assert sent == []


@pytest.mark.parametrize(
    "state", ["cycle_running", "quiet_candidate", "motion_confirming", "unknown"]
)
def test_update_is_deferred_during_cycle_or_uncertain_state(rig, state):
    client, _, sent = rig
    package = make_package(b"\xe9" + b"x" * 100, device_id="sensor", secret=SECRET)
    stage(client, package)
    assert "ota" not in _post(client, SECRET, packet(state=state)).json()
    assert sent == []


def test_update_rejects_auth_payload_conflict_and_expiration(rig):
    client, db, _ = rig
    package = make_package(b"\xe9" + b"x" * 100, device_id="sensor", secret=SECRET)
    assert stage(client, package, "wrong").status_code == 401
    bad = copy.deepcopy(package)
    bad["size"] += 1
    assert stage(client, bad).status_code == 422
    assert stage(client, package).status_code == 202
    bad = copy.deepcopy(package)
    bad["sha256"] = "a" * 64
    assert stage(client, bad).status_code == 409
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE firmware_jobs SET expires_at = ?", (int(time.time()) - 1,))
    assert "ota" not in _post(client, SECRET, packet()).json()


def test_update_is_not_offered_to_old_wrong_device_or_settling_firmware(rig):
    client, _, _ = rig
    stage(client, make_package(b"\xe9" + b"x" * 100, device_id="sensor", secret=SECRET))
    for i, change in enumerate(("old", "other", "settling")):
        raw = packet(event=f"sample-{i}")
        if change == "old":
            raw.pop("diagnostics")
        elif change == "other":
            raw["device_id"] = "another"
        else:
            raw["diagnostics"]["startup_settling"] = True
        assert "ota" not in _post(client, SECRET, raw).json()


def test_failed_job_stops_retrying_and_new_job_supersedes_pending(rig):
    client, db, _ = rig
    first = make_package(b"\xe9" + b"x" * 100, device_id="sensor", secret=SECRET)
    second = make_package(b"\xe9" + b"y" * 100, device_id="sensor", secret=SECRET)
    stage(client, first)
    stage(client, second)
    response = _post(client, SECRET, packet()).json()
    assert json.loads(response["ota"]["manifest"])["job_id"] == second["job_id"]
    assert (
        "ota"
        not in _post(
            client, SECRET, packet(second["job_id"], "failed", event="failed")
        ).json()
    )
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT status FROM firmware_jobs WHERE job_id=?", (first["job_id"],)
            ).fetchone()[0]
            == "superseded"
        )


def test_install_ack_requires_matching_running_image_hash(rig):
    client, db, _ = rig
    package = make_package(b"\xe9" + b"x" * 100, device_id="sensor", secret=SECRET)
    stage(client, package)
    _post(client, SECRET, packet(package["job_id"], "installed"))
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute("SELECT status FROM firmware_jobs").fetchone()[0]
            != "installed"
        )


@pytest.mark.parametrize(
    "image",
    [b"", b"wrong format", b"\xe9" * (0x140000 + 1)],
    ids=["empty", "bad-header", "oversized"],
)
def test_packaging_rejects_missing_invalid_or_oversized_image(image):
    with pytest.raises(ValueError):
        make_package(image, device_id="sensor", secret=SECRET)
