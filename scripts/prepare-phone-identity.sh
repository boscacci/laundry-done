#!/usr/bin/env bash
# Idempotent local certificate bootstrap. Private material stays outside Git.
set -euo pipefail
cd "$(dirname "$0")/.."
identity_dir="${LAUNDRY_IDENTITY_DIR:-$PWD/data/phone-identity}"
umask 077
mkdir -p "$identity_dir"
if [[ ! -f "$identity_dir/ca.pem" ]]; then
  openssl req -x509 -newkey rsa:3072 -nodes -keyout "$identity_dir/ca.key" -out "$identity_dir/ca.pem" -days 3650 -subj '/CN=Personal Phone Alerts CA' -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' >/dev/null 2>&1
fi
if [[ ! -f "$identity_dir/worker.key" ]]; then
  openssl req -newkey rsa:3072 -nodes -keyout "$identity_dir/worker.key" -out "$identity_dir/worker.csr" -subj '/CN=laundry-phone-worker-dev' >/dev/null 2>&1
fi
# Also repair an earlier certificate without key usage; never replace its key.
if [[ ! -f "$identity_dir/worker.pem" ]] || ! openssl x509 -in "$identity_dir/worker.pem" -noout -text | grep -q 'Digital Signature'; then
  extensions="$(mktemp "$identity_dir/.extensions.XXXXXX")"
  trap 'rm -f "$extensions"' EXIT
  printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n' > "$extensions"
  openssl x509 -req -in "$identity_dir/worker.csr" -CA "$identity_dir/ca.pem" -CAkey "$identity_dir/ca.key" -CAcreateserial -out "$identity_dir/worker.pem" -days 90 -sha256 -extfile "$extensions" >/dev/null 2>&1
fi
printf 'Identity prepared; worker certificate expires: '
openssl x509 -in "$identity_dir/worker.pem" -noout -enddate
