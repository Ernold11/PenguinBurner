#define VK_NO_PROTOTYPES

#include <vulkan/vk_layer.h>
#include <vulkan/vulkan.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cctype>
#include <cinttypes>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "latency_emit_plan.h"

namespace {

constexpr const char* kLayerName = "VK_LAYER_PENGUINBURNER_latency";
constexpr const char* kSocketEnv = "PENGUIN_BURNER_LATENCY_SOCKET";
constexpr const char* kEnableEnv = "PENGUIN_BURNER_LATENCY_LAYER";
constexpr const char* kOverlayEnableEnv = "PENGUIN_BURNER_OVERLAY";
constexpr const char* kOverlayStateEnv = "PENGUIN_BURNER_OVERLAY_STATE";
constexpr const char* kOverlayTextEnv = "PENGUIN_BURNER_OVERLAY_TEXT";
constexpr const char* kRecoveryResetEnv = "PENGUIN_BURNER_LATENCY_RECOVERY_RESET";
constexpr const char* kDebugFlowEnv = "PENGUIN_BURNER_LATENCY_DEBUG_FLOW";
constexpr const char* kQueryTimingsEnv = "PENGUIN_BURNER_LATENCY_QUERY_TIMINGS";
constexpr uint64_t kStaleRecoveryDuplicateThreshold = 240;
constexpr uint64_t kStaleRecoveryDuplicateInterval = 600;

template <typename Handle>
uint64_t handle_to_u64(Handle handle) {
    if constexpr (std::is_pointer_v<Handle>) {
        return static_cast<uint64_t>(reinterpret_cast<uintptr_t>(handle));
    } else {
        return static_cast<uint64_t>(handle);
    }
}

template <typename CreateInfo>
auto find_layer_link(CreateInfo* create_info) {
    using LayerInfo = std::conditional_t<
        std::is_same_v<std::remove_const_t<CreateInfo>, VkInstanceCreateInfo>,
        VkLayerInstanceCreateInfo,
        VkLayerDeviceCreateInfo>;

    auto* item = reinterpret_cast<LayerInfo*>(
        const_cast<void*>(static_cast<const void*>(create_info->pNext)));
    while (item) {
        if (item->sType == std::conditional_t<
                    std::is_same_v<std::remove_const_t<CreateInfo>, VkInstanceCreateInfo>,
                    std::integral_constant<VkStructureType, VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO>,
                    std::integral_constant<VkStructureType, VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO>>::value
            && item->function == VK_LAYER_LINK_INFO) {
            return item;
        }
        item = reinterpret_cast<LayerInfo*>(const_cast<void*>(item->pNext));
    }
    return static_cast<LayerInfo*>(nullptr);
}

uint32_t marker_bit(VkLatencyMarkerNV marker) {
    switch (marker) {
        case VK_LATENCY_MARKER_SIMULATION_START_NV:
            return 1u << 0;
        case VK_LATENCY_MARKER_SIMULATION_END_NV:
            return 1u << 1;
        case VK_LATENCY_MARKER_RENDERSUBMIT_START_NV:
            return 1u << 2;
        case VK_LATENCY_MARKER_RENDERSUBMIT_END_NV:
            return 1u << 3;
        case VK_LATENCY_MARKER_PRESENT_START_NV:
            return 1u << 4;
        case VK_LATENCY_MARKER_PRESENT_END_NV:
            return 1u << 5;
        case VK_LATENCY_MARKER_INPUT_SAMPLE_NV:
            return 1u << 6;
        case VK_LATENCY_MARKER_OUT_OF_BAND_RENDERSUBMIT_START_NV:
            return 1u << 7;
        case VK_LATENCY_MARKER_OUT_OF_BAND_RENDERSUBMIT_END_NV:
            return 1u << 8;
        case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_START_NV:
            return 1u << 9;
        case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_END_NV:
            return 1u << 10;
        default:
            return 0;
    }
}

bool report_has_reflex_markers(const VkLatencyTimingsFrameReportNV& report) {
    return report.inputSampleTimeUs || report.simStartTimeUs || report.simEndTimeUs
        || report.renderSubmitStartTimeUs || report.renderSubmitEndTimeUs
        || report.presentStartTimeUs || report.presentEndTimeUs;
}

bool report_has_driver_timing(const VkLatencyTimingsFrameReportNV& report) {
    return report.driverStartTimeUs || report.driverEndTimeUs
        || report.osRenderQueueStartTimeUs || report.osRenderQueueEndTimeUs
        || report.gpuRenderStartTimeUs || report.gpuRenderEndTimeUs;
}

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

class TelemetrySocket {
public:
    void send_line(const char* line, size_t length) {
        init_once();
        if (fd_ < 0 || paths_.empty()) {
            return;
        }

        for (const std::string& path : paths_) {
            sockaddr_un address{};
            address.sun_family = AF_UNIX;
            if (path.size() >= sizeof(address.sun_path)) {
                continue;
            }
            std::memcpy(address.sun_path, path.c_str(), path.size() + 1);

            ssize_t result = ::sendto(
                fd_,
                line,
                length,
                MSG_DONTWAIT | MSG_NOSIGNAL,
                reinterpret_cast<sockaddr*>(&address),
                static_cast<socklen_t>(
                    offsetof(sockaddr_un, sun_path) + path.size() + 1));
            if (result >= 0) {
                return;
            }
            if (errno == ENOENT || errno == ECONNREFUSED) {
                missed_receiver_count_.fetch_add(1, std::memory_order_relaxed);
            }
        }
    }

private:
    void add_path(std::string path) {
        if (path.empty()) {
            return;
        }
        if (std::find(paths_.begin(), paths_.end(), path) == paths_.end()) {
            paths_.push_back(std::move(path));
        }
    }

    void init_once() {
        std::call_once(init_flag_, [&]() {
            const char* explicit_path = std::getenv(kSocketEnv);
            if (explicit_path && explicit_path[0]) {
                add_path(explicit_path);
            }
            if (const char* runtime_dir = std::getenv("XDG_RUNTIME_DIR")) {
                if (runtime_dir[0]) {
                    add_path(
                        std::string(runtime_dir)
                        + "/penguin-burner/latency.sock");
                }
            }
            if (const char* home = std::getenv("HOME")) {
                if (home[0]) {
                    add_path(
                        std::string(home)
                        + "/.cache/penguin-burner/latency.sock");
                }
            }
            if (paths_.empty()) {
                char fallback[128]{};
                std::snprintf(
                    fallback,
                    sizeof(fallback),
                    "/tmp/penguin-burner-latency-%ld.sock",
                    static_cast<long>(::getuid()));
                add_path(fallback);
            }

            fd_ = ::socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        });
    }

    std::once_flag init_flag_{};
    int fd_ = -1;
    std::vector<std::string> paths_{};
    std::atomic<uint64_t> missed_receiver_count_{0};
};

TelemetrySocket g_socket;

struct DeviceTelemetryFlags {
    bool advertised = false;
    bool requested = false;
    bool functions_available = false;
    uint64_t marker_count = 0;
};

struct FlowSnapshot {
    bool advertised = false;
    bool requested = false;
    bool functions_available = false;
    bool swapchain_latency_mode = false;
    uint64_t marker_count = 0;
    uint64_t live_swapchain_count = 0;
    uint64_t present_count = 0;
    uint64_t last_vulkan_present_id = 0;
    uint64_t latest_marker_present_id = 0;
    uint64_t last_input_sample_present_id = 0;
    uint64_t last_simulation_present_id = 0;
    uint64_t last_render_submit_present_id = 0;
    uint64_t last_present_marker_present_id = 0;
    uint64_t last_oob_render_submit_present_id = 0;
    uint64_t last_oob_present_present_id = 0;
    uint64_t last_driver_report_present_id = 0;
    uint64_t driver_report_duplicate_count = 0;
};

struct MarkerTiming {
    uint32_t bits = 0;
    uint64_t input_sample_us = 0;
    uint64_t sim_start_us = 0;
    uint64_t sim_end_us = 0;
    uint64_t render_submit_start_us = 0;
    uint64_t render_submit_end_us = 0;
    uint64_t present_start_us = 0;
    uint64_t present_end_us = 0;
    uint32_t emitted_metric_bits = 0;
};

struct MarkerCounts {
    uint64_t simulation_start = 0;
    uint64_t simulation_end = 0;
    uint64_t render_submit_start = 0;
    uint64_t render_submit_end = 0;
    uint64_t present_start = 0;
    uint64_t present_end = 0;
    uint64_t input_sample = 0;
    uint64_t out_of_band_render_submit_start = 0;
    uint64_t out_of_band_render_submit_end = 0;
    uint64_t out_of_band_present_start = 0;
    uint64_t out_of_band_present_end = 0;
};

struct QueueContext {
    VkDevice device = VK_NULL_HANDLE;
    uint32_t queue_family_index = UINT32_MAX;
    uint32_t queue_index = 0;
    VkQueueFlags queue_flags = 0;
    uint32_t timestamp_valid_bits = 0;
    uint64_t present_count = 0;
    uint64_t out_of_band_notify_count = 0;
    int last_out_of_band_queue_type = -1;
};

struct InstanceContext {
    PFN_vkGetInstanceProcAddr get_instance_proc_addr = nullptr;
    PFN_vkDestroyInstance destroy_instance = nullptr;
    PFN_vkCreateDevice create_device = nullptr;
    PFN_vkEnumeratePhysicalDevices enumerate_physical_devices = nullptr;
    PFN_vkEnumerateDeviceExtensionProperties enumerate_device_extension_properties =
        nullptr;
    PFN_vkGetPhysicalDeviceProperties get_physical_device_properties = nullptr;
    PFN_vkGetPhysicalDeviceQueueFamilyProperties
        get_physical_device_queue_family_properties = nullptr;
    std::string application_name = "unknown";
    std::string engine_name = "unknown";
};

struct DeviceContext {
    VkDevice device = VK_NULL_HANDLE;
    VkPhysicalDevice physical_device = VK_NULL_HANDLE;
    PFN_vkGetDeviceProcAddr get_device_proc_addr = nullptr;
    PFN_vkDestroyDevice destroy_device = nullptr;
    PFN_vkGetDeviceQueue get_device_queue = nullptr;
    PFN_vkGetDeviceQueue2 get_device_queue2 = nullptr;
    PFN_vkCreateSwapchainKHR create_swapchain_khr = nullptr;
    PFN_vkDestroySwapchainKHR destroy_swapchain_khr = nullptr;
    PFN_vkQueuePresentKHR queue_present_khr = nullptr;
    PFN_vkSetLatencySleepModeNV set_latency_sleep_mode_nv = nullptr;
    PFN_vkLatencySleepNV latency_sleep_nv = nullptr;
    PFN_vkSetLatencyMarkerNV set_latency_marker_nv = nullptr;
    PFN_vkGetLatencyTimingsNV get_latency_timings_nv = nullptr;
    PFN_vkQueueNotifyOutOfBandNV queue_notify_out_of_band_nv = nullptr;
    VkPhysicalDeviceProperties physical_device_properties{};
    std::vector<VkQueueFamilyProperties> queue_family_properties;
    bool low_latency_extension_advertised = false;
    bool low_latency_extension_requested = false;
    bool low_latency_functions_available = false;
    uint64_t latest_marker_present_id = 0;
    uint64_t marker_count = 0;
    bool has_latency_sleep_mode_info = false;
    VkLatencySleepModeInfoNV latency_sleep_mode_info{};
    uint64_t latency_sleep_mode_set_count = 0;
    uint64_t latency_sleep_mode_reapply_count = 0;
    uint64_t latency_sleep_count = 0;
    MarkerCounts marker_counts{};
    uint64_t last_input_sample_present_id = 0;
    uint64_t last_simulation_present_id = 0;
    uint64_t last_render_submit_present_id = 0;
    uint64_t last_present_marker_present_id = 0;
    uint64_t last_oob_render_submit_present_id = 0;
    uint64_t last_oob_present_present_id = 0;
    std::unordered_map<uint64_t, uint32_t> marker_bits_by_present_id;
    std::unordered_map<uint64_t, MarkerTiming> marker_timings_by_present_id;
    std::vector<uint64_t> marker_order;
};

struct SwapchainContext {
    VkDevice device = VK_NULL_HANDLE;
    bool swapchain_latency_mode = false;
    uint64_t last_gpu_render_end_us = 0;
    uint64_t last_present_id = 0;
    uint64_t timing_sample_count = 0;
    uint64_t driver_timing_sample_count = 0;
    uint64_t last_driver_report_present_id = 0;
    uint64_t last_emitted_present_id = 0;
    uint64_t duplicate_driver_report_count = 0;
    uint64_t present_count = 0;
    uint64_t last_vulkan_present_id = 0;
    uint64_t last_present_us = 0;
    uint64_t timing_unavailable_count = 0;
    uint64_t timing_empty_count = 0;
    uint64_t latency_recovery_attempt_count = 0;
    uint64_t last_latency_recovery_duplicate_count = 0;
    std::array<uint64_t, 120> present_frametimes{};
    uint32_t present_frametime_index = 0;
    uint32_t present_frametime_count = 0;
};

struct OverlayGpuState {
    bool available = false;
    std::string clock_mhz;
    std::string voltage_mv;
    std::string profile_tier = "Balanced";
};

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
std::mutex g_overlay_file_mutex;

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

bool overlay_enabled() {
    static const bool enabled = env_flag_enabled(kOverlayEnableEnv);
    return enabled;
}

std::string overlay_runtime_path(const char* env_name, const char* file_name) {
    if (const char* explicit_path = std::getenv(env_name)) {
        if (explicit_path[0]) {
            return explicit_path;
        }
    }
    if (const char* runtime_dir = std::getenv("XDG_RUNTIME_DIR")) {
        if (runtime_dir[0]) {
            return std::string(runtime_dir) + "/penguin-burner/" + file_name;
        }
    }
    char fallback[160]{};
    std::snprintf(
        fallback,
        sizeof(fallback),
        "/tmp/penguin-burner-%s-%ld.txt",
        file_name,
        static_cast<long>(::getuid()));
    return fallback;
}

std::string trim_ascii(std::string value) {
    while (!value.empty()
        && std::isspace(static_cast<unsigned char>(value.front()))) {
        value.erase(value.begin());
    }
    while (!value.empty()
        && std::isspace(static_cast<unsigned char>(value.back()))) {
        value.pop_back();
    }
    return value;
}

OverlayGpuState read_overlay_gpu_state() {
    OverlayGpuState state{};
    const std::string path =
        overlay_runtime_path(kOverlayStateEnv, "overlay-state.txt");
    FILE* file = std::fopen(path.c_str(), "r");
    if (!file) {
        return state;
    }
    state.available = true;
    char line[512]{};
    while (std::fgets(line, sizeof(line), file)) {
        std::string text(line);
        const std::size_t sep = text.find('=');
        if (sep == std::string::npos) {
            continue;
        }
        const std::string key = trim_ascii(text.substr(0, sep));
        const std::string value = trim_ascii(text.substr(sep + 1));
        if (key == "clock_mhz") {
            state.clock_mhz = value;
        } else if (key == "voltage_mv") {
            state.voltage_mv = value;
        } else if (key == "profile_tier" && !value.empty()) {
            state.profile_tier = value;
        }
    }
    std::fclose(file);
    return state;
}

uint64_t median_fps_from_present_frametime_locked(
    SwapchainContext& context,
    uint64_t present_frametime_us) {
    if (!present_frametime_us) {
        return 0;
    }
    context.present_frametimes[context.present_frametime_index] =
        present_frametime_us;
    context.present_frametime_index =
        (context.present_frametime_index + 1)
        % static_cast<uint32_t>(context.present_frametimes.size());
    context.present_frametime_count = std::min<uint32_t>(
        context.present_frametime_count + 1,
        static_cast<uint32_t>(context.present_frametimes.size()));
    std::vector<uint64_t> values;
    values.reserve(context.present_frametime_count);
    for (uint32_t i = 0; i < context.present_frametime_count; ++i) {
        uint64_t value = context.present_frametimes[i];
        if (value) {
            values.push_back(value);
        }
    }
    if (values.empty()) {
        return 0;
    }
    std::sort(values.begin(), values.end());
    const uint64_t median = values[values.size() / 2];
    return median ? static_cast<uint64_t>((1000000ull + median / 2) / median) : 0;
}

std::string overlay_value_or_na(const std::string& value) {
    return value.empty() ? "n/a" : value;
}

std::string build_overlay_text(uint64_t fps, const OverlayGpuState& state) {
    char fps_text[64]{};
    if (fps) {
        std::snprintf(fps_text, sizeof(fps_text), "%" PRIu64 " FPS", fps);
    } else {
        std::snprintf(fps_text, sizeof(fps_text), "n/a FPS");
    }
    return std::string(fps_text)
        + " " + overlay_value_or_na(state.clock_mhz) + " MHz"
        + " " + overlay_value_or_na(state.voltage_mv) + " mV"
        + " " + (state.profile_tier.empty() ? "Balanced" : state.profile_tier);
}

void write_overlay_text_file(uint64_t fps, uint64_t now_us) {
    if (!overlay_enabled()) {
        return;
    }
    static uint64_t last_write_us = 0;
    if (last_write_us && now_us > last_write_us && now_us - last_write_us < 250000) {
        return;
    }
    last_write_us = now_us;
    const std::string path =
        overlay_runtime_path(kOverlayTextEnv, "overlay-text.txt");
    const std::string text = build_overlay_text(fps, read_overlay_gpu_state());
    std::lock_guard lock(g_overlay_file_mutex);
    const std::string temp_path = path + ".tmp";
    FILE* file = std::fopen(temp_path.c_str(), "w");
    if (!file) {
        return;
    }
    std::fprintf(file, "%s\n", text.c_str());
    std::fclose(file);
    std::rename(temp_path.c_str(), path.c_str());
}

uint64_t live_swapchain_count_locked(VkDevice device) {
    uint64_t count = 0;
    for (const auto& entry : g_swapchains) {
        if (entry.second.device == device) {
            ++count;
        }
    }
    return count;
}

DeviceTelemetryFlags device_telemetry_flags(VkDevice device) {
    std::lock_guard lock(g_mutex);
    auto it = g_devices.find(device);
    if (it == g_devices.end()) {
        return {};
    }
    return {
        it->second.low_latency_extension_advertised,
        it->second.low_latency_extension_requested,
        it->second.low_latency_functions_available,
        it->second.marker_count,
    };
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

bool swapchain_latency_mode_enabled(const VkSwapchainCreateInfoKHR* create_info) {
    if (!create_info) {
        return false;
    }
    auto* current = reinterpret_cast<const VkBaseInStructure*>(create_info->pNext);
    while (current) {
        if (current->sType == VK_STRUCTURE_TYPE_SWAPCHAIN_LATENCY_CREATE_INFO_NV) {
            auto* latency_info =
                reinterpret_cast<const VkSwapchainLatencyCreateInfoNV*>(current);
            return latency_info->latencyModeEnable == VK_TRUE;
        }
        current = reinterpret_cast<const VkBaseInStructure*>(current->pNext);
    }
    return false;
}

const char* present_mode_name(VkPresentModeKHR present_mode) {
    switch (present_mode) {
        case VK_PRESENT_MODE_IMMEDIATE_KHR:
            return "IMMEDIATE";
        case VK_PRESENT_MODE_MAILBOX_KHR:
            return "MAILBOX";
        case VK_PRESENT_MODE_FIFO_KHR:
            return "FIFO";
        case VK_PRESENT_MODE_FIFO_RELAXED_KHR:
            return "FIFO_RELAXED";
#ifdef VK_KHR_PRESENT_MODE_FIFO_LATEST_READY_EXTENSION_NAME
        case VK_PRESENT_MODE_FIFO_LATEST_READY_KHR:
            return "FIFO_LATEST_READY";
#endif
        default:
            return "UNKNOWN";
    }
}

uint64_t present_id_for_swapchain(
    const VkPresentInfoKHR* present_info,
    uint32_t swapchain_index) {
    if (!present_info || swapchain_index >= present_info->swapchainCount) {
        return 0;
    }

    auto* current =
        reinterpret_cast<const VkBaseInStructure*>(present_info->pNext);
    while (current) {
        if (current->sType == VK_STRUCTURE_TYPE_PRESENT_ID_KHR) {
            auto* present_id = reinterpret_cast<const VkPresentIdKHR*>(current);
            if (present_id->pPresentIds
                && swapchain_index < present_id->swapchainCount) {
                return present_id->pPresentIds[swapchain_index];
            }
#ifdef VK_KHR_PRESENT_ID_2_EXTENSION_NAME
        } else if (current->sType == VK_STRUCTURE_TYPE_PRESENT_ID_2_KHR) {
            auto* present_id = reinterpret_cast<const VkPresentId2KHR*>(current);
            if (present_id->pPresentIds
                && swapchain_index < present_id->swapchainCount) {
                return present_id->pPresentIds[swapchain_index];
            }
#endif
        }
        current = reinterpret_cast<const VkBaseInStructure*>(current->pNext);
    }
    return 0;
}

void send_status_event(
    const char* event,
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t count,
    DeviceTelemetryFlags flags) {
    char line[768]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"status\",\"event\":\"%s\",\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"count\":%" PRIu64 ","
        "\"vk_nv_low_latency2_advertised\":%s,"
        "\"vk_nv_low_latency2_requested\":%s,"
        "\"vk_nv_low_latency2_functions\":%s,"
        "\"marker_count\":%" PRIu64 "}",
        event,
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        count,
        flags.advertised ? "true" : "false",
        flags.requested ? "true" : "false",
        flags.functions_available ? "true" : "false",
        flags.marker_count);

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

void send_swapchain_event(
    const char* event,
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkSwapchainCreateInfoKHR* create_info,
    uint64_t live_swapchain_count,
    bool latency_mode_enabled,
    DeviceTelemetryFlags flags) {
    const VkSwapchainKHR old_swapchain =
        create_info ? create_info->oldSwapchain : VK_NULL_HANDLE;
    const uint32_t min_image_count = create_info ? create_info->minImageCount : 0;
    const uint32_t image_width = create_info ? create_info->imageExtent.width : 0;
    const uint32_t image_height = create_info ? create_info->imageExtent.height : 0;
    const int image_format = create_info ? static_cast<int>(create_info->imageFormat) : 0;
    const int present_mode = create_info ? static_cast<int>(create_info->presentMode) : -1;
    const char* present_mode_label =
        create_info ? present_mode_name(create_info->presentMode) : "UNKNOWN";
    char line[1024]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"status\",\"event\":\"%s\",\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"old_swapchain\":\"0x%016" PRIx64 "\","
        "\"count\":1,\"live_swapchain_count\":%" PRIu64 ","
        "\"min_image_count\":%u,\"image_width\":%u,\"image_height\":%u,"
        "\"image_format\":%d,\"present_mode\":%d,\"present_mode_name\":\"%s\","
        "\"swapchain_latency_mode\":%s,"
        "\"vk_nv_low_latency2_advertised\":%s,"
        "\"vk_nv_low_latency2_requested\":%s,"
        "\"vk_nv_low_latency2_functions\":%s,"
        "\"marker_count\":%" PRIu64 "}",
        event,
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        handle_to_u64(old_swapchain),
        live_swapchain_count,
        min_image_count,
        image_width,
        image_height,
        image_format,
        present_mode,
        present_mode_label,
        latency_mode_enabled ? "true" : "false",
        flags.advertised ? "true" : "false",
        flags.requested ? "true" : "false",
        flags.functions_available ? "true" : "false",
        flags.marker_count);

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

void send_latency_sleep_mode_event(
    const char* event,
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t count,
    VkResult result,
    bool has_sleep_mode_info,
    const VkLatencySleepModeInfoNV& sleep_mode_info,
    uint64_t duplicate_count,
    uint64_t present_count,
    DeviceTelemetryFlags flags) {
    char line[1024]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"status\",\"event\":\"%s\",\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"count\":%" PRIu64 ",\"result\":%d,"
        "\"has_latency_sleep_mode\":%s,"
        "\"low_latency_mode\":%s,\"low_latency_boost\":%s,"
        "\"minimum_interval_us\":%u,"
        "\"driver_report_duplicate_count\":%" PRIu64 ","
        "\"present_count\":%" PRIu64 ","
        "\"vk_nv_low_latency2_advertised\":%s,"
        "\"vk_nv_low_latency2_requested\":%s,"
        "\"vk_nv_low_latency2_functions\":%s,"
        "\"marker_count\":%" PRIu64 "}",
        event,
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        count,
        static_cast<int>(result),
        has_sleep_mode_info ? "true" : "false",
        has_sleep_mode_info && sleep_mode_info.lowLatencyMode ? "true" : "false",
        has_sleep_mode_info && sleep_mode_info.lowLatencyBoost ? "true" : "false",
        has_sleep_mode_info ? sleep_mode_info.minimumIntervalUs : 0,
        duplicate_count,
        present_count,
        flags.advertised ? "true" : "false",
        flags.requested ? "true" : "false",
        flags.functions_available ? "true" : "false",
        flags.marker_count);

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

FlowSnapshot flow_snapshot(VkDevice device, VkSwapchainKHR swapchain) {
    FlowSnapshot snapshot{};
    std::lock_guard lock(g_mutex);

    auto device_it = g_devices.find(device);
    if (device_it != g_devices.end()) {
        const auto& context = device_it->second;
        snapshot.advertised = context.low_latency_extension_advertised;
        snapshot.requested = context.low_latency_extension_requested;
        snapshot.functions_available = context.low_latency_functions_available;
        snapshot.marker_count = context.marker_count;
        snapshot.live_swapchain_count = live_swapchain_count_locked(device);
        snapshot.latest_marker_present_id = context.latest_marker_present_id;
        snapshot.last_input_sample_present_id = context.last_input_sample_present_id;
        snapshot.last_simulation_present_id = context.last_simulation_present_id;
        snapshot.last_render_submit_present_id =
            context.last_render_submit_present_id;
        snapshot.last_present_marker_present_id =
            context.last_present_marker_present_id;
        snapshot.last_oob_render_submit_present_id =
            context.last_oob_render_submit_present_id;
        snapshot.last_oob_present_present_id =
            context.last_oob_present_present_id;
    }

    auto swapchain_it = g_swapchains.find(swapchain);
    if (swapchain_it != g_swapchains.end()) {
        const auto& context = swapchain_it->second;
        snapshot.swapchain_latency_mode = context.swapchain_latency_mode;
        snapshot.present_count = context.present_count;
        snapshot.last_vulkan_present_id = context.last_vulkan_present_id;
        snapshot.last_driver_report_present_id =
            context.last_driver_report_present_id;
        snapshot.driver_report_duplicate_count =
            context.duplicate_driver_report_count;
    }

    return snapshot;
}

void send_flow_state_event(
    const char* event,
    VkDevice device,
    VkSwapchainKHR swapchain,
    VkQueue queue,
    uint64_t count,
    VkResult result,
    int queue_type,
    uint64_t sleep_value,
    const FlowSnapshot& snapshot) {
    char line[1600]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"status\",\"event\":\"%s\",\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\","
        "\"swapchain\":\"0x%016" PRIx64 "\","
        "\"queue\":\"0x%016" PRIx64 "\","
        "\"count\":%" PRIu64 ",\"result\":%d,"
        "\"queue_type\":%d,\"sleep_value\":%" PRIu64 ","
        "\"live_swapchain_count\":%" PRIu64 ","
        "\"swapchain_latency_mode\":%s,"
        "\"present_count\":%" PRIu64 ","
        "\"last_vulkan_present_id\":%" PRIu64 ","
        "\"latest_marker_present_id\":%" PRIu64 ","
        "\"last_input_sample_present_id\":%" PRIu64 ","
        "\"last_simulation_present_id\":%" PRIu64 ","
        "\"last_render_submit_present_id\":%" PRIu64 ","
        "\"last_present_marker_present_id\":%" PRIu64 ","
        "\"last_oob_render_submit_present_id\":%" PRIu64 ","
        "\"last_oob_present_present_id\":%" PRIu64 ","
        "\"last_driver_report_present_id\":%" PRIu64 ","
        "\"driver_report_duplicate_count\":%" PRIu64 ","
        "\"vk_nv_low_latency2_advertised\":%s,"
        "\"vk_nv_low_latency2_requested\":%s,"
        "\"vk_nv_low_latency2_functions\":%s,"
        "\"marker_count\":%" PRIu64 "}",
        event,
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        handle_to_u64(queue),
        count,
        static_cast<int>(result),
        queue_type,
        sleep_value,
        snapshot.live_swapchain_count,
        snapshot.swapchain_latency_mode ? "true" : "false",
        snapshot.present_count,
        snapshot.last_vulkan_present_id,
        snapshot.latest_marker_present_id,
        snapshot.last_input_sample_present_id,
        snapshot.last_simulation_present_id,
        snapshot.last_render_submit_present_id,
        snapshot.last_present_marker_present_id,
        snapshot.last_oob_render_submit_present_id,
        snapshot.last_oob_present_present_id,
        snapshot.last_driver_report_present_id,
        snapshot.driver_report_duplicate_count,
        snapshot.advertised ? "true" : "false",
        snapshot.requested ? "true" : "false",
        snapshot.functions_available ? "true" : "false",
        snapshot.marker_count);

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

uint32_t marker_timing_metric_bits(const MarkerTiming& timing) {
    uint32_t bits = 0;
    if (elapsed_us(timing.input_sample_us, timing.present_end_us)) {
        bits |= 1u << 0;
    }
    if (elapsed_us(timing.render_submit_start_us, timing.render_submit_end_us)) {
        bits |= 1u << 1;
    }
    if (elapsed_us(timing.sim_start_us, timing.sim_end_us)) {
        bits |= 1u << 2;
    }
    if (elapsed_us(timing.present_start_us, timing.present_end_us)) {
        bits |= 1u << 3;
    }
    return bits;
}

const char* marker_timing_quality(const MarkerTiming& timing) {
    if (elapsed_us(timing.input_sample_us, timing.present_end_us)) {
        return "reflex-input-present";
    }
    if (elapsed_us(timing.render_submit_start_us, timing.render_submit_end_us)) {
        return "reflex-render-submit";
    }
    if (elapsed_us(timing.sim_start_us, timing.sim_end_us)) {
        return "reflex-simulation";
    }
    return "reflex-marker-presence";
}

void send_marker_coverage_event(
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t count,
    const MarkerCounts& marker_counts,
    DeviceTelemetryFlags flags) {
    char line[1024]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"status\",\"event\":\"latency-marker-coverage\","
        "\"pid\":%ld,\"device\":\"0x%016" PRIx64 "\","
        "\"swapchain\":\"0x%016" PRIx64 "\",\"count\":%" PRIu64 ","
        "\"simulation_start\":%" PRIu64 ","
        "\"simulation_end\":%" PRIu64 ","
        "\"render_submit_start\":%" PRIu64 ","
        "\"render_submit_end\":%" PRIu64 ","
        "\"present_start\":%" PRIu64 ","
        "\"present_end\":%" PRIu64 ","
        "\"input_sample\":%" PRIu64 ","
        "\"out_of_band_render_submit_start\":%" PRIu64 ","
        "\"out_of_band_render_submit_end\":%" PRIu64 ","
        "\"out_of_band_present_start\":%" PRIu64 ","
        "\"out_of_band_present_end\":%" PRIu64 ","
        "\"vk_nv_low_latency2_advertised\":%s,"
        "\"vk_nv_low_latency2_requested\":%s,"
        "\"vk_nv_low_latency2_functions\":%s,"
        "\"marker_count\":%" PRIu64 "}",
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        count,
        marker_counts.simulation_start,
        marker_counts.simulation_end,
        marker_counts.render_submit_start,
        marker_counts.render_submit_end,
        marker_counts.present_start,
        marker_counts.present_end,
        marker_counts.input_sample,
        marker_counts.out_of_band_render_submit_start,
        marker_counts.out_of_band_render_submit_end,
        marker_counts.out_of_band_present_start,
        marker_counts.out_of_band_present_end,
        flags.advertised ? "true" : "false",
        flags.requested ? "true" : "false",
        flags.functions_available ? "true" : "false",
        flags.marker_count);

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

void send_marker_timing_sample(
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t present_id,
    const MarkerTiming& timing,
    uint64_t sample_count,
    DeviceTelemetryFlags flags) {
    const uint64_t render_submit_us = elapsed_us(
        timing.render_submit_start_us,
        timing.render_submit_end_us);
    const uint64_t input_to_present_us = elapsed_us(
        timing.input_sample_us,
        timing.present_end_us);
    const uint64_t sim_us = elapsed_us(timing.sim_start_us, timing.sim_end_us);
    const uint64_t present_marker_us = elapsed_us(
        timing.present_start_us,
        timing.present_end_us);

    char line[1024]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"timing\",\"measurement\":\"marker-proxy\","
        "\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"present_id\":%" PRIu64 ",\"quality\":\"%s\","
        "\"timing_count\":0,\"sample_count\":%" PRIu64 ","
        "\"marker_bits\":%u,\"vk_nv_low_latency2_advertised\":%s,"
        "\"vk_nv_low_latency2_requested\":%s,"
        "\"vk_nv_low_latency2_functions\":%s,"
        "\"gpu_frame_time_us\":0,"
        "\"render_submit_us\":%" PRIu64 ","
        "\"input_to_present_us\":%" PRIu64 ","
        "\"sim_us\":%" PRIu64 ","
        "\"present_marker_us\":%" PRIu64 ","
        "\"gpu_render_start_us\":0,"
        "\"gpu_render_end_us\":0,"
        "\"driver_start_us\":0,"
        "\"driver_end_us\":0}",
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        present_id,
        marker_timing_quality(timing),
        sample_count,
        timing.bits,
        flags.advertised ? "true" : "false",
        flags.requested ? "true" : "false",
        flags.functions_available ? "true" : "false",
        render_submit_us,
        input_to_present_us,
        sim_us,
        present_marker_us);

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

void send_status_event_once(const char* event, std::atomic<bool>& reported) {
    if (!std::getenv(kEnableEnv)) {
        return;
    }

    bool expected = false;
    if (!reported.compare_exchange_strong(expected, true)) {
        return;
    }

    send_status_event(
        event,
        VK_NULL_HANDLE,
        VK_NULL_HANDLE,
        1,
        {});
}

__attribute__((constructor)) void layer_loaded() {
    if (!std::getenv(kEnableEnv)) {
        return;
    }
    send_status_event(
        "layer-loaded",
        VK_NULL_HANDLE,
        VK_NULL_HANDLE,
        1,
        {});
}

bool extension_requested(
    const VkDeviceCreateInfo* create_info,
    const char* extension_name) {
    if (!create_info || !create_info->ppEnabledExtensionNames) {
        return false;
    }
    for (uint32_t i = 0; i < create_info->enabledExtensionCount; ++i) {
        const char* enabled = create_info->ppEnabledExtensionNames[i];
        if (enabled && std::strcmp(enabled, extension_name) == 0) {
            return true;
        }
    }
    return false;
}

bool extension_advertised(
    const InstanceContext& instance,
    VkPhysicalDevice physical_device,
    const char* extension_name) {
    if (!instance.enumerate_device_extension_properties) {
        return false;
    }

    uint32_t count = 0;
    VkResult result = instance.enumerate_device_extension_properties(
        physical_device,
        nullptr,
        &count,
        nullptr);
    if (result != VK_SUCCESS || count == 0) {
        return false;
    }

    std::vector<VkExtensionProperties> properties(count);
    result = instance.enumerate_device_extension_properties(
        physical_device,
        nullptr,
        &count,
        properties.data());
    if (result != VK_SUCCESS && result != VK_INCOMPLETE) {
        return false;
    }

    return std::any_of(
        properties.begin(),
        properties.begin() + std::min<uint32_t>(count, properties.size()),
        [&](const VkExtensionProperties& property) {
            return std::strcmp(property.extensionName, extension_name) == 0;
        });
}

void store_queue(
    VkDevice device,
    VkQueue queue,
    uint32_t queue_family_index,
    uint32_t queue_index) {
    if (queue == VK_NULL_HANDLE) {
        return;
    }
    QueueContext queue_context{};
    queue_context.device = device;
    queue_context.queue_family_index = queue_family_index;
    queue_context.queue_index = queue_index;

    std::lock_guard lock(g_mutex);
    auto device_it = g_devices.find(device);
    if (device_it != g_devices.end()) {
        if (queue_family_index
            < device_it->second.queue_family_properties.size()) {
            const auto& properties =
                device_it->second.queue_family_properties[queue_family_index];
            queue_context.queue_flags = properties.queueFlags;
            queue_context.timestamp_valid_bits = properties.timestampValidBits;
        }
    }
    g_queues[queue] = queue_context;
}

void record_marker(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkSetLatencyMarkerInfoNV* marker_info) {
    if (!marker_info || marker_info->presentID == 0) {
        return;
    }

    uint32_t bit = marker_bit(marker_info->marker);
    if (!bit) {
        return;
    }

    const uint64_t marker_time_us = now_monotonic_us();
    uint64_t marker_count = 0;
    uint64_t marker_sample_count = 0;
    bool send_marker_sample = false;
    DeviceTelemetryFlags flags{};
    MarkerTiming marker_timing{};
    MarkerCounts marker_counts{};
    {
        std::lock_guard lock(g_mutex);
        auto device_it = g_devices.find(device);
        if (device_it == g_devices.end()) {
            return;
        }

        auto& context = device_it->second;
        context.latest_marker_present_id = marker_info->presentID;
        uint32_t& bits = context.marker_bits_by_present_id[marker_info->presentID];
        if (!bits) {
            context.marker_order.push_back(marker_info->presentID);
            if (context.marker_order.size() > 256) {
                uint64_t stale = context.marker_order.front();
                context.marker_order.erase(context.marker_order.begin());
                context.marker_bits_by_present_id.erase(stale);
                context.marker_timings_by_present_id.erase(stale);
            }
        }
        bits |= bit;
        auto& timing = context.marker_timings_by_present_id[marker_info->presentID];
        timing.bits |= bit;
        switch (marker_info->marker) {
            case VK_LATENCY_MARKER_SIMULATION_START_NV:
                context.marker_counts.simulation_start++;
                context.last_simulation_present_id = marker_info->presentID;
                timing.sim_start_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_SIMULATION_END_NV:
                context.marker_counts.simulation_end++;
                context.last_simulation_present_id = marker_info->presentID;
                timing.sim_end_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_RENDERSUBMIT_START_NV:
                context.marker_counts.render_submit_start++;
                context.last_render_submit_present_id = marker_info->presentID;
                timing.render_submit_start_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_RENDERSUBMIT_END_NV:
                context.marker_counts.render_submit_end++;
                context.last_render_submit_present_id = marker_info->presentID;
                timing.render_submit_end_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_PRESENT_START_NV:
                context.marker_counts.present_start++;
                context.last_present_marker_present_id = marker_info->presentID;
                timing.present_start_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_PRESENT_END_NV:
                context.marker_counts.present_end++;
                context.last_present_marker_present_id = marker_info->presentID;
                timing.present_end_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_INPUT_SAMPLE_NV:
                context.marker_counts.input_sample++;
                context.last_input_sample_present_id = marker_info->presentID;
                timing.input_sample_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_RENDERSUBMIT_START_NV:
                context.marker_counts.out_of_band_render_submit_start++;
                context.last_oob_render_submit_present_id = marker_info->presentID;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_RENDERSUBMIT_END_NV:
                context.marker_counts.out_of_band_render_submit_end++;
                context.last_oob_render_submit_present_id = marker_info->presentID;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_START_NV:
                context.marker_counts.out_of_band_present_start++;
                context.last_oob_present_present_id = marker_info->presentID;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_END_NV:
                context.marker_counts.out_of_band_present_end++;
                context.last_oob_present_present_id = marker_info->presentID;
                break;
            default:
                break;
        }

        marker_count = ++context.marker_count;
        flags = {
            context.low_latency_extension_advertised,
            context.low_latency_extension_requested,
            context.low_latency_functions_available,
            context.marker_count,
        };
        marker_counts = context.marker_counts;
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end()) {
            swapchain_it->second.last_present_id = marker_info->presentID;
        }
        uint32_t metric_bits = marker_timing_metric_bits(timing);
        if (metric_bits && metric_bits != timing.emitted_metric_bits) {
            timing.emitted_metric_bits = metric_bits;
            marker_timing = timing;
            send_marker_sample = true;

            if (swapchain_it != g_swapchains.end()) {
                auto& swapchain_context = swapchain_it->second;
                marker_sample_count = ++swapchain_context.timing_sample_count;
            }
        }
    }

    if (should_report_counter(marker_count)) {
        send_status_event(
            "latency-marker",
            device,
            swapchain,
            marker_count,
            flags);
        send_marker_coverage_event(
            device,
            swapchain,
            marker_count,
            marker_counts,
            flags);
    }
    if (send_marker_sample) {
        send_marker_timing_sample(
            device,
            swapchain,
            marker_info->presentID,
            marker_timing,
            marker_sample_count,
            flags);
    }
}

void send_timing_sample(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkLatencyTimingsFrameReportNV& report,
    uint32_t timing_count) {
    uint32_t marker_bits = 0;
    uint64_t gpu_frame_time_us = 0;
    uint64_t sample_count = 0;
    uint64_t driver_report_count = 0;
    uint64_t driver_report_duplicate_count = 0;
    bool advertised = false;
    bool requested = false;
    bool functions_available = false;

    {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end()) {
            auto& swapchain_context = swapchain_it->second;
            if (report.gpuRenderEndTimeUs && swapchain_context.last_gpu_render_end_us
                && report.gpuRenderEndTimeUs > swapchain_context.last_gpu_render_end_us) {
                gpu_frame_time_us =
                    report.gpuRenderEndTimeUs - swapchain_context.last_gpu_render_end_us;
            }
            if (report.gpuRenderEndTimeUs) {
                swapchain_context.last_gpu_render_end_us = report.gpuRenderEndTimeUs;
            }
            driver_report_count = ++swapchain_context.driver_timing_sample_count;
            if (report.presentID
                && report.presentID == swapchain_context.last_driver_report_present_id) {
                driver_report_duplicate_count =
                    ++swapchain_context.duplicate_driver_report_count;
            } else {
                swapchain_context.last_driver_report_present_id = report.presentID;
                swapchain_context.duplicate_driver_report_count = 0;
            }
            swapchain_context.last_present_id = report.presentID;
            sample_count = ++swapchain_context.timing_sample_count;
        }

        auto device_it = g_devices.find(device);
        if (device_it != g_devices.end()) {
            auto& device_context = device_it->second;
            auto marker_it =
                device_context.marker_bits_by_present_id.find(report.presentID);
            if (marker_it != device_context.marker_bits_by_present_id.end()) {
                marker_bits = marker_it->second;
            }
            advertised = device_context.low_latency_extension_advertised;
            requested = device_context.low_latency_extension_requested;
            functions_available = device_context.low_latency_functions_available;
        }
    }

    uint64_t render_submit_us = 0;
    if (report.renderSubmitStartTimeUs && report.renderSubmitEndTimeUs
        && report.renderSubmitEndTimeUs > report.renderSubmitStartTimeUs) {
        render_submit_us =
            report.renderSubmitEndTimeUs - report.renderSubmitStartTimeUs;
    }

    uint64_t input_to_present_us = 0;
    if (report.inputSampleTimeUs && report.presentEndTimeUs
        && report.presentEndTimeUs > report.inputSampleTimeUs) {
        input_to_present_us = report.presentEndTimeUs - report.inputSampleTimeUs;
    }

    uint64_t render_present_us = 0;
    if (report.presentStartTimeUs && report.gpuRenderEndTimeUs
        && report.gpuRenderEndTimeUs > report.presentStartTimeUs) {
        render_present_us = report.gpuRenderEndTimeUs - report.presentStartTimeUs;
    }

    uint64_t gpu_render_us = 0;
    if (report.gpuRenderStartTimeUs && report.gpuRenderEndTimeUs
        && report.gpuRenderEndTimeUs > report.gpuRenderStartTimeUs) {
        gpu_render_us = report.gpuRenderEndTimeUs - report.gpuRenderStartTimeUs;
    }

    const bool has_reflex_report_timestamps = report_has_reflex_markers(report);
    const bool has_driver_timing = report_has_driver_timing(report);
    const char* quality = "present-frametime";
    if (input_to_present_us) {
        quality = "reflex-input-present";
    } else if (render_submit_us) {
        quality = "reflex-render-submit";
    } else if (has_reflex_report_timestamps) {
        quality = "reflex-markers";
    } else if (has_driver_timing) {
        quality = "driver-timing";
    } else if (marker_bits) {
        quality = "reflex-marker-presence";
    }

    char line[1024]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"timing\",\"measurement\":\"driver-report\","
        "\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"present_id\":%" PRIu64 ",\"quality\":\"%s\","
        "\"timing_count\":%u,\"sample_count\":%" PRIu64 ","
        "\"driver_report_count\":%" PRIu64 ","
        "\"driver_report_duplicate_count\":%" PRIu64 ","
        "\"marker_bits\":%u,\"vk_nv_low_latency2_advertised\":%s,"
        "\"vk_nv_low_latency2_requested\":%s,"
        "\"vk_nv_low_latency2_functions\":%s,"
        "\"gpu_frame_time_us\":%" PRIu64 ","
        "\"gpu_render_us\":%" PRIu64 ","
        "\"render_submit_us\":%" PRIu64 ","
        "\"render_present_us\":%" PRIu64 ","
        "\"input_to_present_us\":%" PRIu64 ","
        "\"input_sample_us\":%" PRIu64 ","
        "\"sim_start_us\":%" PRIu64 ","
        "\"sim_end_us\":%" PRIu64 ","
        "\"render_submit_start_us\":%" PRIu64 ","
        "\"render_submit_end_us\":%" PRIu64 ","
        "\"present_start_us\":%" PRIu64 ","
        "\"present_end_us\":%" PRIu64 ","
        "\"driver_start_us\":%" PRIu64 ","
        "\"driver_end_us\":%" PRIu64 ","
        "\"os_render_queue_start_us\":%" PRIu64 ","
        "\"os_render_queue_end_us\":%" PRIu64 ","
        "\"gpu_render_start_us\":%" PRIu64 ","
        "\"gpu_render_end_us\":%" PRIu64 "}",
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        report.presentID,
        quality,
        timing_count,
        sample_count,
        driver_report_count,
        driver_report_duplicate_count,
        marker_bits,
        advertised ? "true" : "false",
        requested ? "true" : "false",
        functions_available ? "true" : "false",
        gpu_frame_time_us,
        gpu_render_us,
        render_submit_us,
        render_present_us,
        input_to_present_us,
        report.inputSampleTimeUs,
        report.simStartTimeUs,
        report.simEndTimeUs,
        report.renderSubmitStartTimeUs,
        report.renderSubmitEndTimeUs,
        report.presentStartTimeUs,
        report.presentEndTimeUs,
        report.driverStartTimeUs,
        report.driverEndTimeUs,
        report.osRenderQueueStartTimeUs,
        report.osRenderQueueEndTimeUs,
        report.gpuRenderStartTimeUs,
        report.gpuRenderEndTimeUs);

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

// Present-to-present frametime. This is the only cadence signal that works for
// every Vulkan app regardless of Reflex/markers, so it is the lowest tier of the
// quality ladder (quality=present-frametime). It counts *presented* frames, so
// with frame generation active it reflects presented FPS, not the real rendered
// frame cadence -- callers must not treat it as input-bearing latency.
void send_present_pacing_sample(
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t present_frametime_us,
    uint64_t present_count) {
    char line[512]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"timing\",\"measurement\":\"present-pacing\","
        "\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"quality\":\"present-frametime\","
        "\"present_count\":%" PRIu64 ","
        "\"present_frametime_us\":%" PRIu64 "}",
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        present_count,
        present_frametime_us);

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

struct LatencySleepModeReplay {
    PFN_vkSetLatencySleepModeNV set_latency_sleep_mode = nullptr;
    VkLatencySleepModeInfoNV sleep_mode_info{};
    bool has_sleep_mode_info = false;
    uint64_t count = 0;
    DeviceTelemetryFlags flags{};
};

LatencySleepModeReplay load_latency_sleep_mode_replay(VkDevice device) {
    LatencySleepModeReplay replay{};
    std::lock_guard lock(g_mutex);
    auto device_it = g_devices.find(device);
    if (device_it == g_devices.end()) {
        return replay;
    }

    auto& context = device_it->second;
    replay.set_latency_sleep_mode = context.set_latency_sleep_mode_nv;
    replay.has_sleep_mode_info = context.has_latency_sleep_mode_info;
    if (!replay.set_latency_sleep_mode || !replay.has_sleep_mode_info) {
        return replay;
    }
    replay.sleep_mode_info = context.latency_sleep_mode_info;
    replay.count = ++context.latency_sleep_mode_reapply_count;
    replay.flags = {
        context.low_latency_extension_advertised,
        context.low_latency_extension_requested,
        context.low_latency_functions_available,
        context.marker_count,
    };
    return replay;
}

bool reapply_latency_sleep_mode(
    const char* event,
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t duplicate_count,
    uint64_t present_count) {
    LatencySleepModeReplay replay = load_latency_sleep_mode_replay(device);
    if (!replay.set_latency_sleep_mode || !replay.has_sleep_mode_info) {
        return false;
    }

    VkResult result =
        replay.set_latency_sleep_mode(device, swapchain, &replay.sleep_mode_info);
    send_latency_sleep_mode_event(
        event,
        device,
        swapchain,
        replay.count,
        result,
        true,
        replay.sleep_mode_info,
        duplicate_count,
        present_count,
        replay.flags);
    return result == VK_SUCCESS;
}

void maybe_recover_stale_latency_stream(VkDevice device, VkSwapchainKHR swapchain) {
    PFN_vkSetLatencySleepModeNV set_latency_sleep_mode = nullptr;
    VkLatencySleepModeInfoNV sleep_mode_info{};
    bool has_sleep_mode_info = false;
    uint64_t attempt_count = 0;
    uint64_t duplicate_count = 0;
    uint64_t present_count = 0;
    DeviceTelemetryFlags flags{};

    {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it == g_swapchains.end()) {
            return;
        }
        auto& swapchain_context = swapchain_it->second;
        duplicate_count = swapchain_context.duplicate_driver_report_count;
        if (duplicate_count < kStaleRecoveryDuplicateThreshold) {
            return;
        }
        if (swapchain_context.last_latency_recovery_duplicate_count
            && duplicate_count - swapchain_context.last_latency_recovery_duplicate_count
                < kStaleRecoveryDuplicateInterval) {
            return;
        }
        swapchain_context.last_latency_recovery_duplicate_count = duplicate_count;
        attempt_count = ++swapchain_context.latency_recovery_attempt_count;
        present_count = swapchain_context.present_count;

        auto device_it = g_devices.find(device);
        if (device_it != g_devices.end()) {
            const auto& device_context = device_it->second;
            set_latency_sleep_mode = device_context.set_latency_sleep_mode_nv;
            has_sleep_mode_info = device_context.has_latency_sleep_mode_info;
            sleep_mode_info = device_context.latency_sleep_mode_info;
            flags = {
                device_context.low_latency_extension_advertised,
                device_context.low_latency_extension_requested,
                device_context.low_latency_functions_available,
                device_context.marker_count,
            };
        }
    }

    send_flow_state_event(
        "latency-stream-stale",
        device,
        swapchain,
        VK_NULL_HANDLE,
        attempt_count,
        VK_NOT_READY,
        -1,
        0,
        flow_snapshot(device, swapchain));

    if (!set_latency_sleep_mode || !has_sleep_mode_info) {
        send_latency_sleep_mode_event(
            "latency-recovery-unavailable",
            device,
            swapchain,
            attempt_count,
            VK_ERROR_EXTENSION_NOT_PRESENT,
            has_sleep_mode_info,
            sleep_mode_info,
            duplicate_count,
            present_count,
            flags);
        return;
    }

    VkLatencySleepModeInfoNV disabled_sleep_mode_info = sleep_mode_info;
    disabled_sleep_mode_info.lowLatencyMode = VK_FALSE;
    disabled_sleep_mode_info.lowLatencyBoost = VK_FALSE;
    VkResult disable_result =
        set_latency_sleep_mode(device, swapchain, &disabled_sleep_mode_info);
    send_latency_sleep_mode_event(
        "latency-recovery-disable-sleep-mode",
        device,
        swapchain,
        attempt_count,
        disable_result,
        true,
        disabled_sleep_mode_info,
        duplicate_count,
        present_count,
        flags);

    if (env_flag_enabled(kRecoveryResetEnv)) {
        send_latency_sleep_mode_event(
            "latency-recovery-reset-sleep-mode-enter",
            device,
            swapchain,
            attempt_count,
            VK_NOT_READY,
            false,
            disabled_sleep_mode_info,
            duplicate_count,
            present_count,
            flags);
        VkResult reset_result = set_latency_sleep_mode(device, swapchain, nullptr);
        send_latency_sleep_mode_event(
            "latency-recovery-reset-sleep-mode",
            device,
            swapchain,
            attempt_count,
            reset_result,
            false,
            disabled_sleep_mode_info,
            duplicate_count,
            present_count,
            flags);
    }

    VkResult result = set_latency_sleep_mode(device, swapchain, &sleep_mode_info);
    send_latency_sleep_mode_event(
        "latency-recovery-reapply-sleep-mode",
        device,
        swapchain,
        attempt_count,
        result,
        true,
        sleep_mode_info,
        duplicate_count,
        present_count,
        flags);
}

void query_latency_timing(VkDevice device, VkSwapchainKHR swapchain) {
    PFN_vkGetLatencyTimingsNV get_latency_timings = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto device_it = g_devices.find(device);
        if (device_it == g_devices.end()) {
            return;
        }
        get_latency_timings = device_it->second.get_latency_timings_nv;
    }

    if (!get_latency_timings) {
        uint64_t count = 0;
        {
            std::lock_guard lock(g_mutex);
            auto swapchain_it = g_swapchains.find(swapchain);
            if (swapchain_it != g_swapchains.end()) {
                count = ++swapchain_it->second.timing_unavailable_count;
            }
        }
        if (should_report_counter(count)) {
            send_status_event(
                "latency-timing-unavailable",
                device,
                swapchain,
                count,
                device_telemetry_flags(device));
        }
        return;
    }

    VkGetLatencyMarkerInfoNV info{};
    info.sType = VK_STRUCTURE_TYPE_GET_LATENCY_MARKER_INFO_NV;
    get_latency_timings(device, swapchain, &info);
    if (info.timingCount == 0) {
        uint64_t count = 0;
        {
            std::lock_guard lock(g_mutex);
            auto swapchain_it = g_swapchains.find(swapchain);
            if (swapchain_it != g_swapchains.end()) {
                count = ++swapchain_it->second.timing_empty_count;
            }
        }
        if (should_report_counter(count)) {
            send_status_event(
                "latency-timing-empty",
                device,
                swapchain,
                count,
                device_telemetry_flags(device));
        }
        return;
    }

    constexpr uint32_t kMaxTimings = 64;
    const uint32_t timing_count = std::min(info.timingCount, kMaxTimings);
    std::array<VkLatencyTimingsFrameReportNV, kMaxTimings> reports{};
    for (uint32_t i = 0; i < timing_count; ++i) {
        reports[i].sType = VK_STRUCTURE_TYPE_LATENCY_TIMINGS_FRAME_REPORT_NV;
    }

    info.timingCount = timing_count;
    info.pTimings = reports.data();
    get_latency_timings(device, swapchain, &info);
    if (info.timingCount == 0) {
        uint64_t count = 0;
        {
            std::lock_guard lock(g_mutex);
            auto swapchain_it = g_swapchains.find(swapchain);
            if (swapchain_it != g_swapchains.end()) {
                count = ++swapchain_it->second.timing_empty_count;
            }
        }
        if (should_report_counter(count)) {
            send_status_event(
                "latency-timing-empty",
                device,
                swapchain,
                count,
                device_telemetry_flags(device));
        }
        return;
    }

    const uint32_t written_count = std::min(info.timingCount, timing_count);

    // The driver returns a ring of up to kMaxTimings frame reports, oldest
    // first. Emitting only the newest report re-latches the same value every
    // present once the Reflex ring stops advancing, which is what produced the
    // stuck constant latency. Instead, forward every report whose presentID is
    // newer than the last one we already emitted for this swapchain, so the
    // receiver sees genuine per-frame samples. The dedup/reset decision lives in
    // plan_latency_emits() (see latency_emit_plan.h) so it can be unit-tested.
    //
    // Collect the usable reports (preserving original index) and their present
    // IDs for the planner.
    std::vector<uint32_t> usable_indices;
    std::vector<uint64_t> usable_present_ids;
    usable_indices.reserve(written_count);
    usable_present_ids.reserve(written_count);
    for (uint32_t index = 0; index < written_count; ++index) {
        const auto& report = reports[index];
        const bool usable = report.presentID || report_has_driver_timing(report)
            || report_has_reflex_markers(report);
        if (!usable) {
            continue;
        }
        usable_indices.push_back(index);
        usable_present_ids.push_back(report.presentID);
    }

    uint64_t last_emitted = 0;
    {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end()) {
            last_emitted = swapchain_it->second.last_emitted_present_id;
        }
    }

    const penguinburner::LatencyEmitPlan plan =
        penguinburner::plan_latency_emits(usable_present_ids, last_emitted);

    for (std::size_t pos : plan.emit_indices) {
        send_timing_sample(
            device, swapchain, reports[usable_indices[pos]], written_count);
    }

    if (plan.new_last_emitted != last_emitted || plan.reset_detected) {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end()) {
            swapchain_it->second.last_emitted_present_id = plan.new_last_emitted;
        }
    }

    // Nothing newer than last time: the Reflex ring has stopped advancing.
    // Re-emit the newest report once so the receiver's duplicate-report
    // detection can flag the stream as stale instead of us silently dropping
    // it (which would otherwise leave the meter showing a frozen value).
    if (!plan.emitted_fresh && plan.has_newest) {
        send_timing_sample(
            device, swapchain, reports[usable_indices[plan.newest_pos]],
            written_count);
        maybe_recover_stale_latency_stream(device, swapchain);
    }
}

VKAPI_ATTR VkResult VKAPI_CALL layer_create_instance(
    const VkInstanceCreateInfo* create_info,
    const VkAllocationCallbacks* allocator,
    VkInstance* instance) {
    send_status_event_once(
        "create-instance-enter",
        g_reported_create_instance_enter);

    if (!create_info || !instance) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    auto* link = find_layer_link(create_info);
    if (!link || !link->u.pLayerInfo || !link->u.pLayerInfo->pfnNextGetInstanceProcAddr) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    PFN_vkGetInstanceProcAddr next_get_instance_proc_addr =
        link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;

    auto next_create_instance = reinterpret_cast<PFN_vkCreateInstance>(
        next_get_instance_proc_addr(VK_NULL_HANDLE, "vkCreateInstance"));
    if (!next_create_instance) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    VkResult result = next_create_instance(create_info, allocator, instance);
    if (result != VK_SUCCESS) {
        return result;
    }

    InstanceContext context{};
    context.get_instance_proc_addr = next_get_instance_proc_addr;
    context.destroy_instance = reinterpret_cast<PFN_vkDestroyInstance>(
        next_get_instance_proc_addr(*instance, "vkDestroyInstance"));
    context.create_device = reinterpret_cast<PFN_vkCreateDevice>(
        next_get_instance_proc_addr(*instance, "vkCreateDevice"));
    context.enumerate_physical_devices =
        reinterpret_cast<PFN_vkEnumeratePhysicalDevices>(
            next_get_instance_proc_addr(*instance, "vkEnumeratePhysicalDevices"));
    context.enumerate_device_extension_properties =
        reinterpret_cast<PFN_vkEnumerateDeviceExtensionProperties>(
            next_get_instance_proc_addr(
                *instance,
                "vkEnumerateDeviceExtensionProperties"));
    context.get_physical_device_properties =
        reinterpret_cast<PFN_vkGetPhysicalDeviceProperties>(
            next_get_instance_proc_addr(
                *instance,
                "vkGetPhysicalDeviceProperties"));
    context.get_physical_device_queue_family_properties =
        reinterpret_cast<PFN_vkGetPhysicalDeviceQueueFamilyProperties>(
            next_get_instance_proc_addr(
                *instance,
                "vkGetPhysicalDeviceQueueFamilyProperties"));

    if (create_info->pApplicationInfo) {
        if (create_info->pApplicationInfo->pApplicationName) {
            context.application_name = create_info->pApplicationInfo->pApplicationName;
        }
        if (create_info->pApplicationInfo->pEngineName) {
            context.engine_name = create_info->pApplicationInfo->pEngineName;
        }
    }

    {
        std::lock_guard lock(g_mutex);
        g_instances[*instance] = std::move(context);
    }
    send_status_event(
        "create-instance",
        VK_NULL_HANDLE,
        VK_NULL_HANDLE,
        1,
        {});
    return VK_SUCCESS;
}

VKAPI_ATTR void VKAPI_CALL layer_destroy_instance(
    VkInstance instance,
    const VkAllocationCallbacks* allocator) {
    PFN_vkDestroyInstance next_destroy_instance = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_instances.find(instance);
        if (it != g_instances.end()) {
            next_destroy_instance = it->second.destroy_instance;
        }
    }

    if (next_destroy_instance) {
        next_destroy_instance(instance, allocator);
    }

    std::lock_guard lock(g_mutex);
    g_instances.erase(instance);
    for (auto it = g_physical_devices.begin(); it != g_physical_devices.end();) {
        if (it->second == instance) {
            it = g_physical_devices.erase(it);
        } else {
            ++it;
        }
    }
}

VKAPI_ATTR VkResult VKAPI_CALL layer_enumerate_physical_devices(
    VkInstance instance,
    uint32_t* physical_device_count,
    VkPhysicalDevice* physical_devices) {
    PFN_vkEnumeratePhysicalDevices next_enumerate = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_instances.find(instance);
        if (it != g_instances.end()) {
            next_enumerate = it->second.enumerate_physical_devices;
        }
    }

    if (!next_enumerate) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    VkResult result =
        next_enumerate(instance, physical_device_count, physical_devices);
    if ((result == VK_SUCCESS || result == VK_INCOMPLETE) && physical_devices
        && physical_device_count) {
        std::lock_guard lock(g_mutex);
        for (uint32_t i = 0; i < *physical_device_count; ++i) {
            g_physical_devices[physical_devices[i]] = instance;
        }
    }
    return result;
}

VKAPI_ATTR VkResult VKAPI_CALL layer_create_device(
    VkPhysicalDevice physical_device,
    const VkDeviceCreateInfo* create_info,
    const VkAllocationCallbacks* allocator,
    VkDevice* device) {
    if (!create_info || !device) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    VkInstance instance = VK_NULL_HANDLE;
    InstanceContext instance_context{};
    {
        std::lock_guard lock(g_mutex);
        auto physical_it = g_physical_devices.find(physical_device);
        if (physical_it != g_physical_devices.end()) {
            instance = physical_it->second;
        }
        auto instance_it = g_instances.find(instance);
        if (instance_it != g_instances.end()) {
            instance_context = instance_it->second;
        }
    }

    if (!instance_context.create_device) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    auto* link = find_layer_link(create_info);
    if (!link || !link->u.pLayerInfo || !link->u.pLayerInfo->pfnNextGetDeviceProcAddr) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    PFN_vkGetDeviceProcAddr next_get_device_proc_addr =
        link->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;

    const bool advertised = extension_advertised(
        instance_context,
        physical_device,
        VK_NV_LOW_LATENCY_2_EXTENSION_NAME);
    const bool requested = extension_requested(
        create_info,
        VK_NV_LOW_LATENCY_2_EXTENSION_NAME);

    VkResult result =
        instance_context.create_device(
            physical_device,
            create_info,
            allocator,
            device);
    if (result != VK_SUCCESS) {
        return result;
    }

    DeviceContext context{};
    context.device = *device;
    context.physical_device = physical_device;
    context.get_device_proc_addr = next_get_device_proc_addr;
    context.destroy_device = reinterpret_cast<PFN_vkDestroyDevice>(
        next_get_device_proc_addr(*device, "vkDestroyDevice"));
    context.get_device_queue = reinterpret_cast<PFN_vkGetDeviceQueue>(
        next_get_device_proc_addr(*device, "vkGetDeviceQueue"));
    context.get_device_queue2 = reinterpret_cast<PFN_vkGetDeviceQueue2>(
        next_get_device_proc_addr(*device, "vkGetDeviceQueue2"));
    context.create_swapchain_khr = reinterpret_cast<PFN_vkCreateSwapchainKHR>(
        next_get_device_proc_addr(*device, "vkCreateSwapchainKHR"));
    context.destroy_swapchain_khr = reinterpret_cast<PFN_vkDestroySwapchainKHR>(
        next_get_device_proc_addr(*device, "vkDestroySwapchainKHR"));
    context.queue_present_khr = reinterpret_cast<PFN_vkQueuePresentKHR>(
        next_get_device_proc_addr(*device, "vkQueuePresentKHR"));
    context.set_latency_sleep_mode_nv =
        reinterpret_cast<PFN_vkSetLatencySleepModeNV>(
            next_get_device_proc_addr(*device, "vkSetLatencySleepModeNV"));
    context.latency_sleep_nv = reinterpret_cast<PFN_vkLatencySleepNV>(
        next_get_device_proc_addr(*device, "vkLatencySleepNV"));
    context.set_latency_marker_nv = reinterpret_cast<PFN_vkSetLatencyMarkerNV>(
        next_get_device_proc_addr(*device, "vkSetLatencyMarkerNV"));
    context.get_latency_timings_nv = reinterpret_cast<PFN_vkGetLatencyTimingsNV>(
        next_get_device_proc_addr(*device, "vkGetLatencyTimingsNV"));
    context.queue_notify_out_of_band_nv =
        reinterpret_cast<PFN_vkQueueNotifyOutOfBandNV>(
            next_get_device_proc_addr(*device, "vkQueueNotifyOutOfBandNV"));
    if (instance_context.get_physical_device_properties) {
        instance_context.get_physical_device_properties(
            physical_device,
            &context.physical_device_properties);
    }
    if (instance_context.get_physical_device_queue_family_properties) {
        uint32_t queue_family_count = 0;
        instance_context.get_physical_device_queue_family_properties(
            physical_device,
            &queue_family_count,
            nullptr);
        if (queue_family_count) {
            context.queue_family_properties.resize(queue_family_count);
            instance_context.get_physical_device_queue_family_properties(
                physical_device,
                &queue_family_count,
                context.queue_family_properties.data());
            context.queue_family_properties.resize(queue_family_count);
        }
    }
    context.low_latency_extension_advertised = advertised;
    context.low_latency_extension_requested = requested;
    context.low_latency_functions_available =
        context.set_latency_marker_nv && context.get_latency_timings_nv;

    DeviceTelemetryFlags flags{
        advertised,
        requested,
        context.low_latency_functions_available,
        0,
    };
    {
        std::lock_guard lock(g_mutex);
        g_devices[*device] = std::move(context);
    }
    send_status_event(
        "create-device",
        *device,
        VK_NULL_HANDLE,
        1,
        flags);
    return VK_SUCCESS;
}

VKAPI_ATTR void VKAPI_CALL layer_destroy_device(
    VkDevice device,
    const VkAllocationCallbacks* allocator) {
    PFN_vkDestroyDevice next_destroy_device = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_destroy_device = it->second.destroy_device;
        }
    }

    if (next_destroy_device) {
        next_destroy_device(device, allocator);
    }

    std::lock_guard lock(g_mutex);
    g_devices.erase(device);
    for (auto it = g_queues.begin(); it != g_queues.end();) {
        if (it->second.device == device) {
            it = g_queues.erase(it);
        } else {
            ++it;
        }
    }
    for (auto it = g_swapchains.begin(); it != g_swapchains.end();) {
        if (it->second.device == device) {
            it = g_swapchains.erase(it);
        } else {
            ++it;
        }
    }
}

VKAPI_ATTR void VKAPI_CALL layer_get_device_queue(
    VkDevice device,
    uint32_t queue_family_index,
    uint32_t queue_index,
    VkQueue* queue) {
    PFN_vkGetDeviceQueue next_get_device_queue = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_get_device_queue = it->second.get_device_queue;
        }
    }

    if (next_get_device_queue) {
        next_get_device_queue(device, queue_family_index, queue_index, queue);
        if (queue) {
            store_queue(device, *queue, queue_family_index, queue_index);
        }
    }
}

VKAPI_ATTR void VKAPI_CALL layer_get_device_queue2(
    VkDevice device,
    const VkDeviceQueueInfo2* queue_info,
    VkQueue* queue) {
    PFN_vkGetDeviceQueue2 next_get_device_queue2 = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_get_device_queue2 = it->second.get_device_queue2;
        }
    }

    if (next_get_device_queue2) {
        next_get_device_queue2(device, queue_info, queue);
        if (queue && queue_info) {
            store_queue(
                device,
                *queue,
                queue_info->queueFamilyIndex,
                queue_info->queueIndex);
        }
    }
}

VKAPI_ATTR VkResult VKAPI_CALL layer_create_swapchain_khr(
    VkDevice device,
    const VkSwapchainCreateInfoKHR* create_info,
    const VkAllocationCallbacks* allocator,
    VkSwapchainKHR* swapchain) {
    PFN_vkCreateSwapchainKHR next_create_swapchain = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_create_swapchain = it->second.create_swapchain_khr;
        }
    }

    if (!next_create_swapchain) {
        return VK_ERROR_EXTENSION_NOT_PRESENT;
    }

    VkResult result =
        next_create_swapchain(device, create_info, allocator, swapchain);
    const bool latency_mode_enabled = swapchain_latency_mode_enabled(create_info);
    uint64_t live_swapchain_count = 0;
    if (result == VK_SUCCESS && swapchain) {
        std::lock_guard lock(g_mutex);
        SwapchainContext context{};
        context.device = device;
        context.swapchain_latency_mode = latency_mode_enabled;
        g_swapchains[*swapchain] = context;
        live_swapchain_count = live_swapchain_count_locked(device);
    }
    if (result == VK_SUCCESS && swapchain) {
        send_swapchain_event(
            "create-swapchain",
            device,
            *swapchain,
            create_info,
            live_swapchain_count,
            latency_mode_enabled,
            device_telemetry_flags(device));
        reapply_latency_sleep_mode(
            "latency-sleep-mode-reapplied-create",
            device,
            *swapchain,
            0,
            0);
    }
    return result;
}

VKAPI_ATTR void VKAPI_CALL layer_destroy_swapchain_khr(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkAllocationCallbacks* allocator) {
    PFN_vkDestroySwapchainKHR next_destroy_swapchain = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_destroy_swapchain = it->second.destroy_swapchain_khr;
        }
    }

    if (next_destroy_swapchain) {
        next_destroy_swapchain(device, swapchain, allocator);
    }

    bool latency_mode_enabled = false;
    uint64_t live_swapchain_count = 0;
    {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end()) {
            latency_mode_enabled = swapchain_it->second.swapchain_latency_mode;
            g_swapchains.erase(swapchain_it);
        }
        live_swapchain_count = live_swapchain_count_locked(device);
    }
    send_swapchain_event(
        "destroy-swapchain",
        device,
        swapchain,
        nullptr,
        live_swapchain_count,
        latency_mode_enabled,
        device_telemetry_flags(device));
}

VKAPI_ATTR VkResult VKAPI_CALL layer_queue_present_khr(
    VkQueue queue,
    const VkPresentInfoKHR* present_info) {
    VkDevice device = VK_NULL_HANDLE;
    PFN_vkQueuePresentKHR next_queue_present = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(queue);
        if (queue_it != g_queues.end()) {
            device = queue_it->second.device;
            auto device_it = g_devices.find(device);
            if (device_it != g_devices.end()) {
                next_queue_present = device_it->second.queue_present_khr;
            }
        }
    }

    if (!next_queue_present) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    VkResult result = next_queue_present(queue, present_info);
    if (present_info && present_info->pSwapchains) {
        const uint64_t present_time_us = now_monotonic_us();
        for (uint32_t i = 0; i < present_info->swapchainCount; ++i) {
            VkSwapchainKHR swapchain = present_info->pSwapchains[i];
            const uint64_t vulkan_present_id =
                present_id_for_swapchain(present_info, i);
            uint64_t present_count = 0;
            uint64_t present_frametime_us = 0;
            uint64_t overlay_fps = 0;
            {
                std::lock_guard lock(g_mutex);
                auto swapchain_it = g_swapchains.find(swapchain);
                if (swapchain_it != g_swapchains.end()) {
                    auto& swapchain_context = swapchain_it->second;
                    present_count = ++swapchain_context.present_count;
                    if (vulkan_present_id) {
                        swapchain_context.last_vulkan_present_id =
                            vulkan_present_id;
                    }
                    if (swapchain_context.last_present_us
                        && present_time_us > swapchain_context.last_present_us) {
                        present_frametime_us =
                            present_time_us - swapchain_context.last_present_us;
                    }
                    swapchain_context.last_present_us = present_time_us;
                    if (overlay_enabled() && present_frametime_us) {
                        overlay_fps = median_fps_from_present_frametime_locked(
                            swapchain_context,
                            present_frametime_us);
                    }
                }
            }
            if (should_report_counter(present_count)) {
                send_status_event(
                    "present",
                    device,
                    swapchain,
                    present_count,
                    device_telemetry_flags(device));
            }
            if (debug_flow_enabled() && should_report_counter(present_count)) {
                send_flow_state_event(
                    "present-flow",
                    device,
                    swapchain,
                    queue,
                    present_count,
                    result,
                    -1,
                    0,
                    flow_snapshot(device, swapchain));
            }
            // Present-pacing FPS works without Reflex; emit it for every present.
            if (present_frametime_us) {
                send_present_pacing_sample(
                    device, swapchain, present_frametime_us, present_count);
            }
            if (overlay_fps) {
                write_overlay_text_file(overlay_fps, present_time_us);
            }
            if (timing_queries_enabled()) {
                query_latency_timing(device, swapchain);
            } else if (should_report_counter(present_count)) {
                send_status_event(
                    "latency-timing-query-disabled",
                    device,
                    swapchain,
                    present_count,
                    device_telemetry_flags(device));
            }
        }
    }
    return result;
}

VKAPI_ATTR VkResult VKAPI_CALL layer_set_latency_sleep_mode_nv(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkLatencySleepModeInfoNV* sleep_mode_info) {
    PFN_vkSetLatencySleepModeNV next_set_latency_sleep_mode = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_set_latency_sleep_mode = it->second.set_latency_sleep_mode_nv;
        }
    }

    VkLatencySleepModeInfoNV saved_info{};
    bool has_sleep_mode_info = false;
    if (sleep_mode_info) {
        saved_info = *sleep_mode_info;
        saved_info.pNext = nullptr;
        has_sleep_mode_info = true;
    }

    VkResult result = VK_ERROR_EXTENSION_NOT_PRESENT;
    if (next_set_latency_sleep_mode) {
        result = next_set_latency_sleep_mode(device, swapchain, sleep_mode_info);
    }

    uint64_t count = 0;
    DeviceTelemetryFlags flags{};
    if (result == VK_SUCCESS) {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            auto& context = it->second;
            context.has_latency_sleep_mode_info = has_sleep_mode_info;
            context.latency_sleep_mode_info = saved_info;
            count = ++context.latency_sleep_mode_set_count;
            flags = {
                context.low_latency_extension_advertised,
                context.low_latency_extension_requested,
                context.low_latency_functions_available,
                context.marker_count,
            };
        }
    } else {
        flags = device_telemetry_flags(device);
    }

    send_latency_sleep_mode_event(
        "latency-sleep-mode-set",
        device,
        swapchain,
        count,
        result,
        has_sleep_mode_info,
        saved_info,
        0,
        0,
        flags);
    return result;
}

VKAPI_ATTR VkResult VKAPI_CALL layer_latency_sleep_nv(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkLatencySleepInfoNV* sleep_info) {
    PFN_vkLatencySleepNV next_latency_sleep = nullptr;
    uint64_t count = 0;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_latency_sleep = it->second.latency_sleep_nv;
            count = ++it->second.latency_sleep_count;
        }
    }

    VkResult result = VK_ERROR_EXTENSION_NOT_PRESENT;
    if (next_latency_sleep) {
        result = next_latency_sleep(device, swapchain, sleep_info);
    }

    if (debug_flow_enabled() || should_report_counter(count)) {
        send_flow_state_event(
            "latency-sleep",
            device,
            swapchain,
            VK_NULL_HANDLE,
            count,
            result,
            -1,
            sleep_info ? sleep_info->value : 0,
            flow_snapshot(device, swapchain));
    }

    return result;
}

VKAPI_ATTR void VKAPI_CALL layer_set_latency_marker_nv(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkSetLatencyMarkerInfoNV* marker_info) {
    PFN_vkSetLatencyMarkerNV next_set_latency_marker = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_set_latency_marker = it->second.set_latency_marker_nv;
        }
    }

    if (next_set_latency_marker) {
        next_set_latency_marker(device, swapchain, marker_info);
    }
    record_marker(device, swapchain, marker_info);
}

VKAPI_ATTR void VKAPI_CALL layer_queue_notify_out_of_band_nv(
    VkQueue queue,
    const VkOutOfBandQueueTypeInfoNV* queue_type_info) {
    VkDevice device = VK_NULL_HANDLE;
    PFN_vkQueueNotifyOutOfBandNV next_queue_notify = nullptr;
    uint64_t count = 0;
    int queue_type = queue_type_info
        ? static_cast<int>(queue_type_info->queueType)
        : -1;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(queue);
        if (queue_it != g_queues.end()) {
            auto& queue_context = queue_it->second;
            device = queue_context.device;
            count = ++queue_context.out_of_band_notify_count;
            queue_context.last_out_of_band_queue_type = queue_type;
            auto device_it = g_devices.find(device);
            if (device_it != g_devices.end()) {
                next_queue_notify = device_it->second.queue_notify_out_of_band_nv;
            }
        }
    }

    if (next_queue_notify) {
        next_queue_notify(queue, queue_type_info);
    }

    if (debug_flow_enabled() || should_report_counter(count)) {
        send_flow_state_event(
            "latency-queue-out-of-band",
            device,
            VK_NULL_HANDLE,
            queue,
            count,
            next_queue_notify ? VK_SUCCESS : VK_ERROR_EXTENSION_NOT_PRESENT,
            queue_type,
            0,
            flow_snapshot(device, VK_NULL_HANDLE));
    }
}

VKAPI_ATTR void VKAPI_CALL layer_get_latency_timings_nv(
    VkDevice device,
    VkSwapchainKHR swapchain,
    VkGetLatencyMarkerInfoNV* latency_marker_info) {
    PFN_vkGetLatencyTimingsNV next_get_latency_timings = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_get_latency_timings = it->second.get_latency_timings_nv;
        }
    }

    if (next_get_latency_timings) {
        next_get_latency_timings(device, swapchain, latency_marker_info);
    } else if (latency_marker_info) {
        latency_marker_info->timingCount = 0;
    }
}

}  // namespace

extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(
    VkInstance instance,
    const char* name);

extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(
    VkDevice device,
    const char* name);

extern "C" __attribute__((visibility("default"))) VKAPI_ATTR VkResult VKAPI_CALL
vkNegotiateLoaderLayerInterfaceVersion(VkNegotiateLayerInterface* version) {
    send_status_event_once("negotiate", g_reported_negotiate);

    if (!version || version->sType != LAYER_NEGOTIATE_INTERFACE_STRUCT) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    if (version->loaderLayerInterfaceVersion < 2) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    version->loaderLayerInterfaceVersion =
        std::min<uint32_t>(version->loaderLayerInterfaceVersion, 2);
    version->pfnGetInstanceProcAddr = vkGetInstanceProcAddr;
    version->pfnGetDeviceProcAddr = vkGetDeviceProcAddr;
    version->pfnGetPhysicalDeviceProcAddr = nullptr;
    return VK_SUCCESS;
}

extern "C" __attribute__((visibility("default"))) VKAPI_ATTR PFN_vkVoidFunction
VKAPI_CALL vkGetInstanceProcAddr(VkInstance instance, const char* name) {
    send_status_event_once(
        "get-instance-proc-addr",
        g_reported_get_instance_proc_addr);

    if (!name) {
        return nullptr;
    }

    if (std::strcmp(name, "vkGetInstanceProcAddr") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(vkGetInstanceProcAddr);
    }
    if (std::strcmp(name, "vkGetDeviceProcAddr") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(vkGetDeviceProcAddr);
    }
    if (std::strcmp(name, "vkCreateInstance") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_create_instance);
    }
    if (std::strcmp(name, "vkDestroyInstance") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_destroy_instance);
    }
    if (std::strcmp(name, "vkEnumeratePhysicalDevices") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_enumerate_physical_devices);
    }
    if (std::strcmp(name, "vkCreateDevice") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_create_device);
    }
    if (std::strcmp(name, "vkDestroyDevice") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_destroy_device);
    }
    if (std::strcmp(name, "vkGetDeviceQueue") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_get_device_queue);
    }
    if (std::strcmp(name, "vkGetDeviceQueue2") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_get_device_queue2);
    }
    if (std::strcmp(name, "vkCreateSwapchainKHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_create_swapchain_khr);
    }
    if (std::strcmp(name, "vkDestroySwapchainKHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_destroy_swapchain_khr);
    }
    if (std::strcmp(name, "vkQueuePresentKHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_queue_present_khr);
    }
    if (std::strcmp(name, "vkSetLatencySleepModeNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_set_latency_sleep_mode_nv);
    }
    if (std::strcmp(name, "vkLatencySleepNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_latency_sleep_nv);
    }
    if (std::strcmp(name, "vkSetLatencyMarkerNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_set_latency_marker_nv);
    }
    if (std::strcmp(name, "vkGetLatencyTimingsNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_get_latency_timings_nv);
    }
    if (std::strcmp(name, "vkQueueNotifyOutOfBandNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_queue_notify_out_of_band_nv);
    }

    std::lock_guard lock(g_mutex);
    auto it = g_instances.find(instance);
    if (it != g_instances.end() && it->second.get_instance_proc_addr) {
        return it->second.get_instance_proc_addr(instance, name);
    }
    return nullptr;
}

extern "C" __attribute__((visibility("default"))) VKAPI_ATTR PFN_vkVoidFunction
VKAPI_CALL vkGetDeviceProcAddr(VkDevice device, const char* name) {
    send_status_event_once(
        "get-device-proc-addr",
        g_reported_get_device_proc_addr);

    if (!name) {
        return nullptr;
    }

    if (std::strcmp(name, "vkGetDeviceProcAddr") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(vkGetDeviceProcAddr);
    }
    if (std::strcmp(name, "vkDestroyDevice") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_destroy_device);
    }
    if (std::strcmp(name, "vkGetDeviceQueue") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_get_device_queue);
    }
    if (std::strcmp(name, "vkGetDeviceQueue2") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_get_device_queue2);
    }
    if (std::strcmp(name, "vkCreateSwapchainKHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_create_swapchain_khr);
    }
    if (std::strcmp(name, "vkDestroySwapchainKHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_destroy_swapchain_khr);
    }
    if (std::strcmp(name, "vkQueuePresentKHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_queue_present_khr);
    }
    if (std::strcmp(name, "vkSetLatencySleepModeNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_set_latency_sleep_mode_nv);
    }
    if (std::strcmp(name, "vkLatencySleepNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_latency_sleep_nv);
    }
    if (std::strcmp(name, "vkSetLatencyMarkerNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_set_latency_marker_nv);
    }
    if (std::strcmp(name, "vkGetLatencyTimingsNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_get_latency_timings_nv);
    }
    if (std::strcmp(name, "vkQueueNotifyOutOfBandNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_queue_notify_out_of_band_nv);
    }

    std::lock_guard lock(g_mutex);
    auto it = g_devices.find(device);
    if (it != g_devices.end() && it->second.get_device_proc_addr) {
        return it->second.get_device_proc_addr(device, name);
    }
    return nullptr;
}
