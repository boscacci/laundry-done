"""Release gate rejects findings, missing scans, and ambiguous platforms."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

spec = importlib.util.spec_from_file_location(
    "image_security",
    Path(__file__).resolve().parents[2] / "scripts/check-image-security.py",
)
security = importlib.util.module_from_spec(spec)
spec.loader.exec_module(security)


def test_clean_completed_scan_passes():
    assert (
        security.check_scan(
            {
                "imageScanStatus": {"status": "COMPLETE"},
                "imageScanFindings": {"findingSeverityCounts": {}},
            }
        )
        == {}
    )


@pytest.mark.parametrize(
    "severity", ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNDEFINED", "INFORMATIONAL"]
)
def test_every_reported_vulnerability_blocks_release(severity):
    with pytest.raises(ValueError, match="vulnerabilities"):
        security.check_scan(
            {
                "imageScanStatus": {"status": "COMPLETE"},
                "imageScanFindings": {"findingSeverityCounts": {severity: 1}},
            }
        )


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"imageScanStatus": {"status": "FAILED"}},
        {"imageScanStatus": {"status": "COMPLETE"}},
        {"imageScanStatus": {"status": "IN_PROGRESS"}},
    ],
)
def test_missing_or_incomplete_scan_never_passes(response):
    with pytest.raises(ValueError, match="scan"):
        security.check_scan(response)


def test_resolves_runtime_manifest_instead_of_attestation():
    import json

    ecr = SimpleNamespace(
        batch_get_image=lambda **kw: {
            "images": [
                {
                    "imageId": {"imageDigest": "sha256:index"},
                    "imageManifest": json.dumps(
                        {
                            "manifests": [
                                {
                                    "digest": "sha256:attestation",
                                    "platform": {
                                        "architecture": "unknown",
                                        "os": "unknown",
                                    },
                                },
                                {
                                    "digest": "sha256:runtime",
                                    "platform": {
                                        "architecture": "amd64",
                                        "os": "linux",
                                    },
                                },
                            ]
                        }
                    ),
                }
            ]
        }
    )
    assert security.runtime_digest(ecr, "repo", "tag") == (
        "sha256:index",
        "sha256:runtime",
    )


def test_scan_rejects_image_that_differs_from_approved_digest():
    ecr = SimpleNamespace(
        batch_get_image=lambda **kw: {
            "images": [
                {"imageId": {"imageDigest": "sha256:other"}, "imageManifest": "{}"}
            ]
        }
    )
    with pytest.raises(ValueError, match="approved digest"):
        security.runtime_digest(ecr, "repo", "tag", expected="sha256:approved")
