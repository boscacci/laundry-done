"""Bounded shared voice delivery. No phone numbers or freeform audio in its API."""

import copy
import hashlib
import re
import time

TERMINAL = {"completed", "failed", "canceled", "expired", "simulated"}


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.quotas = {}

    def get(self, key):
        return copy.deepcopy(self.rows.get(key))

    def put(self, row, expected=None):
        old = self.rows.get(row["id"])
        if (old or {}).get("version") != expected:
            raise RuntimeError("conflict")
        row = copy.deepcopy(row)
        row["version"] = (expected or 0) + 1
        self.rows[row["id"]] = row
        return row

    def pending(self):
        return [
            copy.deepcopy(r) for r in self.rows.values() if r["status"] not in TERMINAL
        ]

    def reserve(self, project, day, limit, attempt):
        if attempt in self.quotas:
            return
        if (
            self.quotas.get((project, day), 0) >= limit
            or self.quotas.get(("all", day), 0) >= 20
        ):
            raise ValueError("daily quota exceeded")
        self.quotas[(project, day)] = self.quotas.get((project, day), 0) + 1
        self.quotas[("all", day)] = self.quotas.get(("all", day), 0) + 1
        self.quotas[attempt] = True


class Dispatcher:
    def __init__(self, store, dial, projects, clock=time.time, enabled=True, stop=None):
        self.store = store
        self.dial = dial
        self.projects = projects
        self.clock = clock
        self.enabled = enabled
        self.stop = stop

    def authorize(self, project, principal):
        if (
            project not in self.projects
            or principal != self.projects[project]["principal_id"]
        ):
            raise ValueError("project access denied")

    def identity(self, project, event):
        return hashlib.sha256((project + ":" + event).encode()).hexdigest()

    def create(self, project, event, recipient, message, principal, expires_at=None):
        self.authorize(project, principal)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event):
            raise ValueError("invalid event")
        if recipient != "owner" or message not in self.projects[project]["messages"]:
            raise ValueError("unapproved destination or recording")
        if not self.enabled:
            raise ValueError("calls disabled")
        now = int(self.clock())
        key = self.identity(project, event)
        old = self.store.get(key)
        if old:
            if old["status"] == "canceled":
                return old
            if (old["recipient_id"], old["message_key"]) != (recipient, message):
                raise ValueError("event already used")
            return old
        row = {
            "id": key,
            "project": project,
            "event": event,
            "recipient_id": recipient,
            "message_key": message,
            "created_at": now,
            "expires_at": min(int(expires_at or now + 900), now + 900),
            "status": "queued",
            "attempt": 0,
            "next_at": now,
            "version": None,
        }
        if row["expires_at"] <= now:
            raise ValueError("expired request")
        row = self.store.put(row)
        return self.advance(row)

    def save(self, row, **changes):
        old = row["version"]
        row.update(changes)
        return self.store.put(row, old)

    def advance(self, row):
        now = int(self.clock())
        if row["status"] in TERMINAL:
            return row
        if now >= row["expires_at"]:
            return self.save(row, status="expired")
        if not self.enabled:
            return row
        if row["status"] == "queued" and row["next_at"] <= now:
            attempt = row["attempt"] + 1
            if attempt > 2:
                return self.save(row, status="failed")
            self.store.reserve(
                row["project"],
                now // 86400,
                self.projects[row["project"]]["daily_limit"],
                row["id"] + ":" + str(attempt),
            )
            row = self.save(row, status="submitting", attempt=attempt)
        if row["status"] == "submitting":
            # Re-entering after timeout/crash calls Connect with the same token.
            contact = self.dial(row, row["attempt"])
            row = self.save(row, status="accepted", contact_id=contact)
        return row

    def outcome(self, key, contact, status):
        row = self.store.get(key)
        if not row or row["status"] in TERMINAL:
            return
        if row.get("contact_id") not in (None, contact):
            return
        if row.get("resolved_contact_id") == contact:
            return row
        if status == "no_answer" or status == "retryable_failure":
            return self.save(
                row,
                status="queued" if row["attempt"] < 2 else "failed",
                next_at=int(self.clock()) + 120,
                resolved_contact_id=contact,
            )
        if status not in {"completed", "failed", "unknown", "ringing"}:
            raise ValueError("invalid outcome")
        return self.save(row, status=status)

    def cancel(self, project, event, principal):
        self.authorize(project, principal)
        row = self.store.get(self.identity(project, event))
        if not row:
            now = int(self.clock())
            return self.store.put(
                {
                    "id": self.identity(project, event),
                    "project": project,
                    "event": event,
                    "status": "canceled",
                    "attempt": 0,
                    "created_at": now,
                    "expires_at": now + 900,
                }
            )
        if row["status"] not in TERMINAL:
            row = self.save(row, status="canceled")
            if row.get("contact_id") and self.stop:
                self.stop(row)
        return row

    def tick(self):
        for row in self.store.pending():
            try:
                self.advance(row)
            except (RuntimeError, ValueError):
                continue
