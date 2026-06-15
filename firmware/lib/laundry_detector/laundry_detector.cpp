#include "laundry_detector.h"

MovementTrigger::MovementTrigger(const MovementTriggerConfig &config) : config_(config) {}

bool MovementTrigger::observe(unsigned long at_ms, float motion_mg) {
  if (motion_mg < config_.threshold_mg) {
    active_ = false;
    active_started_ms_ = 0;
    return false;
  }

  if (!active_) {
    active_ = true;
    active_started_ms_ = at_ms;
  }

  if (at_ms - active_started_ms_ < config_.confirm_motion_ms) {
    return false;
  }

  if (posted_once_ && at_ms - last_post_ms_ < config_.cooldown_ms) {
    return false;
  }

  posted_once_ = true;
  last_post_ms_ = at_ms;
  return true;
}

void MovementTrigger::reset() {
  active_ = false;
  active_started_ms_ = 0;
  last_post_ms_ = 0;
  posted_once_ = false;
}

LaundryDetector::LaundryDetector(const DetectorConfig &config) : config_(config) {}

Decision LaundryDetector::observe(const MotionWindow &window) {
  Decision decision;
  const MotionWindow classified_window = smoothed_window(window);

  switch (state_) {
  case DetectorState::Idle:
    if (is_active(classified_window)) {
      start_cycle(classified_window);
    }
    break;

  case DetectorState::MotionConfirming:
    if (is_active(classified_window)) {
      note_motion(classified_window);
      if (elapsed(classified_window.at_ms, cycle_started_ms_) >= config_.confirm_motion_ms) {
        state_ = DetectorState::CycleRunning;
      }
    } else {
      reset();
    }
    break;

  case DetectorState::CycleRunning:
    if (is_active(classified_window)) {
      note_motion(classified_window);
    } else if (is_quiet(classified_window)) {
      quiet_started_ms_ = classified_window.at_ms;
      quiet_resume_started_ms_ = 0;
      current_label_ = classify_current_cycle();
      state_ = DetectorState::QuietCandidate;
    }
    break;

  case DetectorState::QuietCandidate:
    if (is_active(classified_window)) {
      if (quiet_resume_started_ms_ == 0) {
        quiet_resume_started_ms_ = classified_window.at_ms;
      }
      if (elapsed(classified_window.at_ms, quiet_resume_started_ms_) >=
          config_.resume_motion_confirm_ms) {
        note_motion(classified_window);
        quiet_resume_started_ms_ = 0;
        state_ = DetectorState::CycleRunning;
      }
    } else if (is_quiet(classified_window)) {
      quiet_resume_started_ms_ = 0;
      const unsigned long quiet_ms = elapsed(classified_window.at_ms, quiet_started_ms_);
      const unsigned long runtime_ms = elapsed(classified_window.at_ms, cycle_started_ms_);
      if (quiet_ms >= config_.done_quiet_ms &&
          runtime_ms >= minimum_runtime_for(current_label_)) {
        state_ = DetectorState::DoneSent;
        last_done_label_ = current_label_;
        last_done_ms_ = classified_window.at_ms;
        decision.should_post = true;
      }
    }
    break;

  case DetectorState::DoneSent:
    if (is_active(classified_window)) {
      start_cycle(classified_window);
    }
    break;
  }

  decision.state = state_;
  decision.label = current_label_;
  return decision;
}

void LaundryDetector::reset() {
  state_ = DetectorState::Idle;
  current_label_ = CycleLabel::Unknown;
  cycle_started_ms_ = 0;
  quiet_started_ms_ = 0;
  quiet_resume_started_ms_ = 0;
  saw_spin_peak_ = false;
  saw_dryer_like_motion_ = false;
  saw_midrange_motion_ = false;
  recent_window_count_ = 0;
  recent_window_index_ = 0;
}

DetectorState LaundryDetector::state() const { return state_; }

CycleLabel LaundryDetector::current_label() const { return current_label_; }

bool LaundryDetector::is_active(const MotionWindow &window) const {
  return window.rms_mg >= config_.active_threshold_mg ||
         window.peak_mg >= config_.active_peak_threshold_mg;
}

bool LaundryDetector::is_quiet(const MotionWindow &window) const {
  return window.rms_mg <= config_.quiet_threshold_mg;
}

unsigned long LaundryDetector::elapsed(unsigned long now, unsigned long then) const {
  return now - then;
}

MotionWindow LaundryDetector::smoothed_window(const MotionWindow &window) {
  recent_windows_[recent_window_index_] = window;
  recent_window_index_ = (recent_window_index_ + 1) % kMaxSmoothingSamples;
  if (recent_window_count_ < kMaxSmoothingSamples) {
    recent_window_count_++;
  }

  uint8_t desired = config_.classification_smoothing_samples;
  if (desired == 0) {
    desired = 1;
  }
  if (desired > kMaxSmoothingSamples) {
    desired = kMaxSmoothingSamples;
  }
  const uint8_t count = recent_window_count_ < desired ? recent_window_count_ : desired;
  float rms_sum = 0.0f;
  float peak_sum = 0.0f;
  for (uint8_t offset = 0; offset < count; offset++) {
    const uint8_t index =
        (recent_window_index_ + kMaxSmoothingSamples - 1 - offset) % kMaxSmoothingSamples;
    rms_sum += recent_windows_[index].rms_mg;
    peak_sum += recent_windows_[index].peak_mg;
  }

  return MotionWindow{
      window.at_ms,
      static_cast<uint16_t>(window.seconds * count),
      rms_sum / count,
      peak_sum / count,
  };
}

CycleLabel LaundryDetector::classify_current_cycle() const {
  if (saw_spin_peak_) {
    return CycleLabel::Washer;
  }

  const bool follows_recent_washer =
      last_done_label_ == CycleLabel::Washer &&
      elapsed(quiet_started_ms_, last_done_ms_) <= config_.dryer_handoff_ms;
  if (follows_recent_washer && saw_dryer_like_motion_) {
    return CycleLabel::Dryer;
  }

  return CycleLabel::Stack;
}

unsigned long LaundryDetector::minimum_runtime_for(CycleLabel label) const {
  if (label == CycleLabel::Dryer) {
    return config_.minimum_dryer_runtime_ms;
  }
  return config_.minimum_washer_runtime_ms;
}

void LaundryDetector::start_cycle(const MotionWindow &window) {
  state_ = DetectorState::MotionConfirming;
  current_label_ = CycleLabel::Unknown;
  cycle_started_ms_ = window.at_ms;
  quiet_started_ms_ = 0;
  quiet_resume_started_ms_ = 0;
  saw_spin_peak_ = false;
  saw_dryer_like_motion_ = false;
  saw_midrange_motion_ = false;
  note_motion(window);
}

void LaundryDetector::note_motion(const MotionWindow &window) {
  if (window.peak_mg >= config_.washer_spin_peak_mg) {
    saw_spin_peak_ = true;
  }
  if (window.rms_mg <= config_.dryer_max_rms_mg) {
    saw_dryer_like_motion_ = true;
  } else {
    saw_midrange_motion_ = true;
  }
}

DetectorConfig telemetry_cadence_detector_config() {
  DetectorConfig config;
  config.active_threshold_mg = 2.0f;
  config.active_peak_threshold_mg = 4.5f;
  config.classification_smoothing_samples = 1;
  config.confirm_motion_ms = 30UL * 1000UL;
  return config;
}

unsigned long telemetry_poll_ms(unsigned long now_ms,
                                DetectorState state,
                                const TelemetryCadenceConfig &config) {
  return telemetry_poll_ms(now_ms,
                           state,
                           config.startup_keep_awake_ms,
                           config.startup_poll_ms,
                           config.idle_poll_ms,
                           config.running_poll_ms);
}

unsigned long telemetry_poll_ms(unsigned long now_ms,
                                DetectorState state,
                                unsigned long startup_keep_awake_ms,
                                unsigned long startup_poll_ms,
                                unsigned long idle_poll_ms,
                                unsigned long running_poll_ms) {
  const bool startup_keep_awake = now_ms < startup_keep_awake_ms;
  if (startup_keep_awake &&
      (state == DetectorState::Idle || state == DetectorState::DoneSent)) {
    return startup_poll_ms;
  }
  if (state == DetectorState::Idle || state == DetectorState::DoneSent) {
    return idle_poll_ms;
  }
  return running_poll_ms;
}

BatteryKeepaliveNap next_battery_keepalive_nap(unsigned long remaining_nap_ms,
                                               const TelemetryCadenceConfig &config) {
  if (remaining_nap_ms == 0) {
    return BatteryKeepaliveNap{};
  }
  if (config.battery_keepalive_interval_ms == 0 ||
      remaining_nap_ms <= config.battery_keepalive_interval_ms) {
    return BatteryKeepaliveNap{remaining_nap_ms, 0, 0};
  }

  const unsigned long remaining_after_sleep =
      remaining_nap_ms - config.battery_keepalive_interval_ms;
  const unsigned long pulse_ms =
      config.battery_keepalive_pulse_ms > remaining_after_sleep
          ? remaining_after_sleep
          : config.battery_keepalive_pulse_ms;
  return BatteryKeepaliveNap{
      config.battery_keepalive_interval_ms,
      pulse_ms,
      remaining_after_sleep - pulse_ms,
  };
}

unsigned long nap_duration_ms(unsigned long sample_started_ms,
                              unsigned long now_ms,
                              unsigned long target_interval_ms) {
  const unsigned long elapsed_ms = now_ms - sample_started_ms;
  if (elapsed_ms >= target_interval_ms) {
    return 1UL;
  }
  return target_interval_ms - elapsed_ms;
}

unsigned long aligned_wall_clock_nap_ms(long epoch_seconds,
                                        unsigned long target_interval_ms) {
  if (target_interval_ms == 0) {
    return 1UL;
  }
  const unsigned long interval_seconds = target_interval_ms / 1000UL;
  if (interval_seconds == 0) {
    return 1UL;
  }
  const unsigned long epoch = epoch_seconds < 0 ? 0UL : static_cast<unsigned long>(epoch_seconds);
  const unsigned long remainder = epoch % interval_seconds;
  const unsigned long seconds_until_boundary =
      remainder == 0 ? interval_seconds : interval_seconds - remainder;
  return seconds_until_boundary * 1000UL;
}
