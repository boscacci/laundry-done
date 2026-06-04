import json

import httpx

from laundry_done_relay import warmup


def test_config_from_env_reads_urls_and_defaults():
    config = warmup.config_from_env(
        {
            "WARMUP_URLS": "https://laundry.example.com/healthz, https://gotify.example.com/health",
        }
    )

    assert config.urls == (
        "https://laundry.example.com/healthz",
        "https://gotify.example.com/health",
    )
    assert config.host_headers == (None, None)
    assert config.interval_seconds == 300
    assert config.timeout_seconds == 10.0
    assert config.verify_tls is True


def test_config_from_env_maps_host_headers_to_urls():
    config = warmup.config_from_env(
        {
            "WARMUP_URLS": "https://caddy/healthz,https://caddy/health",
            "WARMUP_HOST_HEADERS": "laundry.example.com, gotify.example.com",
        }
    )

    assert config.host_headers == ("laundry.example.com", "gotify.example.com")


def test_config_from_env_rejects_mismatched_host_headers():
    try:
        warmup.config_from_env(
            {
                "WARMUP_URLS": "https://caddy/healthz,https://caddy/health",
                "WARMUP_HOST_HEADERS": "laundry.example.com",
            }
        )
    except ValueError as exc:
        assert str(exc) == "WARMUP_HOST_HEADERS must match WARMUP_URLS length"
    else:
        raise AssertionError("mismatched WARMUP_HOST_HEADERS should fail fast")


def test_config_from_env_can_disable_tls_verification():
    config = warmup.config_from_env(
        {
            "WARMUP_URLS": "https://laundry.example.com/healthz",
            "WARMUP_VERIFY_TLS": "false",
        }
    )

    assert config.verify_tls is False


def test_config_from_env_rejects_missing_urls():
    try:
        warmup.config_from_env({})
    except ValueError as exc:
        assert str(exc) == "WARMUP_URLS must contain at least one URL"
    else:
        raise AssertionError("missing WARMUP_URLS should fail fast")


def test_probe_once_reports_success_and_redacts_query_from_logs():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["token"] == "secret"
        assert request.headers["host"] == "laundry.example.com"
        return httpx.Response(200, json={"ok": True})

    records = warmup.probe_once(
        ("https://laundry.example.com/healthz?token=secret",),
        host_headers=("laundry.example.com",),
        timeout_seconds=1.0,
        verify_tls=False,
        transport=httpx.MockTransport(handler),
    )

    assert len(records) == 1
    record = records[0]
    assert record["event"] == "warmup_probe"
    assert record["ok"] is True
    assert record["url"] == "https://laundry.example.com/healthz"
    assert record["host_header"] == "laundry.example.com"
    assert record["status_code"] == 200
    assert "elapsed_ms" in record
    json.dumps(record)


def test_probe_once_reports_retryable_failure_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    records = warmup.probe_once(
        ("https://gotify.example.com/health",),
        timeout_seconds=1.0,
        transport=httpx.MockTransport(handler),
    )

    assert records == [
        {
            "event": "warmup_probe",
            "ok": False,
            "url": "https://gotify.example.com/health",
            "error_type": "ConnectError",
            "error": "connection refused",
        }
    ]
