from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError


class LaundryEvent(BaseModel):
    device_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    state: str = Field(min_length=1)
    cycle_label: str = Field(pattern="^(washer|dryer|stack|unknown)$")
    motion_rms_mg: float = Field(ge=0, allow_inf_nan=False)
    last_motion_ms: int = Field(ge=0)
    firmware_version: str = Field(min_length=1)


class GotifyMessage(BaseModel):
    title: str
    message: str
    priority: int = 5


PushMessage = Callable[[dict], None]
EVENT_RETENTION_DAYS = 14
MAX_CALIBRATION_EVENTS = 200_000
NOTIFICATION_STATES = ("done_sent", "button_pressed", "motion_started")
SERVER_CLASSIFIER_SMOOTHING_SAMPLES = 8
SERVER_DONE_QUIET_SECONDS = 10 * 60
SERVER_MINIMUM_RUNTIME_SECONDS = 8 * 60


def create_app(
    *,
    database_path: str | Path,
    device_secret: str,
    gotify_url: str,
    gotify_app_token: str,
    monitor_tailscale_users: set[str] | None = None,
    monitor_tailscale_ips: set[str] | None = None,
    push_message: PushMessage | None = None,
) -> FastAPI:
    app = FastAPI(title="Laundry Done Relay")
    db_path = Path(database_path)
    sender = push_message or _gotify_sender(gotify_url, gotify_app_token)
    monitor_users = monitor_tailscale_users or set()
    monitor_ips = monitor_tailscale_ips or set()
    _init_db(db_path)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/monitor", response_class=HTMLResponse)
    def monitor() -> HTMLResponse:
        return HTMLResponse(_monitor_html(), headers={"Cache-Control": "no-store"})

    @app.head("/", include_in_schema=False)
    @app.get("/", include_in_schema=False)
    def monitor_root() -> RedirectResponse:
        return RedirectResponse("/monitor")

    @app.post("/api/v1/events", status_code=202)
    async def receive_event(
        request: Request,
        x_laundry_signature: str = Header(default=""),
    ) -> dict:
        body = await request.body()
        if not _valid_signature(device_secret, body, x_laundry_signature):
            raise HTTPException(status_code=401, detail="bad signature")

        try:
            event = LaundryEvent.model_validate_json(body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="invalid event payload") from exc
        duplicate = _store_event(db_path, event, body)
        if not duplicate and event.state in {"done_sent", "button_pressed", "motion_started"}:
            sender(_message_for(event).model_dump())
        if not duplicate and event.state == "calibration_sample":
            _maybe_send_server_done(db_path, event, sender)

        return {"accepted": True, "duplicate": duplicate}

    @app.get("/api/v1/calibration/events")
    def list_calibration_events(
        request: Request,
        limit: int = Query(default=MAX_CALIBRATION_EVENTS, ge=1, le=MAX_CALIBRATION_EVENTS),
        days: int = Query(default=EVENT_RETENTION_DAYS, ge=1, le=EVENT_RETENTION_DAYS),
        max_peak_mg: float | None = Query(default=None, gt=0),
        x_laundry_admin_secret: str = Header(default=""),
    ) -> dict:
        if not _has_admin_access(
            device_secret,
            x_laundry_admin_secret,
            request,
            monitor_tailscale_users=monitor_users,
            monitor_tailscale_ips=monitor_ips,
        ):
            raise HTTPException(status_code=401, detail="bad admin secret")
        return {
            "events": _list_calibration_events(db_path, limit, max_peak_mg, days),
            "notifications": _list_notification_events(db_path, days),
        }

    return app


def app_from_env() -> FastAPI:
    return create_app(
        database_path=os.environ.get("DATABASE_PATH", "/data/events.sqlite3"),
        device_secret=_required_env("DEVICE_SECRET"),
        gotify_url=_required_env("GOTIFY_URL"),
        gotify_app_token=_required_env("GOTIFY_APP_TOKEN"),
        monitor_tailscale_users=_csv_env("MONITOR_TAILSCALE_USERS"),
        monitor_tailscale_ips=_csv_env("MONITOR_TAILSCALE_IPS"),
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _csv_env(name: str) -> set[str]:
    value = os.environ.get(name, "")
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _has_admin_access(
    secret: str,
    received: str,
    request: Request,
    monitor_tailscale_users: set[str] | None = None,
    monitor_tailscale_ips: set[str] | None = None,
) -> bool:
    if hmac.compare_digest(secret, received):
        return True
    tailscale_login = request.headers.get("tailscale-user-login", "").strip().lower()
    allowed_users = monitor_tailscale_users or set()
    if tailscale_login and ("*" in allowed_users or tailscale_login in allowed_users):
        return True
    forwarded_for = _forwarded_client_ip(request)
    client = request.client
    if client is None:
        return False
    allowed_ips = monitor_tailscale_ips or set()
    if forwarded_for:
        return _client_ip_allowed(forwarded_for, allowed_ips)
    return client.host in {"127.0.0.1", "::1", "localhost"} or _client_ip_allowed(client.host, allowed_ips)


def _forwarded_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    return forwarded_for.split(",", 1)[0].strip()


def _client_ip_allowed(client_host: str, allowed_entries: set[str]) -> bool:
    if client_host in allowed_entries:
        return True
    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    for entry in allowed_entries:
        try:
            if "/" in entry and client_ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def _init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
              event_id TEXT PRIMARY KEY,
              device_id TEXT NOT NULL,
              cycle_id TEXT NOT NULL,
              state TEXT NOT NULL,
              cycle_label TEXT NOT NULL,
              motion_rms_mg REAL NOT NULL,
              last_motion_ms INTEGER NOT NULL,
              firmware_version TEXT NOT NULL,
              raw_json TEXT NOT NULL,
              received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_events_state_received_at
            ON events (state, received_at DESC, event_id DESC)
            """
        )


def _store_event(path: Path, event: LaundryEvent, raw_body: bytes) -> bool:
    with sqlite3.connect(path) as conn:
        _prune_expired_events(conn)
        try:
            conn.execute(
                """
                INSERT INTO events (
                  event_id, device_id, cycle_id, state, cycle_label,
                  motion_rms_mg, last_motion_ms, firmware_version, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.device_id,
                    event.cycle_id,
                    event.state,
                    event.cycle_label,
                    event.motion_rms_mg,
                    event.last_motion_ms,
                    event.firmware_version,
                    raw_body.decode("utf-8"),
                ),
            )
            return False
        except sqlite3.IntegrityError:
            return True


def _list_calibration_events(
    path: Path,
    limit: int,
    max_peak_mg: float | None = None,
    days: int = EVENT_RETENTION_DAYS,
) -> list[dict]:
    with sqlite3.connect(path) as conn:
        _prune_expired_events(conn)
        conn.row_factory = sqlite3.Row
        events: list[dict] = []
        offset = 0
        page_size = min(5000, limit)
        while len(events) < limit:
            rows = conn.execute(
                """
                SELECT event_id, device_id, cycle_id, state, cycle_label,
                       motion_rms_mg, last_motion_ms, firmware_version,
                       raw_json, received_at
                FROM events
                WHERE state = 'calibration_sample'
                  AND received_at >= datetime('now', ?)
                ORDER BY received_at DESC, event_id DESC
                LIMIT ? OFFSET ?
                """,
                (f"-{days} days", page_size, offset),
            ).fetchall()
            if not rows:
                break
            offset += len(rows)
            for row in rows:
                raw = json.loads(row["raw_json"])
                if not _within_peak_limit(raw, max_peak_mg):
                    continue
                raw["received_at"] = row["received_at"]
                events.append(raw)
                if len(events) >= limit:
                    break
            if len(rows) < page_size:
                break
    return _annotate_server_phases(events)


def _maybe_send_server_done(path: Path, event: LaundryEvent, sender: PushMessage) -> None:
    samples = _calibration_events_for_device(path, event.device_id)
    if len(samples) < SERVER_CLASSIFIER_SMOOTHING_SAMPLES:
        return
    phases = _server_base_phases(samples)
    latest_sample = samples[-1]
    latest_phase = phases[-1]
    if latest_phase["short"] != "quiet":
        return

    latest_quiet_at = _event_timestamp(latest_sample)
    if latest_quiet_at is None:
        return
    last_notification_received_at = _latest_notification_received_at(path, event.device_id)
    active_indexes = []
    for index, phase_info in enumerate(phases):
        if phase_info["short"] not in {"washer", "dryer", "strong", "gentle"}:
            continue
        if last_notification_received_at is not None:
            sample_received_at = _received_timestamp(samples[index])
            if sample_received_at is None or sample_received_at <= last_notification_received_at:
                continue
        active_indexes.append(index)
    if not active_indexes:
        return
    first_active_at = _event_timestamp(samples[active_indexes[0]])
    last_active_at = _event_timestamp(samples[active_indexes[-1]])
    if first_active_at is None or last_active_at is None:
        return
    if (latest_quiet_at - last_active_at).total_seconds() < SERVER_DONE_QUIET_SECONDS:
        return
    if (latest_quiet_at - first_active_at).total_seconds() < SERVER_MINIMUM_RUNTIME_SECONDS:
        return
    if _has_notification_after(path, event.device_id, last_active_at):
        return

    label = _server_done_label(phases[active_indexes[0] :])
    notification = LaundryEvent(
        device_id=event.device_id,
        event_id=f"server-done-{event.device_id}-{latest_sample['event_id']}",
        cycle_id=latest_sample.get("cycle_id", event.cycle_id),
        state="done_sent",
        cycle_label=label,
        motion_rms_mg=float(latest_sample.get("motion_rms_mg", 0)),
        last_motion_ms=int((latest_quiet_at - last_active_at).total_seconds() * 1000),
        firmware_version="server-classifier",
    )
    raw_body = notification.model_dump_json().encode("utf-8")
    if not _store_event(path, notification, raw_body):
        sender(_message_for(notification).model_dump())


def _calibration_events_for_device(path: Path, device_id: str) -> list[dict]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT raw_json, received_at
            FROM events
            WHERE state = 'calibration_sample'
              AND device_id = ?
              AND received_at >= datetime('now', ?)
            ORDER BY received_at ASC, event_id ASC
            """,
            (device_id, f"-{EVENT_RETENTION_DAYS} days"),
        ).fetchall()
    events = []
    for row in rows:
        raw = json.loads(row["raw_json"])
        raw["received_at"] = row["received_at"]
        events.append(raw)
    return sorted(events, key=lambda item: _event_timestamp(item) or datetime.min.replace(tzinfo=timezone.utc))


def _has_notification_after(path: Path, device_id: str, threshold: datetime) -> bool:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT raw_json, received_at
            FROM events
            WHERE state = 'done_sent'
              AND device_id = ?
              AND received_at >= datetime('now', ?)
            """,
            (device_id, f"-{EVENT_RETENTION_DAYS} days"),
        ).fetchall()
    for row in rows:
        raw = json.loads(row["raw_json"])
        raw["received_at"] = row["received_at"]
        timestamp = _event_timestamp(raw)
        if timestamp is not None and timestamp >= threshold:
            return True
    return False


def _latest_notification_received_at(path: Path, device_id: str) -> datetime | None:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT raw_json, received_at
            FROM events
            WHERE state = 'done_sent'
              AND device_id = ?
              AND received_at >= datetime('now', ?)
            ORDER BY received_at DESC, event_id DESC
            LIMIT 1
            """,
            (device_id, f"-{EVENT_RETENTION_DAYS} days"),
        ).fetchone()
    if row is None:
        return None
    return _received_timestamp({"received_at": row["received_at"]})


def _server_done_label(phases: list[dict]) -> str:
    active_shorts = [
        phase["short"]
        for phase in phases
        if phase["short"] in {"washer", "dryer", "strong", "gentle"}
    ]
    dryer_count = active_shorts.count("dryer")
    washer_like_count = active_shorts.count("washer") + active_shorts.count("strong")
    if dryer_count > 0 and dryer_count >= washer_like_count:
        return "dryer"
    if washer_like_count > 0:
        return "washer"
    if dryer_count > 0:
        return "dryer"
    return "stack"


def _annotate_server_phases(events: list[dict]) -> list[dict]:
    if not events:
        return events
    chronological = list(reversed(events))
    phases = _server_phases(chronological)
    annotated = []
    for event, phase_info in zip(chronological, phases):
        copy = dict(event)
        copy["server_phase"] = phase_info["short"]
        copy["server_phase_label"] = phase_info["label"]
        copy["server_phase_detail"] = phase_info["detail"]
        copy["server_phase_color"] = phase_info["color"]
        annotated.append(copy)
    return list(reversed(annotated))


def _server_phases(events: list[dict]) -> list[dict]:
    recent_washer_value = None
    recent_washer_age = 10**9
    phases = []
    for base in _server_base_phases(events):
        if base["short"] == "handling":
            phases.append(base)
            continue
        if base["short"] in {"washer", "strong"}:
            recent_washer_value = (
                _phase(
                    "washer",
                    "Washer running",
                    "Strong washer-like agitation or spin value pattern.",
                    "rgba(218, 157, 44, 0.18)",
                )
                if base["short"] == "strong"
                else base
            )
            recent_washer_age = 0
            phases.append(base)
            continue
        if base["short"] == "dryer":
            recent_washer_value = None
            recent_washer_age = 10**9
            phases.append(base)
            continue
        if recent_washer_value and recent_washer_age < 6 and base["short"] in {
            "quiet",
            "gentle",
            "settling",
        }:
            recent_washer_age += 1
            phases.append(recent_washer_value)
            continue
        recent_washer_age += 1
        phases.append(base)
    return phases


def _server_base_phases(events: list[dict]) -> list[dict]:
    return [
        _classify_server_event(event)
        for event in _smoothed_events(events, SERVER_CLASSIFIER_SMOOTHING_SAMPLES)
    ]


def _smoothed_events(events: list[dict], window_size: int) -> list[dict]:
    smoothed = []
    for index, event in enumerate(events):
        start = max(0, index - window_size + 1)
        chunk = events[start : index + 1]
        copy = dict(event)
        copy["motion_rms_mg"] = sum(_numeric(item.get("motion_rms_mg")) for item in chunk) / len(
            chunk
        )
        copy["peak_mg"] = sum(_numeric(item.get("peak_mg")) for item in chunk) / len(chunk)
        smoothed.append(copy)
    return smoothed


def _classify_server_event(event: dict) -> dict:
    rms = _numeric(event.get("motion_rms_mg"))
    peak = _numeric(event.get("peak_mg"))
    if peak > 300 or rms > 120:
        return _phase(
            "handling",
            "Handling spike",
            "Likely bumping the sensor, not useful machine rhythm.",
            "rgba(179, 38, 30, 0.18)",
        )
    if rms <= 5 and peak <= 8:
        return _phase(
            "quiet",
            "Quiet / off",
            "Looks like the machine is stopped or only filling quietly.",
            "rgba(95, 128, 95, 0.18)",
        )
    if peak >= 120 or rms >= 60:
        return _phase(
            "strong",
            "Strong wash/spin",
            "Big machine movement, usually washer agitation or spin.",
            "rgba(190, 80, 52, 0.18)",
        )
    washer_candidate = (2.3 <= rms <= 8 and 8 <= peak <= 35) or rms >= 28 or peak >= 65
    dryer_candidate = 12 <= rms <= 28 and 25 <= peak <= 65
    if washer_candidate and not dryer_candidate:
        return _phase(
            "washer",
            "Washer running",
            "Washer-like value pattern: low shake with jolts, or stronger agitation/spin.",
            "rgba(218, 157, 44, 0.18)",
        )
    if dryer_candidate:
        return _phase(
            "dryer",
            "Dryer running",
            "Sustained mid-level tumble pattern from the accelerometer values.",
            "rgba(66, 133, 244, 0.16)",
        )
    if rms >= 3 or peak >= 8:
        return _phase(
            "gentle",
            "Gentle motion",
            "Small but real movement above the quiet baseline.",
            "rgba(66, 133, 244, 0.14)",
        )
    return _phase(
        "settling",
        "Mostly quiet",
        "Near the quiet baseline with tiny movement.",
        "rgba(95, 128, 95, 0.14)",
    )


def _phase(short: str, label: str, detail: str, color: str) -> dict:
    return {"short": short, "label": label, "detail": detail, "color": color}


def _numeric(value, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number


def _event_timestamp(event: dict) -> datetime | None:
    value = event.get("device_time_utc") or event.get("received_at")
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    elif "T" not in text:
        text = text.replace(" ", "T") + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _received_timestamp(event: dict) -> datetime | None:
    received_at = event.get("received_at")
    if not received_at:
        return None
    return _event_timestamp({"received_at": received_at})


def _list_notification_events(path: Path, days: int = EVENT_RETENTION_DAYS) -> list[dict]:
    with sqlite3.connect(path) as conn:
        _prune_expired_events(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT event_id, device_id, cycle_id, state, cycle_label,
                   motion_rms_mg, last_motion_ms, firmware_version, received_at
            FROM events
            WHERE state IN (?, ?, ?)
              AND received_at >= datetime('now', ?)
            ORDER BY received_at DESC, event_id DESC
            """,
            (*NOTIFICATION_STATES, f"-{days} days"),
        ).fetchall()
    notifications: list[dict] = []
    for row in rows:
        event = LaundryEvent(
            device_id=row["device_id"],
            event_id=row["event_id"],
            cycle_id=row["cycle_id"],
            state=row["state"],
            cycle_label=row["cycle_label"],
            motion_rms_mg=row["motion_rms_mg"],
            last_motion_ms=row["last_motion_ms"],
            firmware_version=row["firmware_version"],
        )
        message = _message_for(event)
        notifications.append(
            {
                "event_id": row["event_id"],
                "device_id": row["device_id"],
                "cycle_id": row["cycle_id"],
                "state": row["state"],
                "cycle_label": row["cycle_label"],
                "title": message.title,
                "message": message.message,
                "received_at": row["received_at"],
            }
        )
    return notifications


def _within_peak_limit(raw: dict, max_peak_mg: float | None) -> bool:
    if max_peak_mg is None:
        return True
    try:
        peak_mg = float(raw.get("peak_mg", 0))
    except (TypeError, ValueError):
        return False
    return peak_mg <= max_peak_mg


def _prune_expired_events(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM events WHERE received_at < datetime('now', ?)",
        (f"-{EVENT_RETENTION_DAYS} days",),
    )


def _valid_signature(secret: str, body: bytes, received: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def _monitor_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Laundry Activity Monitor</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #202124;
      --muted: #5f6368;
      --line: #d9dde3;
      --panel: #ffffff;
      --wash: #137a7f;
      --peak: #b85c18;
      --warn: #b3261e;
      --bg: #f5f7f8;
      --quiet: #dce8df;
      --gentle: #e5eef8;
      --active: #fff1cf;
      --strong: #f7ded7;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 22px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 {
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      font-weight: 700;
    }
    main {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 300px;
      gap: 16px;
      padding: 16px;
      max-width: 1280px;
      margin: 0 auto;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    .toolbar {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto auto auto;
      gap: 8px;
      align-items: center;
      padding: 12px;
      border-bottom: 1px solid var(--line);
    }
    .sync-strip {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    input, button, select {
      height: 36px;
      border-radius: 6px;
      border: 1px solid var(--line);
      font: inherit;
    }
    input {
      min-width: 260px;
      padding: 0 10px;
    }
    button {
      padding: 0 12px;
      background: #202124;
      color: #fff;
      cursor: pointer;
    }
    button.secondary {
      background: #fff;
      color: var(--ink);
    }
    .controls {
      display: grid;
      grid-template-columns: repeat(3, minmax(160px, 1fr));
      gap: 12px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfd;
    }
    .control {
      display: grid;
      gap: 6px;
      min-width: 0;
    }
    .control label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .control select,
    .control input[type="range"] {
      width: 100%;
      min-width: 0;
    }
    .range-row {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .range-row input { flex: 1; }
    .range-value {
      width: 68px;
      color: var(--muted);
      font-size: 13px;
      text-align: right;
      white-space: nowrap;
    }
    .status {
      margin-left: auto;
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }
    .chart-wrap {
      position: relative;
      height: min(58vh, 560px);
      min-height: 330px;
      padding: 12px;
    }
    canvas {
      display: block;
      width: 100%;
      height: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfcfd;
    }
    .legend {
      display: flex;
      gap: 14px;
      align-items: center;
      padding: 0 12px 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .key { display: inline-flex; align-items: center; gap: 6px; }
    .swatch { width: 18px; height: 3px; border-radius: 99px; background: var(--wash); }
    .swatch.peak { background: var(--peak); }
    .swatch.phase { height: 10px; border: 1px solid rgba(32, 33, 36, 0.18); }
    .swatch.phase.quiet { background: var(--quiet); }
    .swatch.phase.washer { background: var(--active); }
    .swatch.phase.dryer { background: var(--gentle); }
    .swatch.phase.strong { background: var(--strong); }
    .swatch.notification { width: 3px; height: 16px; background: var(--warn); }
    .explainer {
      padding: 12px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      background: #fff;
    }
    .tooltip {
      position: absolute;
      z-index: 2;
      display: none;
      min-width: 210px;
      padding: 10px;
      border: 1px solid #bfc5cd;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.96);
      box-shadow: 0 12px 28px rgba(32, 33, 36, 0.18);
      color: var(--ink);
      font-size: 13px;
      pointer-events: none;
    }
    .tooltip strong {
      display: block;
      margin-bottom: 6px;
      font-size: 13px;
    }
    .tooltip-row {
      display: flex;
      justify-content: space-between;
      gap: 18px;
      margin-top: 3px;
    }
    aside {
      display: grid;
      gap: 12px;
      align-content: start;
    }
    .metric {
      padding: 14px;
      border-bottom: 1px solid var(--line);
    }
    .metric:last-child { border-bottom: 0; }
    .label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      font-weight: 700;
    }
    .value {
      margin-top: 4px;
      font-size: 28px;
      font-weight: 750;
      line-height: 1.1;
    }
    .small {
      margin-top: 4px;
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .phase-pill {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 4px 9px;
      border-radius: 999px;
      background: var(--gentle);
      font-size: 13px;
      font-weight: 700;
    }
    .connection-card {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      padding: 14px;
      border-bottom: 1px solid var(--line);
    }
    .connection-node {
      display: grid;
      justify-items: center;
      gap: 7px;
      min-height: 92px;
      padding: 10px 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--muted);
      text-align: center;
    }
    .connection-node svg {
      width: 34px;
      height: 34px;
      stroke-width: 1.9;
    }
    .connection-node .node-title {
      color: var(--ink);
      font-size: 13px;
      font-weight: 750;
    }
    .connection-node .node-state {
      font-size: 12px;
      font-weight: 700;
    }
    .ping-row {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      min-height: 18px;
      color: inherit;
      font-size: 12px;
      font-weight: 700;
    }
    .ping-countdown {
      --ping-sweep: 0%;
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: conic-gradient(currentColor var(--ping-sweep), rgba(32, 33, 36, 0.14) 0);
      box-shadow: inset 0 0 0 1px rgba(32, 33, 36, 0.16);
      flex: 0 0 auto;
      position: relative;
    }
    .ping-countdown::after {
      content: "";
      position: absolute;
      inset: 5px;
      border-radius: 50%;
      background: currentColor;
      opacity: 0.16;
    }
    .connection-online {
      border-color: rgba(19, 122, 127, 0.35);
      background: #effaf9;
      color: #0b6f55;
    }
    .connection-online.wake {
      border-color: rgba(66, 133, 244, 0.35);
      background: #eef5ff;
      color: #1a5fbe;
    }
    .connection-napping {
      border-color: rgba(218, 157, 44, 0.35);
      background: #fff8e4;
      color: #8a5b00;
    }
    .connection-offline {
      border-color: rgba(179, 38, 30, 0.36);
      background: #fff7f6;
      color: var(--warn);
    }
    .connection-offline svg .slash {
      display: block;
    }
    svg .slash {
      display: none;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      padding: 8px 10px;
      text-align: left;
      border-top: 1px solid var(--line);
      white-space: nowrap;
    }
    th { color: var(--muted); font-size: 12px; }
    @media (max-width: 820px) {
      header { align-items: flex-start; flex-direction: column; }
      main { grid-template-columns: 1fr; padding: 10px; }
      h1 { font-size: 18px; }
      input { min-width: 0; width: 100%; }
      .toolbar {
        grid-template-columns: 1fr 1fr;
        padding: 10px;
      }
      .sync-strip { padding: 9px 10px; }
      .controls {
        grid-template-columns: 1fr 1fr;
        gap: 10px;
        padding: 10px;
      }
      .smoothing-control { grid-column: 1 / -1; }
      .explainer { padding: 10px; font-size: 12px; }
      .chart-wrap { height: min(58vh, 390px); min-height: 340px; padding: 8px; }
      .legend {
        align-items: flex-start;
        flex-wrap: wrap;
        gap: 8px 12px;
        padding: 0 10px 12px;
      }
      .key { flex: 1 1 128px; }
      .connection-card { padding: 10px; gap: 8px; }
      .connection-node { min-height: 88px; padding: 10px 6px; }
      .metric { padding: 12px; }
      th, td { padding: 7px 8px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Laundry Activity Monitor</h1>
    <div class="small" id="clock">Waiting for samples</div>
  </header>
  <main>
    <section class="panel">
      <div class="sync-strip">
        <span class="status" id="status">Connecting through Tailscale...</span>
        <button class="secondary" id="pause">Pause</button>
      </div>
      <div class="controls">
        <div class="control">
          <label for="zoom">Time range</label>
          <select id="zoom">
            <option value="900000">Live 15 min</option>
            <option value="1920000" selected>Last 32 min</option>
            <option value="3600000">Last 1 hour</option>
            <option value="21600000">Last 6 hours</option>
            <option value="86400000">Last 24 hours</option>
            <option value="604800000">Last 7 days</option>
            <option value="1209600000">Last 14 days</option>
            <option value="all">All loaded</option>
          </select>
        </div>
        <div class="control smoothing-control">
          <label for="scale-mode">Scale</label>
          <select id="scale-mode">
            <option value="auto" selected>Auto</option>
            <option value="detail">Detail 0-120 mg</option>
            <option value="mid">Mid 0-500 mg</option>
            <option value="full">Full 0-5000 mg</option>
          </select>
        </div>
        <div class="control">
          <label for="smooth">Average smoothing</label>
          <div class="range-row">
            <input id="smooth" type="range" min="1" max="12" value="4" step="1">
            <span class="range-value" id="smooth-value">4 samples</span>
          </div>
          <div class="small">Up to about 2 min on a 10-second cadence.</div>
        </div>
      </div>
      <div class="explainer">
        <b>Plain English:</b> mg = 1/1000 g (milli-g). RMS = typical shake;
        peak = biggest jolt. Phase backgrounds are best guesses over time.
      </div>
      <div class="chart-wrap">
        <canvas id="chart"></canvas>
        <div class="tooltip" id="tooltip"></div>
      </div>
      <div class="legend">
        <span class="key"><span class="swatch"></span>Vibration strength</span>
        <span class="key"><span class="swatch peak"></span>Biggest jolt</span>
        <span class="key">
          <span class="swatch phase quiet"></span>
          <span class="swatch phase washer"></span>
          <span class="swatch phase dryer"></span>
          <span class="swatch phase strong"></span>
          Phase backgrounds
        </span>
        <span class="key"><span class="swatch notification"></span>Notification moments</span>
        <span class="key">Filtering jolts &gt; 300 mg (0.3 g handling bumps)</span>
      </div>
    </section>
    <aside>
      <section class="panel">
        <div class="connection-card">
          <div class="connection-node connection-offline" id="link-node">
            <svg viewBox="0 0 48 48" aria-hidden="true" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round">
              <rect x="11" y="14" width="26" height="21" rx="4"></rect>
              <path d="M17 10v4M24 10v4M31 10v4M17 35v4M24 35v4M31 35v4M7 20h4M7 29h4M37 20h4M37 29h4"></path>
              <circle cx="19" cy="24" r="2"></circle>
              <path d="M25 23c2-2 5-2 7 0"></path>
              <path d="M27 27c1-1 3-1 4 0"></path>
              <path class="slash" d="M8 40 40 8"></path>
            </svg>
            <div class="node-title">Arduino Wi-Fi</div>
            <div class="node-state" id="link-state">Disconnected</div>
            <div class="ping-row">
              <span class="ping-countdown" id="ping-countdown" aria-hidden="true"></span>
              <span id="ping-eta">No sync</span>
            </div>
          </div>
          <div class="connection-node connection-offline wake" id="wake-node">
            <svg viewBox="0 0 48 48" aria-hidden="true" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="24" cy="24" r="14"></circle>
              <path d="M24 13v11l7 4"></path>
              <path d="M12 7 7 12M36 7l5 5"></path>
              <path class="slash" d="M8 40 40 8"></path>
            </svg>
            <div class="node-title">Wake state</div>
            <div class="node-state" id="wake-state">Unknown</div>
          </div>
        </div>
        <div class="metric">
          <div class="label">Vibration strength</div>
          <div class="value" id="rms">--</div>
          <div class="small">RMS mg: typical shake over 4 seconds</div>
        </div>
        <div class="metric">
          <div class="label">Biggest jolt</div>
          <div class="value" id="peak">--</div>
          <div class="small">Peak mg: largest instant change</div>
        </div>
        <div class="metric">
          <div class="label">Best guess</div>
          <div class="phase-pill" id="guess">Waiting</div>
          <div class="small" id="guess-detail">Needs samples</div>
        </div>
        <div class="metric">
          <div class="label">Samples</div>
          <div class="value" id="count">0</div>
          <div class="small" id="run">No run yet</div>
        </div>
        <div class="metric">
          <div class="label">Last check-in</div>
          <div class="value" id="last-age">--</div>
          <div class="small" id="last">No latest sample</div>
          <div class="small" id="rssi">Radio detail hidden</div>
        </div>
      </section>
      <section class="panel">
        <table>
          <thead><tr><th>Time</th><th>Shake</th><th>Jolt</th><th>Guess</th></tr></thead>
          <tbody id="rows"></tbody>
        </table>
      </section>
    </aside>
  </main>
  <script>
    const chart = document.getElementById('chart');
    const ctx = chart.getContext('2d');
    const state = { samples: [], notifications: [], timer: null, paused: false, hover: null };
    const els = {
      pause: document.getElementById('pause'),
      status: document.getElementById('status'),
      zoom: document.getElementById('zoom'),
      scaleMode: document.getElementById('scale-mode'),
      smooth: document.getElementById('smooth'),
      smoothValue: document.getElementById('smooth-value'),
      rms: document.getElementById('rms'),
      peak: document.getElementById('peak'),
      guess: document.getElementById('guess'),
      guessDetail: document.getElementById('guess-detail'),
      linkNode: document.getElementById('link-node'),
      linkState: document.getElementById('link-state'),
      pingCountdown: document.getElementById('ping-countdown'),
      pingEta: document.getElementById('ping-eta'),
      wakeNode: document.getElementById('wake-node'),
      wakeState: document.getElementById('wake-state'),
      count: document.getElementById('count'),
      run: document.getElementById('run'),
      lastAge: document.getElementById('last-age'),
      rssi: document.getElementById('rssi'),
      last: document.getElementById('last'),
      rows: document.getElementById('rows'),
      clock: document.getElementById('clock'),
      tooltip: document.getElementById('tooltip'),
    };

    function resizeCanvas() {
      const rect = chart.getBoundingClientRect();
      const scale = window.devicePixelRatio || 1;
      chart.width = Math.max(1, Math.floor(rect.width * scale));
      chart.height = Math.max(1, Math.floor(rect.height * scale));
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
      draw();
    }

    function formatTime(value) {
      if (!value) return '--';
      const text = String(value);
      const date = text.includes('T') ? new Date(text) : new Date(text.replace(' ', 'T') + 'Z');
      if (Number.isNaN(date.getTime())) return '--';
      return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
    }

    function formatAxisTime(timestampMs, rangeMs = 0) {
      const date = new Date(timestampMs);
      if (Number.isNaN(date.getTime())) return '';
      if (rangeMs >= 2 * 24 * 60 * 60 * 1000) {
        return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
      }
      if (rangeMs >= 12 * 60 * 60 * 1000) {
        return date.toLocaleString([], { weekday: 'short', hour: 'numeric' });
      }
      return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }

    function sampleTime(sample) {
      return sample?.device_time_utc || sample?.received_at || null;
    }

    function sampleTimestamp(sample) {
      const time = sampleTime(sample);
      if (!time) return null;
      const text = String(time);
      const date = text.includes('T') ? new Date(text) : new Date(text.replace(' ', 'T') + 'Z');
      const timestamp = date.getTime();
      return Number.isFinite(timestamp) ? timestamp : null;
    }

    function notificationTimestamp(notification) {
      const time = notification?.received_at || null;
      if (!time) return null;
      const text = String(time);
      const date = text.includes('T') ? new Date(text) : new Date(text.replace(' ', 'T') + 'Z');
      const timestamp = date.getTime();
      return Number.isFinite(timestamp) ? timestamp : null;
    }

    function latestActivityTimestamp() {
      const timestamps = [
        ...state.samples.map(sampleTimestamp),
        ...state.notifications.map(notificationTimestamp),
      ].filter(timestamp => Number.isFinite(timestamp));
      return timestamps.length ? Math.max(...timestamps) : null;
    }

    function sampleAgeSeconds(sample) {
      if (!sample) return Infinity;
      const time = sampleTime(sample);
      if (!time) return Infinity;
      const sampleDate = time.includes('T')
        ? new Date(time)
        : new Date(time.replace(' ', 'T') + 'Z');
      if (Number.isNaN(sampleDate.getTime())) return Infinity;
      return Math.max(0, Math.round((Date.now() - sampleDate.getTime()) / 1000));
    }

    function estimateSampleCadenceMs(samples) {
      const timestamps = samples
        .map(sampleTimestamp)
        .filter(timestamp => Number.isFinite(timestamp));
      if (timestamps.length < 2) return 12000;
      const recent = timestamps.slice(-10);
      const deltas = [];
      for (let index = 1; index < recent.length; index++) {
        const delta = recent[index] - recent[index - 1];
        if (delta >= 5000 && delta <= 90000) deltas.push(delta);
      }
      if (deltas.length === 0) return 12000;
      deltas.sort((a, b) => a - b);
      return deltas[Math.floor(deltas.length / 2)];
    }

    function updatePingCountdown() {
      const latest = state.samples[state.samples.length - 1];
      const latestTimestamp = sampleTimestamp(latest);
      const cadenceMs = estimateSampleCadenceMs(state.samples);
      const ageMs = Number.isFinite(latestTimestamp) ? Math.max(0, Date.now() - latestTimestamp) : Infinity;
      const synced = Boolean(latest) && Number.isFinite(ageMs) && ageMs <= Math.max(75000, cadenceMs * 3);
      if (!synced) {
        els.pingCountdown.style.setProperty('--ping-sweep', '0%');
        els.pingEta.textContent = 'No sync';
        return;
      }
      const remainingMs = Math.max(0, cadenceMs - ageMs);
      const sweep = Math.max(0, Math.min(100, (remainingMs / cadenceMs) * 100));
      els.pingCountdown.style.setProperty('--ping-sweep', `${sweep}%`);
      els.pingEta.textContent = remainingMs > 0 ? `Next ~${Math.ceil(remainingMs / 1000)}s` : 'Ping due';
    }

    function numeric(value, fallback = 0) {
      const number = Number(value);
      return Number.isFinite(number) ? number : fallback;
    }

    function formatNumber(value, digits = 1) {
      const number = Number(value);
      return Number.isFinite(number) ? number.toFixed(digits) : '--';
    }

    function formatWifiSignal(value) {
      const number = Number(value);
      return Number.isFinite(number) ? `${number} dBm` : '--';
    }

    function escapeHtml(value) {
      const replacements = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      };
      return String(value ?? '--').replace(/[&<>"']/g, character => replacements[character]);
    }

    function formatAge(seconds) {
      if (!Number.isFinite(seconds)) return '--';
      if (seconds < 90) return `${seconds}s`;
      const minutes = Math.round(seconds / 60);
      if (minutes < 60) return `${minutes} min`;
      const hours = Math.floor(minutes / 60);
      const remainingMinutes = minutes % 60;
      if (hours < 24) {
        return remainingMinutes > 0 ? `${hours} hr ${remainingMinutes} min` : `${hours} hr`;
      }
      const days = Math.floor(hours / 24);
      const remainingHours = hours % 24;
      const dayText = days === 1 ? '1 day' : `${days} days`;
      return remainingHours > 0 ? `${dayText} ${remainingHours} hr` : dayText;
    }

    function setConnectionNode(node, stateEl, online, onlineText) {
      node.classList.toggle('connection-online', online);
      node.classList.toggle('connection-offline', !online);
      stateEl.textContent = online ? onlineText : 'Disconnected';
    }

    function updateConnectionIndicators(latest) {
      const ageSeconds = sampleAgeSeconds(latest);
      const linkOnline = Boolean(latest) && ageSeconds <= 75 && Number.isFinite(Number(latest.wifi_rssi));
      setConnectionNode(els.linkNode, els.linkState, linkOnline, 'Connected');
      els.wakeNode.classList.remove('connection-online', 'connection-offline', 'connection-napping');
      if (!latest || ageSeconds > 75) {
        els.wakeNode.classList.add('connection-offline');
        els.wakeState.textContent = 'Asleep / off';
      } else if (ageSeconds <= 15) {
        els.wakeNode.classList.add('connection-online');
        els.wakeState.textContent = 'Awake now';
      } else {
        els.wakeNode.classList.add('connection-napping');
        els.wakeState.textContent = 'Between samples';
      }
      els.lastAge.textContent = latest ? formatAge(ageSeconds) : '--';
      els.rssi.textContent = linkOnline ? 'Recent radio check-in' : 'No recent radio check-in';
      updatePingCountdown();
    }

    function plotGeometry() {
      const width = chart.clientWidth;
      const height = chart.clientHeight;
      const pad = { left: 48, right: 16, top: 18, bottom: 44 };
      return {
        width,
        height,
        pad,
        plotW: width - pad.left - pad.right,
        plotH: height - pad.top - pad.bottom,
      };
    }

    function timeDomain(samples) {
      const timestamps = [
        ...samples.map(sampleTimestamp),
        ...visibleNotifications().map(notificationTimestamp),
      ];
      const valid = timestamps.filter(timestamp => Number.isFinite(timestamp)).sort((a, b) => a - b);
      if (valid.length >= 2) {
        const start = valid[0];
        const end = valid[valid.length - 1];
        if (end > start) return { start, end, useTime: true };
      }
      return { start: 0, end: Math.max(1, samples.length - 1), useTime: false };
    }

    function xForSample(index, samples, domain, pad, plotW) {
      if (domain.useTime) {
        const timestamp = sampleTimestamp(samples[index]);
        if (Number.isFinite(timestamp)) {
          const ratio = (timestamp - domain.start) / (domain.end - domain.start);
          return pad.left + plotW * Math.max(0, Math.min(1, ratio));
        }
      }
      return pad.left + plotW * (index / Math.max(1, samples.length - 1));
    }

    function niceTimeTickMs(rangeMs, plotW) {
      const targetTicks = Math.max(2, Math.floor(plotW / 110));
      const idealMs = rangeMs / targetTicks;
      const intervals = [
        30 * 1000,
        60 * 1000,
        2 * 60 * 1000,
        5 * 60 * 1000,
        10 * 60 * 1000,
        15 * 60 * 1000,
        30 * 60 * 1000,
        60 * 60 * 1000,
        2 * 60 * 60 * 1000,
        4 * 60 * 60 * 1000,
        6 * 60 * 60 * 1000,
        12 * 60 * 60 * 1000,
        24 * 60 * 60 * 1000,
        2 * 24 * 60 * 60 * 1000,
        7 * 24 * 60 * 60 * 1000,
      ];
      return intervals.find(interval => interval >= idealMs) || intervals[intervals.length - 1];
    }

    function drawTimeAxis(domain, pad, plotW, plotH) {
      if (!domain.useTime) return;
      const axisY = pad.top + plotH;
      const rangeMs = domain.end - domain.start;
      const stepMs = niceTimeTickMs(rangeMs, plotW);
      const firstTick = Math.ceil(domain.start / stepMs) * stepMs;
      ctx.save();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.font = '12px system-ui, sans-serif';
      ctx.strokeStyle = 'rgba(32, 33, 36, 0.16)';
      ctx.fillStyle = '#5f6368';
      ctx.beginPath();
      ctx.moveTo(pad.left, axisY);
      ctx.lineTo(pad.left + plotW, axisY);
      ctx.stroke();
      let lastLabelX = -Infinity;
      for (let tick = firstTick; tick <= domain.end + 1; tick += stepMs) {
        const x = pad.left + plotW * ((tick - domain.start) / rangeMs);
        ctx.strokeStyle = 'rgba(32, 33, 36, 0.10)';
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, axisY + 5);
        ctx.stroke();
        if (x - lastLabelX >= 58) {
          ctx.fillText(formatAxisTime(tick, rangeMs), x, axisY + 10);
          lastLabelX = x;
        }
      }
      ctx.restore();
    }

    function nearestSampleIndexForX(samples, domain, x, pad, plotW) {
      if (!domain.useTime) {
        return Math.round(((x - pad.left) / plotW) * (samples.length - 1));
      }
      const target = domain.start + ((x - pad.left) / plotW) * (domain.end - domain.start);
      let nearestIndex = 0;
      let nearestDistance = Infinity;
      samples.forEach((sample, index) => {
        const timestamp = sampleTimestamp(sample);
        const distance = Math.abs((timestamp ?? target) - target);
        if (distance < nearestDistance) {
          nearestDistance = distance;
          nearestIndex = index;
        }
      });
      return nearestIndex;
    }

    async function loadSamples() {
      if (state.paused) return;
      els.status.textContent = 'Fetching...';
      try {
        const res = await fetch('/api/v1/calibration/events?limit=200000&days=14&max_peak_mg=300');
        if (!res.ok) throw new Error(res.status === 401 ? 'Tailnet access required' : `HTTP ${res.status}`);
        const data = await res.json();
        const events = Array.isArray(data.events) ? data.events : [];
        const notifications = Array.isArray(data.notifications) ? data.notifications : [];
        state.samples = [...events].reverse();
        state.notifications = [...notifications].reverse();
        update();
        els.status.textContent = `Live: ${state.samples.length} samples`;
      } catch (err) {
        els.status.textContent = err.message;
      }
    }

    function update() {
      const latest = state.samples[state.samples.length - 1];
      const phases = classifySamples(state.samples);
      const latestGuess = phases[phases.length - 1] || null;
      els.count.textContent = state.samples.length.toString();
      els.clock.textContent = latest ? `Latest sample ${formatTime(sampleTime(latest))}` : 'Waiting for samples';
      els.rms.textContent = latest ? formatNumber(latest.motion_rms_mg) : '--';
      els.peak.textContent = latest ? formatNumber(latest.peak_mg) : '--';
      els.guess.textContent = latestGuess ? latestGuess.label : 'Waiting';
      els.guess.style.background = latestGuess ? latestGuess.color : 'var(--gentle)';
      els.guessDetail.textContent = latestGuess ? latestGuess.detail : 'Needs samples';
      updateConnectionIndicators(latest);
      els.last.textContent = latest ? `Checked in at ${formatTime(sampleTime(latest))}` : 'No latest sample';
      els.run.textContent = latest ? 'Live samples loaded' : 'No run yet';
      const rowStart = Math.max(0, state.samples.length - 8);
      els.rows.innerHTML = state.samples.slice(rowStart).reverse().map((sample, reverseIndex) => {
        const phase = phases[state.samples.length - 1 - reverseIndex];
        return `<tr><td>${formatTime(sampleTime(sample))}</td><td>${formatNumber(sample.motion_rms_mg)}</td><td>${formatNumber(sample.peak_mg)}</td><td>${phase.short}</td></tr>`;
      }
      ).join('');
      draw();
    }

    function phase(short, label, detail, color) {
      return { short, label, detail, color };
    }

    function phaseFromServerSample(sample) {
      if (!sample || !sample.server_phase) return null;
      return phase(
        sample.server_phase,
        sample.server_phase_label || sample.server_phase,
        sample.server_phase_detail || 'Classified by the relay.',
        sample.server_phase_color || 'rgba(66, 133, 244, 0.14)'
      );
    }

    function classifySample(sample) {
      const rms = numeric(sample.motion_rms_mg);
      const peak = numeric(sample.peak_mg);
      if (peak > 300 || rms > 120) {
        return phase(
          'handling',
          'Handling spike',
          'Likely bumping the sensor, not useful machine rhythm.',
          'rgba(179, 38, 30, 0.18)'
        );
      }
      if (rms <= 5 && peak <= 8) {
        return phase(
          'quiet',
          'Quiet / off',
          'Looks like the machine is stopped or only filling quietly.',
          'rgba(95, 128, 95, 0.18)'
        );
      }
      if (peak >= 120 || rms >= 60) {
        return phase(
          'strong',
          'Strong wash/spin',
          'Big machine movement, usually washer agitation or spin.',
          'rgba(190, 80, 52, 0.18)'
        );
      }
      const washerValueCandidate = (rms >= 2.3 && rms <= 8 && peak >= 8 && peak <= 35) || rms >= 28 || peak >= 65;
      const dryerValueCandidate = rms >= 12 && rms <= 28 && peak >= 25 && peak <= 65;
      if (washerValueCandidate && !dryerValueCandidate) {
        return phase(
          'washer',
          'Washer running',
          'Washer-like value pattern: low shake with jolts, or stronger agitation/spin.',
          'rgba(218, 157, 44, 0.18)'
        );
      }
      if (dryerValueCandidate) {
        return phase(
          'dryer',
          'Dryer running',
          'Sustained mid-level tumble pattern from the accelerometer values.',
          'rgba(66, 133, 244, 0.16)'
        );
      }
      if (rms >= 3 || peak >= 8) {
        return phase(
          'gentle',
          'Gentle motion',
          'Small but real movement above the quiet baseline.',
          'rgba(66, 133, 244, 0.14)'
        );
      }
      return phase(
        'settling',
        'Mostly quiet',
        'Near the quiet baseline with tiny movement.',
        'rgba(95, 128, 95, 0.14)'
      );
    }

    function smoothedClassificationSamples(samples, windowSize = 8) {
      return samples.map((sample, index) => {
        const start = Math.max(0, index - windowSize + 1);
        const chunk = samples.slice(start, index + 1);
        const rms = chunk.reduce((sum, item) => sum + numeric(item.motion_rms_mg), 0) / chunk.length;
        const peak = chunk.reduce((sum, item) => sum + numeric(item.peak_mg), 0) / chunk.length;
        return { ...sample, motion_rms_mg: rms, peak_mg: peak };
      });
    }

    function classifySamples(samples) {
      const serverPhases = samples.map(phaseFromServerSample);
      if (serverPhases.every(Boolean)) return serverPhases;
      let recentWasherValue = null;
      let recentWasherAge = Infinity;
      const smoothedSamples = smoothedClassificationSamples(samples, 8);
      return smoothedSamples.map(sample => {
        const base = classifySample(sample);

        if (base.short === 'handling') {
          return base;
        }
        if (base.short === 'washer' || base.short === 'strong') {
          recentWasherValue = base.short === 'strong'
            ? phase(
                'washer',
                'Washer running',
                'Strong washer-like agitation or spin value pattern.',
                'rgba(218, 157, 44, 0.18)'
              )
            : base;
          recentWasherAge = 0;
          return base;
        }
        if (base.short === 'dryer') {
          recentWasherValue = null;
          recentWasherAge = Infinity;
          return base;
        }
        if (recentWasherValue && recentWasherAge < 6 && (base.short === 'quiet' || base.short === 'gentle' || base.short === 'settling')) {
          recentWasherAge += 1;
          return recentWasherValue;
        }
        recentWasherAge += 1;
        return base;
      });
    }

    function visibleSamples() {
      const zoom = els.zoom.value;
      if (zoom === 'all') return state.samples;
      const latest = state.samples[state.samples.length - 1];
      const latestTimestamp = latestActivityTimestamp();
      const windowMs = Number(zoom);
      if (!Number.isFinite(latestTimestamp) || !Number.isFinite(windowMs)) {
        return latest ? [latest] : [];
      }
      const cutoff = latestTimestamp - windowMs;
      const visible = state.samples.filter(sample => {
        const timestamp = sampleTimestamp(sample);
        return Number.isFinite(timestamp) && timestamp >= cutoff && timestamp <= latestTimestamp;
      });
      return visible.length > 0 ? visible : [latest];
    }

    function visibleNotifications() {
      const zoom = els.zoom.value;
      if (zoom === 'all') return state.notifications;
      const latestTimestamp = latestActivityTimestamp();
      const windowMs = Number(zoom);
      if (!Number.isFinite(latestTimestamp) || !Number.isFinite(windowMs)) return [];
      const cutoff = latestTimestamp - windowMs;
      return state.notifications.filter(notification => {
        const timestamp = notificationTimestamp(notification);
        return Number.isFinite(timestamp) && timestamp >= cutoff && timestamp <= latestTimestamp;
      });
    }

    function movingAverage(samples, key, windowSize) {
      if (windowSize <= 1) return samples.map(sample => numeric(sample[key]));
      return samples.map((_, index) => {
        const start = Math.max(0, index - windowSize + 1);
        const chunk = samples.slice(start, index + 1);
        return chunk.reduce((sum, sample) => sum + numeric(sample[key]), 0) / chunk.length;
      });
    }

    function scaleMaximum(values) {
      const mode = els.scaleMode.value;
      if (mode === 'detail') return 120;
      if (mode === 'mid') return 500;
      if (mode === 'full') return 5000;
      const maxValue = Math.max(1, ...values);
      if (maxValue <= 5) return 5;
      if (maxValue <= 12) return 12;
      return Math.ceil(maxValue / 10) * 10;
    }

    function draw() {
      const { width, height, pad, plotW, plotH } = plotGeometry();
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = '#fbfcfd';
      ctx.fillRect(0, 0, width, height);
      ctx.strokeStyle = '#d9dde3';
      ctx.lineWidth = 1;
      ctx.font = '12px system-ui, sans-serif';
      ctx.fillStyle = '#5f6368';

      const samples = visibleSamples();
      const phases = classifySamples(state.samples).slice(-samples.length);
      const smoothing = Number(els.smooth.value);
      const rmsValues = movingAverage(samples, 'motion_rms_mg', smoothing);
      const peakValues = movingAverage(samples, 'peak_mg', smoothing);
      const scaleMax = scaleMaximum([...rmsValues, ...peakValues]);
      const domain = timeDomain(samples);

      for (let i = 0; i <= 4; i++) {
        const y = pad.top + plotH * (i / 4);
        const value = scaleMax - (scaleMax * i / 4);
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(width - pad.right, y);
        ctx.stroke();
        ctx.fillText(value.toFixed(0), 8, y + 4);
      }

      if (samples.length < 2) {
        ctx.fillStyle = '#5f6368';
        ctx.fillText('Waiting for calibration samples...', pad.left + 12, pad.top + 28);
        return;
      }

      function point(index, value) {
        const x = xForSample(index, samples, domain, pad, plotW);
        const clamped = Math.min(value, scaleMax);
        const y = pad.top + plotH - (plotH * clamped / scaleMax);
        return [x, y];
      }
      function lineFor(values, color) {
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        values.forEach((value, index) => {
          const [x, y] = point(index, value);
          if (index === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.stroke();
      }
      drawClassificationBands(samples, phases, domain, pad, plotW, plotH);
      lineFor(rmsValues, '#137a7f');
      lineFor(peakValues, '#b85c18');
      drawNotificationMarkers(samples, domain, pad, plotW, plotH);
      drawTimeAxis(domain, pad, plotW, plotH);
      drawHover(samples, phases, rmsValues, peakValues, scaleMax, domain);
    }

    function drawClassificationBands(samples, phases, domain, pad, plotW, plotH) {
      if (samples.length < 2) return;
      let start = 0;
      let current = phases[0].label;
      for (let index = 1; index <= samples.length; index++) {
        const next = index < samples.length ? phases[index].label : null;
        if (next === current) continue;
        const startX = xForSample(start, samples, domain, pad, plotW);
        const endX = xForSample(index - 1, samples, domain, pad, plotW);
        const phaseInfo = phases[start];
        ctx.fillStyle = phaseInfo.color;
        ctx.fillRect(startX, pad.top, Math.max(3, endX - startX), plotH);
        if (endX - startX > 72) {
          ctx.fillStyle = 'rgba(32, 33, 36, 0.62)';
          ctx.font = '12px system-ui, sans-serif';
          ctx.fillText(phaseInfo.label, startX + 6, pad.top + 16);
        }
        start = index;
        current = next;
      }
    }

    function drawNotificationMarkers(samples, domain, pad, plotW, plotH) {
      if (samples.length < 2 || !domain.useTime) return;
      const visible = state.notifications.filter(notification => {
        const timestamp = notificationTimestamp(notification);
        return Number.isFinite(timestamp) && timestamp >= domain.start && timestamp <= domain.end;
      });
      visible.forEach(notification => {
        const timestamp = notificationTimestamp(notification);
        const x = pad.left + ((timestamp - domain.start) / (domain.end - domain.start || 1)) * plotW;
        ctx.save();
        ctx.strokeStyle = 'rgba(179, 38, 30, 0.78)';
        ctx.fillStyle = 'rgba(179, 38, 30, 0.92)';
        ctx.lineWidth = 1.5;
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + plotH);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.arc(x, pad.top + 12, 4, 0, Math.PI * 2);
        ctx.fill();
        const title = notification.title || 'Gotify notification';
        if (x < pad.left + plotW - 120) {
          ctx.font = '12px system-ui, sans-serif';
          ctx.fillText(title, x + 7, pad.top + 16);
        }
        ctx.restore();
      });
    }

    function drawHover(samples, phases, rmsValues, peakValues, scaleMax, domain) {
      if (state.hover === null || samples.length < 2) {
        els.tooltip.style.display = 'none';
        return;
      }
      const { width, pad, plotW, plotH } = plotGeometry();
      const index = Math.max(0, Math.min(samples.length - 1, state.hover));
      const sample = samples[index];
      const phaseInfo = phases[index];
      const x = xForSample(index, samples, domain, pad, plotW);
      const rmsY = pad.top + plotH - (plotH * Math.min(rmsValues[index], scaleMax) / scaleMax);
      const peakY = pad.top + plotH - (plotH * Math.min(peakValues[index], scaleMax) / scaleMax);

      ctx.strokeStyle = 'rgba(32, 33, 36, 0.45)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, pad.top);
      ctx.lineTo(x, pad.top + plotH);
      ctx.stroke();
      ctx.fillStyle = '#137a7f';
      ctx.beginPath();
      ctx.arc(x, rmsY, 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#b85c18';
      ctx.beginPath();
      ctx.arc(x, peakY, 4, 0, Math.PI * 2);
      ctx.fill();

      els.tooltip.innerHTML = `
        <strong>Sensor sample ${escapeHtml(formatTime(sampleTime(sample)))}</strong>
        <div class="tooltip-row"><span>Guess</span><b>${escapeHtml(phaseInfo.label)}</b></div>
        <div class="tooltip-row"><span>Vibration</span><b>${escapeHtml(formatNumber(sample.motion_rms_mg, 2))} mg</b></div>
        <div class="tooltip-row"><span>Jolt</span><b>${escapeHtml(formatNumber(sample.peak_mg, 2))} mg</b></div>
        <div class="tooltip-row"><span>Wi-Fi signal</span><b>${escapeHtml(formatWifiSignal(sample.wifi_rssi))}</b></div>
        <div class="tooltip-row"><span>Relay received</span><b>${escapeHtml(formatTime(sample.received_at))}</b></div>
      `;
      const tipWidth = 230;
      const left = x > width - tipWidth - 24 ? x - tipWidth - 12 : x + 12;
      els.tooltip.style.left = `${Math.max(12, left)}px`;
      els.tooltip.style.top = `${pad.top + 12}px`;
      els.tooltip.style.display = 'block';
    }

    function startLiveUpdates() {
      loadSamples();
      clearInterval(state.timer);
      state.timer = setInterval(loadSamples, 2000);
    }

    els.pause.addEventListener('click', () => {
      state.paused = !state.paused;
      els.pause.textContent = state.paused ? 'Resume' : 'Pause';
      els.status.textContent = state.paused ? 'Paused' : 'Resumed';
      if (!state.paused) loadSamples();
    });
    els.zoom.addEventListener('change', draw);
    els.scaleMode.addEventListener('change', draw);
    els.smooth.addEventListener('input', () => {
      const value = Number(els.smooth.value);
      els.smoothValue.textContent = value === 1 ? 'off' : `${value} samples`;
      draw();
    });
    chart.addEventListener('mousemove', event => {
      const samples = visibleSamples();
      const { pad, plotW } = plotGeometry();
      const rect = chart.getBoundingClientRect();
      const x = event.clientX - rect.left;
      if (samples.length < 2 || x < pad.left || x > pad.left + plotW) {
        state.hover = null;
        draw();
        return;
      }
      const domain = timeDomain(samples);
      state.hover = nearestSampleIndexForX(samples, domain, x, pad, plotW);
      draw();
    });
    chart.addEventListener('mouseleave', () => {
      state.hover = null;
      draw();
    });
    window.addEventListener('resize', resizeCanvas);
    setInterval(updatePingCountdown, 250);
    resizeCanvas();
    startLiveUpdates();
  </script>
</body>
</html>"""


def _message_for(event: LaundryEvent) -> GotifyMessage:
    if event.state == "button_pressed":
        return GotifyMessage(
            title="Laundry button pressed",
            message="ESP32 button test reached the relay.",
        )
    if event.state == "motion_started":
        return GotifyMessage(
            title="Laundry motion detected",
            message=(
                f"Fast motion trigger from {event.device_id}: "
                f"{event.motion_rms_mg:.1f} mg ({event.event_id})."
            ),
        )
    if event.cycle_label == "washer":
        return GotifyMessage(title="Washer done", message="No washer motion for 10 min.")
    if event.cycle_label == "dryer":
        return GotifyMessage(title="Dryer done", message="No dryer motion for 10 min.")
    return GotifyMessage(
        title="Laundry stack stopped",
        message="No washer/dryer stack motion for 10 min.",
    )


def _gotify_sender(gotify_url: str, app_token: str) -> PushMessage:
    def send(message: dict) -> None:
        url = f"{gotify_url.rstrip('/')}/message"
        response = httpx.post(url, params={"token": app_token}, json=message, timeout=5.0)
        response.raise_for_status()

    return send


app = app_from_env() if os.environ.get("LAUNDRY_RELAY_FROM_ENV") == "1" else FastAPI()
