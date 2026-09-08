"""Shared OTA package limits and cryptographic domain separation."""

import hashlib
import hmac


MAX_IMAGE_BYTES = 0x140000


def mac(secret, message):
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def encryption_key(secret):
    return bytes.fromhex(mac(secret, b"laundry-ota-aes-gcm-v1"))


def package_aad(package):
    return (
        f"laundry-ota-v1\n{package['job_id']}\n{package['device_id']}\n"
        f"{package['size']}\n{package['sha256']}"
    ).encode()
