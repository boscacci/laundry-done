"""Provider-free tests of shared calling policy and project boundaries."""

import sys
from pathlib import Path
import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "services" / "phone_alerts")
)
from dispatcher import Dispatcher, MemoryStore


@pytest.fixture
def service():
    calls = []
    store = MemoryStore()
    d = Dispatcher(
        store,
        lambda job, attempt: (
            calls.append((job["id"], attempt, job["message_key"]))
            or "contact-" + str(attempt)
        ),
        {
            "laundry": {
                "principal_id": "ROLE1",
                "daily_limit": 10,
                "messages": ["laundry-01", "laundry-02", "test"],
            }
        },
        clock=lambda: 1000,
    )
    return d, store, calls


def test_same_event_creates_one_call_and_transport_retry_is_idempotent(service):
    d, s, c = service
    a = d.create("laundry", "event", "owner", "laundry-01", "ROLE1")
    b = d.create("laundry", "event", "owner", "laundry-01", "ROLE1")
    assert a["id"] == b["id"] and len(c) == 1


def test_cross_project_and_unapproved_destination_or_asset_are_denied(service):
    d, s, c = service
    for project, recipient, message, principal in [
        ("other", "owner", "test", "ROLE1"),
        ("laundry", "stranger", "test", "ROLE1"),
        ("laundry", "owner", "https://evil", "ROLE1"),
        ("laundry", "owner", "test", "ROLE2"),
    ]:
        with pytest.raises(ValueError):
            d.create(project, "event", recipient, message, principal)
    assert c == []


def test_failure_retries_once_after_two_minutes_with_same_clip(service):
    d, s, c = service
    j = d.create("laundry", "event", "owner", "laundry-01", "ROLE1")
    d.outcome(j["id"], "contact-1", "no_answer")
    d.tick()
    assert len(c) == 1
    d.clock = lambda: 1121
    d.tick()
    assert c[-1][1:] == (2, "laundry-01")
    d.outcome(j["id"], "contact-2", "no_answer")
    d.clock = lambda: 1400
    d.tick()
    assert len(c) == 2 and s.get(j["id"])["status"] == "failed"


def test_completed_playback_is_terminal_even_with_late_disconnect(service):
    d, s, c = service
    j = d.create("laundry", "event", "owner", "test", "ROLE1")
    d.outcome(j["id"], "contact-1", "completed")
    d.outcome(j["id"], "contact-1", "no_answer")
    d.clock = lambda: 1200
    d.tick()
    assert len(c) == 1 and s.get(j["id"])["status"] == "completed"


def test_expired_or_canceled_jobs_never_redial(service):
    d, s, c = service
    j = d.create("laundry", "event", "owner", "test", "ROLE1")
    d.cancel("laundry", "event", "ROLE1")
    d.clock = lambda: 2000
    d.tick()
    assert len(c) == 1 and s.get(j["id"])["status"] == "canceled"


def test_daily_quota_and_unknown_outcome_do_not_dial_indefinitely(service):
    d, s, c = service
    d.projects["laundry"]["daily_limit"] = 1
    j = d.create("laundry", "event", "owner", "test", "ROLE1")
    with pytest.raises(ValueError):
        d.create("laundry", "next", "owner", "test", "ROLE1")
    d.outcome(j["id"], "contact-1", "unknown")
    d.clock = lambda: 1200
    d.tick()
    assert len(c) == 1


def test_cancel_before_delayed_submission_leaves_tombstone(service):
    d, s, c = service
    d.cancel("laundry", "late", "ROLE1")
    result = d.create("laundry", "late", "owner", "test", "ROLE1")
    assert result["status"] == "canceled" and c == []


def test_duplicate_final_no_answer_event_cannot_postpone_retry(service):
    d, s, c = service
    j = d.create("laundry", "event", "owner", "test", "ROLE1")
    d.outcome(j["id"], "contact-1", "no_answer")
    d.clock = lambda: 1100
    d.outcome(j["id"], "contact-1", "no_answer")
    d.clock = lambda: 1121
    d.tick()
    assert len(c) == 2


def test_silent_keypress_timeout_callback_retries_once_without_late_event_regression(
    service, monkeypatch
):
    import handler as cloud

    d, store, calls = service
    job = d.create("laundry", "silent-test", "owner", "test", "ROLE1")
    monkeypatch.setattr(cloud, "engine", lambda: d)
    monkeypatch.setenv("INSTANCE_ID", "instance")

    def callback(contact):
        return cloud.handler(
            {
                "Details": {
                    "ContactData": {
                        "InstanceARN": "arn:aws:connect:us-west-2:123:instance/instance",
                        "ContactId": contact,
                        "Attributes": {"job_id": job["id"]},
                    },
                    "Parameters": {"outcome": "no_answer"},
                }
            },
            None,
        )

    assert callback("contact-1") == {"ok": "true"}
    d.outcome(job["id"], "contact-1", "unknown")
    d.clock = lambda: 1121
    d.tick()
    assert len(calls) == 2
    callback("contact-2")
    d.clock = lambda: 1400
    d.tick()
    assert len(calls) == 2
    assert store.get(job["id"])["status"] == "failed"
