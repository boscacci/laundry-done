"""Owner-labeled stationary desk and finishing-dryer regressions."""

import csv
from datetime import timedelta
from pathlib import Path

import pytest

from laundry_done_relay.app import _classify_server_event, _server_base_phases
from test_app import _calibration_payload, _classify_with_dashboard_js, _post
from test_notification_evidence import START, monitor  # noqa: F401


def measured_windows(name="desk_noise"):
    path = Path(__file__).resolve().parents[2] / f"tests/fixtures/{name}.csv"
    with path.open() as stream:
        return [
            {"motion_rms_mg": float(row["rms_mg"]), "peak_mg": float(row["peak_mg"])}
            for row in csv.DictReader(stream)
        ]


def test_measured_desk_spikes_are_quiet_in_relay_and_dashboard(tmp_path):
    samples = measured_windows()
    assert len(samples) == 17
    assert {_classify_server_event(s)["short"] for s in samples} == {"quiet"}
    assert set(_classify_with_dashboard_js(tmp_path, samples)) == {"quiet"}


def test_entire_measured_dryer_capture_is_motion_not_an_appliance_guess(tmp_path):
    samples = measured_windows("dryer_finishing")
    assert len(samples) == 41
    assert {_classify_server_event(s)["short"] for s in samples} == {"active"}
    assert set(_classify_with_dashboard_js(tmp_path, samples)) == {"active"}


@pytest.mark.parametrize(
    "rms,peak", [(36.72009, 86.93901), (42.6087, 106.9219), (48.51279, 112.7674)]
)
def test_known_dryer_motion_does_not_claim_washer_identity(tmp_path, rms, peak):
    sample = {"motion_rms_mg": rms, "peak_mg": peak}
    assert _classify_server_event(sample)["short"] == "active"
    assert _classify_with_dashboard_js(tmp_path, [sample]) == ["active"]


@pytest.mark.parametrize(
    "sample",
    [
        {},
        {"motion_rms_mg": None, "peak_mg": 2},
        {"motion_rms_mg": -1, "peak_mg": 2},
        {"motion_rms_mg": 3, "peak_mg": 2},
        {"motion_rms_mg": True, "peak_mg": 2},
        {"motion_rms_mg": 0, "peak_mg": 0, "sample_valid": False},
    ],
)
def test_invalid_samples_are_never_smoothed_into_quiet(tmp_path, sample):
    assert _classify_server_event(sample)["short"] == "invalid"
    assert _server_base_phases([sample])[-1]["short"] == "invalid"
    assert _classify_with_dashboard_js(tmp_path, [sample]) == ["invalid"]


@pytest.mark.parametrize(
    "rms,peak,expected",
    [
        (3.5, 19.99, "quiet"),
        (3.5001, 19.99, "settling"),
        (3.9999, 19.99, "settling"),
        (4.0, 19.99, "active"),
        (2, 20, "active"),
        (2, 300, "active"),
        (2, 300.01, "handling"),
    ],
)
def test_noise_threshold_boundaries_match_dashboard(tmp_path, rms, peak, expected):
    sample = {"motion_rms_mg": rms, "peak_mg": peak}
    assert _classify_server_event(sample)["short"] == expected
    assert _classify_with_dashboard_js(tmp_path, [sample]) == [expected]


def test_real_noise_does_not_arm_or_postpone_completion(monitor):  # noqa: F811
    def send(seconds, sample):
        payload = _calibration_payload(
            event_id=f"sample-{seconds}",
            cycle_id="measured-replay",
            at=START + timedelta(seconds=seconds),
            rms=sample["motion_rms_mg"],
            peak=sample["peak_mg"],
        )
        assert _post(monitor[0], "test-secret", payload).status_code == 202

    noise = measured_windows()
    for seconds in range(0, 900, 10):
        send(seconds, noise[(seconds // 10) % len(noise)])
    assert monitor[1] == []
    for seconds in range(900, 1510, 10):
        send(seconds, {"motion_rms_mg": 42.6087, "peak_mg": 106.9219})
    for seconds in range(1510, 1750, 10):
        send(seconds, noise[(seconds // 10) % len(noise)])
    assert monitor[1] == []
    send(1750, noise[0])
    assert len(monitor[1]) == 1
    assert monitor[1][0]["title"] == "Laundry stack stopped"
    for seconds in range(1760, 1850, 10):
        send(seconds, noise[(seconds // 10) % len(noise)])
    assert len(monitor[1]) == 1
