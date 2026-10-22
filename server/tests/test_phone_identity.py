"""The issued identity must satisfy AWS Roles Anywhere's X.509 requirements."""

import os
import subprocess
from pathlib import Path
from cryptography import x509


def test_identity_bootstrap_issues_v3_digital_signature_leaf_and_is_idempotent(
    tmp_path,
):
    script = Path(__file__).resolve().parents[2] / "scripts/prepare-phone-identity.sh"
    env = {**os.environ, "LAUNDRY_IDENTITY_DIR": str(tmp_path)}
    subprocess.run([str(script)], env=env, check=True, capture_output=True)
    leaf = (tmp_path / "worker.pem").read_bytes()
    cert = x509.load_pem_x509_certificate(leaf)
    assert cert.version == x509.Version.v3
    assert cert.extensions.get_extension_for_class(
        x509.KeyUsage
    ).value.digital_signature
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    subprocess.run([str(script)], env=env, check=True, capture_output=True)
    assert (tmp_path / "worker.pem").read_bytes() == leaf
