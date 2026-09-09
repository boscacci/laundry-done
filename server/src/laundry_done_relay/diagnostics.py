"""Read wireless device diagnostics without sending messages or modifying history."""

import argparse
import json
from pathlib import Path
import sqlite3


DIAGNOSTIC_FIELDS = (
    "version",
    "reset_reason",
    "detector_state",
    "active_continuous_power",
    "startup_keep_awake",
    "startup_settling",
    "light_sleep_enabled",
    "last_nap_requested_ms",
    "last_light_sleep_result",
    "keepalive_completed_count",
    "last_keepalive_completed_ms",
    "sample_post_successes",
    "sample_post_failures",
    "previous_sample_http_status",
    "free_heap_bytes",
    "min_free_heap_bytes",
    "ota_capable",
    "firmware_build",
    "ota_job_id",
    "ota_result",
    "installed_sha256",
)
RESET_REASONS = {
    0: "unknown",
    1: "power_on",
    2: "external",
    3: "software",
    4: "panic",
    5: "interrupt_watchdog",
    6: "task_watchdog",
    7: "watchdog",
    8: "deep_sleep",
    9: "brownout",
    10: "sdio",
}


def read_packets(path: Path, *, device_id: str, limit: int = 12) -> list[dict]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute(
            """SELECT raw_json, received_at FROM events
               WHERE state = 'calibration_sample' AND device_id = ?
               ORDER BY received_at DESC, rowid DESC LIMIT ?""",
            (device_id, limit),
        ).fetchall()
    packets = []
    for raw_json, received_at in reversed(rows):
        raw = json.loads(raw_json)
        extra = raw.get("diagnostics")
        extra = extra if isinstance(extra, dict) else {}
        diagnostics = {key: extra[key] for key in DIAGNOSTIC_FIELDS if key in extra}
        packet = {
            key: raw.get(key)
            for key in (
                "event_id",
                "cycle_id",
                "uptime_ms",
                "motion_rms_mg",
                "peak_mg",
                "wifi_rssi",
            )
        }
        packet.update(
            received_at=received_at,
            diagnostics=diagnostics,
            reset_reason_name=RESET_REASONS.get(
                diagnostics.get("reset_reason"), "unavailable"
            ),
        )
        packets.append(packet)
    return packets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument(
        "--limit", type=int, choices=range(1, 101), default=12, metavar="1..100"
    )
    args = parser.parse_args()
    try:
        for packet in read_packets(
            args.database, device_id=args.device_id, limit=args.limit
        ):
            print(json.dumps(packet), flush=True)
    except (sqlite3.Error, ValueError, OSError) as exc:
        parser.exit(1, f"Unable to read diagnostics ({type(exc).__name__}).\n")


if __name__ == "__main__":
    main()
