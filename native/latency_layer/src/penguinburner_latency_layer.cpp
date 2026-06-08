#define VK_NO_PROTOTYPES

#include <vulkan/vk_layer.h>
#include <vulkan/vulkan.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
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

namespace {

constexpr const char* kLayerName = "VK_LAYER_PENGUINBURNER_latency";
constexpr const char* kSocketEnv = "PENGUIN_BURNER_LATENCY_SOCKET";
constexpr const char* kEnableEnv = "PENGUIN_BURNER_LATENCY_LAYER";
constexpr const char* kGpuTimestampsEnv =
    "PENGUIN_BURNER_LATENCY_GPU_TIMESTAMPS";
constexpr const char* kGpuTimestampIntervalEnv =
    "PENGUIN_BURNER_LATENCY_GPU_TIMESTAMP_INTERVAL";
constexpr const char* kInjectFrameIdsEnv =
    "PENGUIN_BURNER_LATENCY_INJECT_FRAME_IDS";
constexpr const char* kGpuTimestampsUnsafeOptIn = "unsafe-submit-wrapper";
constexpr const char* kGpuTimestampsUnsafeSideSubmitOptIn =
    "unsafe-side-submit";
constexpr uint32_t kGpuTimestampSlots = 96;

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

bool pnext_has_structure(const void* p_next, VkStructureType s_type) {
    auto* current = reinterpret_cast<const VkBaseInStructure*>(p_next);
    while (current) {
        if (current->sType == s_type) {
            return true;
        }
        current = reinterpret_cast<const VkBaseInStructure*>(current->pNext);
    }
    return false;
}

template <typename T>
T* find_mutable_pnext(void* p_next, VkStructureType s_type) {
    auto* current = reinterpret_cast<VkBaseOutStructure*>(p_next);
    while (current) {
        if (current->sType == s_type) {
            return reinterpret_cast<T*>(current);
        }
        current = reinterpret_cast<VkBaseOutStructure*>(current->pNext);
    }
    return nullptr;
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

bool env_enabled(const char* name) {
    const char* value = std::getenv(name);
    if (!value || !value[0]) {
        return false;
    }
    return std::strcmp(value, "0") != 0 && std::strcmp(value, "false") != 0
        && std::strcmp(value, "False") != 0 && std::strcmp(value, "FALSE") != 0;
}

bool gpu_timestamps_unsafe_opted_in() {
    const char* value = std::getenv(kGpuTimestampsEnv);
    return value
        && (std::strcmp(value, kGpuTimestampsUnsafeOptIn) == 0
            || std::strcmp(value, kGpuTimestampsUnsafeSideSubmitOptIn) == 0);
}

bool gpu_timestamps_side_submit_opted_in() {
    const char* value = std::getenv(kGpuTimestampsEnv);
    return value && std::strcmp(value, kGpuTimestampsUnsafeSideSubmitOptIn) == 0;
}

const char* gpu_timestamp_mode_name(bool enabled, bool side_submit) {
    if (!enabled) {
        return "disabled";
    }
    return side_submit ? "unsafe-side-submit" : "unsafe-submit-wrapper";
}

uint32_t env_uint32(const char* name, uint32_t default_value, uint32_t max_value) {
    const char* value = std::getenv(name);
    if (!value || !value[0]) {
        return default_value;
    }
    char* end = nullptr;
    unsigned long parsed = std::strtoul(value, &end, 10);
    if (end == value || parsed == 0) {
        return default_value;
    }
    if (parsed > max_value) {
        return max_value;
    }
    return static_cast<uint32_t>(parsed);
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
    bool saw_present = false;
    uint64_t present_count = 0;
};

struct GpuTimestampSlot {
    bool active = false;
    VkQueue queue = VK_NULL_HANDLE;
    uint64_t sequence = 0;
    uint64_t cpu_submit_us = 0;
};

struct GpuTimestampResources {
    bool initialized = false;
    bool failed = false;
    uint32_t queue_family_index = UINT32_MAX;
    VkCommandPool command_pool = VK_NULL_HANDLE;
    VkQueryPool query_pool = VK_NULL_HANDLE;
    std::array<VkCommandBuffer, kGpuTimestampSlots> pre_command_buffers{};
    std::array<VkCommandBuffer, kGpuTimestampSlots> post_command_buffers{};
    std::array<GpuTimestampSlot, kGpuTimestampSlots> slots{};
    uint32_t next_slot = 0;
    uint64_t sample_count = 0;
    uint64_t active_slot_drop_count = 0;
};

struct GpuTimestampResult {
    VkDevice device = VK_NULL_HANDLE;
    VkQueue queue = VK_NULL_HANDLE;
    uint32_t queue_family_index = UINT32_MAX;
    uint64_t sequence = 0;
    uint64_t gpu_submit_us = 0;
    uint64_t cpu_age_us = 0;
    bool advertised = false;
    bool requested = false;
    bool functions_available = false;
    bool side_submit = false;
};

struct PreparedGpuTimestamp {
    bool enabled = false;
    VkDevice device = VK_NULL_HANDLE;
    VkQueue queue = VK_NULL_HANDLE;
    uint32_t queue_family_index = UINT32_MAX;
    uint32_t slot_index = UINT32_MAX;
    uint64_t sequence = 0;
    VkCommandBuffer pre_command_buffer = VK_NULL_HANDLE;
    VkCommandBuffer post_command_buffer = VK_NULL_HANDLE;
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
    PFN_vkQueueSubmit queue_submit = nullptr;
    PFN_vkQueueSubmit2 queue_submit2 = nullptr;
    PFN_vkQueueSubmit2KHR queue_submit2_khr = nullptr;
    PFN_vkCreateSwapchainKHR create_swapchain_khr = nullptr;
    PFN_vkDestroySwapchainKHR destroy_swapchain_khr = nullptr;
    PFN_vkQueuePresentKHR queue_present_khr = nullptr;
    PFN_vkSetLatencyMarkerNV set_latency_marker_nv = nullptr;
    PFN_vkGetLatencyTimingsNV get_latency_timings_nv = nullptr;
    PFN_vkCreateCommandPool create_command_pool = nullptr;
    PFN_vkDestroyCommandPool destroy_command_pool = nullptr;
    PFN_vkAllocateCommandBuffers allocate_command_buffers = nullptr;
    PFN_vkBeginCommandBuffer begin_command_buffer = nullptr;
    PFN_vkEndCommandBuffer end_command_buffer = nullptr;
    PFN_vkCreateQueryPool create_query_pool = nullptr;
    PFN_vkDestroyQueryPool destroy_query_pool = nullptr;
    PFN_vkGetQueryPoolResults get_query_pool_results = nullptr;
    PFN_vkCmdResetQueryPool cmd_reset_query_pool = nullptr;
    PFN_vkCmdWriteTimestamp cmd_write_timestamp = nullptr;
    VkPhysicalDeviceProperties physical_device_properties{};
    std::vector<VkQueueFamilyProperties> queue_family_properties;
    bool low_latency_extension_advertised = false;
    bool low_latency_extension_requested = false;
    bool low_latency_functions_available = false;
    bool frame_id_injection_enabled = false;
    bool present_id_extension_enabled = false;
    bool gpu_timestamps_enabled = false;
    bool gpu_timestamps_side_submit = false;
    uint32_t gpu_timestamp_interval = 1;
    uint64_t gpu_timestamp_submit_count = 0;
    uint64_t latest_marker_present_id = 0;
    uint64_t marker_count = 0;
    MarkerCounts marker_counts{};
    std::unordered_map<uint64_t, uint32_t> marker_bits_by_present_id;
    std::unordered_map<uint64_t, MarkerTiming> marker_timings_by_present_id;
    std::vector<uint64_t> marker_order;
    std::unordered_map<uint32_t, GpuTimestampResources>
        gpu_timestamp_resources_by_family;
};

struct SwapchainContext {
    VkDevice device = VK_NULL_HANDLE;
    uint64_t last_gpu_render_end_us = 0;
    uint64_t last_present_id = 0;
    uint64_t timing_sample_count = 0;
    uint64_t driver_timing_sample_count = 0;
    uint64_t last_driver_report_present_id = 0;
    uint64_t duplicate_driver_report_count = 0;
    uint64_t present_count = 0;
    uint64_t timing_unavailable_count = 0;
    uint64_t timing_empty_count = 0;
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

bool should_report_counter(uint64_t count) {
    return count <= 3 || (count % 600) == 0;
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

void send_gpu_timestamp_status_event(
    const char* event,
    VkDevice device,
    VkQueue queue,
    uint32_t queue_family_index,
    VkQueueFlags queue_flags,
    uint32_t timestamp_valid_bits,
    uint32_t interval,
    uint64_t count,
    uint64_t active_slot_drop_count,
    bool enabled,
    bool side_submit) {
    char line[2048]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"status\",\"event\":\"%s\",\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"queue\":\"0x%016" PRIx64 "\","
        "\"queue_family\":%u,\"queue_flags\":%" PRIu64 ","
        "\"timestamp_valid_bits\":%u,"
        "\"gpu_timestamps_enabled\":%s,"
        "\"gpu_timestamp_mode\":\"%s\","
        "\"gpu_timestamp_interval\":%u,"
        "\"gpu_timestamp_slot_drops\":%" PRIu64 ","
        "\"count\":%" PRIu64 "}",
        event,
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(queue),
        queue_family_index,
        static_cast<uint64_t>(queue_flags),
        timestamp_valid_bits,
        enabled ? "true" : "false",
        gpu_timestamp_mode_name(enabled, side_submit),
        interval,
        active_slot_drop_count,
        count);

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

void send_gpu_timestamp_sample(const GpuTimestampResult& result) {
    char line[1024]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"timing\",\"measurement\":\"gpu-submit-proxy\","
        "\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"queue\":\"0x%016" PRIx64 "\","
        "\"queue_family\":%u,\"submit_sequence\":%" PRIu64 ","
        "\"quality\":\"gpu-submit-proxy\","
        "\"vk_nv_low_latency2_advertised\":%s,"
        "\"vk_nv_low_latency2_requested\":%s,"
        "\"vk_nv_low_latency2_functions\":%s,"
        "\"gpu_timestamp_mode\":\"%s\","
        "\"gpu_submit_us\":%" PRIu64 ","
        "\"gpu_frame_time_us\":0,"
        "\"render_submit_us\":0,"
        "\"input_to_present_us\":0,"
        "\"gpu_render_start_us\":0,"
        "\"gpu_render_end_us\":0,"
        "\"driver_start_us\":0,"
        "\"driver_end_us\":0,"
        "\"cpu_age_us\":%" PRIu64 "}",
        static_cast<long>(::getpid()),
        handle_to_u64(result.device),
        handle_to_u64(result.queue),
        result.queue_family_index,
        result.sequence,
        result.advertised ? "true" : "false",
        result.requested ? "true" : "false",
        result.functions_available ? "true" : "false",
        gpu_timestamp_mode_name(true, result.side_submit),
        result.gpu_submit_us,
        result.cpu_age_us);

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
    bool gpu_timestamps_enabled = false;
    bool gpu_timestamps_side_submit = false;
    uint32_t gpu_timestamp_interval = 1;

    {
        std::lock_guard lock(g_mutex);
        auto device_it = g_devices.find(device);
        if (device_it != g_devices.end()) {
            gpu_timestamps_enabled = device_it->second.gpu_timestamps_enabled;
            gpu_timestamps_side_submit =
                device_it->second.gpu_timestamps_side_submit;
            gpu_timestamp_interval = device_it->second.gpu_timestamp_interval;
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

    if (gpu_timestamps_enabled) {
        send_gpu_timestamp_status_event(
            "gpu-timestamp-queue",
            device,
            queue,
            queue_context.queue_family_index,
            queue_context.queue_flags,
            queue_context.timestamp_valid_bits,
            gpu_timestamp_interval,
            1,
            0,
            true,
            gpu_timestamps_side_submit);
    }
}

void release_gpu_timestamp_resources_locked(
    DeviceContext& context,
    GpuTimestampResources& resources) {
    if (resources.query_pool != VK_NULL_HANDLE && context.destroy_query_pool) {
        context.destroy_query_pool(context.device, resources.query_pool, nullptr);
    }
    if (resources.command_pool != VK_NULL_HANDLE && context.destroy_command_pool) {
        context.destroy_command_pool(context.device, resources.command_pool, nullptr);
    }

    resources.command_pool = VK_NULL_HANDLE;
    resources.query_pool = VK_NULL_HANDLE;
    resources.pre_command_buffers = {};
    resources.post_command_buffers = {};
    resources.slots = {};
    resources.initialized = false;
}

void release_all_gpu_timestamp_resources_locked(DeviceContext& context) {
    for (auto& [_, resources] : context.gpu_timestamp_resources_by_family) {
        release_gpu_timestamp_resources_locked(context, resources);
    }
    context.gpu_timestamp_resources_by_family.clear();
}

bool record_gpu_timestamp_command_buffers_locked(
    DeviceContext& context,
    GpuTimestampResources& resources) {
    for (uint32_t slot = 0; slot < kGpuTimestampSlots; ++slot) {
        VkCommandBufferBeginInfo begin_info{};
        begin_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
        begin_info.flags = VK_COMMAND_BUFFER_USAGE_SIMULTANEOUS_USE_BIT;

        VkResult result = context.begin_command_buffer(
            resources.pre_command_buffers[slot],
            &begin_info);
        if (result != VK_SUCCESS) {
            return false;
        }
        context.cmd_reset_query_pool(
            resources.pre_command_buffers[slot],
            resources.query_pool,
            slot * 2,
            2);
        context.cmd_write_timestamp(
            resources.pre_command_buffers[slot],
            VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
            resources.query_pool,
            slot * 2);
        result = context.end_command_buffer(resources.pre_command_buffers[slot]);
        if (result != VK_SUCCESS) {
            return false;
        }

        result = context.begin_command_buffer(
            resources.post_command_buffers[slot],
            &begin_info);
        if (result != VK_SUCCESS) {
            return false;
        }
        context.cmd_write_timestamp(
            resources.post_command_buffers[slot],
            VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
            resources.query_pool,
            slot * 2 + 1);
        result = context.end_command_buffer(resources.post_command_buffers[slot]);
        if (result != VK_SUCCESS) {
            return false;
        }
    }
    return true;
}

bool init_gpu_timestamp_resources_locked(
    DeviceContext& context,
    uint32_t queue_family_index) {
    auto& resources =
        context.gpu_timestamp_resources_by_family[queue_family_index];
    if (resources.initialized) {
        return true;
    }
    if (resources.failed) {
        return false;
    }

    resources.queue_family_index = queue_family_index;
    VkQueueFlags queue_flags = 0;
    uint32_t timestamp_valid_bits = 0;
    if (queue_family_index < context.queue_family_properties.size()) {
        const auto& properties =
            context.queue_family_properties[queue_family_index];
        queue_flags = properties.queueFlags;
        timestamp_valid_bits = properties.timestampValidBits;
    }
    auto send_init_status = [&](const char* event) {
        send_gpu_timestamp_status_event(
            event,
            context.device,
            VK_NULL_HANDLE,
            queue_family_index,
            queue_flags,
            timestamp_valid_bits,
            context.gpu_timestamp_interval,
            resources.sample_count,
            resources.active_slot_drop_count,
            context.gpu_timestamps_enabled,
            context.gpu_timestamps_side_submit);
    };

    if (!context.create_command_pool || !context.destroy_command_pool
        || !context.allocate_command_buffers || !context.begin_command_buffer
        || !context.end_command_buffer || !context.create_query_pool
        || !context.destroy_query_pool || !context.get_query_pool_results
        || !context.cmd_reset_query_pool || !context.cmd_write_timestamp) {
        resources.failed = true;
        send_init_status("gpu-timestamp-init-missing-functions");
        return false;
    }

    VkCommandPoolCreateInfo command_pool_info{};
    command_pool_info.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    command_pool_info.flags = VK_COMMAND_POOL_CREATE_TRANSIENT_BIT;
    command_pool_info.queueFamilyIndex = queue_family_index;

    VkResult result = context.create_command_pool(
        context.device,
        &command_pool_info,
        nullptr,
        &resources.command_pool);
    if (result != VK_SUCCESS) {
        resources.failed = true;
        send_init_status("gpu-timestamp-init-command-pool-failed");
        return false;
    }

    VkQueryPoolCreateInfo query_pool_info{};
    query_pool_info.sType = VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;
    query_pool_info.queryType = VK_QUERY_TYPE_TIMESTAMP;
    query_pool_info.queryCount = kGpuTimestampSlots * 2;

    result = context.create_query_pool(
        context.device,
        &query_pool_info,
        nullptr,
        &resources.query_pool);
    if (result != VK_SUCCESS) {
        release_gpu_timestamp_resources_locked(context, resources);
        resources.failed = true;
        send_init_status("gpu-timestamp-init-query-pool-failed");
        return false;
    }

    std::array<VkCommandBuffer, kGpuTimestampSlots * 2> command_buffers{};
    VkCommandBufferAllocateInfo allocate_info{};
    allocate_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocate_info.commandPool = resources.command_pool;
    allocate_info.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocate_info.commandBufferCount =
        static_cast<uint32_t>(command_buffers.size());
    result = context.allocate_command_buffers(
        context.device,
        &allocate_info,
        command_buffers.data());
    if (result != VK_SUCCESS) {
        release_gpu_timestamp_resources_locked(context, resources);
        resources.failed = true;
        send_init_status("gpu-timestamp-init-command-buffer-failed");
        return false;
    }

    for (uint32_t slot = 0; slot < kGpuTimestampSlots; ++slot) {
        resources.pre_command_buffers[slot] = command_buffers[slot * 2];
        resources.post_command_buffers[slot] = command_buffers[slot * 2 + 1];
    }

    if (!record_gpu_timestamp_command_buffers_locked(context, resources)) {
        release_gpu_timestamp_resources_locked(context, resources);
        resources.failed = true;
        send_init_status("gpu-timestamp-init-record-failed");
        return false;
    }

    resources.initialized = true;
    send_init_status("gpu-timestamp-ready");
    return true;
}

bool collect_gpu_timestamp_slot_locked(
    DeviceContext& context,
    GpuTimestampResources& resources,
    uint32_t slot_index,
    GpuTimestampResult* result_out) {
    if (slot_index >= kGpuTimestampSlots) {
        return true;
    }

    auto& slot = resources.slots[slot_index];
    if (!slot.active) {
        return true;
    }

    std::array<uint64_t, 4> query_data{};
    VkResult result = context.get_query_pool_results(
        context.device,
        resources.query_pool,
        slot_index * 2,
        2,
        sizeof(query_data),
        query_data.data(),
        sizeof(uint64_t) * 2,
        VK_QUERY_RESULT_64_BIT | VK_QUERY_RESULT_WITH_AVAILABILITY_BIT);
    if (result != VK_SUCCESS && result != VK_NOT_READY) {
        slot.active = false;
        return true;
    }

    const bool start_available = query_data[1] != 0;
    const bool end_available = query_data[3] != 0;
    if (!start_available || !end_available) {
        return false;
    }

    uint64_t start = query_data[0];
    uint64_t end = query_data[2];
    if (resources.queue_family_index < context.queue_family_properties.size()) {
        uint32_t valid_bits =
            context
                .queue_family_properties[resources.queue_family_index]
                .timestampValidBits;
        if (valid_bits > 0 && valid_bits < 64) {
            const uint64_t mask = (1ull << valid_bits) - 1ull;
            start &= mask;
            end &= mask;
        }
    }

    if (end > start && result_out) {
        const double duration_ns =
            static_cast<double>(end - start)
            * static_cast<double>(
                context.physical_device_properties.limits.timestampPeriod);
        result_out->device = context.device;
        result_out->queue = slot.queue;
        result_out->queue_family_index = resources.queue_family_index;
        result_out->sequence = slot.sequence;
        result_out->gpu_submit_us =
            static_cast<uint64_t>(duration_ns / 1000.0);
        result_out->cpu_age_us = elapsed_us(slot.cpu_submit_us, now_monotonic_us());
        result_out->advertised = context.low_latency_extension_advertised;
        result_out->requested = context.low_latency_extension_requested;
        result_out->functions_available =
            context.low_latency_functions_available;
        result_out->side_submit = context.gpu_timestamps_side_submit;
    }

    slot.active = false;
    return true;
}

PreparedGpuTimestamp prepare_gpu_timestamp_submission(
    VkQueue queue,
    uint32_t command_buffer_count) {
    PreparedGpuTimestamp prepared{};
    std::vector<GpuTimestampResult> collected_results;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(queue);
        if (queue_it == g_queues.end() || command_buffer_count == 0) {
            return prepared;
        }
        auto device_it = g_devices.find(queue_it->second.device);
        if (device_it == g_devices.end()) {
            return prepared;
        }

        auto& context = device_it->second;
        auto& queue_context = queue_it->second;
        if (!context.gpu_timestamps_enabled
            || !(queue_context.queue_flags & VK_QUEUE_GRAPHICS_BIT)
            || queue_context.timestamp_valid_bits == 0
            || (context.gpu_timestamps_side_submit
                && !queue_context.saw_present)) {
            return prepared;
        }

        const uint64_t submit_count = ++context.gpu_timestamp_submit_count;
        if (((submit_count - 1) % context.gpu_timestamp_interval) != 0) {
            return prepared;
        }

        if (!init_gpu_timestamp_resources_locked(
                context,
                queue_context.queue_family_index)) {
            return prepared;
        }

        auto& resources =
            context.gpu_timestamp_resources_by_family
                [queue_context.queue_family_index];
        const uint32_t slot_index = resources.next_slot;
        GpuTimestampResult old_result{};
        const bool slot_reusable = collect_gpu_timestamp_slot_locked(
            context,
            resources,
            slot_index,
            &old_result);
        if (old_result.gpu_submit_us) {
            collected_results.push_back(old_result);
        }
        if (!slot_reusable) {
            resources.active_slot_drop_count++;
            if (should_report_counter(resources.active_slot_drop_count)) {
                send_gpu_timestamp_status_event(
                    "gpu-timestamp-slot-busy",
                    context.device,
                    queue,
                    queue_context.queue_family_index,
                    queue_context.queue_flags,
                    queue_context.timestamp_valid_bits,
                    context.gpu_timestamp_interval,
                    resources.sample_count,
                    resources.active_slot_drop_count,
                    true,
                    context.gpu_timestamps_side_submit);
            }
            return prepared;
        }

        auto& slot = resources.slots[slot_index];
        slot.active = true;
        slot.queue = queue;
        slot.sequence = ++resources.sample_count;
        slot.cpu_submit_us = now_monotonic_us();

        resources.next_slot = (slot_index + 1) % kGpuTimestampSlots;

        prepared.enabled = true;
        prepared.device = context.device;
        prepared.queue = queue;
        prepared.queue_family_index = queue_context.queue_family_index;
        prepared.slot_index = slot_index;
        prepared.sequence = slot.sequence;
        prepared.pre_command_buffer = resources.pre_command_buffers[slot_index];
        prepared.post_command_buffer =
            resources.post_command_buffers[slot_index];
    }

    for (const auto& result : collected_results) {
        send_gpu_timestamp_sample(result);
    }
    return prepared;
}

void discard_gpu_timestamp_submissions(
    const std::vector<PreparedGpuTimestamp>& prepared_submissions) {
    std::lock_guard lock(g_mutex);
    for (const auto& prepared : prepared_submissions) {
        if (!prepared.enabled) {
            continue;
        }
        auto device_it = g_devices.find(prepared.device);
        if (device_it == g_devices.end()) {
            continue;
        }
        auto& resources =
            device_it->second.gpu_timestamp_resources_by_family
                [prepared.queue_family_index];
        if (prepared.slot_index >= kGpuTimestampSlots) {
            continue;
        }
        auto& slot = resources.slots[prepared.slot_index];
        if (slot.active && slot.sequence == prepared.sequence) {
            slot.active = false;
        }
    }
}

void discard_gpu_timestamp_submission(const PreparedGpuTimestamp& prepared) {
    if (!prepared.enabled) {
        return;
    }
    discard_gpu_timestamp_submissions({prepared});
}

void report_gpu_timestamp_side_submit_status(
    const PreparedGpuTimestamp& prepared,
    const char* event) {
    if (!prepared.enabled) {
        return;
    }

    VkQueueFlags queue_flags = 0;
    uint32_t timestamp_valid_bits = 0;
    uint32_t interval = 0;
    uint64_t active_slot_drop_count = 0;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(prepared.queue);
        if (queue_it != g_queues.end()) {
            queue_flags = queue_it->second.queue_flags;
            timestamp_valid_bits = queue_it->second.timestamp_valid_bits;
        }
        auto device_it = g_devices.find(prepared.device);
        if (device_it != g_devices.end()) {
            interval = device_it->second.gpu_timestamp_interval;
            auto resources_it =
                device_it->second.gpu_timestamp_resources_by_family.find(
                    prepared.queue_family_index);
            if (resources_it
                != device_it->second.gpu_timestamp_resources_by_family.end()) {
                active_slot_drop_count =
                    resources_it->second.active_slot_drop_count;
            }
        }
    }

    send_gpu_timestamp_status_event(
        event,
        prepared.device,
        prepared.queue,
        prepared.queue_family_index,
        queue_flags,
        timestamp_valid_bits,
        interval,
        prepared.sequence,
        active_slot_drop_count,
        true,
        true);
}

bool submit_gpu_timestamp_side_command_buffer(
    VkQueue queue,
    PFN_vkQueueSubmit next_queue_submit,
    const PreparedGpuTimestamp& prepared,
    VkCommandBuffer command_buffer,
    const char* failure_event) {
    if (!prepared.enabled || !next_queue_submit
        || command_buffer == VK_NULL_HANDLE) {
        return false;
    }

    VkSubmitInfo submit_info{};
    submit_info.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submit_info.commandBufferCount = 1;
    submit_info.pCommandBuffers = &command_buffer;

    VkResult result =
        next_queue_submit(queue, 1, &submit_info, VK_NULL_HANDLE);
    if (result != VK_SUCCESS) {
        report_gpu_timestamp_side_submit_status(prepared, failure_event);
        return false;
    }
    return true;
}

uint32_t submit_command_buffer_count(
    const VkSubmitInfo* submits,
    uint32_t submit_count) {
    uint32_t command_buffer_count = 0;
    if (!submits) {
        return 0;
    }
    for (uint32_t i = 0; i < submit_count; ++i) {
        command_buffer_count += submits[i].commandBufferCount;
    }
    return command_buffer_count;
}

uint32_t submit2_command_buffer_count(
    const VkSubmitInfo2* submits,
    uint32_t submit_count) {
    uint32_t command_buffer_count = 0;
    if (!submits) {
        return 0;
    }
    for (uint32_t i = 0; i < submit_count; ++i) {
        command_buffer_count += submits[i].commandBufferInfoCount;
    }
    return command_buffer_count;
}

uint32_t first_submit2_device_mask(
    const VkSubmitInfo2* submits,
    uint32_t submit_count) {
    if (!submits) {
        return 0;
    }
    for (uint32_t i = 0; i < submit_count; ++i) {
        if (submits[i].pCommandBufferInfos
            && submits[i].commandBufferInfoCount > 0) {
            return submits[i].pCommandBufferInfos[0].deviceMask;
        }
    }
    return 0;
}

bool submit_gpu_timestamp_side_command_buffer2(
    VkQueue queue,
    PFN_vkQueueSubmit2 next_queue_submit2,
    const PreparedGpuTimestamp& prepared,
    VkCommandBuffer command_buffer,
    uint32_t device_mask,
    const char* failure_event) {
    if (!prepared.enabled || !next_queue_submit2
        || command_buffer == VK_NULL_HANDLE) {
        return false;
    }

    VkCommandBufferSubmitInfo command_buffer_info{};
    command_buffer_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_SUBMIT_INFO;
    command_buffer_info.commandBuffer = command_buffer;
    command_buffer_info.deviceMask = device_mask;

    VkSubmitInfo2 submit_info{};
    submit_info.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO_2;
    submit_info.commandBufferInfoCount = 1;
    submit_info.pCommandBufferInfos = &command_buffer_info;

    VkResult result =
        next_queue_submit2(queue, 1, &submit_info, VK_NULL_HANDLE);
    if (result != VK_SUCCESS) {
        report_gpu_timestamp_side_submit_status(prepared, failure_event);
        return false;
    }
    return true;
}

void poll_gpu_timestamp_results(VkDevice device) {
    std::vector<GpuTimestampResult> collected_results;
    {
        std::lock_guard lock(g_mutex);
        auto device_it = g_devices.find(device);
        if (device_it == g_devices.end()) {
            return;
        }
        auto& context = device_it->second;
        for (auto& [_, resources] : context.gpu_timestamp_resources_by_family) {
            for (uint32_t slot = 0; slot < kGpuTimestampSlots; ++slot) {
                GpuTimestampResult result{};
                collect_gpu_timestamp_slot_locked(
                    context,
                    resources,
                    slot,
                    &result);
                if (result.gpu_submit_us) {
                    collected_results.push_back(result);
                }
            }
        }
    }

    for (const auto& result : collected_results) {
        send_gpu_timestamp_sample(result);
    }
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
                timing.sim_start_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_SIMULATION_END_NV:
                context.marker_counts.simulation_end++;
                timing.sim_end_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_RENDERSUBMIT_START_NV:
                context.marker_counts.render_submit_start++;
                timing.render_submit_start_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_RENDERSUBMIT_END_NV:
                context.marker_counts.render_submit_end++;
                timing.render_submit_end_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_PRESENT_START_NV:
                context.marker_counts.present_start++;
                timing.present_start_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_PRESENT_END_NV:
                context.marker_counts.present_end++;
                timing.present_end_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_INPUT_SAMPLE_NV:
                context.marker_counts.input_sample++;
                timing.input_sample_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_RENDERSUBMIT_START_NV:
                context.marker_counts.out_of_band_render_submit_start++;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_RENDERSUBMIT_END_NV:
                context.marker_counts.out_of_band_render_submit_end++;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_START_NV:
                context.marker_counts.out_of_band_present_start++;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_END_NV:
                context.marker_counts.out_of_band_present_end++;
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
    for (uint32_t offset = 0; offset < written_count; ++offset) {
        const uint32_t index = written_count - 1 - offset;
        const auto& report = reports[index];
        if (report.presentID || report_has_driver_timing(report)
            || report_has_reflex_markers(report)) {
            send_timing_sample(device, swapchain, report, written_count);
            return;
        }
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
    const bool inject_frame_ids = env_enabled(kInjectFrameIdsEnv);
    const bool present_id_advertised = extension_advertised(
        instance_context,
        physical_device,
        VK_KHR_PRESENT_ID_EXTENSION_NAME);
    const bool present_id_requested = extension_requested(
        create_info,
        VK_KHR_PRESENT_ID_EXTENSION_NAME);

    VkDeviceCreateInfo amended_create_info = *create_info;
    std::vector<const char*> enabled_extensions;
    VkPhysicalDevicePresentIdFeaturesKHR present_id_features{};
    bool present_id_extension_enabled = present_id_requested;
    if (inject_frame_ids && present_id_advertised) {
        enabled_extensions.assign(
            create_info->ppEnabledExtensionNames,
            create_info->ppEnabledExtensionNames + create_info->enabledExtensionCount);
        if (!present_id_requested) {
            enabled_extensions.push_back(VK_KHR_PRESENT_ID_EXTENSION_NAME);
            amended_create_info.ppEnabledExtensionNames = enabled_extensions.data();
            amended_create_info.enabledExtensionCount =
                static_cast<uint32_t>(enabled_extensions.size());
            present_id_extension_enabled = true;
        }

        void* mutable_pnext = const_cast<void*>(amended_create_info.pNext);
        auto* existing_present_id_features =
            find_mutable_pnext<VkPhysicalDevicePresentIdFeaturesKHR>(
                mutable_pnext,
                VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_FEATURES_KHR);
        if (existing_present_id_features) {
            existing_present_id_features->presentId = VK_TRUE;
        } else {
            present_id_features.sType =
                VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_FEATURES_KHR;
            present_id_features.presentId = VK_TRUE;
            present_id_features.pNext =
                const_cast<void*>(amended_create_info.pNext);
            amended_create_info.pNext = &present_id_features;
        }
    }

    VkResult result =
        instance_context.create_device(
            physical_device,
            &amended_create_info,
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
    context.queue_submit = reinterpret_cast<PFN_vkQueueSubmit>(
        next_get_device_proc_addr(*device, "vkQueueSubmit"));
    context.queue_submit2 = reinterpret_cast<PFN_vkQueueSubmit2>(
        next_get_device_proc_addr(*device, "vkQueueSubmit2"));
    context.queue_submit2_khr = reinterpret_cast<PFN_vkQueueSubmit2KHR>(
        next_get_device_proc_addr(*device, "vkQueueSubmit2KHR"));
    context.create_swapchain_khr = reinterpret_cast<PFN_vkCreateSwapchainKHR>(
        next_get_device_proc_addr(*device, "vkCreateSwapchainKHR"));
    context.destroy_swapchain_khr = reinterpret_cast<PFN_vkDestroySwapchainKHR>(
        next_get_device_proc_addr(*device, "vkDestroySwapchainKHR"));
    context.queue_present_khr = reinterpret_cast<PFN_vkQueuePresentKHR>(
        next_get_device_proc_addr(*device, "vkQueuePresentKHR"));
    context.set_latency_marker_nv = reinterpret_cast<PFN_vkSetLatencyMarkerNV>(
        next_get_device_proc_addr(*device, "vkSetLatencyMarkerNV"));
    context.get_latency_timings_nv = reinterpret_cast<PFN_vkGetLatencyTimingsNV>(
        next_get_device_proc_addr(*device, "vkGetLatencyTimingsNV"));
    context.create_command_pool = reinterpret_cast<PFN_vkCreateCommandPool>(
        next_get_device_proc_addr(*device, "vkCreateCommandPool"));
    context.destroy_command_pool = reinterpret_cast<PFN_vkDestroyCommandPool>(
        next_get_device_proc_addr(*device, "vkDestroyCommandPool"));
    context.allocate_command_buffers =
        reinterpret_cast<PFN_vkAllocateCommandBuffers>(
            next_get_device_proc_addr(*device, "vkAllocateCommandBuffers"));
    context.begin_command_buffer = reinterpret_cast<PFN_vkBeginCommandBuffer>(
        next_get_device_proc_addr(*device, "vkBeginCommandBuffer"));
    context.end_command_buffer = reinterpret_cast<PFN_vkEndCommandBuffer>(
        next_get_device_proc_addr(*device, "vkEndCommandBuffer"));
    context.create_query_pool = reinterpret_cast<PFN_vkCreateQueryPool>(
        next_get_device_proc_addr(*device, "vkCreateQueryPool"));
    context.destroy_query_pool = reinterpret_cast<PFN_vkDestroyQueryPool>(
        next_get_device_proc_addr(*device, "vkDestroyQueryPool"));
    context.get_query_pool_results =
        reinterpret_cast<PFN_vkGetQueryPoolResults>(
            next_get_device_proc_addr(*device, "vkGetQueryPoolResults"));
    context.cmd_reset_query_pool =
        reinterpret_cast<PFN_vkCmdResetQueryPool>(
            next_get_device_proc_addr(*device, "vkCmdResetQueryPool"));
    context.cmd_write_timestamp =
        reinterpret_cast<PFN_vkCmdWriteTimestamp>(
            next_get_device_proc_addr(*device, "vkCmdWriteTimestamp"));
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
    context.frame_id_injection_enabled = inject_frame_ids;
    context.present_id_extension_enabled = present_id_extension_enabled;
    const bool gpu_timestamps_requested = env_enabled(kGpuTimestampsEnv);
    context.gpu_timestamps_enabled = gpu_timestamps_unsafe_opted_in();
    context.gpu_timestamps_side_submit = gpu_timestamps_side_submit_opted_in();
    context.gpu_timestamp_interval =
        env_uint32(kGpuTimestampIntervalEnv, 240, 3600);

    DeviceTelemetryFlags flags{
        advertised,
        requested,
        context.low_latency_functions_available,
        0,
    };
    const bool gpu_timestamps_enabled = context.gpu_timestamps_enabled;
    const bool gpu_timestamps_side_submit = context.gpu_timestamps_side_submit;
    const uint32_t gpu_timestamp_interval = context.gpu_timestamp_interval;
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
    if (gpu_timestamps_enabled || gpu_timestamps_requested) {
        send_gpu_timestamp_status_event(
            gpu_timestamps_enabled
                ? "gpu-timestamp-config"
                : "gpu-timestamp-disabled-requires-unsafe-opt-in",
            *device,
            VK_NULL_HANDLE,
            UINT32_MAX,
            0,
            0,
            gpu_timestamp_interval,
            1,
            0,
            gpu_timestamps_enabled,
            gpu_timestamps_side_submit);
    }
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
            release_all_gpu_timestamp_resources_locked(it->second);
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
    if (result == VK_SUCCESS && swapchain) {
        std::lock_guard lock(g_mutex);
        g_swapchains[*swapchain] = SwapchainContext{device, 0, 0, 0, 0, 0, 0};
    }
    if (result == VK_SUCCESS && swapchain) {
        send_status_event(
            "create-swapchain",
            device,
            *swapchain,
            1,
            device_telemetry_flags(device));
    }
    return result;
}

VKAPI_ATTR VkResult VKAPI_CALL layer_queue_submit(
    VkQueue queue,
    uint32_t submit_count,
    const VkSubmitInfo* submits,
    VkFence fence) {
    VkDevice device = VK_NULL_HANDLE;
    PFN_vkQueueSubmit next_queue_submit = nullptr;
    bool inject_submit_frame_ids = false;
    bool gpu_timestamps_side_submit = false;
    uint64_t submit_present_id = 0;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(queue);
        if (queue_it != g_queues.end()) {
            device = queue_it->second.device;
            auto device_it = g_devices.find(device);
            if (device_it != g_devices.end()) {
                next_queue_submit = device_it->second.queue_submit;
                inject_submit_frame_ids =
                    device_it->second.frame_id_injection_enabled;
                gpu_timestamps_side_submit =
                    device_it->second.gpu_timestamps_side_submit;
                submit_present_id = device_it->second.latest_marker_present_id;
            }
        }
    }

    if (!next_queue_submit) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    if (!submits || submit_count == 0) {
        return next_queue_submit(queue, submit_count, submits, fence);
    }

    std::vector<VkSubmitInfo> wrapped_submits(submits, submits + submit_count);
    std::vector<VkLatencySubmissionPresentIdNV> latency_submission_present_ids;
    if (inject_submit_frame_ids && submit_present_id) {
        latency_submission_present_ids.resize(submit_count);
        for (uint32_t i = 0; i < submit_count; ++i) {
            auto& present_id_info = latency_submission_present_ids[i];
            present_id_info.sType =
                VK_STRUCTURE_TYPE_LATENCY_SUBMISSION_PRESENT_ID_NV;
            present_id_info.presentID = submit_present_id;
            present_id_info.pNext = wrapped_submits[i].pNext;
            wrapped_submits[i].pNext = &present_id_info;
        }
    }

    if (gpu_timestamps_side_submit) {
        PreparedGpuTimestamp prepared = prepare_gpu_timestamp_submission(
            queue,
            submit_command_buffer_count(submits, submit_count));
        const bool pre_submitted = submit_gpu_timestamp_side_command_buffer(
            queue,
            next_queue_submit,
            prepared,
            prepared.pre_command_buffer,
            "gpu-timestamp-side-pre-submit-failed");
        if (!pre_submitted) {
            discard_gpu_timestamp_submission(prepared);
        }

        VkResult result =
            next_queue_submit(queue, submit_count, wrapped_submits.data(), fence);
        if (pre_submitted && result == VK_SUCCESS) {
            if (!submit_gpu_timestamp_side_command_buffer(
                    queue,
                    next_queue_submit,
                    prepared,
                    prepared.post_command_buffer,
                    "gpu-timestamp-side-post-submit-failed")) {
                report_gpu_timestamp_side_submit_status(
                    prepared,
                    "gpu-timestamp-side-post-abandoned");
            }
            poll_gpu_timestamp_results(device);
        } else if (pre_submitted) {
            report_gpu_timestamp_side_submit_status(
                prepared,
                "gpu-timestamp-side-original-submit-failed");
        }
        return result;
    }

    std::vector<std::vector<VkCommandBuffer>> wrapped_command_buffers(submit_count);
    std::vector<PreparedGpuTimestamp> prepared_submissions;

    for (uint32_t i = 0; i < submit_count; ++i) {
        const auto& original = submits[i];
        if (!original.pCommandBuffers || original.commandBufferCount == 0) {
            continue;
        }

        PreparedGpuTimestamp prepared =
            prepare_gpu_timestamp_submission(queue, original.commandBufferCount);
        if (!prepared.enabled) {
            continue;
        }

        auto& command_buffers = wrapped_command_buffers[i];
        command_buffers.reserve(original.commandBufferCount + 2);
        command_buffers.push_back(prepared.pre_command_buffer);
        command_buffers.insert(
            command_buffers.end(),
            original.pCommandBuffers,
            original.pCommandBuffers + original.commandBufferCount);
        command_buffers.push_back(prepared.post_command_buffer);
        wrapped_submits[i].commandBufferCount =
            static_cast<uint32_t>(command_buffers.size());
        wrapped_submits[i].pCommandBuffers = command_buffers.data();
        prepared_submissions.push_back(prepared);
    }

    VkResult result =
        next_queue_submit(queue, submit_count, wrapped_submits.data(), fence);
    if (result != VK_SUCCESS) {
        discard_gpu_timestamp_submissions(prepared_submissions);
    } else {
        poll_gpu_timestamp_results(device);
    }
    return result;
}

VkResult layer_queue_submit2_common(
    VkQueue queue,
    uint32_t submit_count,
    const VkSubmitInfo2* submits,
    VkFence fence,
    PFN_vkQueueSubmit2 next_queue_submit2) {
    VkDevice device = VK_NULL_HANDLE;
    bool inject_submit_frame_ids = false;
    bool gpu_timestamps_side_submit = false;
    uint64_t submit_present_id = 0;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(queue);
        if (queue_it != g_queues.end()) {
            device = queue_it->second.device;
            auto device_it = g_devices.find(device);
            if (device_it != g_devices.end()) {
                inject_submit_frame_ids =
                    device_it->second.frame_id_injection_enabled;
                gpu_timestamps_side_submit =
                    device_it->second.gpu_timestamps_side_submit;
                submit_present_id = device_it->second.latest_marker_present_id;
            }
        }
    }

    if (!next_queue_submit2) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }
    if (!submits || submit_count == 0) {
        return next_queue_submit2(queue, submit_count, submits, fence);
    }

    std::vector<VkSubmitInfo2> wrapped_submits(submits, submits + submit_count);
    std::vector<VkLatencySubmissionPresentIdNV> latency_submission_present_ids;
    if (inject_submit_frame_ids && submit_present_id) {
        latency_submission_present_ids.resize(submit_count);
        for (uint32_t i = 0; i < submit_count; ++i) {
            auto& present_id_info = latency_submission_present_ids[i];
            present_id_info.sType =
                VK_STRUCTURE_TYPE_LATENCY_SUBMISSION_PRESENT_ID_NV;
            present_id_info.presentID = submit_present_id;
            present_id_info.pNext = wrapped_submits[i].pNext;
            wrapped_submits[i].pNext = &present_id_info;
        }
    }

    if (gpu_timestamps_side_submit) {
        PreparedGpuTimestamp prepared = prepare_gpu_timestamp_submission(
            queue,
            submit2_command_buffer_count(submits, submit_count));
        const uint32_t device_mask =
            first_submit2_device_mask(submits, submit_count);
        const bool pre_submitted = submit_gpu_timestamp_side_command_buffer2(
            queue,
            next_queue_submit2,
            prepared,
            prepared.pre_command_buffer,
            device_mask,
            "gpu-timestamp-side-pre-submit2-failed");
        if (!pre_submitted) {
            discard_gpu_timestamp_submission(prepared);
        }

        VkResult result =
            next_queue_submit2(queue, submit_count, wrapped_submits.data(), fence);
        if (pre_submitted && result == VK_SUCCESS) {
            if (!submit_gpu_timestamp_side_command_buffer2(
                    queue,
                    next_queue_submit2,
                    prepared,
                    prepared.post_command_buffer,
                    device_mask,
                    "gpu-timestamp-side-post-submit2-failed")) {
                report_gpu_timestamp_side_submit_status(
                    prepared,
                    "gpu-timestamp-side-post2-abandoned");
            }
            poll_gpu_timestamp_results(device);
        } else if (pre_submitted) {
            report_gpu_timestamp_side_submit_status(
                prepared,
                "gpu-timestamp-side-original-submit2-failed");
        }
        return result;
    }

    std::vector<std::vector<VkCommandBufferSubmitInfo>>
        wrapped_command_buffer_infos(submit_count);
    std::vector<PreparedGpuTimestamp> prepared_submissions;

    for (uint32_t i = 0; i < submit_count; ++i) {
        const auto& original = submits[i];
        if (!original.pCommandBufferInfos
            || original.commandBufferInfoCount == 0) {
            continue;
        }

        PreparedGpuTimestamp prepared = prepare_gpu_timestamp_submission(
            queue,
            original.commandBufferInfoCount);
        if (!prepared.enabled) {
            continue;
        }

        VkCommandBufferSubmitInfo pre_info{};
        pre_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_SUBMIT_INFO;
        pre_info.commandBuffer = prepared.pre_command_buffer;

        VkCommandBufferSubmitInfo post_info{};
        post_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_SUBMIT_INFO;
        post_info.commandBuffer = prepared.post_command_buffer;

        auto& command_buffer_infos = wrapped_command_buffer_infos[i];
        command_buffer_infos.reserve(original.commandBufferInfoCount + 2);
        command_buffer_infos.push_back(pre_info);
        command_buffer_infos.insert(
            command_buffer_infos.end(),
            original.pCommandBufferInfos,
            original.pCommandBufferInfos + original.commandBufferInfoCount);
        command_buffer_infos.push_back(post_info);
        wrapped_submits[i].commandBufferInfoCount =
            static_cast<uint32_t>(command_buffer_infos.size());
        wrapped_submits[i].pCommandBufferInfos = command_buffer_infos.data();
        prepared_submissions.push_back(prepared);
    }

    VkResult result =
        next_queue_submit2(queue, submit_count, wrapped_submits.data(), fence);
    if (result != VK_SUCCESS) {
        discard_gpu_timestamp_submissions(prepared_submissions);
    } else {
        poll_gpu_timestamp_results(device);
    }
    return result;
}

VKAPI_ATTR VkResult VKAPI_CALL layer_queue_submit2(
    VkQueue queue,
    uint32_t submit_count,
    const VkSubmitInfo2* submits,
    VkFence fence) {
    PFN_vkQueueSubmit2 next_queue_submit2 = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(queue);
        if (queue_it != g_queues.end()) {
            auto device_it = g_devices.find(queue_it->second.device);
            if (device_it != g_devices.end()) {
                next_queue_submit2 = device_it->second.queue_submit2;
            }
        }
    }
    return layer_queue_submit2_common(
        queue,
        submit_count,
        submits,
        fence,
        next_queue_submit2);
}

VKAPI_ATTR VkResult VKAPI_CALL layer_queue_submit2_khr(
    VkQueue queue,
    uint32_t submit_count,
    const VkSubmitInfo2* submits,
    VkFence fence) {
    PFN_vkQueueSubmit2 next_queue_submit2 = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(queue);
        if (queue_it != g_queues.end()) {
            auto device_it = g_devices.find(queue_it->second.device);
            if (device_it != g_devices.end()) {
                next_queue_submit2 = device_it->second.queue_submit2_khr;
            }
        }
    }
    return layer_queue_submit2_common(
        queue,
        submit_count,
        submits,
        fence,
        next_queue_submit2);
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

    std::lock_guard lock(g_mutex);
    g_swapchains.erase(swapchain);
}

VKAPI_ATTR VkResult VKAPI_CALL layer_queue_present_khr(
    VkQueue queue,
    const VkPresentInfoKHR* present_info) {
    VkDevice device = VK_NULL_HANDLE;
    PFN_vkQueuePresentKHR next_queue_present = nullptr;
    bool inject_present_frame_ids = false;
    bool present_id_extension_enabled = false;
    uint64_t present_id = 0;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(queue);
        if (queue_it != g_queues.end()) {
            device = queue_it->second.device;
            auto device_it = g_devices.find(device);
            if (device_it != g_devices.end()) {
                next_queue_present = device_it->second.queue_present_khr;
                inject_present_frame_ids =
                    device_it->second.frame_id_injection_enabled;
                present_id_extension_enabled =
                    device_it->second.present_id_extension_enabled;
                present_id = device_it->second.latest_marker_present_id;
            }
        }
    }

    if (!next_queue_present) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    VkPresentInfoKHR wrapped_present_info{};
    VkPresentIdKHR present_id_info{};
    std::vector<uint64_t> present_ids;
    const VkPresentInfoKHR* next_present_info = present_info;
    if (inject_present_frame_ids && present_id_extension_enabled && present_id
        && present_info && present_info->pSwapchains && present_info->swapchainCount
        && !pnext_has_structure(present_info->pNext, VK_STRUCTURE_TYPE_PRESENT_ID_KHR)) {
        wrapped_present_info = *present_info;
        present_ids.assign(present_info->swapchainCount, present_id);
        present_id_info.sType = VK_STRUCTURE_TYPE_PRESENT_ID_KHR;
        present_id_info.pNext = wrapped_present_info.pNext;
        present_id_info.swapchainCount = present_info->swapchainCount;
        present_id_info.pPresentIds = present_ids.data();
        wrapped_present_info.pNext = &present_id_info;
        next_present_info = &wrapped_present_info;
    }

    VkResult result = next_queue_present(queue, next_present_info);
    QueueContext present_queue_context{};
    uint64_t present_queue_count = 0;
    uint32_t present_queue_interval = 0;
    bool report_present_queue = false;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(queue);
        if (queue_it != g_queues.end()) {
            queue_it->second.saw_present = true;
            present_queue_count = ++queue_it->second.present_count;
            present_queue_context = queue_it->second;
            auto device_it = g_devices.find(queue_it->second.device);
            if (device_it != g_devices.end()) {
                present_queue_interval =
                    device_it->second.gpu_timestamp_interval;
                report_present_queue =
                    device_it->second.gpu_timestamps_side_submit
                    && should_report_counter(present_queue_count);
            }
        }
    }
    if (report_present_queue) {
        send_gpu_timestamp_status_event(
            "gpu-timestamp-present-queue",
            present_queue_context.device,
            queue,
            present_queue_context.queue_family_index,
            present_queue_context.queue_flags,
            present_queue_context.timestamp_valid_bits,
            present_queue_interval,
            present_queue_count,
            0,
            true,
            true);
    }
    if (present_info && present_info->pSwapchains) {
        for (uint32_t i = 0; i < present_info->swapchainCount; ++i) {
            VkSwapchainKHR swapchain = present_info->pSwapchains[i];
            uint64_t present_count = 0;
            {
                std::lock_guard lock(g_mutex);
                auto swapchain_it = g_swapchains.find(swapchain);
                if (swapchain_it != g_swapchains.end()) {
                    present_count = ++swapchain_it->second.present_count;
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
            query_latency_timing(device, swapchain);
        }
        poll_gpu_timestamp_results(device);
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
    if (std::strcmp(name, "vkQueueSubmit") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_queue_submit);
    }
    if (std::strcmp(name, "vkQueueSubmit2") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_queue_submit2);
    }
    if (std::strcmp(name, "vkQueueSubmit2KHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_queue_submit2_khr);
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
    if (std::strcmp(name, "vkSetLatencyMarkerNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_set_latency_marker_nv);
    }
    if (std::strcmp(name, "vkGetLatencyTimingsNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_get_latency_timings_nv);
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
    if (std::strcmp(name, "vkQueueSubmit") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_queue_submit);
    }
    if (std::strcmp(name, "vkQueueSubmit2") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_queue_submit2);
    }
    if (std::strcmp(name, "vkQueueSubmit2KHR") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_queue_submit2_khr);
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
    if (std::strcmp(name, "vkSetLatencyMarkerNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(layer_set_latency_marker_nv);
    }
    if (std::strcmp(name, "vkGetLatencyTimingsNV") == 0) {
        return reinterpret_cast<PFN_vkVoidFunction>(
            layer_get_latency_timings_nv);
    }

    std::lock_guard lock(g_mutex);
    auto it = g_devices.find(device);
    if (it != g_devices.end() && it->second.get_device_proc_addr) {
        return it->second.get_device_proc_addr(device, name);
    }
    return nullptr;
}
