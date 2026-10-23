# Shared phone alerts: development and release

The implementation uses Amazon Connect, private prerecorded audio, an
IAM-authenticated dispatcher, and a transactional local outbox. See PLAN.md for
policy and the real-load acceptance gate. The destination is kept in Secrets
Manager; browser and device payloads cannot choose phone numbers.

## Development

1. `scripts/validate.sh` runs application/provider-policy tests and validates IaC.
2. `scripts/test-container.sh` also runs the native firmware suite in isolation.
3. `conda run -n dell python scripts/generate-audio.py` generates twelve immutable
   clips (ten variations, test, fallback), with hashes and duration checks.
4. Review and commit. `scripts/deploy-phone-dev.sh` provisions the dedicated AWS
   dev stack from that clean commit, with calling disabled, and uploads audio.
5. Set LAUNDRY_REGISTRY to the IaC repository_url output and run
   `scripts/release-dev.sh`. It builds/publishes once, deploys by digest, and runs
   browser smoke checks at http://127.0.0.1:18088/monitor with simulated calls.

Install browser dependencies with `npm ci --prefix tests/browser` and
`tests/browser/node_modules/.bin/playwright install chromium`, or set
PLAYWRIGHT_MODULE and PLAYWRIGHT_CHROMIUM to existing installations.

The first cold test/toolchain build can exceed 90 seconds because it downloads
pinned compilers. Separate builders isolate cleanup from other projects. Normal
application tests take about 50 seconds; do not remove checks to shorten a cold run.

## Recipient and real-call testing

After the number/dispatcher exists, configure the confirmed recipient without
printing it:

```sh
conda run -n dell python scripts/configure-phone-worker.py --existing-destination-ending LAST_FOUR
```

The setup selects exactly one previously configured AWS destination. Pending
End User Messaging verification does not establish Connect authorization; Connect
outbound permissions and actual phone playback must be tested separately.

The certificate bootstrap leaves CA material outside Git. Only the worker leaf
certificate/key/config are mounted into the runtime. Set their ownership to UID
10001 before running the worker; keep CA keys restricted to the operator.
Renew the 90-day worker certificate before expiry and update the mounted leaf
files. Revoking the trust anchor/profile disables future temporary credentials.

Do not enable automatic calling until actual phone tests and detector acceptance
pass. Test calls must be deliberately initiated. Development defaults to a
simulation worker; changing the AWS enable flag does not itself connect dev to it.

## Status and failure recovery

The UI separates packet freshness, activity evidence, session status, and delivery
outcomes. Queued jobs expire in 15 minutes. Start/Cancel/Pause/Resume controls are
idempotent; manual Start does not fabricate eight minutes of activity evidence.

An AWS timeout retries the same job and Connect client token. Unknown outcomes do
not create a second attempt. Only confirmed no-answer/retryable failure may queue
one second call after two minutes. Duplicate or late provider events must not
regress a completed job. Playback requires pressing 1 within eight seconds after answering. Before that
keypress, the call is silent: no instruction prompt, greeting, or laundry audio.
Missing or invalid input and input errors disconnect silently and are handled as
no-answer, with at most one retry. Never play a voicemail message.

The caller does not control whether a carrier creates an empty voicemail entry
for a silent connection. It never sends audio without the keypress. Probabilistic
answering-machine detection is not used as proof of a human answer.

Inspect CloudWatch Lambda errors and job state independently when the local
service is unavailable. The owner selected dashboard-only operational reporting;
there are no outage emails or operational calls.

## Production and rollback

The current implementation is a dev pilot, not a production-ready cutover.
Production cloud isolation, cloud/flow artifact promotion, physical load
acceptance, and failure-alarm injection remain tracked in PLAN.md. Do not use the
promotion scaffold until those prerequisites are complete.

Production is a separate release decision. Preserve the existing stack until
phone acceptance. Cut an annotated version tag on the default-branch dev-tested
commit. A durable human approval record identifies tag, exact image digest,
production target, approver/time, manual dev smoke result, and approval URL.
Run `scripts/promote.sh TAG APPROVAL_RECORD` with the exact approved digest;
there is no rebuild. Rollback uses a prior approved tag/digest and the same gate.

Before first non-root rollout, back up SQLite with its backup API and migrate
only the exact relay/worker data files' ownership to UID 10001. Discover the Caddy
peer and set LAUNDRY_TRUSTED_PROXY_IPS; never copy an entire private network into
the trust list. Restrict LAN administrative access while preserving signed ESP32
telemetry. Device OTA is between loads, with installed-image hash confirmation.

Historical done_sent events retain unverified delivery status and are not replayed.
Retire Gotify only after real phone and load acceptance, preserving its data and
all unrelated shared front-door routes.

## Runtime image security

The previous Bookworm runtime had seven HIGH, two MEDIUM, and two LOW ECR
findings. It is replaced with a digest-pinned Python 3.13 Alpine 3.24 image,
including patched zlib 1.3.2-r1 and no mount/nsenter utilities. The official AWS
signing helper runs with Alpine's gcompat library; verify real Roles Anywhere
credentials after runtime changes.

The release scripts require a completed ECR scan of the exact runtime manifest
with zero findings at every severity. Failed, missing, incomplete, or ambiguous
scans block deployment. The scan record binds the image index and runtime digest;
there are no ignored CVEs or severity exceptions. Re-scan on promotion as well.

The image runs as UID 10001, strips setuid/setgid file bits, and dev/release
services drop all capabilities, prohibit privilege escalation, and use read-only
root filesystems. Python runtime dependency auditing is a separate required check.
