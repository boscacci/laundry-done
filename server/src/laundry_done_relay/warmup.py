from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Mapping, TextIO
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class WarmupConfig:
    urls: tuple[str, ...]
    host_headers: tuple[str | None, ...]
    interval_seconds: int = 300
    timeout_seconds: float = 10.0
    verify_tls: bool = True


def config_from_env(env: Mapping[str, str] | None = None) -> WarmupConfig:
    values = os.environ if env is None else env
    urls = tuple(item.strip() for item in values.get("WARMUP_URLS", "").split(",") if item.strip())
    if not urls:
        raise ValueError("WARMUP_URLS must contain at least one URL")
    host_headers = _host_headers_from_env(values.get("WARMUP_HOST_HEADERS", ""), len(urls))
    return WarmupConfig(
        urls=urls,
        host_headers=host_headers,
        interval_seconds=_positive_int(
            values.get("WARMUP_INTERVAL_SECONDS", "300"), "WARMUP_INTERVAL_SECONDS"
        ),
        timeout_seconds=_positive_float(
            values.get("WARMUP_TIMEOUT_SECONDS", "10"), "WARMUP_TIMEOUT_SECONDS"
        ),
        verify_tls=_bool_env(values.get("WARMUP_VERIFY_TLS", "true"), "WARMUP_VERIFY_TLS"),
    )


def probe_once(
    urls: tuple[str, ...],
    *,
    host_headers: tuple[str | None, ...] | None = None,
    timeout_seconds: float,
    verify_tls: bool = True,
    transport=None,
) -> list[dict]:
    import httpx

    headers_by_url = host_headers or tuple(None for _ in urls)
    if len(headers_by_url) != len(urls):
        raise ValueError("host_headers must match urls length")

    records = []
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout_seconds,
        verify=verify_tls,
        transport=transport,
    ) as client:
        for url, host_header in zip(urls, headers_by_url):
            safe_url = _redacted_url(url)
            start = time.monotonic()
            try:
                response = client.get(
                    url,
                    headers={"Host": host_header} if host_header else None,
                )
            except httpx.HTTPError as exc:
                record = {
                    "event": "warmup_probe",
                    "ok": False,
                    "url": safe_url,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                if host_header:
                    record["host_header"] = host_header
                records.append(record)
                continue
            record = {
                "event": "warmup_probe",
                "ok": 200 <= response.status_code < 400,
                "url": safe_url,
                "status_code": response.status_code,
                "elapsed_ms": round((time.monotonic() - start) * 1000, 1),
            }
            if host_header:
                record["host_header"] = host_header
            records.append(record)
    return records


def emit_records(records: list[dict], stream: TextIO = sys.stdout) -> None:
    for record in records:
        print(json.dumps(record, separators=(",", ":")), file=stream, flush=True)


def run_forever(config: WarmupConfig) -> None:
    emit_records(
        [
            {
                "event": "warmup_started",
                "urls": [_redacted_url(url) for url in config.urls],
                "host_headers": list(config.host_headers),
                "interval_seconds": config.interval_seconds,
                "timeout_seconds": config.timeout_seconds,
                "verify_tls": config.verify_tls,
            }
        ]
    )
    while True:
        emit_records(
            probe_once(
                config.urls,
                host_headers=config.host_headers,
                timeout_seconds=config.timeout_seconds,
                verify_tls=config.verify_tls,
            )
        )
        time.sleep(config.interval_seconds)


def main() -> None:
    run_forever(config_from_env())


def _positive_int(value: str, name: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if number <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return number


def _positive_float(value: str, name: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if number <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return number


def _bool_env(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _host_headers_from_env(value: str, url_count: int) -> tuple[str | None, ...]:
    if not value.strip():
        return tuple(None for _ in range(url_count))
    host_headers = tuple(item.strip() or None for item in value.split(","))
    if len(host_headers) != url_count:
        raise ValueError("WARMUP_HOST_HEADERS must match WARMUP_URLS length")
    return host_headers


def _redacted_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


if __name__ == "__main__":
    main()
