#!/usr/bin/env python
"""Recover stale Connect inventory safely; import the single claimed number."""

import json
import subprocess
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def main():
    root = Path(__file__).resolve().parents[1]
    if subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=root, text=True
    ).strip():
        raise SystemExit("Clean reviewed commit required")
    state = json.loads(
        subprocess.check_output(
            ["tofu", "-chdir=infra/phone-alerts", "show", "-json"], cwd=root, text=True
        )
    )
    resources = state["values"]["root_module"]["resources"]
    if any(r["address"] == "aws_connect_phone_number.shared" for r in resources):
        print("Phone number already managed by IaC")
        return
    instance = next(
        r["values"]["arn"]
        for r in resources
        if r["address"] == "aws_connect_instance.shared"
    )
    client = boto3.client("connect", region_name="us-west-2")
    existing = client.list_phone_numbers_v2(TargetArn=instance, MaxResults=100)[
        "ListPhoneNumbersSummaryList"
    ]
    if len(existing) > 1:
        raise SystemExit(
            "Multiple numbers found; resolve ownership before claiming another"
        )
    arn = existing[0]["PhoneNumberArn"] if existing else None
    if arn is None:
        for prefix in ["+1206", "+1425", "+1650", "+1415", "+1"]:
            candidates = client.search_available_phone_numbers(
                TargetArn=instance,
                PhoneNumberCountryCode="US",
                PhoneNumberType="DID",
                PhoneNumberPrefix=prefix,
                MaxResults=10,
            )["AvailableNumbersList"]
            for candidate in candidates:
                try:
                    result = client.claim_phone_number(
                        TargetArn=instance,
                        PhoneNumber=candidate["PhoneNumber"],
                        PhoneNumberDescription="Shared personal project alert caller ID",
                        ClientToken=str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL, instance + candidate["PhoneNumber"]
                            )
                        ),
                        Tags={
                            "Project": "shared-phone-alerts",
                            "ManagedBy": "opentofu",
                            "Environment": "dev",
                        },
                    )
                    arn = result["PhoneNumberArn"]
                    break
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "InvalidParameterException":
                        raise
            if arn:
                break
    if arn is None:
        raise SystemExit("No claimable US number found; request AWS inventory support")
    # The CLI import reports only the ARN; destination and originating digits are
    # never written into this source file or printed by this script.
    subprocess.run(
        [
            "tofu",
            "-chdir=infra/phone-alerts",
            "import",
            "aws_connect_phone_number.shared",
            arn,
        ],
        cwd=root,
        check=True,
    )


if __name__ == "__main__":
    main()
