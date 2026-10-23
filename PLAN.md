# Reliable Laundry Detection and Shared Phone Alerts

Status: implementation in progress, October 22, 2026. Automated validation and
dev deployment precede real-phone and owner-observed load acceptance. Production
cutover and Gotify retirement remain gated on that acceptance.

### Current delivery status

Implemented: durable completion/outbox, bounded monitoring leases, authenticated
dashboard controls, simulated dev worker, shared AWS Connect number, private
prerecorded audio library, IAM-authenticated dispatcher, short-lived worker
credentials, deployment scripts, and isolated dev browser smoke tests.

The owner confirmed hearing the complete prerecorded real-phone test on October
22. AWS recorded completed playback on attempt one. This proves answered-call
delivery under the original flow. The new silent keypad gate requires a fresh
answered-call test and an unanswered-call test.

Remaining acceptance and release work:

- Verify that voicemail receives no audio, a human keypress unlocks playback,
  and unanswered calls retry once. Test carrier filtering and Do Not Disturb. Listen to every clip; generation and format checks alone do
  not establish intelligibility.
- Capture three washer and three dryer cycles, freeze detector parameters, and
  validate independent captures. Validate the monitoring lease on the physical
  power bank and install firmware between loads.
- Finish production cloud isolation and promotion of versioned cloud/flow/audio
  artifacts; current cloud deployment is dev. Do not point production at dev.
- Complete failure-alarm injection and remaining operational metrics, cost
  visibility, certificate renewal, and production release/rollback exercises.
- Require a completed image scan with zero vulnerability findings before release.
- Merge the validated release candidate, obtain one recorded production approval
  after manual dev smoke, deploy, observe real loads, then retire Gotify.

Production, device firmware, and existing Gotify hosting remain unchanged.

## Goal and agreed behavior

Recognize finished laundry reliably, call the owner's US cell phone, and make
monitoring and delivery failures visible. Replace Gotify as the primary alert.

- Pre-generate ten short audio messages; reuse them without synthesis or an LLM
  request during calls. Goofy wording is welcome, but the meaning must stay clear.
- Use one dedicated originating number for laundry and future personal projects.
- Call at any hour. Retry once after a confirmed failed or unanswered call.
- Never leave a voicemail message. Wait silently for the owner to press 1 before
  playback; disconnect silently after eight seconds without valid input.
- Keep automatic load detection with manual Start load and Cancel controls.
- Favor reliable monitoring over battery savings.
- Show operational failures in dashboards; do not add email or operational calls.

## 1. Shared calling service and reusable recordings

### Architecture

Use Amazon Connect in the existing AWS account, initially targeting us-west-2,
with one US number and a small reusable outbound flow. The flow plays a selected
private S3 recording, records the outcome, and disconnects; no human agent is needed.
Configure inbound calls to play a brief identification message and disconnect.

This replaces the earlier AWS End User Messaging proposal. That API accepts text
for Polly synthesis; Connect can play prerecorded S3 audio. Its outbound API also
supports a client idempotency token, useful for safe submission retries.

Sources: [Connect S3 prompts](https://docs.aws.amazon.com/connect/latest/adminguide/setup-prompts-s3.html),
[outbound calls](https://docs.aws.amazon.com/connect/latest/APIReference/API_StartOutboundVoiceContact.html).

```text
Sensor -> Relay -> SQLite completion + outbox -> Local notification worker
                                                    |
                                           IAM-authenticated request
                                                    |
                                Shared AWS call dispatcher + durable job store
                                                    |
                              Amazon Connect -> Private S3 audio -> Cell phone
                                                    |
                                      Call events + playback outcome
                                                    |
                                       Job status -> Local dashboard
```

Build the shared dispatcher with API Gateway IAM authorization, Lambda, and
DynamoDB. Connect flow callbacks and contact events update durable call status.
Use EventBridge to consume supported Connect contact events; record successful
playback explicitly after the Play prompt block. A disconnected call alone is
not proof that playback completed.

The interface accepts `project_id`, `event_id`, `recipient_id`, and `message_key`.
It returns a durable job ID and exposes authenticated status lookup and cancellation.
Resolve phone numbers and audio paths from server-side configuration. Callers must
not supply arbitrary destinations, S3 URLs, flow IDs, or caller IDs.

Laundry is the first registered project. Future projects get separate IAM
permissions, approved recipients, audio prefixes, usage accounting, and quotas.
Keep the shared number and core resources in a separate IaC state so removing
laundry cannot delete another project's calling service. Do not claim the number
is portable across AWS products without a separate migration assessment.

### Audio library

Generate the clips once with Polly, listen to each, normalize loudness, and convert
to Connect's required 8 kHz mono mu-law WAV format. Keep each under 20 seconds.
Store immutable, versioned objects in a private S3 bucket. Keep scripts, generation
settings, duration, and content hashes in a version-controlled manifest; publish
audio as release assets. No call recordings or microphone capture are required.

Choose a clip once when the notification job is created. Avoid the immediately
previous variant for that recipient, persist the choice, and reuse it on retries.
Keep one plain fallback recording for missing or invalid assets. Validate the
fallback before enabling calls; if both assets fail, report failure visibly.

Initial script candidates, each preceded by “Laundry update”:

1. “Your laundry appears finished. The stack has gone quiet. Time to check your load.”
2. “The spin council has adjourned. Your laundry appears finished. Please check the load.”
3. “Your socks have requested extraction. The laundry stack is quiet. Please check your load.”
4. “Attention, captain. The laundry mission appears complete. Retrieve the textiles.”
5. “The clothes have stopped doing whatever clothes do in there. Your load may be ready.”
6. “This is your extremely small laundry department. The stack is quiet. Please investigate.”
7. “The textile situation has stabilized. Your laundry appears finished. Go check it.”
8. “A tiny robot believes your laundry is done. The robot requests human verification.”
9. “Your laundry has entered its mysterious silent era. Please check whether the load is finished.”
10. “The laundry goblin reports silence. Your load appears finished. Please collect your belongings.”

Preview clips in the dashboard. Do not infer washer versus dryer from amplitude,
or claim clothes are dry. Appliance-specific libraries can follow explicit user
selection and separate validation.

### Calling policy and provisioning

- Submit the first call within 30 seconds of confirmed completion when available.
- Allow two attempts per completion: initial call plus one retry, two minutes
  after a confirmed busy, unanswered, or retryable failed result.
- Expire jobs 15 minutes after completion. Cancel pending attempts on resumed
  motion or cancellation; do not retry a completed playback.
- Use a stable Connect `ClientToken` per attempt. Transport retries reuse it;
  a deliberate second call gets a new attempt ID and token.
- Reconcile unknown outcomes; never convert missing events into a fresh call.
- Require an explicit human keypress before any outbound audio. Do not rely on
  probabilistic answering-machine detection. Missing/invalid input or an input
  error disconnects silently and permits the same bounded retry as no-answer.
  Show playback completion separately from merely answering the phone.

Verify Connect-specific availability, quotas, destination permissions, and number
inventory before provisioning. The earlier End User Messaging sandbox and $1 limit
are not evidence about Connect readiness. Quote the number, Connect usage,
telephony, storage, and monitoring costs against 60 loads and at most 120 calls
per month. Keep a $10/month planning target, subject to actual pricing.

Apply per-project and shared daily limits: laundry gets ten attempts/day and the
shared service starts at twenty/day. Add budget visibility and a global disable
switch. Budget warnings are not a guaranteed hard billing cutoff.

Confirm the actual recipient securely during setup. Test answering, voicemail,
no-answer, carrier spam filtering, and Do Not Disturb on the real phone. Add the
originating number to contacts. Answer and press 1 within eight seconds to hear
the recording. Confirm that unanswered calls play no prompts or messages.

## 2. Reliable detection and durable delivery

The October 22 audit passed 102 relay and 38 native firmware tests. Live readings
nevertheless classified the running washer as quiet at times. Completion had only
6m47s of qualifying activity against an eight-minute threshold. The deployed relay
also lacks the committed large-jolt smoothing correction.

Ship that correction through the release workflow. Capture labeled complete
washer and dryer runs, including fill, soak, agitation, spin, pauses, actual stops,
incidental bumps, and both appliances operating. Preserve raw/invalid readings;
missing telemetry is never quiet evidence.

Separate Waiting, Activity detected, Monitoring, Quiet confirmation, and Complete
from Insufficient evidence, Telemetry lost, and Monitoring expired. Keep current
thresholds as a baseline; calibrate from training captures and validate frozen
parameters on independent runs. Do not simply lower thresholds until one example
passes. If running and stopped remain inseparable, improve mounting or add separate
appliance sensors before claiming reliable individual detection.

Each load gets an episode ID distinct from device boot/session IDs. Completion
and its outbox job must commit in one SQLite transaction. The current code stores
`done_sent` before delivery succeeds; replace this with explicit detection,
submission, and outcome records. Preserve historical rows as legacy unverified
delivery and never backfill them into new calls.

The local worker retries submission safely using the same shared job identity.
The dispatcher deduplicates by project, episode, notification type, and attempt.
Handle duplicate/out-of-order events and worker crashes without losing accepted
jobs or placing duplicate calls. Store phone numbers in Secrets Manager and keep
numbers and credentials out of ordinary logs.

## 3. Power behavior and usable status

Automatic detection or Start load creates a bounded monitoring session. Keep the
sensor reporting through quiet fill/pause phases instead of releasing battery
support after five quiet minutes. Default maximum duration is four hours; expiry
must report Monitoring expired, never Laundry done.

Release power support after confirmed completion, cancellation, or expiry. Use
authenticated, versioned commands in telemetry responses, tied to device/session
with replay protection. Continue bounded local monitoring during connectivity
loss. Validate the policy on the actual bank and support USB power. OTA remains
between loads, with installed hash verification.

The dashboard must separately show:

- Sensor connectivity: packet age, signal, reboot history, and power mode.
- Load evidence: activity accumulated, quiet countdown, and blocked-completion reason.
- Delivery: selected clip, queued/submitted status, attempts, playback result, expiry,
  and actionable failure details.

During monitoring, replace “Quiet / off” with “Low vibration — may be filling or
paused.” Add Start load, Cancel, Pause calls, clip preview, and an explicit Test
call action. Mask the recipient. Preserve mobile layout and existing chart controls.

Add authenticated status/control APIs under `/api/v1`; preserve telemetry
compatibility. Validate mutation origins and idempotency keys. Only trusted proxies
may supply identity headers; LAN telemetry access must not grant administration.

## 4. Release engineering, security, and operations

Use OpenTofu with remote state for durable AWS resources. Use separate dev/prod
roles, storage, flows, and job namespaces. Default dev to simulated calling; real
dev calls require explicit test actions to the allowlisted owner number.

Give the home worker short-lived credentials through IAM Roles Anywhere rather
than mounting broad personal AWS credentials. Grant each project access only to
its registered calling operations. Use a small shared admin role to configure
projects, recipients, recordings, and limits.

Create checked-in scripts that validate a clean commit, build a minimal non-root
image once, publish it to ECR, record the digest, deploy automatically to isolated
dev, and run Playwright. Version cloud code/flows and audio alongside the release.
Promote the same dev-tested artifacts after one recorded production approval.
Keep immutable release tags, rollback instructions, schema compatibility, and
task-scoped image/cache cleanup. Do not OTA during a running load.

Track sensor freshness, worker/cloud heartbeat, queue age, submission latency,
failed/unknown calls, quota blocks, playback errors, and deployed-version drift.
Keep structured logs with project/episode/attempt IDs and sanitized errors. Retain
application delivery history for 30 days; restrict provider records containing PII.

Provide dashboard alarms and an independently accessible AWS health view. Per the
owner's preference, do not send email or operational phone alerts. A total local
outage therefore requires checking the cloud view; it cannot announce itself
through an unavailable local dashboard.

Retire Gotify only after phone acceptance: archive its data, disable its sender,
remove dedicated hosting and credentials, and update documentation. Preserve
shared Tailscale/SNI infrastructure and other projects' resources.

## 5. Implementation order and acceptance

1. Add episode IDs, transactional outbox, migrations, and delivery regressions.
2. Provision the shared calling service, number, audio library, and real-phone proof.
3. Implement detector/power changes, labeled replay validation, and dashboard controls.
4. Complete dev deployment, observability, rollback, and security checks.
5. Promote an approved production release, observe complete loads, then retire Gotify.

Automated tests must cover provider errors, restarts around submission, concurrent
workers, stable idempotency, duplicate/out-of-order callbacks, one-retry limits,
expiry, cancellation, resumed motion, and unknown outcomes. Test cross-project
authorization, arbitrary destination/asset rejection, shared rate limits, missing
audio fallback, and consistent clip selection across retries.

Replay fill/pause, handling noise, invalid readings, disconnects, reboot, rearming,
and session expiry. Run all existing suites, firmware compilation, appropriate
dependency/security checks, and repeatable desktop/mobile Playwright tests against dev.

Acceptance requires three independently observed washer and three dryer cycles,
including gentle washing, with no premature or missed completions. Target a
decision within ten minutes of the observed final stop. Concurrent appliances are
stack monitoring, not guaranteed individual-appliance completion.

Verify real human-confirmed playback and silent voicemail handling, at most one
unanswered-call retry,
accurate failure displays, and rollback without historical notification replay.
Test all ten recordings over the playback path without making ten unsolicited
calls. Completion means real laundry produces an intelligible phone call—not
merely a passing test suite or successful API submission.
