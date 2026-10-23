#!/usr/bin/env python
"""Fail closed on incomplete scans or any ECR vulnerability finding."""

import argparse
import json
import time
from pathlib import Path

import boto3


def runtime_digest(ecr, repository, tag, expected=None):
    result = ecr.batch_get_image(
        repositoryName=repository, imageIds=[{"imageTag": tag}]
    )
    images = result.get("images", [])
    if len(images) != 1:
        raise ValueError("exact release image is missing")
    parent = images[0]["imageId"]["imageDigest"]
    if expected is not None and parent != expected:
        raise ValueError("scan image differs from approved digest")
    manifest = json.loads(images[0]["imageManifest"])
    if "manifests" not in manifest:
        return parent, parent
    runtimes = [
        m["digest"]
        for m in manifest["manifests"]
        if m.get("platform") == {"architecture": "amd64", "os": "linux"}
    ]
    if len(runtimes) != 1:
        raise ValueError("ambiguous runtime platform")
    return parent, runtimes[0]


def check_scan(result):
    if result.get("imageScanStatus", {}).get("status") != "COMPLETE":
        raise ValueError("security scan is not complete")
    counts = result.get("imageScanFindings", {}).get("findingSeverityCounts")
    if not isinstance(counts, dict):
        raise ValueError("security scan has no finding counts")
    if any(value != 0 for value in counts.values()):
        raise ValueError(
            "image has vulnerabilities: " + json.dumps(counts, sort_keys=True)
        )
    return counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repository", default="laundry-done")
    p.add_argument("--tag", required=True)
    p.add_argument("--record", required=True)
    p.add_argument("--expected-digest", required=True)
    args = p.parse_args()
    ecr = boto3.client("ecr", region_name="us-west-2")
    parent, runtime = runtime_digest(
        ecr, args.repository, args.tag, args.expected_digest
    )
    deadline = time.monotonic() + 180
    while True:
        try:
            result = ecr.describe_image_scan_findings(
                repositoryName=args.repository, imageId={"imageDigest": runtime}
            )
        except ecr.exceptions.ScanNotFoundException:
            result = {"imageScanStatus": {"status": "PENDING"}}
        status = result.get("imageScanStatus", {}).get("status")
        if status in {"PENDING", "IN_PROGRESS"} and time.monotonic() < deadline:
            time.sleep(3)
            continue
        counts = check_scan(result)
        record = {
            "image_digest": parent,
            "runtime_digest": runtime,
            "scan": status,
            "findings": counts,
        }
        Path(args.record).write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record))
        return


if __name__ == "__main__":
    main()
