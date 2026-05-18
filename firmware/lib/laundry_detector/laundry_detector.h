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
  float active_threshold_mg = 30.0f;
  float quiet_threshold_mg = 12.0f;
  float washer_spin_peak_mg = 120.0f;
  float dryer_max_rms_mg = 45.0f;
  unsigned long confirm_motion_ms = 60UL * 1000UL;
  unsigned long minimum_washer_runtime_ms = 8UL * 60UL * 1000UL;
  unsigned long minimum_dryer_runtime_ms = 10UL * 60UL * 1000UL;
  unsigned long done_quiet_ms = 10UL * 60UL * 1000UL;
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
  unsigned long last_done_ms_ = 0;
  bool saw_spin_peak_ = false;
  bool saw_dryer_like_motion_ = false;
  bool saw_midrange_motion_ = false;
};
