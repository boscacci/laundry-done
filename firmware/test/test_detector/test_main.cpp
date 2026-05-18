#include <unity.h>

#include "laundry_detector.h"

static MotionWindow quiet(unsigned long at_ms) {
  return MotionWindow{at_ms, 4, 3.0f, 5.0f};
}

static MotionWindow washer(unsigned long at_ms) {
  return MotionWindow{at_ms, 4, 74.0f, 160.0f};
}

static MotionWindow dryer(unsigned long at_ms) {
  return MotionWindow{at_ms, 4, 34.0f, 44.0f};
}

void test_washer_cycle_alerts_once_after_quiet_period() {
  DetectorConfig config;
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
  DetectorConfig config;
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
  DetectorConfig config;
  LaundryDetector detector(config);

  detector.observe(MotionWindow{0, 4, 52.0f, 68.0f});
  detector.observe(MotionWindow{12UL * 60UL * 1000UL, 4, 55.0f, 70.0f});
  detector.observe(quiet(35UL * 60UL * 1000UL));
  Decision done = detector.observe(quiet(46UL * 60UL * 1000UL));

  TEST_ASSERT_TRUE(done.should_post);
  TEST_ASSERT_EQUAL(CycleLabel::Stack, done.label);
}

void test_short_bump_is_ignored() {
  DetectorConfig config;
  LaundryDetector detector(config);

  Decision bump = detector.observe(MotionWindow{0, 4, 42.0f, 45.0f});
  TEST_ASSERT_EQUAL(DetectorState::MotionConfirming, bump.state);

  Decision quiet_again = detector.observe(quiet(30UL * 1000UL));
  TEST_ASSERT_EQUAL(DetectorState::Idle, quiet_again.state);
  TEST_ASSERT_FALSE(quiet_again.should_post);
}

int main(int argc, char **argv) {
  UNITY_BEGIN();
  RUN_TEST(test_washer_cycle_alerts_once_after_quiet_period);
  RUN_TEST(test_dryer_after_washer_is_labeled_dryer);
  RUN_TEST(test_ambiguous_tandem_motion_gets_stack_label);
  RUN_TEST(test_short_bump_is_ignored);
  return UNITY_END();
}
