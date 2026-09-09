#include <unity.h>
#include <initializer_list>
#include <fstream>
#include <cstdio>
#include <string>
#include <vector>
#include <limits>

#include "laundry_detector.h"

void setUp() {}
void tearDown() {}

static std::vector<MotionWindow> measured_windows(const char *path, size_t expected_count) {
  std::ifstream file(path);
  TEST_ASSERT_TRUE_MESSAGE(file.good(), "Measured desk fixture must be available");
  std::string line;
  std::getline(file, line);
  std::vector<MotionWindow> windows;
  while (std::getline(file, line)) {
    unsigned long t;
    float rms, peak;
    TEST_ASSERT_EQUAL(3, std::sscanf(line.c_str(), "%lu,%f,%f", &t, &rms, &peak));
    windows.push_back(MotionWindow{t, 4, rms, peak});
  }
  TEST_ASSERT_EQUAL(expected_count, windows.size());
  return windows;
}

static std::vector<MotionWindow> desk_noise() {
  return measured_windows("tests/fixtures/desk_noise.csv", 17);
}

void test_entire_measured_dryer_capture_stays_running() {
  LaundryDetector detector(telemetry_cadence_detector_config());
  for (const auto &window : measured_windows("tests/fixtures/dryer_finishing.csv", 41)) {
    const Decision result = detector.observe(window);
    if (window.at_ms >= 30000UL) TEST_ASSERT_EQUAL(DetectorState::CycleRunning, result.state);
    TEST_ASSERT_FALSE(result.should_post);
  }
}

void test_mounted_off_capture_never_confirms_motion() {
  LaundryDetector detector(telemetry_cadence_detector_config());
  for (const auto &window : measured_windows("tests/fixtures/mounted_off.csv", 22)) {
    const Decision result = detector.observe(window);
    TEST_ASSERT_NOT_EQUAL(DetectorState::CycleRunning, result.state);
    TEST_ASSERT_FALSE(result.should_post);
  }
  TEST_ASSERT_EQUAL(DetectorState::Idle, detector.state());
}

void test_measured_desk_noise_never_arms_a_cycle() {
  LaundryDetector detector(telemetry_cadence_detector_config());
  for (const auto &window : desk_noise()) {
    const Decision decision = detector.observe(window);
    TEST_ASSERT_EQUAL(DetectorState::Idle, decision.state);
    TEST_ASSERT_FALSE(decision.should_post);
  }
}

void test_measured_noise_after_tumbling_releases_power_without_device_alert() {
  LaundryDetector detector(telemetry_cadence_detector_config());
  for (unsigned long t = 0; t <= 600000UL; t += 10000UL) {
    detector.observe(MotionWindow{t, 4, 42.6087f, 106.9219f});
  }
  TEST_ASSERT_EQUAL(DetectorState::CycleRunning, detector.state());
  // Repeated measured noise, including all spikes, must not restart motion.
  unsigned long t = 610000UL;
  for (int repeat = 0; repeat < 3; repeat++) {
    for (auto window : desk_noise()) {
      window.at_ms = t;
      t += 10000UL;
      const Decision result = detector.observe(window);
      TEST_ASSERT_FALSE(result.should_post);  // Relay owns notification evidence.
    }
  }
  TEST_ASSERT_EQUAL(DetectorState::DoneSent, detector.state());
  TEST_ASSERT_FALSE(battery_keepalive_allowed(t, detector.state(), TelemetryCadenceConfig{}));
}

void test_invalid_sensor_windows_do_not_count_as_quiet() {
  LaundryDetector detector(telemetry_cadence_detector_config());
  detector.observe(MotionWindow{0, 4, 42, 100});
  detector.observe(MotionWindow{30000, 4, 42, 100});
  for (unsigned long t = 40000; t < 700000; t += 10000) {
    const Decision result = detector.observe(MotionWindow{t, 0, 0, 0});
    TEST_ASSERT_EQUAL(DetectorState::CycleRunning, result.state);
    TEST_ASSERT_FALSE(result.should_post);
  }
  const float nan = std::numeric_limits<float>::quiet_NaN();
  for (auto window : {MotionWindow{700000, 4, nan, nan},
                     MotionWindow{710000, 4, -1, 2}, MotionWindow{720000, 4, 3, 2}}) {
    TEST_ASSERT_EQUAL(DetectorState::CycleRunning, detector.observe(window).state);
  }
}

void test_cadence_keeps_power_for_restarted_server_quiet_countdown() {
  for (float interruption_rms : {3.75f, 42.0f}) {
    LaundryDetector detector(telemetry_cadence_detector_config());
    detector.observe(MotionWindow{0, 4, 42, 100});
    detector.observe(MotionWindow{30000, 4, 42, 100});
    detector.observe(MotionWindow{40000, 4, 2.5f, 6});
    // A late ambiguous or active reading restarts the server's quiet timer.
    detector.observe(MotionWindow{320000, 4, interruption_rms, 100 * (interruption_rms / 42)});
    const Decision shortly_after = detector.observe(MotionWindow{350000, 4, 2.5f, 6});
    TEST_ASSERT_EQUAL(DetectorState::QuietCandidate, shortly_after.state);
    TEST_ASSERT_TRUE(battery_keepalive_allowed(350000, shortly_after.state, TelemetryCadenceConfig{}));
    TEST_ASSERT_EQUAL(DetectorState::DoneSent,
        detector.observe(MotionWindow{650000, 4, 2.5f, 6}).state);
  }
}

static MotionWindow quiet(unsigned long at_ms) {
  return MotionWindow{at_ms, 4, 0.9f, 2.0f};
}

static MotionWindow washer(unsigned long at_ms) {
  return MotionWindow{at_ms, 4, 74.0f, 160.0f};
}

static MotionWindow dryer(unsigned long at_ms) {
  return MotionWindow{at_ms, 4, 34.0f, 44.0f};
}

static MotionWindow bedding_wash(unsigned long at_ms, float rms_mg = 4.1f, float peak_mg = 8.1f) {
  return MotionWindow{at_ms, 4, rms_mg, peak_mg};
}

static MotionWindow isolated_peak_jolt(unsigned long at_ms) {
  return MotionWindow{at_ms, 4, 2.4f, 16.0f};
}

static DetectorConfig unsmoothed_config() {
  DetectorConfig config;
  config.classification_smoothing_samples = 1;
  return config;
}

void test_washer_cycle_alerts_once_after_quiet_period() {
  DetectorConfig config = unsmoothed_config();
  config.minimum_washer_runtime_ms = 8UL * 60UL * 1000UL;
  config.done_quiet_ms = 10UL * 60UL * 1000UL;
  LaundryDetector detector(config);

  Decision first = detector.observe(washer(0));
  TEST_ASSERT_EQUAL(DetectorState::MotionConfirming, first.state);
  TEST_ASSERT_FALSE(first.should_post);

  Decision running = detector.observe(washer(9UL * 60UL * 1000UL));
  TEST_ASSERT_EQUAL(DetectorState::CycleRunning, running.state);
  TEST_ASSERT_FALSE(running.should_post);

  Decision quieting = detector.observe(quiet(15UL * 60UL * 1000UL));
  TEST_ASSERT_EQUAL(DetectorState::QuietCandidate, quieting.state);
  TEST_ASSERT_FALSE(quieting.should_post);

  Decision done = detector.observe(quiet(25UL * 60UL * 1000UL + 1UL));
  TEST_ASSERT_EQUAL(DetectorState::DoneSent, done.state);
  TEST_ASSERT_TRUE(done.should_post);
  TEST_ASSERT_EQUAL(CycleLabel::Washer, done.label);

  Decision duplicate = detector.observe(quiet(26UL * 60UL * 1000UL));
  TEST_ASSERT_EQUAL(DetectorState::DoneSent, duplicate.state);
  TEST_ASSERT_FALSE(duplicate.should_post);
}

void test_dryer_after_washer_is_labeled_dryer() {
  DetectorConfig config = unsmoothed_config();
  config.done_quiet_ms = 10UL * 60UL * 1000UL;
  LaundryDetector detector(config);

  detector.observe(washer(0));
  detector.observe(washer(9UL * 60UL * 1000UL));
  detector.observe(quiet(20UL * 60UL * 1000UL));
  Decision washer_done = detector.observe(quiet(31UL * 60UL * 1000UL));
  TEST_ASSERT_TRUE(washer_done.should_post);
  TEST_ASSERT_EQUAL(CycleLabel::Washer, washer_done.label);

  Decision dryer_start = detector.observe(dryer(38UL * 60UL * 1000UL));
  TEST_ASSERT_EQUAL(DetectorState::MotionConfirming, dryer_start.state);

  detector.observe(dryer(50UL * 60UL * 1000UL));
  detector.observe(quiet(65UL * 60UL * 1000UL));
  Decision dryer_done = detector.observe(quiet(76UL * 60UL * 1000UL));
  TEST_ASSERT_TRUE(dryer_done.should_post);
  TEST_ASSERT_EQUAL(CycleLabel::Dryer, dryer_done.label);
}

void test_ambiguous_tandem_motion_gets_stack_label() {
  DetectorConfig config = unsmoothed_config();
  LaundryDetector detector(config);

  detector.observe(MotionWindow{0, 4, 52.0f, 68.0f});
  detector.observe(MotionWindow{12UL * 60UL * 1000UL, 4, 55.0f, 70.0f});
  detector.observe(quiet(35UL * 60UL * 1000UL));
  Decision done = detector.observe(quiet(46UL * 60UL * 1000UL));

  TEST_ASSERT_TRUE(done.should_post);
  TEST_ASSERT_EQUAL(CycleLabel::Stack, done.label);
}

void test_short_bump_is_ignored() {
  DetectorConfig config = unsmoothed_config();
  LaundryDetector detector(config);

  Decision bump = detector.observe(MotionWindow{0, 4, 42.0f, 45.0f});
  TEST_ASSERT_EQUAL(DetectorState::MotionConfirming, bump.state);

  Decision quiet_again = detector.observe(quiet(30UL * 1000UL));
  TEST_ASSERT_EQUAL(DetectorState::Idle, quiet_again.state);
  TEST_ASSERT_FALSE(quiet_again.should_post);
}

void test_default_classification_uses_smoothed_recent_samples() {
  LaundryDetector detector;

  for (unsigned int index = 0; index < 7; index++) {
    Decision quiet_sample = detector.observe(quiet(index * 10UL * 1000UL));
    TEST_ASSERT_EQUAL(DetectorState::Idle, quiet_sample.state);
  }

  Decision noisy_sample = detector.observe(MotionWindow{70UL * 1000UL, 4, 10.0f, 24.0f});

  TEST_ASSERT_EQUAL(DetectorState::Idle, noisy_sample.state);
  TEST_ASSERT_FALSE(noisy_sample.should_post);
}

void test_calibrated_bedding_wash_signal_is_treated_as_running() {
  LaundryDetector detector;

  Decision first = detector.observe(bedding_wash(0));
  TEST_ASSERT_EQUAL(DetectorState::MotionConfirming, first.state);
  TEST_ASSERT_FALSE(first.should_post);

  Decision running = detector.observe(bedding_wash(61UL * 1000UL, 2.86f, 8.11f));
  TEST_ASSERT_EQUAL(DetectorState::CycleRunning, running.state);
  TEST_ASSERT_FALSE(running.should_post);

  Decision gentle_running = detector.observe(bedding_wash(5UL * 60UL * 1000UL, 1.9f, 7.9f));
  TEST_ASSERT_EQUAL(DetectorState::CycleRunning, gentle_running.state);
  TEST_ASSERT_FALSE(gentle_running.should_post);
}

void test_low_washer_motion_without_spin_peak_uses_stack_label() {
  DetectorConfig config = unsmoothed_config();
  config.minimum_washer_runtime_ms = 8UL * 60UL * 1000UL;
  config.done_quiet_ms = 10UL * 60UL * 1000UL;
  LaundryDetector detector(config);

  detector.observe(bedding_wash(0));
  detector.observe(bedding_wash(9UL * 60UL * 1000UL, 3.8f, 8.2f));
  detector.observe(quiet(20UL * 60UL * 1000UL));
  Decision done = detector.observe(quiet(31UL * 60UL * 1000UL));

  TEST_ASSERT_TRUE(done.should_post);
  TEST_ASSERT_EQUAL(CycleLabel::Stack, done.label);
}

void test_quiet_candidate_ignores_isolated_peak_jolt_before_done() {
  DetectorConfig config = unsmoothed_config();
  config.minimum_washer_runtime_ms = 8UL * 60UL * 1000UL;
  config.done_quiet_ms = 10UL * 60UL * 1000UL;
  LaundryDetector detector(config);

  detector.observe(washer(0));
  detector.observe(washer(9UL * 60UL * 1000UL));

  Decision quieting = detector.observe(quiet(15UL * 60UL * 1000UL));
  TEST_ASSERT_EQUAL(DetectorState::QuietCandidate, quieting.state);

  Decision jolt = detector.observe(isolated_peak_jolt(20UL * 60UL * 1000UL));
  TEST_ASSERT_EQUAL(DetectorState::QuietCandidate, jolt.state);
  TEST_ASSERT_FALSE(jolt.should_post);

  Decision done = detector.observe(quiet(25UL * 60UL * 1000UL + 1UL));
  TEST_ASSERT_EQUAL(DetectorState::DoneSent, done.state);
  TEST_ASSERT_TRUE(done.should_post);
  TEST_ASSERT_EQUAL(CycleLabel::Washer, done.label);
}

void test_quiet_candidate_ignores_repeated_peak_only_noise_before_done() {
  DetectorConfig config = unsmoothed_config();
  config.minimum_dryer_runtime_ms = 10UL * 60UL * 1000UL;
  config.done_quiet_ms = 10UL * 60UL * 1000UL;
  LaundryDetector detector(config);

  detector.observe(dryer(0));
  detector.observe(dryer(10UL * 60UL * 1000UL));

  Decision quieting = detector.observe(quiet(15UL * 60UL * 1000UL));
  TEST_ASSERT_EQUAL(DetectorState::QuietCandidate, quieting.state);

  detector.observe(isolated_peak_jolt(19UL * 60UL * 1000UL));
  detector.observe(quiet(19UL * 60UL * 1000UL + 45UL * 1000UL));
  detector.observe(isolated_peak_jolt(20UL * 60UL * 1000UL));
  detector.observe(quiet(20UL * 60UL * 1000UL + 15UL * 1000UL));
  detector.observe(isolated_peak_jolt(24UL * 60UL * 1000UL + 45UL * 1000UL));

  Decision done = detector.observe(quiet(25UL * 60UL * 1000UL + 1UL));
  TEST_ASSERT_EQUAL(DetectorState::DoneSent, done.state);
  TEST_ASSERT_TRUE(done.should_post);
}

void test_quiet_candidate_resumes_after_sustained_motion() {
  DetectorConfig config = unsmoothed_config();
  config.minimum_washer_runtime_ms = 8UL * 60UL * 1000UL;
  config.done_quiet_ms = 10UL * 60UL * 1000UL;
  config.resume_motion_confirm_ms = 30UL * 1000UL;
  LaundryDetector detector(config);

  detector.observe(washer(0));
  detector.observe(washer(9UL * 60UL * 1000UL));
  detector.observe(quiet(15UL * 60UL * 1000UL));

  Decision first_resume = detector.observe(washer(20UL * 60UL * 1000UL));
  TEST_ASSERT_EQUAL(DetectorState::QuietCandidate, first_resume.state);
  TEST_ASSERT_FALSE(first_resume.should_post);

  Decision confirmed_resume = detector.observe(washer(20UL * 60UL * 1000UL + 30001UL));
  TEST_ASSERT_EQUAL(DetectorState::CycleRunning, confirmed_resume.state);
  TEST_ASSERT_FALSE(confirmed_resume.should_post);

  detector.observe(quiet(21UL * 60UL * 1000UL));

  Decision old_deadline = detector.observe(quiet(25UL * 60UL * 1000UL + 1UL));
  TEST_ASSERT_EQUAL(DetectorState::QuietCandidate, old_deadline.state);
  TEST_ASSERT_FALSE(old_deadline.should_post);

  Decision done = detector.observe(quiet(31UL * 60UL * 1000UL + 1UL));
  TEST_ASSERT_EQUAL(DetectorState::DoneSent, done.state);
  TEST_ASSERT_TRUE(done.should_post);
}

void test_motion_trigger_posts_after_three_seconds_of_continuous_motion() {
  MovementTrigger trigger(MovementTriggerConfig{
      .threshold_mg = 30.0f,
      .confirm_motion_ms = 3000UL,
      .cooldown_ms = 60000UL,
  });

  TEST_ASSERT_FALSE(trigger.observe(0, 45.0f));
  TEST_ASSERT_FALSE(trigger.observe(2999UL, 48.0f));
  TEST_ASSERT_TRUE(trigger.observe(3000UL, 51.0f));
}

void test_motion_trigger_resets_when_motion_drops_before_confirmation() {
  MovementTrigger trigger(MovementTriggerConfig{
      .threshold_mg = 30.0f,
      .confirm_motion_ms = 3000UL,
      .cooldown_ms = 60000UL,
  });

  TEST_ASSERT_FALSE(trigger.observe(0, 45.0f));
  TEST_ASSERT_FALSE(trigger.observe(2000UL, 12.0f));
  TEST_ASSERT_FALSE(trigger.observe(4000UL, 45.0f));
  TEST_ASSERT_FALSE(trigger.observe(6999UL, 45.0f));
  TEST_ASSERT_TRUE(trigger.observe(7000UL, 45.0f));
}

void test_motion_trigger_enforces_cooldown_after_posting() {
  MovementTrigger trigger(MovementTriggerConfig{
      .threshold_mg = 30.0f,
      .confirm_motion_ms = 3000UL,
      .cooldown_ms = 60000UL,
  });

  TEST_ASSERT_FALSE(trigger.observe(0, 45.0f));
  TEST_ASSERT_TRUE(trigger.observe(3000UL, 45.0f));
  TEST_ASSERT_FALSE(trigger.observe(4000UL, 45.0f));
  TEST_ASSERT_FALSE(trigger.observe(62000UL, 45.0f));
  TEST_ASSERT_TRUE(trigger.observe(63000UL, 45.0f));
}

void test_startup_keep_awake_uses_short_idle_poll_during_manual_battery_wake_window() {
  const unsigned long keep_awake_ms = 10UL * 60UL * 1000UL;
  const unsigned long keep_awake_poll_ms = 10000UL;
  const unsigned long idle_poll_ms = 30UL * 1000UL;
  const unsigned long running_poll_ms = 10000UL;

  TEST_ASSERT_EQUAL(
      keep_awake_poll_ms,
      telemetry_poll_ms(
          9UL * 60UL * 1000UL,
          DetectorState::Idle,
          keep_awake_ms,
          keep_awake_poll_ms,
          idle_poll_ms,
          running_poll_ms));
  TEST_ASSERT_EQUAL(
      keep_awake_poll_ms,
      telemetry_poll_ms(
          9UL * 60UL * 1000UL,
          DetectorState::DoneSent,
          keep_awake_ms,
          keep_awake_poll_ms,
          idle_poll_ms,
          running_poll_ms));
  TEST_ASSERT_EQUAL(
      idle_poll_ms,
      telemetry_poll_ms(
          10UL * 60UL * 1000UL + 1UL,
          DetectorState::Idle,
          keep_awake_ms,
          keep_awake_poll_ms,
          idle_poll_ms,
          running_poll_ms));
  TEST_ASSERT_EQUAL(
      running_poll_ms,
      telemetry_poll_ms(
          20UL * 60UL * 1000UL,
          DetectorState::CycleRunning,
          keep_awake_ms,
          keep_awake_poll_ms,
          idle_poll_ms,
          running_poll_ms));
}

void test_default_telemetry_cadence_lets_power_bank_auto_off_when_idle() {
  const TelemetryCadenceConfig config;

  TEST_ASSERT_EQUAL(5UL * 60UL * 1000UL, config.startup_keep_awake_ms);
  TEST_ASSERT_EQUAL(10UL * 1000UL, config.startup_poll_ms);
  TEST_ASSERT_EQUAL(2UL * 60UL * 1000UL, config.idle_poll_ms);
  TEST_ASSERT_EQUAL(10UL * 1000UL, config.running_poll_ms);
  TEST_ASSERT_EQUAL(15UL * 1000UL, config.battery_keepalive_interval_ms);
  TEST_ASSERT_EQUAL(2500UL, config.battery_keepalive_pulse_ms);
  TEST_ASSERT_EQUAL(25UL * 1000UL, config.active_load_pulse_interval_ms);
  TEST_ASSERT_EQUAL(8000UL, config.active_load_pulse_ms);
  TEST_ASSERT_TRUE(config.idle_poll_ms > 40UL * 1000UL);
  TEST_ASSERT_TRUE(config.active_load_pulse_interval_ms < 40UL * 1000UL);

  TEST_ASSERT_EQUAL(
      config.startup_poll_ms,
      telemetry_poll_ms(config.startup_keep_awake_ms - 1UL, DetectorState::Idle, config));
  TEST_ASSERT_EQUAL(
      config.idle_poll_ms,
      telemetry_poll_ms(config.startup_keep_awake_ms + 1UL, DetectorState::Idle, config));
  TEST_ASSERT_EQUAL(
      config.running_poll_ms,
      telemetry_poll_ms(10UL * 60UL * 1000UL, DetectorState::QuietCandidate, config));
}

void test_default_wifi_power_policy_avoids_high_draw_preconnect_scans() {
  const WifiPowerPolicy policy = brownout_resistant_wifi_policy();

  TEST_ASSERT_FALSE(policy.preconnect_scan);
  TEST_ASSERT_FALSE(policy.failure_scan);
  TEST_ASSERT_EQUAL(8, policy.tx_power_quarter_dbm);
  TEST_ASSERT_EQUAL(80, policy.cpu_frequency_mhz);
}

void test_idle_and_done_naps_do_not_pulse_after_startup_keep_awake_window() {
  const TelemetryCadenceConfig config;

  TEST_ASSERT_FALSE(battery_keepalive_allowed(
      config.startup_keep_awake_ms + 1UL,
      DetectorState::Idle,
      config));
  TEST_ASSERT_FALSE(battery_keepalive_allowed(
      config.startup_keep_awake_ms + 1UL,
      DetectorState::DoneSent,
      config));

  BatteryKeepaliveNap idle = next_battery_keepalive_nap(
      config.idle_poll_ms,
      config.startup_keep_awake_ms + 1UL,
      DetectorState::Idle,
      config);
  TEST_ASSERT_EQUAL(config.idle_poll_ms, idle.sleep_ms);
  TEST_ASSERT_EQUAL(0UL, idle.awake_pulse_ms);
  TEST_ASSERT_EQUAL(0UL, idle.remaining_after_slice_ms);

  BatteryKeepaliveNap done = next_battery_keepalive_nap(
      config.idle_poll_ms,
      config.startup_keep_awake_ms + 1UL,
      DetectorState::DoneSent,
      config);
  TEST_ASSERT_EQUAL(config.idle_poll_ms, done.sleep_ms);
  TEST_ASSERT_EQUAL(0UL, done.awake_pulse_ms);
  TEST_ASSERT_EQUAL(0UL, done.remaining_after_slice_ms);
}

void test_running_and_quiet_candidate_naps_still_keep_power_bank_awake() {
  const TelemetryCadenceConfig config;
  const unsigned long long_running_nap_ms = 30UL * 1000UL;

  TEST_ASSERT_TRUE(battery_keepalive_allowed(
      config.startup_keep_awake_ms + 1UL,
      DetectorState::CycleRunning,
      config));
  TEST_ASSERT_TRUE(battery_keepalive_allowed(
      config.startup_keep_awake_ms + 1UL,
      DetectorState::QuietCandidate,
      config));

  BatteryKeepaliveNap running = next_battery_keepalive_nap(
      long_running_nap_ms,
      config.startup_keep_awake_ms + 1UL,
      DetectorState::CycleRunning,
      config);
  TEST_ASSERT_EQUAL(config.battery_keepalive_interval_ms, running.sleep_ms);
  TEST_ASSERT_EQUAL(config.battery_keepalive_pulse_ms, running.awake_pulse_ms);
  TEST_ASSERT_EQUAL(
      long_running_nap_ms - config.battery_keepalive_interval_ms - config.battery_keepalive_pulse_ms,
      running.remaining_after_slice_ms);
}

void test_active_cycle_load_pulse_runs_before_forty_second_power_bank_cutoff() {
  const TelemetryCadenceConfig config;

  TEST_ASSERT_EQUAL(
      0UL,
      active_cycle_load_pulse_ms(
          config.active_load_pulse_interval_ms - 1UL,
          DetectorState::CycleRunning,
          0UL,
          config));
  TEST_ASSERT_EQUAL(
      config.active_load_pulse_ms,
      active_cycle_load_pulse_ms(
          config.active_load_pulse_interval_ms,
          DetectorState::CycleRunning,
          0UL,
          config));
  TEST_ASSERT_EQUAL(
      config.active_load_pulse_ms,
      active_cycle_load_pulse_ms(
          config.active_load_pulse_interval_ms * 2UL,
          DetectorState::QuietCandidate,
          config.active_load_pulse_interval_ms,
          config));
}

void test_active_cycle_load_pulse_skips_expired_startup_and_done_states() {
  const TelemetryCadenceConfig config;

  TEST_ASSERT_EQUAL(
      0UL,
      active_cycle_load_pulse_ms(
          config.startup_keep_awake_ms,
          DetectorState::Idle,
          0UL,
          config));
  TEST_ASSERT_EQUAL(
      0UL,
      active_cycle_load_pulse_ms(
          config.active_load_pulse_interval_ms * 3UL,
          DetectorState::DoneSent,
          0UL,
          config));
}

void test_battery_keepalive_splits_long_idle_nap_with_awake_pulse() {
  const TelemetryCadenceConfig config;

  BatteryKeepaliveNap first = next_battery_keepalive_nap(30UL * 1000UL, config);
  TEST_ASSERT_EQUAL(15UL * 1000UL, first.sleep_ms);
  TEST_ASSERT_EQUAL(2500UL, first.awake_pulse_ms);
  TEST_ASSERT_EQUAL(12500UL, first.remaining_after_slice_ms);

  BatteryKeepaliveNap second = next_battery_keepalive_nap(
      first.remaining_after_slice_ms,
      config);
  TEST_ASSERT_EQUAL(12500UL, second.sleep_ms);
  TEST_ASSERT_EQUAL(0UL, second.awake_pulse_ms);
  TEST_ASSERT_EQUAL(0UL, second.remaining_after_slice_ms);
}

void test_battery_keepalive_does_not_split_short_running_nap() {
  const TelemetryCadenceConfig config;

  BatteryKeepaliveNap slice = next_battery_keepalive_nap(10UL * 1000UL, config);
  TEST_ASSERT_EQUAL(10UL * 1000UL, slice.sleep_ms);
  TEST_ASSERT_EQUAL(0UL, slice.awake_pulse_ms);
  TEST_ASSERT_EQUAL(0UL, slice.remaining_after_slice_ms);
}

void test_cadence_detector_returns_to_idle_after_single_handling_jolt() {
  LaundryDetector detector(telemetry_cadence_detector_config());
  const TelemetryCadenceConfig cadence;

  Decision jolt = detector.observe(MotionWindow{0, 4, 52.0f, 510.0f});
  TEST_ASSERT_EQUAL(DetectorState::MotionConfirming, jolt.state);
  TEST_ASSERT_EQUAL(cadence.running_poll_ms, telemetry_poll_ms(0, jolt.state, cadence));

  Decision quiet_again = detector.observe(quiet(30UL * 1000UL));
  TEST_ASSERT_EQUAL(DetectorState::Idle, quiet_again.state);
  TEST_ASSERT_EQUAL(
      cadence.idle_poll_ms,
      telemetry_poll_ms(cadence.startup_keep_awake_ms + 1UL, quiet_again.state, cadence));
}

void test_cadence_detector_does_not_assume_noise_is_gentle_washing() {
  LaundryDetector detector(telemetry_cadence_detector_config());

  Decision gentle_start = detector.observe(MotionWindow{0, 4, 2.46f, 4.69f});
  TEST_ASSERT_EQUAL(DetectorState::Idle, gentle_start.state);

  Decision confirmed = detector.observe(MotionWindow{30UL * 1000UL + 1UL, 4, 3.01f, 5.35f});
  TEST_ASSERT_EQUAL(DetectorState::Idle, confirmed.state);
}

void test_cadence_done_leaves_notification_evidence_to_the_relay() {
  LaundryDetector detector(telemetry_cadence_detector_config());

  detector.observe(MotionWindow{0, 4, 20.0f, 104.0f});
  Decision running =
      detector.observe(MotionWindow{30UL * 1000UL + 1UL, 4, 20.0f, 104.0f});
  TEST_ASSERT_EQUAL(DetectorState::CycleRunning, running.state);
  TEST_ASSERT_FALSE(should_post_done_event(running));

  Decision quieting = detector.observe(MotionWindow{20UL * 60UL * 1000UL, 4, 1.0f, 2.0f});
  TEST_ASSERT_EQUAL(DetectorState::QuietCandidate, quieting.state);
  TEST_ASSERT_FALSE(should_post_done_event(quieting));

  Decision done = detector.observe(MotionWindow{30UL * 60UL * 1000UL + 1UL, 4, 1.0f, 2.0f});
  TEST_ASSERT_EQUAL(DetectorState::DoneSent, done.state);
  TEST_ASSERT_FALSE(done.should_post);
  TEST_ASSERT_FALSE(should_post_done_event(done));

  Decision duplicate = detector.observe(MotionWindow{32UL * 60UL * 1000UL, 4, 1.0f, 2.0f});
  TEST_ASSERT_EQUAL(DetectorState::DoneSent, duplicate.state);
  TEST_ASSERT_FALSE(should_post_done_event(duplicate));
}

void test_nap_duration_keeps_sample_start_cadence_near_target_interval() {
  TEST_ASSERT_EQUAL(9000UL, nap_duration_ms(1000UL, 2000UL, 10000UL));
  TEST_ASSERT_EQUAL(1UL, nap_duration_ms(1000UL, 12000UL, 10000UL));
}

void test_aligned_wall_clock_nap_targets_next_interval_boundary() {
  TEST_ASSERT_EQUAL(10000UL, aligned_wall_clock_nap_ms(0, 10000UL));
  TEST_ASSERT_EQUAL(8000UL, aligned_wall_clock_nap_ms(12, 10000UL));
  TEST_ASSERT_EQUAL(1000UL, aligned_wall_clock_nap_ms(29, 10000UL));
  TEST_ASSERT_EQUAL(10000UL, aligned_wall_clock_nap_ms(30, 10000UL));
}

void test_startup_ignores_handling_then_waits_through_quiet_fill() {
  const TelemetryCadenceConfig config;
  LaundryDetector detector(telemetry_cadence_detector_config());
  for (unsigned long t = 0; t < config.startup_settle_ms; t += 10000UL) {
    Decision result = observe_after_startup_settle(detector, washer(t), config);
    TEST_ASSERT_EQUAL(DetectorState::Idle, result.state);
    TEST_ASSERT_FALSE(result.should_post);
    TEST_ASSERT_TRUE(startup_keeps_radio_awake(t, config));
  }
  for (unsigned long t = config.startup_settle_ms; t < 4UL * 60000UL; t += 10000UL) {
    Decision result = observe_after_startup_settle(detector, quiet(t), config);
    TEST_ASSERT_EQUAL(DetectorState::Idle, result.state);
    TEST_ASSERT_FALSE(result.should_post);
    TEST_ASSERT_FALSE(startup_keeps_radio_awake(t, config));
    TEST_ASSERT_TRUE(battery_keepalive_allowed(t, result.state, config));
    TEST_ASSERT_EQUAL(config.startup_poll_ms, telemetry_poll_ms(t, result.state, config));
  }
  Decision first = observe_after_startup_settle(detector, bedding_wash(4UL * 60000UL), config);
  TEST_ASSERT_EQUAL(DetectorState::MotionConfirming, first.state);
  Decision confirmed = observe_after_startup_settle(detector, bedding_wash(4UL * 60000UL + 30000UL), config);
  TEST_ASSERT_EQUAL(DetectorState::CycleRunning, confirmed.state);
  TEST_ASSERT_FALSE(confirmed.should_post);
}

void test_startup_settling_and_awake_window_boundaries() {
  const TelemetryCadenceConfig config;
  LaundryDetector detector(telemetry_cadence_detector_config());
  TEST_ASSERT_EQUAL(DetectorState::Idle,
      observe_after_startup_settle(detector, washer(config.startup_settle_ms - 1UL), config).state);
  TEST_ASSERT_EQUAL(DetectorState::MotionConfirming,
      observe_after_startup_settle(detector, washer(config.startup_settle_ms), config).state);
  TEST_ASSERT_TRUE(startup_keeps_radio_awake(config.startup_settle_ms - 1UL, config));
  TEST_ASSERT_FALSE(startup_keeps_radio_awake(config.startup_settle_ms, config));
  TEST_ASSERT_FALSE(startup_keeps_radio_awake(config.startup_keep_awake_ms - 1UL, config));
  TEST_ASSERT_EQUAL(config.idle_poll_ms,
      telemetry_poll_ms(config.startup_keep_awake_ms, DetectorState::Idle, config));
  TEST_ASSERT_EQUAL(config.running_poll_ms,
      telemetry_poll_ms(config.startup_keep_awake_ms, DetectorState::CycleRunning, config));
}

void test_waiting_for_initial_motion_uses_bounded_keepalive_not_continuous_radio() {
  const TelemetryCadenceConfig config;
  const unsigned long waiting_ms = 2UL * 60000UL;
  TEST_ASSERT_FALSE(startup_keeps_radio_awake(waiting_ms, config));
  TEST_ASSERT_EQUAL(config.active_load_pulse_ms,
      active_cycle_load_pulse_ms(waiting_ms, DetectorState::Idle, 0, config));
  TEST_ASSERT_EQUAL(0UL,
      active_cycle_load_pulse_ms(waiting_ms, DetectorState::Idle, waiting_ms, config));
  TEST_ASSERT_EQUAL(0UL,
      active_cycle_load_pulse_ms(config.startup_keep_awake_ms, DetectorState::Idle, 0, config));
  TEST_ASSERT_FALSE(battery_keepalive_allowed(config.startup_keep_awake_ms, DetectorState::Idle, config));
  // Expiring the start allowance must not shut down an established cycle,
  // including the quiet evidence the server needs before sending an alert.
  for (DetectorState state : {DetectorState::MotionConfirming,
                             DetectorState::CycleRunning,
                             DetectorState::QuietCandidate}) {
    TEST_ASSERT_TRUE(battery_keepalive_allowed(config.startup_keep_awake_ms, state, config));
    TEST_ASSERT_EQUAL(config.running_poll_ms,
        telemetry_poll_ms(config.startup_keep_awake_ms, state, config));
    TEST_ASSERT_EQUAL(config.active_load_pulse_ms,
        active_cycle_load_pulse_ms(config.startup_keep_awake_ms, state, 0, config));
  }
}

void test_wireless_updates_require_settled_idle_state() {
  const TelemetryCadenceConfig config;
  TEST_ASSERT_FALSE(wireless_update_allowed(config.startup_settle_ms - 1UL, DetectorState::Idle, config));
  TEST_ASSERT_TRUE(wireless_update_allowed(config.startup_settle_ms, DetectorState::Idle, config));
  TEST_ASSERT_TRUE(wireless_update_allowed(config.startup_settle_ms, DetectorState::DoneSent, config));
  for (DetectorState state : {DetectorState::MotionConfirming, DetectorState::CycleRunning,
                             DetectorState::QuietCandidate}) {
    TEST_ASSERT_FALSE(wireless_update_allowed(config.startup_keep_awake_ms, state, config));
  }
}

void test_active_cycle_keeps_radio_awake_until_idle_or_done() {
  for (DetectorState state : {DetectorState::MotionConfirming, DetectorState::CycleRunning,
                             DetectorState::QuietCandidate}) {
    TEST_ASSERT_TRUE(active_cycle_requires_continuous_power(state));
  }
  TEST_ASSERT_FALSE(active_cycle_requires_continuous_power(DetectorState::Idle));
  TEST_ASSERT_FALSE(active_cycle_requires_continuous_power(DetectorState::DoneSent));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_active_cycle_keeps_radio_awake_until_idle_or_done);
  RUN_TEST(test_mounted_off_capture_never_confirms_motion);
  RUN_TEST(test_cadence_keeps_power_for_restarted_server_quiet_countdown);
  RUN_TEST(test_entire_measured_dryer_capture_stays_running);
  RUN_TEST(test_invalid_sensor_windows_do_not_count_as_quiet);
  RUN_TEST(test_measured_desk_noise_never_arms_a_cycle);
  RUN_TEST(test_measured_noise_after_tumbling_releases_power_without_device_alert);
  RUN_TEST(test_wireless_updates_require_settled_idle_state);
  RUN_TEST(test_waiting_for_initial_motion_uses_bounded_keepalive_not_continuous_radio);
  RUN_TEST(test_startup_ignores_handling_then_waits_through_quiet_fill);
  RUN_TEST(test_startup_settling_and_awake_window_boundaries);
  RUN_TEST(test_washer_cycle_alerts_once_after_quiet_period);
  RUN_TEST(test_dryer_after_washer_is_labeled_dryer);
  RUN_TEST(test_ambiguous_tandem_motion_gets_stack_label);
  RUN_TEST(test_short_bump_is_ignored);
  RUN_TEST(test_default_classification_uses_smoothed_recent_samples);
  RUN_TEST(test_calibrated_bedding_wash_signal_is_treated_as_running);
  RUN_TEST(test_low_washer_motion_without_spin_peak_uses_stack_label);
  RUN_TEST(test_quiet_candidate_ignores_isolated_peak_jolt_before_done);
  RUN_TEST(test_quiet_candidate_ignores_repeated_peak_only_noise_before_done);
  RUN_TEST(test_quiet_candidate_resumes_after_sustained_motion);
  RUN_TEST(test_motion_trigger_posts_after_three_seconds_of_continuous_motion);
  RUN_TEST(test_motion_trigger_resets_when_motion_drops_before_confirmation);
  RUN_TEST(test_motion_trigger_enforces_cooldown_after_posting);
  RUN_TEST(test_startup_keep_awake_uses_short_idle_poll_during_manual_battery_wake_window);
  RUN_TEST(test_default_telemetry_cadence_lets_power_bank_auto_off_when_idle);
  RUN_TEST(test_default_wifi_power_policy_avoids_high_draw_preconnect_scans);
  RUN_TEST(test_idle_and_done_naps_do_not_pulse_after_startup_keep_awake_window);
  RUN_TEST(test_running_and_quiet_candidate_naps_still_keep_power_bank_awake);
  RUN_TEST(test_active_cycle_load_pulse_runs_before_forty_second_power_bank_cutoff);
  RUN_TEST(test_active_cycle_load_pulse_skips_expired_startup_and_done_states);
  RUN_TEST(test_battery_keepalive_splits_long_idle_nap_with_awake_pulse);
  RUN_TEST(test_battery_keepalive_does_not_split_short_running_nap);
  RUN_TEST(test_cadence_detector_returns_to_idle_after_single_handling_jolt);
  RUN_TEST(test_cadence_detector_does_not_assume_noise_is_gentle_washing);
  RUN_TEST(test_cadence_done_leaves_notification_evidence_to_the_relay);
  RUN_TEST(test_nap_duration_keeps_sample_start_cadence_near_target_interval);
  RUN_TEST(test_aligned_wall_clock_nap_targets_next_interval_boundary);
  return UNITY_END();
}
