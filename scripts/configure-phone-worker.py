#!/usr/bin/env python
"""Configure short-lived local identity and recipient without printing secrets."""

import argparse
import json
import os
import subprocess
from pathlib import Path
import boto3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--existing-destination-ending", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    outputs = json.loads(
        subprocess.check_output(
            ["tofu", "-chdir=infra/phone-alerts", "output", "-json"],
            cwd=root,
            text=True,
        )
    )

    def value(key):
        return outputs[key]["value"]

    client = boto3.client("pinpoint-sms-voice-v2", region_name="us-west-2")
    numbers = client.describe_verified_destination_numbers()[
        "VerifiedDestinationNumbers"
    ]
    candidates = [
        n["DestinationPhoneNumber"]
        for n in numbers
        if n["DestinationPhoneNumber"].endswith(args.existing_destination_ending)
    ]
    if len(candidates) != 1:
        raise SystemExit(
            "Recipient selection is ambiguous; configure securely before proceeding"
        )
    secret = boto3.client("secretsmanager", region_name="us-west-2")
    secret.put_secret_value(
        SecretId=value("recipient_secret_arn"), SecretString=candidates[0]
    )
    folder = root / "data/phone-identity/worker"
    folder.mkdir(mode=0o700, exist_ok=True)
    for name in ["worker.pem", "worker.key"]:
        target = folder / name
        if not target.exists():
            target.write_bytes((folder.parent / name).read_bytes())
        os.chmod(target, 0o600)
    config = folder / "config"
    config.write_text(
        "[default]\nregion = us-west-2\ncredential_process = /usr/local/bin/aws_signing_helper credential-process "
        "--certificate /credentials/worker.pem --private-key /credentials/worker.key "
        f"--trust-anchor-arn {value('trust_anchor_arn')} --profile-arn {value('profile_arn')} --role-arn {value('role_arn')}\n"
    )
    os.chmod(config, 0o600)
    print(json.dumps({"configured": True, "recipient_last_four": candidates[0][-4:]}))


if __name__ == "__main__":
    main()
