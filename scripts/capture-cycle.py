#!/usr/bin/env python
"""Read-only, bounded capture for owner-labeled full-cycle validation."""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--database", default="data/relay/events.sqlite3")
    p.add_argument("--device", default="laundry-stack-1")
    p.add_argument("--start", required=True)
    p.add_argument("--stop", required=True)
    p.add_argument("--appliance", choices=["washer", "dryer", "stack"], required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    stop = datetime.fromisoformat(args.stop.replace("Z", "+00:00"))
    if (
        not start.tzinfo
        or not stop.tzinfo
        or not 0 < (stop - start).total_seconds() <= 8 * 3600
    ):
        raise SystemExit("Specify an observed UTC/offset window of at most eight hours")
    output = Path(args.output)
    if output.exists():
        raise SystemExit("Refusing to overwrite a capture")
    db = sqlite3.connect(Path(args.database).resolve().as_uri() + "?mode=ro", uri=True)
    cursor = db.execute(
        "SELECT received_at,raw_json FROM events WHERE device_id=? AND state='calibration_sample' AND received_at>=? AND received_at<? ORDER BY received_at,event_id",
        (
            args.device,
            start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            stop.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as f:
        f.write(
            json.dumps(
                {
                    "kind": "owner_label",
                    "appliance": args.appliance,
                    "start": args.start,
                    "stop": args.stop,
                    "note": args.note,
                }
            )
            + "\n"
        )
        count = 0
        for at, raw in cursor:
            sample = json.loads(raw)
            # Explicit measurement allowlist excludes OTA credentials/URLs and PII.
            safe = {
                k: sample[k]
                for k in [
                    "event_id",
                    "cycle_id",
                    "uptime_ms",
                    "motion_rms_mg",
                    "peak_mg",
                    "sample_valid",
                    "sample_count",
                    "device_time_utc",
                ]
                if k in sample
            }
            safe["received_at"] = at
            f.write(json.dumps(safe) + "\n")
            count += 1
    print(json.dumps({"samples": count, "captured": bool(count)}))


if __name__ == "__main__":
    main()
