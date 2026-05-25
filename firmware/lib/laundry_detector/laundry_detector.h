#pragma once

#include <cstdint>

enum class DetectorState {
  Idle,
  MotionConfirming,
  CycleRunning,
  QuietCandidate,
  DoneSent,
};

enum class CycleLabel {
  Unknown,
  Washer,
  Dryer,
  Stack,
};

struct DetectorConfig {
  float active_threshold_mg = 3.0f;
  float active_peak_threshold_mg = 8.0f;
  float quiet_threshold_mg = 1.5f;
  float washer_spin_peak_mg = 120.0f;
  float dryer_max_rms_mg = 45.0f;
  uint8_t classification_smoothing_samples = 8;
  unsigned long confirm_motion_ms = 60UL * 1000UL;
  unsigned long minimum_washer_runtime_ms = 8UL * 60UL * 1000UL;
  unsigned long minimum_dryer_runtime_ms = 10UL * 60UL * 1000UL;
  unsigned long done_quiet_ms = 10UL * 60UL * 1000UL;
  unsigned long resume_motion_confirm_ms = 30UL * 1000UL;
  unsigned long dryer_handoff_ms = 4UL * 60UL * 60UL * 1000UL;
};

struct MotionWindow {
  unsigned long at_ms;
  uint16_t seconds;
  float rms_mg;
  float peak_mg;
};

struct Decision {
  DetectorState state = DetectorState::Idle;
  CycleLabel label = CycleLabel::Unknown;
  bool should_post = false;
};

struct MovementTriggerConfig {
  float threshold_mg = 30.0f;
  unsigned long confirm_motion_ms = 3000UL;
  unsigned long cooldown_ms = 60000UL;
};

class MovementTrigger {
public:
  explicit MovementTrigger(const MovementTriggerConfig &config = MovementTriggerConfig());

  bool observe(unsigned long at_ms, float motion_mg);
  void reset();

private:
  MovementTriggerConfig config_;
  bool active_ = false;
  unsigned long active_started_ms_ = 0;
  unsigned long last_post_ms_ = 0;
  bool posted_once_ = false;
};

class LaundryDetector {
public:
  explicit LaundryDetector(const DetectorConfig &config = DetectorConfig());

  Decision observe(const MotionWindow &window);
  void reset();

  DetectorState state() const;
  CycleLabel current_label() const;

private:
  bool is_active(const MotionWindow &window) const;
  bool is_quiet(const MotionWindow &window) const;
  unsigned long elapsed(unsigned long now, unsigned long then) const;
  MotionWindow smoothed_window(const MotionWindow &window);
  CycleLabel classify_current_cycle() const;
  unsigned long minimum_runtime_for(CycleLabel label) const;
  void start_cycle(const MotionWindow &window);
  void note_motion(const MotionWindow &window);

  DetectorConfig config_;
  DetectorState state_ = DetectorState::Idle;
  CycleLabel current_label_ = CycleLabel::Unknown;
  CycleLabel last_done_label_ = CycleLabel::Unknown;
  unsigned long cycle_started_ms_ = 0;
  unsigned long quiet_started_ms_ = 0;
  unsigned long quiet_resume_started_ms_ = 0;
  unsigned long last_done_ms_ = 0;
  bool saw_spin_peak_ = false;
  bool saw_dryer_like_motion_ = false;
  bool saw_midrange_motion_ = false;
  static constexpr uint8_t kMaxSmoothingSamples = 8;
  MotionWindow recent_windows_[kMaxSmoothingSamples] = {};
  uint8_t recent_window_count_ = 0;
  uint8_t recent_window_index_ = 0;
};

unsigned long telemetry_poll_ms(unsigned long now_ms,
                                DetectorState state,
                                unsigned long startup_keep_awake_ms,
                                unsigned long startup_poll_ms,
                                unsigned long idle_poll_ms,
                                unsigned long running_poll_ms);

unsigned long nap_duration_ms(unsigned long sample_started_ms,
                              unsigned long now_ms,
                              unsigned long target_interval_ms);

unsigned long aligned_wall_clock_nap_ms(long epoch_seconds,
                                        unsigned long target_interval_ms);
