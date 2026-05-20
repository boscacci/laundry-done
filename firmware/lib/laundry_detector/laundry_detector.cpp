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

  switch (state_) {
  case DetectorState::Idle:
    if (is_active(window)) {
      start_cycle(window);
    }
    break;

  case DetectorState::MotionConfirming:
    if (is_active(window)) {
      note_motion(window);
      if (elapsed(window.at_ms, cycle_started_ms_) >= config_.confirm_motion_ms) {
        state_ = DetectorState::CycleRunning;
      }
    } else if (elapsed(window.at_ms, cycle_started_ms_) < config_.confirm_motion_ms) {
      reset();
    }
    break;

  case DetectorState::CycleRunning:
    if (is_active(window)) {
      note_motion(window);
    } else if (is_quiet(window)) {
      current_label_ = classify_current_cycle();
      state_ = DetectorState::QuietCandidate;
      quiet_started_ms_ = window.at_ms;
    }
    break;

  case DetectorState::QuietCandidate:
    if (is_active(window)) {
      note_motion(window);
      state_ = DetectorState::CycleRunning;
    } else if (is_quiet(window)) {
      const unsigned long quiet_ms = elapsed(window.at_ms, quiet_started_ms_);
      const unsigned long runtime_ms = elapsed(window.at_ms, cycle_started_ms_);
      if (quiet_ms >= config_.done_quiet_ms &&
          runtime_ms >= minimum_runtime_for(current_label_)) {
        state_ = DetectorState::DoneSent;
        last_done_label_ = current_label_;
        last_done_ms_ = window.at_ms;
        decision.should_post = true;
      }
    }
    break;

  case DetectorState::DoneSent:
    if (is_active(window)) {
      start_cycle(window);
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
  saw_spin_peak_ = false;
  saw_dryer_like_motion_ = false;
  saw_midrange_motion_ = false;
}

DetectorState LaundryDetector::state() const { return state_; }

CycleLabel LaundryDetector::current_label() const { return current_label_; }

bool LaundryDetector::is_active(const MotionWindow &window) const {
  return window.rms_mg >= config_.active_threshold_mg;
}

bool LaundryDetector::is_quiet(const MotionWindow &window) const {
  return window.rms_mg <= config_.quiet_threshold_mg;
}

unsigned long LaundryDetector::elapsed(unsigned long now, unsigned long then) const {
  return now - then;
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

  if (saw_dryer_like_motion_ && !saw_midrange_motion_) {
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
