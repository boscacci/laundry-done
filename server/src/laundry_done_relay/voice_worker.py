"""Outbox worker; AWS credentials come from the restricted credential process."""

import json
import os
import time
from urllib.parse import quote

import httpx
from laundry_done_relay import delivery


class VoiceClient:
    def __init__(self):
        import boto3

        self.session = boto3.Session()
        self.base = os.environ["VOICE_API_URL"].rstrip("/")
        self.region = os.environ.get("AWS_REGION", "us-west-2")

    def request(self, method, job):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        url = f"{self.base}/projects/laundry/jobs/{quote(job['id'], safe='')}"
        body = (
            json.dumps(
                {
                    "recipient_id": job["recipient_id"],
                    "message_key": job["message_key"],
                    "expires_at": int(job["expires_at"]),
                }
            )
            if method == "PUT"
            else ""
        )
        request = AWSRequest(
            method=method,
            url=url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(
            self.session.get_credentials().get_frozen_credentials(),
            "execute-api",
            self.region,
        ).add_auth(request)
        res = httpx.request(
            method, url, content=body, headers=dict(request.headers), timeout=15
        )
        res.raise_for_status()
        return res.json()

    def submit(self, job):
        return self.request("PUT", job)

    def poll(self, job):
        return self.request("GET", job)

    def cancel(self, job):
        return self.request("DELETE", job)


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-event", help="Explicit real-phone test with a stable idempotency key"
    )
    args = parser.parse_args()
    if args.test_event:
        import hashlib

        client = VoiceClient()
        job = {
            "id": hashlib.sha256(args.test_event.encode()).hexdigest(),
            "recipient_id": "owner",
            "message_key": "test",
            "expires_at": int(time.time()) + 900,
        }
        result = client.submit(job)
        print(json.dumps(result), flush=True)
        for _ in range(45):
            time.sleep(2)
            result = client.poll(job)
            if result["status"] in delivery.TERMINAL:
                print(json.dumps(result), flush=True)
                return
        print(json.dumps({"status": "pending", "event_id": job["id"]}), flush=True)
        return
    path = os.environ.get("DATABASE_PATH", "/data/events.sqlite3")
    delivery.init_db(path)
    backend = os.environ.get("ALERT_BACKEND", "simulation")
    if backend == "voice":
        client = VoiceClient()
        submit, poll, cancel = client.submit, client.poll, client.cancel
    elif backend == "simulation":

        def submit(job):
            return {"status": "simulated", "job_id": job["id"]}

        poll = cancel = None
    else:
        raise ValueError("worker requires voice or simulation backend")
    while True:
        try:
            delivery.work_once(path, submit, poll=poll, cancel=cancel)
        except Exception as exc:
            print(
                json.dumps({"event": "worker_error", "error": type(exc).__name__}),
                flush=True,
            )
        time.sleep(2)


if __name__ == "__main__":
    main()
