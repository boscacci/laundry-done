from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from pathlib import Path
from typing import Callable

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field


class LaundryEvent(BaseModel):
    device_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    cycle_id: str = Field(min_length=1)
    state: str = Field(min_length=1)
    cycle_label: str = Field(pattern="^(washer|dryer|stack|unknown)$")
    motion_rms_mg: float
    last_motion_ms: int = Field(ge=0)
    firmware_version: str = Field(min_length=1)


class GotifyMessage(BaseModel):
    title: str
    message: str
    priority: int = 5


PushMessage = Callable[[dict], None]


def create_app(
    *,
    database_path: str | Path,
    device_secret: str,
    gotify_url: str,
    gotify_app_token: str,
    push_message: PushMessage | None = None,
) -> FastAPI:
    app = FastAPI(title="Laundry Done Relay")
    db_path = Path(database_path)
    sender = push_message or _gotify_sender(gotify_url, gotify_app_token)
    _init_db(db_path)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/api/v1/events", status_code=202)
    async def receive_event(
        request: Request,
        x_laundry_signature: str = Header(default=""),
    ) -> dict:
        body = await request.body()
        if not _valid_signature(device_secret, body, x_laundry_signature):
            raise HTTPException(status_code=401, detail="bad signature")

        event = LaundryEvent.model_validate_json(body)
        duplicate = _store_event(db_path, event, body)
        if not duplicate and event.state == "done_sent":
            sender(_message_for(event).model_dump())

        return {"accepted": True, "duplicate": duplicate}

    return app


def app_from_env() -> FastAPI:
    return create_app(
        database_path=os.environ.get("DATABASE_PATH", "/data/events.sqlite3"),
        device_secret=_required_env("DEVICE_SECRET"),
        gotify_url=_required_env("GOTIFY_URL"),
        gotify_app_token=_required_env("GOTIFY_APP_TOKEN"),
    )


app = app_from_env() if os.environ.get("LAUNDRY_RELAY_FROM_ENV") == "1" else FastAPI()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


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


def _store_event(path: Path, event: LaundryEvent, raw_body: bytes) -> bool:
    with sqlite3.connect(path) as conn:
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


def _valid_signature(secret: str, body: bytes, received: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def _message_for(event: LaundryEvent) -> GotifyMessage:
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
