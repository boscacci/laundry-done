"""Stage an encrypted Wi-Fi update without writing or logging credentials."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import uuid

import httpx

from laundry_done_relay.ota import MAX_IMAGE_BYTES, encryption_key, mac, package_aad


def make_package(image, *, device_id, secret, ttl_seconds=600):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not image or image[0] != 0xE9 or len(image) > MAX_IMAGE_BYTES:
        raise ValueError("invalid or oversized ESP32 application image")
    package = dict(
        version=1,
        job_id=uuid.uuid4().hex,
        device_id=device_id,
        size=len(image),
        sha256=hashlib.sha256(image).hexdigest(),
        nonce=secrets.token_bytes(12).hex(),
        ttl_seconds=ttl_seconds,
    )
    encrypted = AESGCM(encryption_key(secret)).encrypt(
        bytes.fromhex(package["nonce"]), image, package_aad(package)
    )
    package.update(
        tag=encrypted[-16:].hex(), ciphertext=base64.b64encode(encrypted[:-16]).decode()
    )
    return package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--relay", required=True, help="Relay origin, without /api/v1/events"
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Existing ignored Arduino config; otherwise use DEVICE_SECRET environment",
    )
    parser.add_argument("--device-id", default="laundry-stack-1")
    parser.add_argument("--firmware", type=Path)
    parser.add_argument("--status", help="Read status of this job instead of staging")
    parser.add_argument("--ttl-seconds", type=int, default=600)
    args = parser.parse_args()
    try:
        secret = os.environ.get("DEVICE_SECRET", "")
        if args.config:
            match = re.search(
                r'^\s*#define\s+DEVICE_SECRET\s+("(?:[^"\\]|\\.)*")',
                args.config.read_text(),
                re.MULTILINE,
            )
            if match:
                secret = json.loads(match[1])
        if not secret or secret == "configure-me":
            raise ValueError("missing device secret")
        origin = args.relay.rstrip("/")
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            if args.status:
                response = client.get(
                    f"{origin}/api/v1/firmware/{args.status}/status",
                    headers={
                        "x-laundry-signature": mac(
                            secret, f"ota-status-v1\n{args.status}".encode()
                        )
                    },
                )
            else:
                if not args.firmware or args.firmware.stat().st_size > MAX_IMAGE_BYTES:
                    raise ValueError("missing or oversized firmware")
                package = make_package(
                    args.firmware.read_bytes(),
                    device_id=args.device_id,
                    secret=secret,
                    ttl_seconds=args.ttl_seconds,
                )
                body = json.dumps(package, separators=(",", ":")).encode()
                response = client.post(
                    f"{origin}/api/v1/firmware",
                    content=body,
                    headers={
                        "content-type": "application/json",
                        "x-laundry-signature": mac(secret, b"ota-stage-v1\n" + body),
                    },
                )
            response.raise_for_status()
            result = response.json()
            print(
                json.dumps(
                    {
                        key: result[key]
                        for key in (
                            "accepted",
                            "duplicate",
                            "job_id",
                            "status",
                            "expires_at",
                        )
                        if key in result
                    }
                )
            )
    except (ValueError, OSError, httpx.HTTPError) as exc:
        parser.exit(
            1,
            f"Firmware operation failed ({type(exc).__name__}); credentials and payload omitted.\n",
        )


if __name__ == "__main__":
    main()
