// Shared global state, env-flag gates, and leaf time/counter helpers.
#include "latency_layer_internal.h"

namespace pblayer {
bool env_flag_enabled(const char* name) {
    const char* value = std::getenv(name);
    if (!value || !*value) {
        return false;
    }
    return std::strcmp(value, "0") != 0 && std::strcmp(value, "false") != 0
        && std::strcmp(value, "False") != 0 && std::strcmp(value, "no") != 0
        && std::strcmp(value, "off") != 0;
}

bool debug_flow_enabled() {
    static const bool enabled = env_flag_enabled(kDebugFlowEnv);
    return enabled;
}

bool display_latency_enabled() {
    static const bool enabled = env_flag_enabled(kDisplayLatencyEnv);
    return enabled;
}

bool inject_present_id_enabled() {
    static const bool enabled = env_flag_enabled(kInjectPresentIdEnv);
    return enabled;
}

TelemetrySocket g_socket;

std::mutex g_mutex;

std::unordered_map<VkInstance, InstanceContext> g_instances;

std::unordered_map<VkPhysicalDevice, VkInstance> g_physical_devices;

std::unordered_map<VkDevice, DeviceContext> g_devices;

std::unordered_map<VkQueue, QueueContext> g_queues;

std::unordered_map<VkSwapchainKHR, SwapchainContext> g_swapchains;

std::atomic<bool> g_reported_negotiate{false};

std::atomic<bool> g_reported_get_instance_proc_addr{false};

std::atomic<bool> g_reported_get_device_proc_addr{false};

std::atomic<bool> g_reported_create_instance_enter{false};

std::mutex g_overlay_config_mutex;

std::mutex g_overlay_file_mutex;

// Latest user overlay scale multiplier, published by overlay_text_config() so
// the present-path draw can read it without threading config through every call.
std::atomic<double> g_overlay_user_scale{1.0};

bool should_report_counter(uint64_t count) {
    return count <= 3 || (count % 600) == 0;
}

bool env_value_is_false(const char* value) {
    if (!value || !*value) {
        return false;
    }
    std::string text(value);
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return text == "0" || text == "false" || text == "no" || text == "off";
}

bool timing_queries_enabled() {
    return !env_value_is_false(std::getenv(kQueryTimingsEnv));
}

uint64_t now_monotonic_us() {
    using Clock = std::chrono::steady_clock;
    return static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            Clock::now().time_since_epoch())
            .count());
}

uint64_t elapsed_us(uint64_t start_us, uint64_t end_us) {
    if (!start_us || !end_us || end_us <= start_us) {
        return 0;
    }
    return end_us - start_us;
}

}  // namespace pblayer
