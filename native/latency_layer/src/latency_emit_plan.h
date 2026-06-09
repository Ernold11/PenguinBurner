// Pure decision logic for which Vulkan latency frame reports to emit.
//
// Extracted from query_latency_timing() so it can be unit-tested without a
// Vulkan device. Given the present IDs of the reports the driver returned
// (oldest-first) and the highest present ID we have already emitted for this
// swapchain, decide:
//   - which reports are genuinely new and should be forwarded,
//   - the new "last emitted" watermark,
//   - whether the driver restarted its present-ID counter (swapchain
//     recreation on a resolution/HDR/fullscreen change), which must reset the
//     watermark instead of dropping every fresh frame as "already seen",
//   - the newest report (for the stale re-emit fallback).
//
// A present_id of 0 means the driver did not tag the report; such reports are
// always treated as fresh (never deduped) and never advance the watermark.

#ifndef PENGUINBURNER_LATENCY_EMIT_PLAN_H
#define PENGUINBURNER_LATENCY_EMIT_PLAN_H

#include <cstddef>
#include <cstdint>
#include <vector>

namespace penguinburner {

struct LatencyEmitPlan {
    std::vector<std::size_t> emit_indices;  // report indices to forward, in order
    std::uint64_t new_last_emitted = 0;     // updated watermark
    std::size_t newest_pos = 0;             // index of newest usable report
    bool has_newest = false;                // any usable report present
    bool emitted_fresh = false;             // at least one fresh report emitted
    bool reset_detected = false;            // driver restarted present-ID counter
};

// `present_ids[i]` is report i's presentID (0 == untagged). Callers should pass
// only reports they already deem "usable" (has presentID, driver timing, or
// reflex markers); index positions are preserved so the caller can map back.
inline LatencyEmitPlan plan_latency_emits(
    const std::vector<std::uint64_t>& present_ids,
    std::uint64_t last_emitted) {
    LatencyEmitPlan plan;
    plan.new_last_emitted = last_emitted;

    // Find the newest (highest) present ID; last-wins on ties so the most
    // recently produced duplicate is chosen for the stale fallback.
    std::uint64_t newest_present_id = 0;
    std::uint64_t max_tagged = 0;
    bool any_tagged = false;
    for (std::size_t i = 0; i < present_ids.size(); ++i) {
        const std::uint64_t pid = present_ids[i];
        if (!plan.has_newest || pid >= newest_present_id) {
            newest_present_id = pid;
            plan.newest_pos = i;
            plan.has_newest = true;
        }
        if (pid != 0) {
            any_tagged = true;
            if (pid > max_tagged) {
                max_tagged = pid;
            }
        }
    }

    // Reset detection: the driver returned tagged reports but the highest one is
    // strictly below our watermark. The present-ID counter restarted, so our old
    // watermark is meaningless and would otherwise drop every fresh frame.
    if (any_tagged && last_emitted != 0 && max_tagged < last_emitted) {
        plan.reset_detected = true;
        last_emitted = 0;
        plan.new_last_emitted = 0;
    }

    for (std::size_t i = 0; i < present_ids.size(); ++i) {
        const std::uint64_t pid = present_ids[i];
        // Already-seen tagged reports are deduped away. Untagged (0) reports are
        // always fresh.
        if (pid != 0 && pid <= last_emitted) {
            continue;
        }
        plan.emit_indices.push_back(i);
        plan.emitted_fresh = true;
        if (pid > plan.new_last_emitted) {
            plan.new_last_emitted = pid;
        }
    }

    return plan;
}

}  // namespace penguinburner

#endif  // PENGUINBURNER_LATENCY_EMIT_PLAN_H
