// Standalone unit test for the pure per-frame emit-decision logic.
// Build + run:
//   g++ -std=c++17 -Wall -Wextra -o /tmp/test_emit_plan native/latency_layer/test/test_latency_emit_plan.cpp && /tmp/test_emit_plan
//
// No framework: asserts + a tiny harness. Exit 0 = all passed.

#include "../src/latency_emit_plan.h"

#include <cstdio>

namespace {

int g_failures = 0;

#define CHECK(cond)                                                      \
    do {                                                                 \
        if (!(cond)) {                                                   \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);  \
            ++g_failures;                                                \
        }                                                                \
    } while (0)

// Fresh advancing present IDs are all emitted, last_emitted advances.
void test_fresh_sequence_all_emitted() {
    auto plan = penguinburner::plan_latency_emits({841, 842, 843}, /*last_emitted=*/840);
    CHECK(plan.emit_indices.size() == 3);
    CHECK(plan.emitted_fresh);
    CHECK(!plan.reset_detected);
    CHECK(plan.new_last_emitted == 843);
}

// Already-seen present IDs are deduped away; nothing fresh emitted.
void test_stale_ring_nothing_fresh() {
    auto plan = penguinburner::plan_latency_emits({843, 843, 843}, /*last_emitted=*/843);
    CHECK(plan.emit_indices.empty());
    CHECK(!plan.emitted_fresh);
    CHECK(!plan.reset_detected);
    CHECK(plan.has_newest);
    CHECK(plan.new_last_emitted == 843);  // unchanged
}

// THE BUG: the driver/Reflex present-id counter restarts low (e.g. swapchain
// recreation on a resolution/HDR/fullscreen change). last_emitted is still high
// from before, so a naive `pid <= last_emitted` drops every genuinely fresh
// frame forever and the stream looks permanently stale. The plan must detect the
// backward jump, reset, and emit the fresh low-numbered frames.
void test_present_id_reset_emits_fresh() {
    auto plan = penguinburner::plan_latency_emits({1, 2, 3}, /*last_emitted=*/843);
    CHECK(plan.reset_detected);
    CHECK(plan.emit_indices.size() == 3);
    CHECK(plan.emitted_fresh);
    CHECK(plan.new_last_emitted == 3);
}

// present_id == 0 means the driver did not tag the report: treat as always-fresh
// (never deduped), and it must not be mistaken for a backward-jump reset.
void test_untagged_reports_always_fresh_no_reset() {
    auto plan = penguinburner::plan_latency_emits({0, 0}, /*last_emitted=*/843);
    CHECK(!plan.reset_detected);
    CHECK(plan.emit_indices.size() == 2);
    CHECK(plan.emitted_fresh);
    CHECK(plan.new_last_emitted == 843);  // untagged frames don't advance the watermark
}

// Empty / no-usable-reports: nothing to do, no newest.
void test_empty() {
    auto plan = penguinburner::plan_latency_emits({}, /*last_emitted=*/843);
    CHECK(plan.emit_indices.empty());
    CHECK(!plan.emitted_fresh);
    CHECK(!plan.has_newest);
    CHECK(plan.new_last_emitted == 843);
}

// Newest is selected for the stale re-emit fallback (highest present_id, last on ties).
void test_newest_position() {
    auto plan = penguinburner::plan_latency_emits({843, 841, 842}, /*last_emitted=*/843);
    CHECK(plan.has_newest);
    CHECK(plan.newest_pos == 0);  // 843 is the highest
}

}  // namespace

int main() {
    test_fresh_sequence_all_emitted();
    test_stale_ring_nothing_fresh();
    test_present_id_reset_emits_fresh();
    test_untagged_reports_always_fresh_no_reset();
    test_empty();
    test_newest_position();
    if (g_failures == 0) {
        std::printf("all latency_emit_plan tests passed\n");
        return 0;
    }
    std::printf("%d test(s) failed\n", g_failures);
    return 1;
}
