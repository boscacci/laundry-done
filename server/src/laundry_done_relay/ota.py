"""Bounded, authenticated update queue; firmware artifacts are encrypted at source."""

import base64
import hashlib
import hmac
import json
import re
import sqlite3
import time

from fastapi import HTTPException, Request
from fastapi.responses import Response

from laundry_done_relay.ota_crypto import MAX_IMAGE_BYTES, mac


MAX_PACKAGE_BYTES = 2 * MAX_IMAGE_BYTES


def init_ota(path):
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS firmware_jobs (
            job_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
            metadata TEXT NOT NULL, ciphertext BLOB NOT NULL,
            fingerprint TEXT NOT NULL, expires_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending')""")


def validate_package(raw):
    if not isinstance(raw, dict):
        raise ValueError("invalid package")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError("invalid version")
    for key, length in (("job_id", 32), ("sha256", 64), ("nonce", 24), ("tag", 32)):
        if not isinstance(raw.get(key), str) or not re.fullmatch(
            f"[0-9a-f]{{{length}}}", raw[key]
        ):
            raise ValueError("invalid metadata")
    if not isinstance(raw.get("device_id"), str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", raw["device_id"]
    ):
        raise ValueError("invalid device")
    if type(raw.get("size")) is not int or not 1 <= raw["size"] <= MAX_IMAGE_BYTES:
        raise ValueError("invalid image size")
    if type(raw.get("ttl_seconds")) is not int or not 60 <= raw["ttl_seconds"] <= 3600:
        raise ValueError("invalid expiration")
    if type(raw.get("interrupt_monitoring", False)) is not bool:
        raise ValueError("invalid maintenance flag")
    ciphertext = base64.b64decode(raw["ciphertext"], validate=True)
    if len(ciphertext) != raw["size"]:
        raise ValueError("invalid ciphertext size")
    metadata = {
        key: raw[key]
        for key in ("version", "job_id", "device_id", "size", "sha256", "nonce", "tag")
    }
    metadata["interrupt_monitoring"] = raw.get("interrupt_monitoring", False)
    return metadata, ciphertext


def install_ota_routes(app, path, secret):
    init_ota(path)

    @app.middleware("http")
    async def prevent_update_caching(request: Request, call_next):
        response = await call_next(request)
        if (
            request.url.path.startswith("/api/v1/firmware")
            or request.url.path == "/api/v1/events"
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    def authorize(request, message):
        if not hmac.compare_digest(
            request.headers.get("x-laundry-signature", ""), mac(secret, message)
        ):
            raise HTTPException(401, "bad signature")

    @app.post("/api/v1/firmware", status_code=202)
    async def stage(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_PACKAGE_BYTES:
                raise HTTPException(413, "package too large")
        authorize(request, b"ota-stage-v1\n" + body)
        try:
            raw = json.loads(body)
            metadata, ciphertext = validate_package(raw)
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, "invalid update package") from None
        fingerprint = hashlib.sha256(body).hexdigest()
        now = int(time.time())
        with sqlite3.connect(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT fingerprint FROM firmware_jobs WHERE job_id=?", (raw["job_id"],)
            ).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise HTTPException(
                        409, "job already exists with different content"
                    )
                return {"accepted": True, "duplicate": True, "job_id": raw["job_id"]}
            conn.execute(
                "DELETE FROM firmware_jobs WHERE expires_at < ?", (now - 86400,)
            )
            if conn.execute("SELECT COUNT(*) FROM firmware_jobs").fetchone()[0] >= 20:
                raise HTTPException(
                    409, "update queue full; wait for retention cleanup"
                )
            conn.execute(
                "UPDATE firmware_jobs SET status='superseded', ciphertext=X'' "
                "WHERE device_id=? AND status IN ('pending','offered')",
                (raw["device_id"],),
            )
            conn.execute(
                "INSERT INTO firmware_jobs(job_id,device_id,metadata,ciphertext,fingerprint,expires_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    raw["job_id"],
                    raw["device_id"],
                    json.dumps(metadata, separators=(",", ":")),
                    ciphertext,
                    fingerprint,
                    now + raw["ttl_seconds"],
                ),
            )
        return {"accepted": True, "duplicate": False, "job_id": raw["job_id"]}

    @app.get("/api/v1/firmware/{job_id}")
    def download(job_id: str, request: Request):
        authorize(request, f"ota-download-v1\n{job_id}".encode())
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT ciphertext FROM firmware_jobs WHERE job_id=? "
                "AND expires_at>=? AND status IN ('pending','offered')",
                (job_id, int(time.time())),
            ).fetchone()
        if not row:
            raise HTTPException(404, "update unavailable")
        return Response(
            bytes(row[0]),
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/firmware/{job_id}/status")
    def status(job_id: str, request: Request):
        authorize(request, f"ota-status-v1\n{job_id}".encode())
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT status,expires_at FROM firmware_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if not row:
            raise HTTPException(404, "update unavailable")
        return {"job_id": job_id, "status": row[0], "expires_at": row[1]}


def offer_update(path, secret, raw):
    extra = raw.get("diagnostics")
    if (
        raw.get("state") != "calibration_sample"
        or not isinstance(extra, dict)
        or extra.get("ota_capable") is not True
    ):
        return None
    with sqlite3.connect(path) as conn:
        result = extra.get("ota_result")
        job = extra.get("ota_job_id")
        if result == "installed":
            installed = conn.execute(
                "SELECT metadata FROM firmware_jobs WHERE job_id=? AND device_id=?",
                (job if isinstance(job, str) else "", raw["device_id"]),
            ).fetchone()
            if (
                not installed
                or extra.get("installed_sha256") != json.loads(installed[0])["sha256"]
            ):
                result = None
        if (
            isinstance(job, str)
            and isinstance(result, str)
            and result in {"installed", "failed"}
        ):
            conn.execute(
                "UPDATE firmware_jobs SET status=?, ciphertext=X'' WHERE job_id=? AND device_id=? "
                "AND status IN ('pending','offered')",
                (result, job, raw["device_id"]),
            )
        if extra.get("startup_settling") is not False:
            return None
        row = conn.execute(
            "SELECT metadata,expires_at FROM firmware_jobs WHERE device_id=? "
            "AND expires_at>=? AND status IN ('pending','offered') ORDER BY rowid DESC LIMIT 1",
            (raw["device_id"], int(time.time())),
        ).fetchone()
        if not row:
            return None
        metadata = json.loads(row[0])
        if extra.get("detector_state") not in (
            "idle",
            "done_sent",
        ) and not metadata.get("interrupt_monitoring", False):
            return None
        metadata.update(event_id=raw["event_id"], expires_at=row[1])
        manifest = json.dumps(metadata, separators=(",", ":"))
        conn.execute(
            "UPDATE firmware_jobs SET status='offered' WHERE job_id=?",
            (metadata["job_id"],),
        )
    return {
        "manifest": manifest,
        "signature": mac(secret, b"ota-offer-v1\n" + manifest.encode()),
    }
