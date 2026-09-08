# Measured-noise classification correction

This branch corrects the detector using owner-labeled 2026-09-07 readings. It is
not yet a validated washer-versus-dryer classifier. A single sensor on a shared
stack cannot identify the running appliance from vibration strength alone, and
one appliance can mask the other stopping. Notifications therefore say
`Laundry stack stopped`, not an inferred washer/dryer identity.

## Evidence and reproducibility

The credential-free CSV fixtures under `tests/fixtures` contain all delivered
windows in each stated interval, with relative device uptime, RMS and peak in mg.
No outliers were removed and missing packets were not interpolated.

- `desk_noise.csv`: stationary USB-powered desk, 2026-09-07 21:58:04–22:03:04 UTC,
  17 packets. RMS 1.877–3.151 mg; peak 4.086–15.869 mg. Four packet sequence gaps.
- `dryer_finishing.csv`: mounted on the stack's side, owner reports dryer finishing
  and washer off, 2026-09-07 22:14:01–22:24:01 UTC. This is a running capture,
  **not** a stopped-machine baseline or a complete dryer cycle.

Both were selected read-only from relay SQLite `events`, filtered by the device,
`state='calibration_sample'` and the half-open `received_at` intervals above,
ordered by `received_at`. Source measurements are `raw_json.motion_rms_mg`,
`raw_json.peak_mg` and `raw_json.uptime_ms`. RMS means successive XYZ acceleration
differences over four seconds, not absolute acceleration or a frequency spectrum.

## Candidate behavior

- Production firmware and relay: active at RMS >= 4 mg **or** peak >= 20 mg;
  quiet at RMS <= 3.5 mg with peak < 20 mg. The gap is uncertain, not quiet.
- The old 2 mg / 4.5 mg cadence thresholds treated desk noise as running. Old
  gentle-washer fixtures overlap that same noise and cannot prove separability.
- The relay retains eight-window smoothing, eight minutes of observed activity,
  and four minutes of raw quiet confirmed by the smoothed signal. A long upload
  gap, reboot or invalid sample is not quiet evidence. Raw noise spikes inside
  the measured envelope no longer restart the countdown.
- The production local detector is a power/cadence controller only. After five
  minutes of quiet it releases keepalive, regardless of whether a brief apparent
  run qualified for notification. It does not send its own completion alert.
  The extra minute gives the relay time beyond its four-minute decision window;
  any active or uncertain reading restarts the local quiet budget as well, so a
  late interruption cannot leave the relay counting down after local power-off.
  it is not a guarantee when uploads fail. Initial startup allowance stays five
  minutes, with only the first thirty seconds continuously awake.
- Missing/invalid sensor readings are not treated as stopped. New telemetry adds
  `sample_valid` and reports actual rather than nominal sample counts. Older
  firmware has no validity flag, so a plausible zero reading cannot be diagnosed
  retroactively from its stored RMS alone.

## Validation and deployment gate

Run `conda run -n sr python -m pytest server/tests`, `pio test -e native` on a
host with GCC, and `pio run -e esp32dev`. Both native and relay regressions replay
the same measured fixtures; browser fallback classifications are executed in Node.
Tests simulate a complete run followed by real desk noise and assert one alert
at four quiet minutes, without sending real Gotify messages.

Before production promotion, capture the mounted stack after the owner confirms
the drum has stopped, then validate a real quiet-to-running-to-quiet transition.
Do not OTA during a load. The running capture confirms a large separation from
desk noise, but stopped mounted noise and gentle washer motion are still unproven.
Do not silently raise thresholds further if those overlap: mounting, sampling
quality or richer signal features may be required. A device started near the end
of a cycle may never collect eight minutes of activity and therefore will not arm.

The ignored desktop notebook/snapshot from the desk capture remain under
`outputs/desk-noise-2026-09-07`. Firmware binaries contain credentials and must
never be committed or served as plain files.
