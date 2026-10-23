"""Transactional completion outbox, monitoring sessions and idempotent controls.

No provider calls occur inside a SQLite transaction. Epoch seconds are UTC.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time

TERMINAL = {"completed", "failed", "canceled", "expired", "simulated"}


def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db(path):
    with connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS monitoring_sessions (
          id TEXT PRIMARY KEY, device_id TEXT NOT NULL, boot_id TEXT,
          started_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL,
          manual INTEGER NOT NULL DEFAULT 0, completed_at REAL);
        CREATE INDEX IF NOT EXISTS monitoring_device ON monitoring_sessions(device_id, started_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS one_monitoring_session ON monitoring_sessions(device_id)
          WHERE status='monitoring';
        CREATE TABLE IF NOT EXISTS notification_jobs (
          id TEXT PRIMARY KEY, episode_id TEXT NOT NULL UNIQUE, device_id TEXT NOT NULL,
          recipient_id TEXT NOT NULL, message_key TEXT NOT NULL, status TEXT NOT NULL,
          created_at REAL NOT NULL, expires_at REAL NOT NULL, next_at REAL NOT NULL,
          lease_until REAL NOT NULL DEFAULT 0, error TEXT, provider_id TEXT);
        CREATE INDEX IF NOT EXISTS notification_work ON notification_jobs(status,next_at);
        CREATE TABLE IF NOT EXISTS alert_settings (id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL);
        INSERT OR IGNORE INTO alert_settings VALUES (1,0);
        CREATE TABLE IF NOT EXISTS control_requests (
          key TEXT PRIMARY KEY, payload TEXT NOT NULL, response TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS worker_health (id INTEGER PRIMARY KEY CHECK(id=1), at REAL NOT NULL, error TEXT);
        """)


def enqueue(db, *, episode_id, device_id, now=None, test=False):
    now = time.time() if now is None else now
    if db.execute("SELECT paused FROM alert_settings WHERE id=1").fetchone()[0]:
        return None
    previous = db.execute(
        "SELECT message_key FROM notification_jobs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    choices = [
        f"laundry-{i:02}"
        for i in range(1, 11)
        if not previous or f"laundry-{i:02}" != previous[0]
    ]
    key = "test" if test else secrets.choice(choices)
    identity = hashlib.sha256(("laundry:" + episode_id).encode()).hexdigest()
    db.execute(
        """INSERT OR IGNORE INTO notification_jobs
      (id,episode_id,device_id,recipient_id,message_key,status,created_at,expires_at,next_at)
      VALUES (?,?,?,?,?,'queued',?,?,?)""",
        (identity, episode_id, device_id, "owner", key, now, now + 900, now),
    )
    return identity


def enqueue_completion(db, event, raw):
    # Server-generated completion episode keys are derived from the observed run,
    # whereas older explicit firmware events are deduplicated by boot/cycle.
    episode = raw.get("_episode_id") or f"legacy:{event.device_id}:{event.cycle_id}"
    active = db.execute(
        "SELECT id FROM monitoring_sessions WHERE device_id=? AND status='monitoring'",
        (event.device_id,),
    ).fetchone()
    if active:
        episode = active[0]
        db.execute(
            "UPDATE monitoring_sessions SET status='complete',completed_at=? WHERE id=?",
            (time.time(), episode),
        )
    enqueue(db, episode_id=episode, device_id=event.device_id)


def jobs(path, limit=30):
    with connect(path) as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT * FROM notification_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        ]


def observe(path, raw, active, now=None):
    now = time.time() if now is None else now
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE monitoring_sessions SET status='expired' WHERE status='monitoring' AND expires_at<=?",
            (now,),
        )
        current = db.execute(
            "SELECT * FROM monitoring_sessions WHERE device_id=? AND status='monitoring'",
            (raw["device_id"],),
        ).fetchone()
        if current and current["boot_id"] not in (None, raw["cycle_id"]):
            db.execute(
                "UPDATE monitoring_sessions SET status='interrupted' WHERE id=?",
                (current["id"],),
            )
            current = None
        if current and current["boot_id"] is None:
            db.execute(
                "UPDATE monitoring_sessions SET boot_id=? WHERE id=?",
                (raw["cycle_id"], current["id"]),
            )
        latest = db.execute(
            "SELECT * FROM monitoring_sessions WHERE device_id=? ORDER BY started_at DESC LIMIT 1",
            (raw["device_id"],),
        ).fetchone()
        if (
            latest
            and latest["status"] in {"canceled", "expired"}
            and latest["boot_id"] in (None, raw["cycle_id"])
        ):
            return
        # A single raw jolt must never establish a session. Caller uses smoothed
        # classification and still requires eight minutes before completion.
        if active:
            db.execute(
                "UPDATE notification_jobs SET status='canceled' WHERE device_id=? AND status IN ('queued','accepted','ringing','unknown')",
                (raw["device_id"],),
            )
            if current is None:
                episode = hashlib.sha256(raw["event_id"].encode()).hexdigest()
                db.execute(
                    "INSERT INTO monitoring_sessions VALUES (?,?,?,?,?,'monitoring',0,NULL)",
                    (episode, raw["device_id"], raw["cycle_id"], now, now + 14400),
                )


def control(path, action, device_id, key, now=None):
    if action not in {"start", "cancel", "pause", "resume", "test"}:
        raise ValueError("invalid action")
    now = time.time() if now is None else now
    payload = json.dumps([action, device_id])
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT * FROM control_requests WHERE key=?", (key,)
        ).fetchone()
        if old:
            if old["payload"] != payload:
                raise ValueError("idempotency key already used")
            return json.loads(old["response"])
        result = {"action": action}
        if action in {"pause", "resume"}:
            db.execute(
                "UPDATE alert_settings SET paused=? WHERE id=1",
                (int(action == "pause"),),
            )
            if action == "pause":
                db.execute(
                    "UPDATE notification_jobs SET status='canceled' WHERE status IN ('queued','accepted','ringing','unknown')"
                )
        if action == "start":
            db.execute(
                "UPDATE monitoring_sessions SET status='canceled' WHERE device_id=? AND status='monitoring'",
                (device_id,),
            )
            identity = secrets.token_hex(16)
            db.execute(
                "INSERT INTO monitoring_sessions VALUES (?,?,NULL,?,?,'monitoring',1,NULL)",
                (identity, device_id, now, now + 14400),
            )
            result["episode_id"] = identity
        if action == "cancel":
            db.execute(
                "UPDATE monitoring_sessions SET status='canceled' WHERE device_id=? AND status='monitoring'",
                (device_id,),
            )
        if action in {"cancel", "start"}:
            db.execute(
                "UPDATE notification_jobs SET status='canceled' WHERE device_id=? AND status IN ('queued','accepted','ringing','unknown')",
                (device_id,),
            )
        if action == "test":
            if db.execute("SELECT paused FROM alert_settings WHERE id=1").fetchone()[0]:
                raise ValueError("calls are paused")
            result["job_id"] = enqueue(
                db, episode_id="test:" + key, device_id=device_id, now=now, test=True
            )
        db.execute(
            "INSERT INTO control_requests VALUES (?,?,?,?)",
            (key, payload, json.dumps(result), now),
        )
        db.execute(
            "DELETE FROM control_requests WHERE created_at<?", (now - 30 * 86400,)
        )
        return result


def status(path, device_id, now=None):
    now = time.time() if now is None else now
    with connect(path) as db:
        session = db.execute(
            "SELECT * FROM monitoring_sessions WHERE device_id=? ORDER BY started_at DESC LIMIT 1",
            (device_id,),
        ).fetchone()
        health = db.execute("SELECT * FROM worker_health WHERE id=1").fetchone()
        paused = bool(
            db.execute("SELECT paused FROM alert_settings WHERE id=1").fetchone()[0]
        )
    session = dict(session) if session else None
    if session and session["status"] == "monitoring" and session["expires_at"] <= now:
        session["status"] = "expired"
    return {
        "session": session,
        "paused": paused,
        "delivery": jobs(path),
        "worker": dict(health) if health else None,
    }


def work_once(path, submit, now=None, poll=None, cancel=None):
    now = time.time() if now is None else now
    with connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE notification_jobs SET status='expired' WHERE expires_at<=? AND status NOT IN ('completed','failed','canceled','simulated')",
            (now,),
        )
        row = db.execute(
            "SELECT * FROM notification_jobs WHERE status IN ('queued','accepted','ringing','unknown','canceled','expired') AND next_at<=? AND lease_until<=? ORDER BY created_at LIMIT 1",
            (now, now),
        ).fetchone()
        if not row:
            db.execute("INSERT OR REPLACE INTO worker_health VALUES (1,?,NULL)", (now,))
            return
        job = dict(row)
        db.execute(
            "UPDATE notification_jobs SET lease_until=? WHERE id=?",
            (now + 60, job["id"]),
        )
    try:
        if job["status"] in {"canceled", "expired"}:
            if cancel:
                cancel(job)
            result = {"status": job["status"]}
            next_at = now + 30 * 86400
        else:
            result = poll(job) if job["provider_id"] and poll else submit(job)
            if result["status"] not in TERMINAL | {
                "accepted",
                "ringing",
                "unknown",
                "queued",
            }:
                raise ValueError("invalid provider status")
            next_at = now + 10
        with connect(path) as db:
            # User cancellation racing a provider request wins locally. Next
            # iteration forwards cancellation using the same deterministic job ID.
            db.execute(
                "UPDATE notification_jobs SET status=CASE WHEN status IN ('canceled','expired') THEN status ELSE ? END, provider_id=?,error=NULL,next_at=?,lease_until=0 WHERE id=?",
                (result["status"], result.get("job_id", job["id"]), next_at, job["id"]),
            )
            db.execute("INSERT OR REPLACE INTO worker_health VALUES (1,?,NULL)", (now,))
    except Exception as exc:
        # Do not persist exception strings: SDK errors can contain destination PII.
        error = type(exc).__name__
        with connect(path) as db:
            db.execute(
                "UPDATE notification_jobs SET error=?,next_at=?,lease_until=0 WHERE id=?",
                (error, now + 30, job["id"]),
            )
            db.execute(
                "INSERT OR REPLACE INTO worker_health VALUES (1,?,?)", (now, error)
            )
