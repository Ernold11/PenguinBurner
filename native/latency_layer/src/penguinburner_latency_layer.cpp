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
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
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
constexpr const char* kOverlayEnableEnv = "PENGUIN_BURNER_OVERLAY";
constexpr const char* kOverlayEnableEnvAlias = "PB_OVERLAY";
constexpr const char* kOverlayStateEnv = "PENGUIN_BURNER_OVERLAY_STATE";
constexpr const char* kOverlayTextEnv = "PENGUIN_BURNER_OVERLAY_TEXT";
constexpr const char* kOverlayConfigEnv = "PENGUIN_BURNER_OVERLAY_CONFIG";
constexpr const char* kRecoveryResetEnv = "PENGUIN_BURNER_LATENCY_RECOVERY_RESET";
constexpr const char* kDebugFlowEnv = "PENGUIN_BURNER_LATENCY_DEBUG_FLOW";
constexpr const char* kQueryTimingsEnv = "PENGUIN_BURNER_LATENCY_QUERY_TIMINGS";
// Opt-in, default off: measure the present->scanout tail via VK_KHR_present_wait.
// Read-only -- only ever waits on present IDs the app/DXVK already supplied; it
// never injects present IDs or enables extensions the app did not request.
constexpr const char* kDisplayLatencyEnv = "PENGUIN_BURNER_LATENCY_DISPLAY";
// EXPERIMENTAL, default off, separate opt-in: when the app enabled
// VK_KHR_present_id but does not stamp a present ID itself (e.g. vkd3d-proton),
// prepend our own VkPresentIdKHR so vkWaitForPresentKHR has something to key on.
// This MUTATES the present chain -- the previously-removed inject path crashed on
// the dxvk-nvapi/vkd3d/NVIDIA stack -- so it is deliberately the narrowest form:
// present-chain only (never amends device creation) and only when the app has not
// already chained a VkPresentIdKHR. Requires PENGUIN_BURNER_LATENCY_DISPLAY too.
constexpr const char* kInjectPresentIdEnv =
    "PENGUIN_BURNER_LATENCY_INJECT_PRESENT_ID";
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

bool display_latency_enabled() {
    static const bool enabled = env_flag_enabled(kDisplayLatencyEnv);
    return enabled;
}

bool inject_present_id_enabled() {
    static const bool enabled = env_flag_enabled(kInjectPresentIdEnv);
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
    bool present_wait_advertised = false;
    bool present_wait_requested = false;
    bool present_id_advertised = false;
    bool present_id_requested = false;
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
    uint64_t oob_render_submit_start_us = 0;
    uint64_t oob_render_submit_end_us = 0;
    uint64_t oob_present_start_us = 0;
    uint64_t oob_present_end_us = 0;
    int64_t present_marker_lag_us = 0;
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
    PFN_vkDeviceWaitIdle device_wait_idle = nullptr;
    PFN_vkGetDeviceQueue get_device_queue = nullptr;
    PFN_vkGetDeviceQueue2 get_device_queue2 = nullptr;
    PFN_vkCreateSwapchainKHR create_swapchain_khr = nullptr;
    PFN_vkDestroySwapchainKHR destroy_swapchain_khr = nullptr;
    PFN_vkGetSwapchainImagesKHR get_swapchain_images_khr = nullptr;
    PFN_vkQueuePresentKHR queue_present_khr = nullptr;
    PFN_vkQueueSubmit queue_submit = nullptr;
    PFN_vkCreateImageView create_image_view = nullptr;
    PFN_vkDestroyImageView destroy_image_view = nullptr;
    PFN_vkCreateRenderPass create_render_pass = nullptr;
    PFN_vkDestroyRenderPass destroy_render_pass = nullptr;
    PFN_vkCreateFramebuffer create_framebuffer = nullptr;
    PFN_vkDestroyFramebuffer destroy_framebuffer = nullptr;
    PFN_vkCreateCommandPool create_command_pool = nullptr;
    PFN_vkDestroyCommandPool destroy_command_pool = nullptr;
    PFN_vkAllocateCommandBuffers allocate_command_buffers = nullptr;
    PFN_vkResetCommandBuffer reset_command_buffer = nullptr;
    PFN_vkBeginCommandBuffer begin_command_buffer = nullptr;
    PFN_vkEndCommandBuffer end_command_buffer = nullptr;
    PFN_vkCmdBeginRenderPass cmd_begin_render_pass = nullptr;
    PFN_vkCmdEndRenderPass cmd_end_render_pass = nullptr;
    PFN_vkCmdClearAttachments cmd_clear_attachments = nullptr;
    PFN_vkCreateSemaphore create_semaphore = nullptr;
    PFN_vkDestroySemaphore destroy_semaphore = nullptr;
    PFN_vkCreateFence create_fence = nullptr;
    PFN_vkDestroyFence destroy_fence = nullptr;
    PFN_vkGetFenceStatus get_fence_status = nullptr;
    PFN_vkResetFences reset_fences = nullptr;
    PFN_vkWaitForFences wait_for_fences = nullptr;
    PFN_vkSetLatencySleepModeNV set_latency_sleep_mode_nv = nullptr;
    PFN_vkLatencySleepNV latency_sleep_nv = nullptr;
    PFN_vkSetLatencyMarkerNV set_latency_marker_nv = nullptr;
    PFN_vkGetLatencyTimingsNV get_latency_timings_nv = nullptr;
    PFN_vkQueueNotifyOutOfBandNV queue_notify_out_of_band_nv = nullptr;
    PFN_vkWaitForPresentKHR wait_for_present_khr = nullptr;
    VkPhysicalDeviceProperties physical_device_properties{};
    std::vector<VkQueueFamilyProperties> queue_family_properties;
    bool low_latency_extension_advertised = false;
    bool low_latency_extension_requested = false;
    bool low_latency_functions_available = false;
    // VK_KHR_present_wait / VK_KHR_present_id availability, recorded read-only at
    // device creation. Display-latency capture only activates when the app itself
    // enabled both (the *_requested flags).
    bool present_wait_advertised = false;
    bool present_wait_requested = false;
    bool present_id_advertised = false;
    bool present_id_requested = false;
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

struct BaseMarkerState {
    uint64_t frame_id = 0;
    uint64_t timestamp_us = 0;
};

struct OverlayRenderResources {
    bool attempted = false;
    bool ready = false;
    uint32_t queue_family_index = UINT32_MAX;
    VkRenderPass render_pass = VK_NULL_HANDLE;
    VkCommandPool command_pool = VK_NULL_HANDLE;
    std::vector<VkImage> images;
    std::vector<VkImageView> image_views;
    std::vector<VkFramebuffer> framebuffers;
    std::vector<VkCommandBuffer> command_buffers;
    std::vector<VkSemaphore> signal_semaphores;
    std::vector<VkFence> submit_fences;
    std::vector<uint8_t> submit_pending;
};

struct SwapchainContext {
    VkDevice device = VK_NULL_HANDLE;
    bool swapchain_latency_mode = false;
    VkFormat image_format = VK_FORMAT_UNDEFINED;
    VkExtent2D image_extent{};
    uint32_t image_array_layers = 1;
    VkImageUsageFlags image_usage = 0;
    uint64_t last_gpu_render_end_us = 0;
    uint64_t last_present_id = 0;
    // Monotonic counter for present IDs we inject (present-id injection path).
    uint64_t last_injected_present_id = 0;
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
    BaseMarkerState out_of_band_present_start{};
    BaseMarkerState present_start{};
    BaseMarkerState render_submit_start{};
    OverlayRenderResources overlay{};
};

struct OverlayGpuState {
    bool available = false;
    bool framegen_active = false;
    std::string present_fps;
    std::string framegen_fps;
    std::string latency_ms;
    std::string display_latency_ms;
    std::string clock_mhz;
    std::string voltage_mv;
    std::string power_w;
    std::string gpu_util_pct;
    std::string cpu_util_pct;
    std::string cpu_peak_thread_pct;
    std::string fan_pct;
    std::string temperature_c;
    std::string uv_offset_mv;
    std::string profile_id;
    std::string profile_tier = "Balanced";
};

struct OverlayTextConfig {
    bool enabled = true;
    std::vector<std::string> items;
    // User-selected overlay scale multiplier from overlay.toml. 1.0 keeps the
    // resolution-adaptive size; 0.5/2.0 shrink/grow it. The Python UI only
    // writes 0.5/1.0/2.0, but the value is clamped on read for safety.
    double scale = 1.0;
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

std::string trim_ascii(std::string value);

double parse_overlay_scale(const std::string& value) {
    const std::string text = trim_ascii(value);
    if (text.empty()) {
        return 1.0;
    }
    errno = 0;
    char* end = nullptr;
    const double parsed = std::strtod(text.c_str(), &end);
    if (end == text.c_str() || errno != 0 || !(parsed > 0.0)) {
        return 1.0;
    }
    // Guard against absurd values; the UI only offers 0.5/1.0/2.0.
    if (parsed < 0.25) {
        return 0.25;
    }
    if (parsed > 4.0) {
        return 4.0;
    }
    return parsed;
}

bool value_is_true(const std::string& value) {
    std::string text = trim_ascii(value);
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return text == "1" || text == "true" || text == "yes" || text == "on";
}

bool value_is_false(const std::string& value) {
    std::string text = trim_ascii(value);
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return text == "0" || text == "false" || text == "no" || text == "off";
}

std::string overlay_env_value() {
    if (const char* value = std::getenv(kOverlayEnableEnv)) {
        return trim_ascii(value);
    }
    if (const char* value = std::getenv(kOverlayEnableEnvAlias)) {
        return trim_ascii(value);
    }
    return "";
}

bool overlay_enabled() {
    static const bool enabled = !value_is_false(overlay_env_value());
    return enabled;
}

bool overlay_env_fallback_enabled() {
    static const bool enabled = value_is_true(overlay_env_value());
    return enabled;
}

std::string overlay_runtime_path(const char* env_name, const char* file_name) {
    if (const char* explicit_path = std::getenv(env_name)) {
        if (explicit_path[0]) {
            return explicit_path;
        }
    }
    // Inside Steam's pressure-vessel container XDG_RUNTIME_DIR only holds
    // forwarded sockets; the home directory is shared with the host.
    if (const char* home = std::getenv("HOME")) {
        if (home[0] && std::strcmp(home, "/root") != 0) {
            return std::string(home) + "/.cache/penguin-burner/" + file_name;
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

std::string profile_voltage_mv_from_id(const std::string& profile_id) {
    std::string text = profile_id;
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    std::size_t pos = text.find("mv");
    while (pos != std::string::npos) {
        std::size_t start = pos;
        while (start > 0
            && std::isdigit(static_cast<unsigned char>(text[start - 1]))) {
            --start;
        }
        if (start < pos) {
            return text.substr(start, pos - start);
        }
        pos = text.find("mv", pos + 2);
    }
    return "";
}

const std::array<const char*, 13> kOverlayItemOrder = {
    "base_fps",
    "fg_fps",
    "latency_ms",
    "clock_mhz",
    "voltage_mv",
    "power_w",
    "profile",
    "gpu_util_pct",
    "cpu_util_pct",
    "cpu_peak_thread_pct",
    "fan_pct",
    "temperature_c",
    "uv_offset_mv",
};

OverlayTextConfig default_overlay_text_config(bool enabled = true) {
    return OverlayTextConfig{enabled, {
        "base_fps",
        "fg_fps",
        "clock_mhz",
        "voltage_mv",
        "power_w",
        "profile",
    }};
}

bool overlay_item_known(const std::string& item) {
    for (const char* known_item : kOverlayItemOrder) {
        if (item == known_item) {
            return true;
        }
    }
    return false;
}

std::string overlay_config_path() {
    if (const char* explicit_path = std::getenv(kOverlayConfigEnv)) {
        if (explicit_path[0]) {
            return explicit_path;
        }
    }
    if (const char* home = std::getenv("HOME")) {
        if (home[0] && std::strcmp(home, "/root") != 0) {
            const std::string preferred =
                std::string(home) + "/.config/penguin-burner/overlay.toml";
            if (FILE* file = std::fopen(preferred.c_str(), "r")) {
                std::fclose(file);
                return preferred;
            }
            return std::string(home) + "/.config/PenguinBurner/overlay.toml";
        }
    }
    return "";
}

std::vector<std::string> parse_overlay_items_line(const std::string& line) {
    std::vector<std::string> items;
    const std::size_t start = line.find('[');
    const std::size_t end = line.find(']', start == std::string::npos ? 0 : start);
    if (start == std::string::npos || end == std::string::npos || end <= start) {
        return items;
    }
    std::string current;
    bool in_quote = false;
    for (std::size_t index = start + 1; index < end; ++index) {
        const char ch = line[index];
        if (ch == '"') {
            if (in_quote) {
                const std::string item = trim_ascii(current);
                if (overlay_item_known(item)
                    && std::find(items.begin(), items.end(), item) == items.end()) {
                    items.push_back(item);
                }
                current.clear();
            }
            in_quote = !in_quote;
            continue;
        }
        if (in_quote) {
            current.push_back(ch);
        }
    }
    return items;
}

std::vector<std::string> normalize_overlay_items(
    const std::vector<std::string>& requested) {
    std::vector<std::string> items;
    for (const char* ordered_item : kOverlayItemOrder) {
        const std::string item(ordered_item);
        if (std::find(requested.begin(), requested.end(), item) != requested.end()) {
            items.push_back(item);
        }
    }
    if (items.empty()) {
        items.push_back("base_fps");
    }
    return items;
}

OverlayTextConfig read_overlay_text_config() {
    if (!overlay_enabled()) {
        return default_overlay_text_config(false);
    }
    const bool fallback_enabled = overlay_env_fallback_enabled();
    const std::string path = overlay_config_path();
    if (path.empty()) {
        return default_overlay_text_config(fallback_enabled);
    }
    FILE* file = std::fopen(path.c_str(), "r");
    if (!file) {
        return default_overlay_text_config(fallback_enabled);
    }
    bool enabled = fallback_enabled;
    double scale = 1.0;
    std::vector<std::string> requested;
    char line_buffer[512]{};
    while (std::fgets(line_buffer, sizeof(line_buffer), file)) {
        std::string line(line_buffer);
        const std::size_t sep = line.find('=');
        if (sep == std::string::npos) {
            continue;
        }
        const std::string key = trim_ascii(line.substr(0, sep));
        const std::string value = trim_ascii(line.substr(sep + 1));
        if (key == "items") {
            requested = parse_overlay_items_line(line);
        } else if (key == "enabled") {
            enabled = value_is_true(value);
        } else if (key == "scale") {
            scale = parse_overlay_scale(value);
        }
    }
    std::fclose(file);
    OverlayTextConfig result = requested.empty()
        ? default_overlay_text_config(enabled)
        : OverlayTextConfig{enabled, normalize_overlay_items(requested), scale};
    result.scale = scale;
    return result;
}

OverlayTextConfig overlay_text_config(uint64_t now_us) {
    static uint64_t last_read_us = 0;
    static OverlayTextConfig config = default_overlay_text_config(false);
    std::lock_guard lock(g_overlay_config_mutex);
    if (!last_read_us || now_us < last_read_us
        || now_us - last_read_us >= 250000) {
        config = read_overlay_text_config();
        last_read_us = now_us;
        g_overlay_user_scale.store(config.scale, std::memory_order_relaxed);
    }
    return config;
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
        if (key == "present_fps") {
            state.present_fps = value;
        } else if (key == "framegen_fps") {
            state.framegen_fps = value;
        } else if (key == "framegen_active") {
            state.framegen_active = value_is_true(value);
        } else if (key == "clock_mhz") {
            state.clock_mhz = value;
        } else if (key == "latency_ms") {
            state.latency_ms = value;
        } else if (key == "display_latency_ms") {
            state.display_latency_ms = value;
        } else if (key == "voltage_mv") {
            state.voltage_mv = value;
        } else if (key == "power_w") {
            state.power_w = value;
        } else if (key == "gpu_util_pct") {
            state.gpu_util_pct = value;
        } else if (key == "cpu_util_pct") {
            state.cpu_util_pct = value;
        } else if (key == "cpu_peak_thread_pct") {
            state.cpu_peak_thread_pct = value;
        } else if (key == "fan_pct") {
            state.fan_pct = value;
        } else if (key == "temperature_c") {
            state.temperature_c = value;
        } else if (key == "uv_offset_mv") {
            state.uv_offset_mv = value;
        } else if (key == "profile_id") {
            state.profile_id = value;
        } else if (key == "profile_tier" && !value.empty()) {
            state.profile_tier = value;
        }
    }
    std::fclose(file);
    const std::string requested_voltage_mv =
        profile_voltage_mv_from_id(state.profile_id);
    if (!requested_voltage_mv.empty()) {
        state.voltage_mv = requested_voltage_mv;
    }
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

bool overlay_value_missing(const std::string& value) {
    std::string text = trim_ascii(value);
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return text.empty() || text == "n/a";
}

std::string overlay_optional_value(const std::string& value, const char* suffix) {
    if (overlay_value_missing(value)) {
        return "";
    }
    std::string text = trim_ascii(value);
    std::string lower = text;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    std::string suffix_text(suffix);
    std::string suffix_lower = suffix_text;
    std::transform(
        suffix_lower.begin(),
        suffix_lower.end(),
        suffix_lower.begin(),
        [](unsigned char ch) {
            return static_cast<char>(std::tolower(ch));
        });
    if (lower.size() >= suffix_lower.size()
        && lower.rfind(suffix_lower) == lower.size() - suffix_lower.size()) {
        return text;
    }
    return text + suffix_text;
}

// Render latency plus the optional present->scanout display tail, as one number.
// When the display field is absent (display-latency capture not enabled via the
// Steam params), this collapses to the render latency alone.
std::string overlay_combined_latency_value(
    const std::string& render_ms,
    const std::string& display_ms) {
    if (overlay_value_missing(render_ms)) {
        return "";
    }
    const std::string render_text = trim_ascii(render_ms);
    errno = 0;
    char* render_end = nullptr;
    const long render = std::strtol(render_text.c_str(), &render_end, 10);
    if (errno != 0 || render_end == render_text.c_str()) {
        return render_text;  // non-numeric; show as-is
    }
    long total = render;
    if (!overlay_value_missing(display_ms)) {
        const std::string display_text = trim_ascii(display_ms);
        errno = 0;
        char* display_end = nullptr;
        const long display = std::strtol(display_text.c_str(), &display_end, 10);
        if (errno == 0 && display_end != display_text.c_str()) {
            total += display;
        }
    }
    return std::to_string(total);
}

std::string overlay_signed_value(const std::string& value) {
    if (overlay_value_missing(value)) {
        return "";
    }
    std::string text = trim_ascii(value);
    std::string lower = text;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    if (lower.size() >= 2 && lower.rfind("mv") == lower.size() - 2) {
        text = trim_ascii(text.substr(0, text.size() - 2));
    }
    char* end = nullptr;
    const long parsed = std::strtol(text.c_str(), &end, 10);
    if (end == text.c_str()) {
        return text;
    }
    char signed_text[64]{};
    std::snprintf(signed_text, sizeof(signed_text), "%+ld", parsed);
    return signed_text;
}

void append_overlay_part(std::string& text, const std::string& part) {
    if (part.empty()) {
        return;
    }
    if (!text.empty()) {
        text += " ";
    }
    text += part;
}

std::string overlay_fps_value_text(const std::string& raw_value, uint64_t fallback_fps) {
    std::string value = trim_ascii(raw_value);
    if (!value.empty()) {
        std::string lower = value;
        std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
            return static_cast<char>(std::tolower(ch));
        });
        if (lower.size() >= 3 && lower.rfind("fps") == lower.size() - 3) {
            return value;
        }
        return value + " FPS";
    }
    if (fallback_fps) {
        char fps_text[64]{};
        std::snprintf(fps_text, sizeof(fps_text), "%" PRIu64 " FPS", fallback_fps);
        return fps_text;
    }
    return "n/a FPS";
}

std::string overlay_fps_text(uint64_t fps, const OverlayGpuState& state) {
    return overlay_fps_value_text(state.present_fps, fps);
}

std::string overlay_framegen_fps_text(uint64_t fps, const OverlayGpuState& state) {
    std::string value = trim_ascii(state.framegen_fps);
    if (!value.empty()) {
        std::string lower = value;
        std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
            return static_cast<char>(std::tolower(ch));
        });
        if (lower.size() >= 3 && lower.rfind("fps") == lower.size() - 3) {
            value = trim_ascii(value.substr(0, value.size() - 3));
        }
        return value.empty() ? "n/a" : value;
    }
    if (fps) {
        char fps_text[64]{};
        std::snprintf(fps_text, sizeof(fps_text), "%" PRIu64, fps);
        return fps_text;
    }
    return "n/a";
}

bool overlay_parse_fps_value(const std::string& raw_value, double* out) {
    std::string value = trim_ascii(raw_value);
    if (value.empty()) {
        return false;
    }
    std::string lower = value;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    if (lower == "n/a") {
        return false;
    }
    if (lower.size() >= 3 && lower.rfind("fps") == lower.size() - 3) {
        value = trim_ascii(value.substr(0, value.size() - 3));
    }
    char* end = nullptr;
    const double parsed = std::strtod(value.c_str(), &end);
    if (end == value.c_str() || parsed <= 0.0) {
        return false;
    }
    *out = parsed;
    return true;
}

std::string compact_profile_tier(const std::string& value) {
    const std::string tier = trim_ascii(value).empty() ? "Balanced" : trim_ascii(value);
    std::string lower = tier;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    if (lower == "balanced") {
        return "BAL";
    }
    if (lower == "efficiency") {
        return "EFF";
    }
    if (lower == "performance") {
        return "PERF";
    }
    std::string compact = tier.size() > 6 ? tier.substr(0, 6) : tier;
    std::transform(compact.begin(), compact.end(), compact.begin(), [](unsigned char ch) {
        return static_cast<char>(std::toupper(ch));
    });
    return compact;
}

std::string build_overlay_text(
    uint64_t fps,
    const OverlayGpuState& state,
    uint64_t now_us) {
    std::string text;
    const OverlayTextConfig config = overlay_text_config(now_us);
    if (!config.enabled) {
        return "";
    }
    for (const std::string& item : config.items) {
        if (item == "base_fps") {
            append_overlay_part(text, overlay_fps_text(fps, state));
        } else if (item == "fg_fps") {
            double output_fps = 0.0;
            if (state.framegen_active
                && overlay_parse_fps_value(state.framegen_fps, &output_fps)) {
                append_overlay_part(
                    text,
                    overlay_framegen_fps_text(fps, state) + " FG");
            }
        } else if (item == "clock_mhz") {
            append_overlay_part(text, overlay_value_or_na(state.clock_mhz) + " MHz");
        } else if (item == "voltage_mv") {
            append_overlay_part(text, overlay_value_or_na(state.voltage_mv) + " mV");
        } else if (item == "power_w") {
            append_overlay_part(text, overlay_value_or_na(state.power_w) + " W");
        } else if (item == "profile") {
            append_overlay_part(text, compact_profile_tier(state.profile_tier));
        } else if (item == "gpu_util_pct") {
            const std::string util = overlay_optional_value(state.gpu_util_pct, "%");
            if (!util.empty()) {
                append_overlay_part(text, "GPU " + util);
            }
        } else if (item == "cpu_util_pct") {
            const std::string util = overlay_optional_value(state.cpu_util_pct, "%");
            append_overlay_part(text, "CPU " + (util.empty() ? "--%" : util));
        } else if (item == "cpu_peak_thread_pct") {
            const std::string util =
                overlay_optional_value(state.cpu_peak_thread_pct, "%");
            append_overlay_part(text, "CPU-T " + (util.empty() ? "--%" : util));
        } else if (item == "fan_pct") {
            const std::string fan = overlay_optional_value(state.fan_pct, "%");
            if (!fan.empty()) {
                append_overlay_part(text, "FAN " + fan);
            }
        } else if (item == "temperature_c") {
            const std::string temp = overlay_optional_value(state.temperature_c, " C");
            if (!temp.empty()) {
                append_overlay_part(text, "T " + temp);
            }
        } else if (item == "latency_ms") {
            const std::string latency = overlay_optional_value(
                overlay_combined_latency_value(
                    state.latency_ms, state.display_latency_ms),
                " ms");
            if (!latency.empty()) {
                append_overlay_part(text, "LAT " + latency);
            }
        } else if (item == "uv_offset_mv") {
            const std::string uv = overlay_optional_value(
                overlay_signed_value(state.uv_offset_mv),
                " mV");
            if (!uv.empty()) {
                append_overlay_part(text, "UV " + uv);
            }
        }
    }
    return text.empty() ? "PB WAITING" : text;
}

void write_overlay_text_file(uint64_t fps, uint64_t now_us) {
    static uint64_t last_write_us = 0;
    if (last_write_us && now_us > last_write_us && now_us - last_write_us < 250000) {
        return;
    }
    last_write_us = now_us;
    const std::string path =
        overlay_runtime_path(kOverlayTextEnv, "overlay-text.txt");
    const std::string text = build_overlay_text(fps, read_overlay_gpu_state(), now_us);
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

std::string cached_overlay_text(uint64_t fps, uint64_t now_us) {
    static uint64_t last_read_us = 0;
    static std::string last_text;
    std::lock_guard lock(g_overlay_file_mutex);
    if (!last_read_us || now_us < last_read_us
        || now_us - last_read_us >= 250000) {
        last_text = build_overlay_text(fps, read_overlay_gpu_state(), now_us);
        last_read_us = now_us;
    }
    return last_text;
}

VkClearColorValue overlay_color(float r, float g, float b, float a) {
    VkClearColorValue value{};
    value.float32[0] = r;
    value.float32[1] = g;
    value.float32[2] = b;
    value.float32[3] = a;
    return value;
}

std::array<uint8_t, 7> overlay_glyph_rows(char ch) {
    switch (static_cast<char>(std::toupper(static_cast<unsigned char>(ch)))) {
        case '0': return {0x0e, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0e};
        case '1': return {0x04, 0x0c, 0x04, 0x04, 0x04, 0x04, 0x0e};
        case '2': return {0x0e, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1f};
        case '3': return {0x1e, 0x01, 0x01, 0x0e, 0x01, 0x01, 0x1e};
        case '4': return {0x02, 0x06, 0x0a, 0x12, 0x1f, 0x02, 0x02};
        case '5': return {0x1f, 0x10, 0x1e, 0x01, 0x01, 0x11, 0x0e};
        case '6': return {0x06, 0x08, 0x10, 0x1e, 0x11, 0x11, 0x0e};
        case '7': return {0x1f, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08};
        case '8': return {0x0e, 0x11, 0x11, 0x0e, 0x11, 0x11, 0x0e};
        case '9': return {0x0e, 0x11, 0x11, 0x0f, 0x01, 0x02, 0x0c};
        case 'A': return {0x0e, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11};
        case 'B': return {0x1e, 0x11, 0x11, 0x1e, 0x11, 0x11, 0x1e};
        case 'C': return {0x0e, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0e};
        case 'D': return {0x1e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1e};
        case 'E': return {0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x1f};
        case 'F': return {0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x10};
        case 'G': return {0x0e, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0f};
        case 'H': return {0x11, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11};
        case 'I': return {0x0e, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0e};
        case 'J': return {0x01, 0x01, 0x01, 0x01, 0x11, 0x11, 0x0e};
        case 'K': return {0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11};
        case 'L': return {0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1f};
        case 'M': return {0x11, 0x1b, 0x15, 0x15, 0x11, 0x11, 0x11};
        case 'N': return {0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11};
        case 'O': return {0x0e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e};
        case 'P': return {0x1e, 0x11, 0x11, 0x1e, 0x10, 0x10, 0x10};
        case 'Q': return {0x0e, 0x11, 0x11, 0x11, 0x15, 0x12, 0x0d};
        case 'R': return {0x1e, 0x11, 0x11, 0x1e, 0x14, 0x12, 0x11};
        case 'S': return {0x0f, 0x10, 0x10, 0x0e, 0x01, 0x01, 0x1e};
        case 'T': return {0x1f, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04};
        case 'U': return {0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e};
        case 'V': return {0x11, 0x11, 0x11, 0x11, 0x11, 0x0a, 0x04};
        case 'W': return {0x11, 0x11, 0x11, 0x15, 0x15, 0x1b, 0x11};
        case 'X': return {0x11, 0x11, 0x0a, 0x04, 0x0a, 0x11, 0x11};
        case 'Y': return {0x11, 0x11, 0x0a, 0x04, 0x04, 0x04, 0x04};
        case 'Z': return {0x1f, 0x01, 0x02, 0x04, 0x08, 0x10, 0x1f};
        case '+': return {0x00, 0x04, 0x04, 0x1f, 0x04, 0x04, 0x00};
        case '-': return {0x00, 0x00, 0x00, 0x1f, 0x00, 0x00, 0x00};
        case '/': return {0x01, 0x01, 0x02, 0x04, 0x08, 0x10, 0x10};
        case '%': return {0x19, 0x19, 0x02, 0x04, 0x08, 0x13, 0x13};
        case '.': return {0x00, 0x00, 0x00, 0x00, 0x00, 0x06, 0x06};
        default: return {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
    }
}

uint32_t overlay_char_width(char ch, uint32_t scale) {
    return ch == ' ' ? 3u * scale : 5u * scale;
}

uint32_t overlay_text_width(const std::string& text, uint32_t scale) {
    uint32_t width = 0;
    bool first = true;
    for (char ch : text) {
        if (!first) {
            width += scale;
        }
        first = false;
        width += overlay_char_width(ch, scale);
    }
    return width;
}

uint32_t overlay_base_resolution_scale(uint32_t height) {
    // Snap the display height to the nearest standard tier (1080p/1440p/4K)
    // rather than assuming a 16:9 width, then map it to the bitmap-font pixel
    // scale. Keying off height keeps ultrawide and other odd resolutions on the
    // right tier (e.g. 3440x1440 stays 1440p, not 4K).
    struct Tier {
        uint32_t height;
        uint32_t scale;
    };
    static constexpr Tier kTiers[] = {
        {1080u, 2u},
        {1440u, 3u},
        {2160u, 4u},
    };
    uint32_t best_scale = kTiers[0].scale;
    uint32_t best_diff = height > kTiers[0].height
        ? height - kTiers[0].height
        : kTiers[0].height - height;
    for (const Tier& tier : kTiers) {
        const uint32_t diff = height > tier.height
            ? height - tier.height
            : tier.height - height;
        if (diff < best_diff) {
            best_diff = diff;
            best_scale = tier.scale;
        }
    }
    return best_scale;
}

uint32_t overlay_resolution_scale(const VkExtent2D& extent) {
    const uint32_t base = overlay_base_resolution_scale(extent.height);
    const double user_scale = g_overlay_user_scale.load(std::memory_order_relaxed);
    // Round to the nearest integer font scale (base values are positive, so a
    // simple +0.5 truncation rounds correctly), then keep at least 1 pixel.
    const double scaled = static_cast<double>(base) * user_scale;
    long rounded = static_cast<long>(scaled + 0.5);
    if (rounded < 1) {
        return 1u;
    }
    if (rounded > 8) {
        return 8u;
    }
    return static_cast<uint32_t>(rounded);
}

void overlay_clear_rect(
    const DeviceContext& device_context,
    VkCommandBuffer command_buffer,
    const VkExtent2D& extent,
    int32_t x,
    int32_t y,
    uint32_t width,
    uint32_t height,
    VkClearColorValue color) {
    if (!device_context.cmd_clear_attachments || !width || !height) {
        return;
    }
    if (x >= static_cast<int32_t>(extent.width)
        || y >= static_cast<int32_t>(extent.height)) {
        return;
    }
    if (x < 0) {
        const uint32_t clipped = static_cast<uint32_t>(-x);
        if (clipped >= width) {
            return;
        }
        width -= clipped;
        x = 0;
    }
    if (y < 0) {
        const uint32_t clipped = static_cast<uint32_t>(-y);
        if (clipped >= height) {
            return;
        }
        height -= clipped;
        y = 0;
    }
    width = std::min<uint32_t>(
        width,
        static_cast<uint32_t>(static_cast<int32_t>(extent.width) - x));
    height = std::min<uint32_t>(
        height,
        static_cast<uint32_t>(static_cast<int32_t>(extent.height) - y));
    VkClearAttachment attachment{};
    attachment.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    attachment.colorAttachment = 0;
    attachment.clearValue.color = color;
    VkClearRect rect{};
    rect.rect.offset = {x, y};
    rect.rect.extent = {width, height};
    rect.baseArrayLayer = 0;
    rect.layerCount = 1;
    device_context.cmd_clear_attachments(command_buffer, 1, &attachment, 1, &rect);
}

void overlay_draw_glyph(
    const DeviceContext& device_context,
    VkCommandBuffer command_buffer,
    const VkExtent2D& extent,
    char ch,
    int32_t x,
    int32_t y,
    uint32_t scale,
    VkClearColorValue color) {
    const auto rows = overlay_glyph_rows(ch);
    for (uint32_t row = 0; row < rows.size(); ++row) {
        uint8_t bits = rows[row];
        uint32_t col = 0;
        while (col < 5) {
            const bool enabled = (bits & (1u << (4u - col))) != 0;
            if (!enabled) {
                ++col;
                continue;
            }
            const uint32_t start_col = col;
            while (col < 5 && (bits & (1u << (4u - col)))) {
                ++col;
            }
            overlay_clear_rect(
                device_context,
                command_buffer,
                extent,
                x + static_cast<int32_t>(start_col * scale),
                y + static_cast<int32_t>(row * scale),
                (col - start_col) * scale,
                scale,
                color);
        }
    }
}

void overlay_draw_text(
    const DeviceContext& device_context,
    VkCommandBuffer command_buffer,
    const VkExtent2D& extent,
    const std::string& text) {
    if (text.empty() || extent.width < 64 || extent.height < 32) {
        return;
    }
    const uint32_t scale = overlay_resolution_scale(extent);
    const uint32_t padding = 2 * scale;
    const uint32_t margin = 5 * scale;
    const uint32_t text_height = 7 * scale;
    uint32_t text_width = overlay_text_width(text, scale);
    if (text_width + padding * 2 + margin * 2 > extent.width) {
        text_width = extent.width > margin * 2 + padding * 2
            ? extent.width - margin * 2 - padding * 2
            : 0;
    }
    if (!text_width) {
        return;
    }
    const int32_t x0 = static_cast<int32_t>(
        extent.width - text_width - padding * 2 - margin);
    const int32_t y0 = static_cast<int32_t>(margin);
    overlay_clear_rect(
        device_context,
        command_buffer,
        extent,
        x0,
        y0,
        text_width + padding * 2,
        text_height + padding * 2,
        overlay_color(0.0f, 0.0f, 0.0f, 1.0f));

    int32_t pen_x = x0 + static_cast<int32_t>(padding);
    const int32_t pen_y = y0 + static_cast<int32_t>(padding);
    const int32_t max_x = x0 + static_cast<int32_t>(padding + text_width);
    const VkClearColorValue text_color = overlay_color(1.0f, 1.0f, 1.0f, 1.0f);
    for (char ch : text) {
        const uint32_t advance = overlay_char_width(ch, scale);
        if (pen_x + static_cast<int32_t>(advance) > max_x) {
            break;
        }
        if (ch != ' ') {
            overlay_draw_glyph(
                device_context,
                command_buffer,
                extent,
                ch,
                pen_x,
                pen_y,
                scale,
                text_color);
        }
        pen_x += static_cast<int32_t>(advance + scale);
    }
}

bool overlay_device_functions_available(const DeviceContext& context) {
    return context.get_swapchain_images_khr && context.queue_submit
        && context.create_image_view && context.destroy_image_view
        && context.create_render_pass && context.destroy_render_pass
        && context.create_framebuffer && context.destroy_framebuffer
        && context.create_command_pool && context.destroy_command_pool
        && context.allocate_command_buffers && context.reset_command_buffer
        && context.begin_command_buffer && context.end_command_buffer
        && context.cmd_begin_render_pass && context.cmd_end_render_pass
        && context.cmd_clear_attachments && context.create_semaphore
        && context.destroy_semaphore && context.create_fence
        && context.destroy_fence && context.get_fence_status
        && context.reset_fences && context.wait_for_fences;
}

void destroy_overlay_resources(
    const DeviceContext& device_context,
    SwapchainContext& swapchain_context) {
    auto& overlay = swapchain_context.overlay;
    if (device_context.wait_for_fences) {
        std::vector<VkFence> pending;
        for (std::size_t i = 0; i < overlay.submit_fences.size(); ++i) {
            if (overlay.submit_fences[i] != VK_NULL_HANDLE
                && i < overlay.submit_pending.size()
                && overlay.submit_pending[i]) {
                pending.push_back(overlay.submit_fences[i]);
            }
        }
        if (!pending.empty()) {
            device_context.wait_for_fences(
                device_context.device,
                static_cast<uint32_t>(pending.size()),
                pending.data(),
                VK_TRUE,
                1000000000ull);
        }
    }
    for (VkFence fence : overlay.submit_fences) {
        if (fence != VK_NULL_HANDLE && device_context.destroy_fence) {
            device_context.destroy_fence(device_context.device, fence, nullptr);
        }
    }
    for (VkSemaphore semaphore : overlay.signal_semaphores) {
        if (semaphore != VK_NULL_HANDLE && device_context.destroy_semaphore) {
            device_context.destroy_semaphore(device_context.device, semaphore, nullptr);
        }
    }
    for (VkFramebuffer framebuffer : overlay.framebuffers) {
        if (framebuffer != VK_NULL_HANDLE && device_context.destroy_framebuffer) {
            device_context.destroy_framebuffer(device_context.device, framebuffer, nullptr);
        }
    }
    for (VkImageView image_view : overlay.image_views) {
        if (image_view != VK_NULL_HANDLE && device_context.destroy_image_view) {
            device_context.destroy_image_view(device_context.device, image_view, nullptr);
        }
    }
    if (overlay.command_pool != VK_NULL_HANDLE
        && device_context.destroy_command_pool) {
        device_context.destroy_command_pool(device_context.device, overlay.command_pool, nullptr);
    }
    if (overlay.render_pass != VK_NULL_HANDLE
        && device_context.destroy_render_pass) {
        device_context.destroy_render_pass(device_context.device, overlay.render_pass, nullptr);
    }
    overlay = {};
}

bool init_overlay_resources(
    const DeviceContext& device_context,
    VkSwapchainKHR swapchain,
    SwapchainContext& swapchain_context,
    uint32_t queue_family_index) {
    auto& overlay = swapchain_context.overlay;
    if (overlay.ready && overlay.queue_family_index == queue_family_index) {
        return true;
    }
    // Resources are bound to the first queue family that presents this
    // swapchain; presents from other families skip the overlay instead of
    // destroying resources that may still be in flight.
    if (overlay.attempted) {
        return false;
    }

    overlay.attempted = true;
    overlay.queue_family_index = queue_family_index;
    if (!overlay_enabled() || !overlay_device_functions_available(device_context)
        || queue_family_index == UINT32_MAX
        || swapchain_context.image_format == VK_FORMAT_UNDEFINED
        || swapchain_context.image_extent.width == 0
        || swapchain_context.image_extent.height == 0
        || swapchain_context.image_array_layers != 1
        || !(swapchain_context.image_usage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT)) {
        return false;
    }

    uint32_t image_count = 0;
    VkResult result = device_context.get_swapchain_images_khr(
        device_context.device,
        swapchain,
        &image_count,
        nullptr);
    if (result != VK_SUCCESS || image_count == 0) {
        return false;
    }
    overlay.images.resize(image_count);
    result = device_context.get_swapchain_images_khr(
        device_context.device,
        swapchain,
        &image_count,
        overlay.images.data());
    if ((result != VK_SUCCESS && result != VK_INCOMPLETE) || image_count == 0) {
        destroy_overlay_resources(device_context, swapchain_context);
        return false;
    }
    overlay.images.resize(image_count);

    VkAttachmentDescription attachment{};
    attachment.format = swapchain_context.image_format;
    attachment.samples = VK_SAMPLE_COUNT_1_BIT;
    attachment.loadOp = VK_ATTACHMENT_LOAD_OP_LOAD;
    attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    attachment.initialLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    attachment.finalLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;

    VkAttachmentReference color_attachment{};
    color_attachment.attachment = 0;
    color_attachment.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = 1;
    subpass.pColorAttachments = &color_attachment;

    std::array<VkSubpassDependency, 2> dependencies{};
    dependencies[0].srcSubpass = VK_SUBPASS_EXTERNAL;
    dependencies[0].dstSubpass = 0;
    dependencies[0].srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependencies[0].dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependencies[0].dstAccessMask =
        VK_ACCESS_COLOR_ATTACHMENT_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    dependencies[0].dependencyFlags = VK_DEPENDENCY_BY_REGION_BIT;
    dependencies[1].srcSubpass = 0;
    dependencies[1].dstSubpass = VK_SUBPASS_EXTERNAL;
    dependencies[1].srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependencies[1].dstStageMask = VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT;
    dependencies[1].srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    dependencies[1].dependencyFlags = VK_DEPENDENCY_BY_REGION_BIT;

    VkRenderPassCreateInfo render_pass_info{};
    render_pass_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
    render_pass_info.attachmentCount = 1;
    render_pass_info.pAttachments = &attachment;
    render_pass_info.subpassCount = 1;
    render_pass_info.pSubpasses = &subpass;
    render_pass_info.dependencyCount =
        static_cast<uint32_t>(dependencies.size());
    render_pass_info.pDependencies = dependencies.data();
    result = device_context.create_render_pass(
        device_context.device,
        &render_pass_info,
        nullptr,
        &overlay.render_pass);
    if (result != VK_SUCCESS) {
        destroy_overlay_resources(device_context, swapchain_context);
        return false;
    }

    overlay.image_views.resize(image_count, VK_NULL_HANDLE);
    overlay.framebuffers.resize(image_count, VK_NULL_HANDLE);
    for (uint32_t i = 0; i < image_count; ++i) {
        VkImageViewCreateInfo view_info{};
        view_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        view_info.image = overlay.images[i];
        view_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
        view_info.format = swapchain_context.image_format;
        view_info.components = {
            VK_COMPONENT_SWIZZLE_IDENTITY,
            VK_COMPONENT_SWIZZLE_IDENTITY,
            VK_COMPONENT_SWIZZLE_IDENTITY,
            VK_COMPONENT_SWIZZLE_IDENTITY,
        };
        view_info.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        view_info.subresourceRange.baseMipLevel = 0;
        view_info.subresourceRange.levelCount = 1;
        view_info.subresourceRange.baseArrayLayer = 0;
        view_info.subresourceRange.layerCount = 1;
        result = device_context.create_image_view(
            device_context.device,
            &view_info,
            nullptr,
            &overlay.image_views[i]);
        if (result != VK_SUCCESS) {
            destroy_overlay_resources(device_context, swapchain_context);
            return false;
        }

        VkFramebufferCreateInfo framebuffer_info{};
        framebuffer_info.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
        framebuffer_info.renderPass = overlay.render_pass;
        framebuffer_info.attachmentCount = 1;
        framebuffer_info.pAttachments = &overlay.image_views[i];
        framebuffer_info.width = swapchain_context.image_extent.width;
        framebuffer_info.height = swapchain_context.image_extent.height;
        framebuffer_info.layers = 1;
        result = device_context.create_framebuffer(
            device_context.device,
            &framebuffer_info,
            nullptr,
            &overlay.framebuffers[i]);
        if (result != VK_SUCCESS) {
            destroy_overlay_resources(device_context, swapchain_context);
            return false;
        }
    }

    VkCommandPoolCreateInfo pool_info{};
    pool_info.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    pool_info.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pool_info.queueFamilyIndex = queue_family_index;
    result = device_context.create_command_pool(
        device_context.device,
        &pool_info,
        nullptr,
        &overlay.command_pool);
    if (result != VK_SUCCESS) {
        destroy_overlay_resources(device_context, swapchain_context);
        return false;
    }

    overlay.command_buffers.resize(image_count, VK_NULL_HANDLE);
    VkCommandBufferAllocateInfo allocate_info{};
    allocate_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocate_info.commandPool = overlay.command_pool;
    allocate_info.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocate_info.commandBufferCount = image_count;
    result = device_context.allocate_command_buffers(
        device_context.device,
        &allocate_info,
        overlay.command_buffers.data());
    if (result != VK_SUCCESS) {
        destroy_overlay_resources(device_context, swapchain_context);
        return false;
    }

    overlay.signal_semaphores.resize(image_count, VK_NULL_HANDLE);
    VkSemaphoreCreateInfo semaphore_info{};
    semaphore_info.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;
    for (uint32_t i = 0; i < image_count; ++i) {
        result = device_context.create_semaphore(
            device_context.device,
            &semaphore_info,
            nullptr,
            &overlay.signal_semaphores[i]);
        if (result != VK_SUCCESS) {
            destroy_overlay_resources(device_context, swapchain_context);
            return false;
        }
    }

    overlay.submit_fences.resize(image_count, VK_NULL_HANDLE);
    overlay.submit_pending.assign(image_count, 0);
    VkFenceCreateInfo fence_info{};
    fence_info.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    for (uint32_t i = 0; i < image_count; ++i) {
        result = device_context.create_fence(
            device_context.device,
            &fence_info,
            nullptr,
            &overlay.submit_fences[i]);
        if (result != VK_SUCCESS) {
            destroy_overlay_resources(device_context, swapchain_context);
            return false;
        }
    }

    overlay.ready = true;
    return true;
}

VkSemaphore submit_overlay_locked(
    const DeviceContext& device_context,
    VkSwapchainKHR swapchain,
    SwapchainContext& swapchain_context,
    const QueueContext& queue_context,
    VkQueue queue,
    uint32_t image_index,
    const VkPresentInfoKHR* present_info,
    const std::string& text) {
    if (text.empty() || !overlay_enabled()
        || !(queue_context.queue_flags & VK_QUEUE_GRAPHICS_BIT)
        || !present_info) {
        return VK_NULL_HANDLE;
    }
    if (!init_overlay_resources(
            device_context,
            swapchain,
            swapchain_context,
            queue_context.queue_family_index)) {
        return VK_NULL_HANDLE;
    }
    auto& overlay = swapchain_context.overlay;
    if (image_index >= overlay.command_buffers.size()
        || image_index >= overlay.framebuffers.size()
        || image_index >= overlay.signal_semaphores.size()
        || image_index >= overlay.submit_fences.size()
        || image_index >= overlay.submit_pending.size()) {
        return VK_NULL_HANDLE;
    }

    // The previous submit that used this image's command buffer must have
    // retired before the buffer is reset; skip the overlay for this frame
    // rather than stall the present path.
    VkFence fence = overlay.submit_fences[image_index];
    if (overlay.submit_pending[image_index]) {
        if (device_context.get_fence_status(device_context.device, fence)
            != VK_SUCCESS) {
            return VK_NULL_HANDLE;
        }
        overlay.submit_pending[image_index] = 0;
    }
    if (device_context.reset_fences(device_context.device, 1, &fence)
        != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }

    VkCommandBuffer command_buffer = overlay.command_buffers[image_index];
    VkResult result = device_context.reset_command_buffer(command_buffer, 0);
    if (result != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }

    VkCommandBufferBeginInfo begin_info{};
    begin_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    begin_info.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    result = device_context.begin_command_buffer(command_buffer, &begin_info);
    if (result != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }

    VkRenderPassBeginInfo render_pass_info{};
    render_pass_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
    render_pass_info.renderPass = overlay.render_pass;
    render_pass_info.framebuffer = overlay.framebuffers[image_index];
    render_pass_info.renderArea.offset = {0, 0};
    render_pass_info.renderArea.extent = swapchain_context.image_extent;
    device_context.cmd_begin_render_pass(
        command_buffer,
        &render_pass_info,
        VK_SUBPASS_CONTENTS_INLINE);
    overlay_draw_text(
        device_context,
        command_buffer,
        swapchain_context.image_extent,
        text);
    device_context.cmd_end_render_pass(command_buffer);

    result = device_context.end_command_buffer(command_buffer);
    if (result != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }

    std::vector<VkPipelineStageFlags> wait_stages(
        present_info->waitSemaphoreCount,
        VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT);
    VkSemaphore signal = overlay.signal_semaphores[image_index];
    VkSubmitInfo submit_info{};
    submit_info.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submit_info.waitSemaphoreCount = present_info->waitSemaphoreCount;
    submit_info.pWaitSemaphores = present_info->pWaitSemaphores;
    submit_info.pWaitDstStageMask =
        wait_stages.empty() ? nullptr : wait_stages.data();
    submit_info.commandBufferCount = 1;
    submit_info.pCommandBuffers = &command_buffer;
    submit_info.signalSemaphoreCount = 1;
    submit_info.pSignalSemaphores = &signal;
    result = device_context.queue_submit(queue, 1, &submit_info, fence);
    if (result != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }
    overlay.submit_pending[image_index] = 1;
    return signal;
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

// True when the present chain already carries a VkPresentIdKHR (or the v2
// variant). If it does, we must not prepend a second one -- the app owns it.
bool present_info_has_present_id_struct(const VkPresentInfoKHR* present_info) {
    if (!present_info) {
        return false;
    }
    auto* current =
        reinterpret_cast<const VkBaseInStructure*>(present_info->pNext);
    while (current) {
        if (current->sType == VK_STRUCTURE_TYPE_PRESENT_ID_KHR) {
            return true;
        }
#ifdef VK_KHR_PRESENT_ID_2_EXTENSION_NAME
        if (current->sType == VK_STRUCTURE_TYPE_PRESENT_ID_2_KHR) {
            return true;
        }
#endif
        current = reinterpret_cast<const VkBaseInStructure*>(current->pNext);
    }
    return false;
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
        snapshot.present_wait_advertised = context.present_wait_advertised;
        snapshot.present_wait_requested = context.present_wait_requested;
        snapshot.present_id_advertised = context.present_id_advertised;
        snapshot.present_id_requested = context.present_id_requested;
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
        "\"vk_khr_present_wait_advertised\":%s,"
        "\"vk_khr_present_wait_requested\":%s,"
        "\"vk_khr_present_id_advertised\":%s,"
        "\"vk_khr_present_id_requested\":%s,"
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
        snapshot.present_wait_advertised ? "true" : "false",
        snapshot.present_wait_requested ? "true" : "false",
        snapshot.present_id_advertised ? "true" : "false",
        snapshot.present_id_requested ? "true" : "false",
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
    if (elapsed_us(timing.sim_start_us, timing.present_end_us)) {
        bits |= 1u << 4;
    }
    if (elapsed_us(timing.render_submit_start_us, timing.present_end_us)) {
        bits |= 1u << 5;
    }
    if (elapsed_us(timing.sim_start_us, timing.oob_present_end_us)) {
        bits |= 1u << 6;
    }
    return bits;
}

// Mirrored by the Python receiver's quality ladder; see
// latency_telemetry/receiver.py MARKER_TIMING_QUALITIES.
const char* marker_timing_quality(const MarkerTiming& timing) {
    if (elapsed_us(timing.input_sample_us, timing.present_end_us)) {
        return "reflex-input-present";
    }
    if (elapsed_us(timing.sim_start_us, timing.present_end_us)) {
        return "reflex-sim-present";
    }
    if (elapsed_us(timing.sim_start_us, timing.oob_present_end_us)) {
        return "reflex-sim-oob-present";
    }
    if (elapsed_us(timing.render_submit_start_us, timing.present_end_us)) {
        return "reflex-submit-present";
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
    const uint64_t sim_to_present_us = elapsed_us(
        timing.sim_start_us,
        timing.present_end_us);
    const uint64_t submit_to_present_us = elapsed_us(
        timing.render_submit_start_us,
        timing.present_end_us);
    const uint64_t sim_to_oob_present_us = elapsed_us(
        timing.sim_start_us,
        timing.oob_present_end_us);
    const uint64_t input_to_oob_present_us = elapsed_us(
        timing.input_sample_us,
        timing.oob_present_end_us);

    char line[1280]{};
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
        "\"sim_to_present_us\":%" PRIu64 ","
        "\"submit_to_present_us\":%" PRIu64 ","
        "\"sim_to_oob_present_us\":%" PRIu64 ","
        "\"input_to_oob_present_us\":%" PRIu64 ","
        "\"present_marker_lag_us\":%" PRId64 ","
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
        present_marker_us,
        sim_to_present_us,
        submit_to_present_us,
        sim_to_oob_present_us,
        input_to_oob_present_us,
        timing.present_marker_lag_us);

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
                timing.oob_render_submit_start_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_RENDERSUBMIT_END_NV:
                context.marker_counts.out_of_band_render_submit_end++;
                context.last_oob_render_submit_present_id = marker_info->presentID;
                timing.oob_render_submit_end_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_START_NV:
                context.marker_counts.out_of_band_present_start++;
                context.last_oob_present_present_id = marker_info->presentID;
                timing.oob_present_start_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_END_NV:
                context.marker_counts.out_of_band_present_end++;
                context.last_oob_present_present_id = marker_info->presentID;
                timing.oob_present_end_us = marker_time_us;
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
            // Settles whether PRESENT_END means presented or enqueued: a
            // tight, mostly-positive distribution vs. the layer's own
            // vkQueuePresentKHR time means the marker fires at the real
            // present (see latency-meter plan, Phase 1 step 5).
            if (marker_info->marker == VK_LATENCY_MARKER_PRESENT_END_NV
                && swapchain_it->second.last_present_us) {
                timing.present_marker_lag_us =
                    static_cast<int64_t>(marker_time_us)
                    - static_cast<int64_t>(swapchain_it->second.last_present_us);
            }
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

void send_display_latency_sample(
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t present_id,
    uint64_t display_latency_us) {
    char line[512]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"timing\",\"measurement\":\"display-latency\","
        "\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"quality\":\"present-wait\","
        "\"present_id\":%" PRIu64 ","
        "\"display_latency_us\":%" PRIu64 "}",
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        present_id,
        display_latency_us);

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

// Diagnostic: what the inject path decided for a present.
void send_inject_debug_event(
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t present_count,
    bool chain_has_present_id,
    bool present_id_at_head,
    bool want_inject,
    uint64_t injected_present_id) {
    char line[512]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"status\",\"event\":\"present-id-inject\",\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"count\":%" PRIu64 ","
        "\"chain_has_present_id\":%s,\"present_id_at_head\":%s,"
        "\"want_inject\":%s,\"injected_present_id\":%" PRIu64 "}",
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        present_count,
        chain_has_present_id ? "true" : "false",
        present_id_at_head ? "true" : "false",
        want_inject ? "true" : "false",
        injected_present_id);
    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

// Diagnostic: result of a vkWaitForPresentKHR call (so wait failures are visible).
void send_wait_result_debug(
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t present_id,
    int result,
    uint64_t display_latency_us) {
    char line[512]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"status\",\"event\":\"present-wait-result\",\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"present_id\":%" PRIu64 ",\"result\":%d,"
        "\"display_latency_us\":%" PRIu64 "}",
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        present_id,
        result,
        display_latency_us);
    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

// Measures the present->scanout tail without touching the app's Vulkan work: a
// dedicated thread blocks in vkWaitForPresentKHR on present IDs the app already
// supplied, and timestamps when each present is latched/displayed. Owned per
// VkDevice in g_display_waiters and torn down before any swapchain/device the
// app destroys, so no wait is ever in flight across a destroy.
class DisplayLatencyWaiter {
public:
    DisplayLatencyWaiter(VkDevice device, PFN_vkWaitForPresentKHR wait_fn)
        : device_(device),
          wait_fn_(wait_fn),
          thread_([this]() { run(); }) {}

    ~DisplayLatencyWaiter() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stop_ = true;
        }
        cv_.notify_all();
        if (thread_.joinable()) {
            thread_.join();
        }
    }

    DisplayLatencyWaiter(const DisplayLatencyWaiter&) = delete;
    DisplayLatencyWaiter& operator=(const DisplayLatencyWaiter&) = delete;

    void enqueue(
        VkSwapchainKHR swapchain,
        uint64_t present_id,
        uint64_t submit_us) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (stop_ || disabled_) {
                return;
            }
            if (pending_.size() >= kMaxPending) {
                pending_.pop_front();
            }
            pending_.push_back(Pending{swapchain, present_id, submit_us});
        }
        cv_.notify_one();
    }

private:
    struct Pending {
        VkSwapchainKHR swapchain = VK_NULL_HANDLE;
        uint64_t present_id = 0;
        uint64_t submit_us = 0;
    };

    // Keep the backlog shallow: if the wait thread falls behind the present
    // cadence the oldest entries are dropped rather than reporting stale tails.
    static constexpr size_t kMaxPending = 8;
    // Bound each wait so a stalled swapchain (or teardown) cannot wedge the
    // thread; on timeout the sample is simply skipped.
    static constexpr uint64_t kWaitTimeoutNs = 200ull * 1000ull * 1000ull;
    // Present->scanout is a sub-frame tail; anything larger is a backlog/clock
    // artifact, not a real display latency, so discard it.
    static constexpr uint64_t kMaxPlausibleUs = 100ull * 1000ull;
    static constexpr uint64_t kErrorDisableThreshold = 16;

    void run() {
        for (;;) {
            Pending item;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                cv_.wait(lock, [this]() {
                    return stop_ || !pending_.empty();
                });
                if (stop_ && pending_.empty()) {
                    return;
                }
                if (disabled_) {
                    pending_.clear();
                    if (stop_) {
                        return;
                    }
                    continue;
                }
                item = pending_.front();
                pending_.pop_front();
            }

            const VkResult result = wait_fn_(
                device_, item.swapchain, item.present_id, kWaitTimeoutNs);
            const uint64_t now_us = now_monotonic_us();
            const uint64_t latency_us =
                now_us > item.submit_us ? now_us - item.submit_us : 0;
            // Diagnostic: surface the first handful of wait results (success or
            // failure) so we can see whether the wait path is the blocker.
            if (processed_count_ < 8 || (processed_count_ % 600) == 0) {
                send_wait_result_debug(
                    device_,
                    item.swapchain,
                    item.present_id,
                    static_cast<int>(result),
                    latency_us);
            }
            ++processed_count_;
            if (result == VK_SUCCESS) {
                if (latency_us > 0 && latency_us <= kMaxPlausibleUs) {
                    send_display_latency_sample(
                        device_,
                        item.swapchain,
                        item.present_id,
                        latency_us);
                }
                error_count_ = 0;
            } else if (
                result == VK_TIMEOUT || result == VK_SUBOPTIMAL_KHR
                || result == VK_ERROR_OUT_OF_DATE_KHR) {
                // Transient: no usable timestamp, but not a reason to give up.
            } else if (++error_count_ >= kErrorDisableThreshold) {
                std::lock_guard<std::mutex> lock(mutex_);
                disabled_ = true;
            }
        }
    }

    VkDevice device_;
    PFN_vkWaitForPresentKHR wait_fn_;
    std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<Pending> pending_;
    bool stop_ = false;
    bool disabled_ = false;
    uint64_t error_count_ = 0;
    uint64_t processed_count_ = 0;
    std::thread thread_;
};

std::mutex g_display_waiters_mutex;
std::unordered_map<VkDevice, std::unique_ptr<DisplayLatencyWaiter>>
    g_display_waiters;

void display_latency_enqueue(
    VkDevice device,
    PFN_vkWaitForPresentKHR wait_fn,
    VkSwapchainKHR swapchain,
    uint64_t present_id,
    uint64_t submit_us) {
    if (!wait_fn || !present_id) {
        return;
    }
    std::lock_guard<std::mutex> lock(g_display_waiters_mutex);
    std::unique_ptr<DisplayLatencyWaiter>& slot = g_display_waiters[device];
    if (!slot) {
        slot = std::make_unique<DisplayLatencyWaiter>(device, wait_fn);
    }
    slot->enqueue(swapchain, present_id, submit_us);
}

// Stop and join the device's wait thread, returning before the caller destroys
// the swapchain/device. Any in-flight wait targets a still-live swapchain (the
// app has not yet called the driver's destroy), so the join is bounded by the
// present completing or kWaitTimeoutNs. Recreated lazily on the next present.
void display_latency_stop_device(VkDevice device) {
    std::unique_ptr<DisplayLatencyWaiter> waiter;
    {
        std::lock_guard<std::mutex> lock(g_display_waiters_mutex);
        auto it = g_display_waiters.find(device);
        if (it == g_display_waiters.end()) {
            return;
        }
        waiter = std::move(it->second);
        g_display_waiters.erase(it);
    }
    // waiter destructor joins the thread here, outside the lock.
}

const char* latency_marker_name(VkLatencyMarkerNV marker) {
    switch (marker) {
        case VK_LATENCY_MARKER_RENDERSUBMIT_START_NV:
            return "rendersubmit-start";
        case VK_LATENCY_MARKER_PRESENT_START_NV:
            return "present-start";
        case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_START_NV:
            return "oob-present-start";
        default:
            return "other";
    }
}

bool is_base_frame_marker(VkLatencyMarkerNV marker) {
    return marker == VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_START_NV
        || marker == VK_LATENCY_MARKER_PRESENT_START_NV
        || marker == VK_LATENCY_MARKER_RENDERSUBMIT_START_NV;
}

BaseMarkerState* base_marker_state_for(
    SwapchainContext& context,
    VkLatencyMarkerNV marker) {
    switch (marker) {
        case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_START_NV:
            return &context.out_of_band_present_start;
        case VK_LATENCY_MARKER_PRESENT_START_NV:
            return &context.present_start;
        case VK_LATENCY_MARKER_RENDERSUBMIT_START_NV:
            return &context.render_submit_start;
        default:
            return nullptr;
    }
}

void send_base_frame_marker_sample(
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t base_frame_id,
    uint64_t base_frame_frametime_us,
    VkLatencyMarkerNV marker) {
    char line[640]{};
    const int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"timing\","
        "\"measurement\":\"base-frame-marker-pacing\","
        "\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"quality\":\"base-frame-marker\","
        "\"base_frame_id\":%" PRIu64 ","
        "\"base_frame_frametime_us\":%" PRIu64 ","
        "\"marker\":%d,"
        "\"marker_name\":\"%s\"}",
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        base_frame_id,
        base_frame_frametime_us,
        static_cast<int>(marker),
        latency_marker_name(marker));

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

void observe_latency_marker(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkSetLatencyMarkerInfoNV* marker_info) {
    if (!marker_info || !marker_info->presentID
        || !is_base_frame_marker(marker_info->marker)) {
        return;
    }

    const uint64_t marker_time_us = now_monotonic_us();
    uint64_t base_frame_frametime_us = 0;
    {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it == g_swapchains.end()) {
            return;
        }

        auto* state = base_marker_state_for(swapchain_it->second, marker_info->marker);
        if (!state) {
            return;
        }

        if (state->frame_id == marker_info->presentID) {
            return;
        }

        if (state->timestamp_us && marker_time_us > state->timestamp_us) {
            base_frame_frametime_us =
                marker_time_us - state->timestamp_us;
        }

        state->frame_id = marker_info->presentID;
        state->timestamp_us = marker_time_us;
    }

    if (base_frame_frametime_us) {
        send_base_frame_marker_sample(
            device,
            swapchain,
            marker_info->presentID,
            base_frame_frametime_us,
            marker_info->marker);
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
    // receiver sees genuine per-frame samples.
    std::vector<uint32_t> usable_indices;
    usable_indices.reserve(written_count);
    for (uint32_t index = 0; index < written_count; ++index) {
        const auto& report = reports[index];
        const bool usable = report.presentID || report_has_driver_timing(report)
            || report_has_reflex_markers(report);
        if (!usable) {
            continue;
        }
        usable_indices.push_back(index);
    }

    uint64_t last_emitted = 0;
    {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end()) {
            last_emitted = swapchain_it->second.last_emitted_present_id;
        }
    }

    bool has_newest = false;
    uint32_t newest_index = 0;
    uint64_t newest_present_id = 0;
    bool saw_lower_present_id = false;
    bool saw_higher_present_id = false;
    for (uint32_t index : usable_indices) {
        const uint64_t present_id = reports[index].presentID;
        if (!has_newest || present_id >= newest_present_id) {
            has_newest = true;
            newest_index = index;
            newest_present_id = present_id;
        }
        if (last_emitted && present_id && present_id < last_emitted) {
            saw_lower_present_id = true;
        }
        if (present_id > last_emitted) {
            saw_higher_present_id = true;
        }
    }

    // A frozen ring keeps its newest entry equal to last_emitted; that is
    // staleness, not a presentID reset. Treating it as a reset re-emits all
    // 64 stale reports every present (observed live at ~3.5k samples/s on
    // 2026-06-11) and defeats the receiver's duplicate detection because the
    // IDs cycle. Only call it a reset when even the newest ID went backwards.
    const bool reset_detected =
        last_emitted && saw_lower_present_id && !saw_higher_present_id
        && newest_present_id < last_emitted;
    const uint64_t emit_after = reset_detected ? 0 : last_emitted;
    uint64_t new_last_emitted = reset_detected ? 0 : last_emitted;
    bool emitted_fresh = false;
    for (uint32_t index : usable_indices) {
        const uint64_t present_id = reports[index].presentID;
        if (present_id && present_id <= emit_after) {
            continue;
        }
        send_timing_sample(device, swapchain, reports[index], written_count);
        emitted_fresh = true;
        if (present_id > new_last_emitted) {
            new_last_emitted = present_id;
        }
    }

    if (new_last_emitted != last_emitted || reset_detected) {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end()) {
            swapchain_it->second.last_emitted_present_id = new_last_emitted;
        }
    }

    // Nothing newer than last time: the Reflex ring has stopped advancing.
    // Re-emit the newest report once so the receiver's duplicate-report
    // detection can flag the stream as stale instead of us silently dropping
    // it (which would otherwise leave the meter showing a frozen value).
    if (!emitted_fresh && has_newest) {
        send_timing_sample(device, swapchain, reports[newest_index], written_count);
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
    const bool present_wait_advertised = extension_advertised(
        instance_context,
        physical_device,
        VK_KHR_PRESENT_WAIT_EXTENSION_NAME);
    const bool present_wait_requested = extension_requested(
        create_info,
        VK_KHR_PRESENT_WAIT_EXTENSION_NAME);
    const bool present_id_advertised = extension_advertised(
        instance_context,
        physical_device,
        VK_KHR_PRESENT_ID_EXTENSION_NAME);
    const bool present_id_requested = extension_requested(
        create_info,
        VK_KHR_PRESENT_ID_EXTENSION_NAME);

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
    context.device_wait_idle = reinterpret_cast<PFN_vkDeviceWaitIdle>(
        next_get_device_proc_addr(*device, "vkDeviceWaitIdle"));
    context.get_device_queue = reinterpret_cast<PFN_vkGetDeviceQueue>(
        next_get_device_proc_addr(*device, "vkGetDeviceQueue"));
    context.get_device_queue2 = reinterpret_cast<PFN_vkGetDeviceQueue2>(
        next_get_device_proc_addr(*device, "vkGetDeviceQueue2"));
    context.create_swapchain_khr = reinterpret_cast<PFN_vkCreateSwapchainKHR>(
        next_get_device_proc_addr(*device, "vkCreateSwapchainKHR"));
    context.destroy_swapchain_khr = reinterpret_cast<PFN_vkDestroySwapchainKHR>(
        next_get_device_proc_addr(*device, "vkDestroySwapchainKHR"));
    context.get_swapchain_images_khr =
        reinterpret_cast<PFN_vkGetSwapchainImagesKHR>(
            next_get_device_proc_addr(*device, "vkGetSwapchainImagesKHR"));
    context.queue_present_khr = reinterpret_cast<PFN_vkQueuePresentKHR>(
        next_get_device_proc_addr(*device, "vkQueuePresentKHR"));
    context.queue_submit = reinterpret_cast<PFN_vkQueueSubmit>(
        next_get_device_proc_addr(*device, "vkQueueSubmit"));
    context.create_image_view = reinterpret_cast<PFN_vkCreateImageView>(
        next_get_device_proc_addr(*device, "vkCreateImageView"));
    context.destroy_image_view = reinterpret_cast<PFN_vkDestroyImageView>(
        next_get_device_proc_addr(*device, "vkDestroyImageView"));
    context.create_render_pass = reinterpret_cast<PFN_vkCreateRenderPass>(
        next_get_device_proc_addr(*device, "vkCreateRenderPass"));
    context.destroy_render_pass = reinterpret_cast<PFN_vkDestroyRenderPass>(
        next_get_device_proc_addr(*device, "vkDestroyRenderPass"));
    context.create_framebuffer = reinterpret_cast<PFN_vkCreateFramebuffer>(
        next_get_device_proc_addr(*device, "vkCreateFramebuffer"));
    context.destroy_framebuffer = reinterpret_cast<PFN_vkDestroyFramebuffer>(
        next_get_device_proc_addr(*device, "vkDestroyFramebuffer"));
    context.create_command_pool = reinterpret_cast<PFN_vkCreateCommandPool>(
        next_get_device_proc_addr(*device, "vkCreateCommandPool"));
    context.destroy_command_pool = reinterpret_cast<PFN_vkDestroyCommandPool>(
        next_get_device_proc_addr(*device, "vkDestroyCommandPool"));
    context.allocate_command_buffers =
        reinterpret_cast<PFN_vkAllocateCommandBuffers>(
            next_get_device_proc_addr(*device, "vkAllocateCommandBuffers"));
    context.reset_command_buffer = reinterpret_cast<PFN_vkResetCommandBuffer>(
        next_get_device_proc_addr(*device, "vkResetCommandBuffer"));
    context.begin_command_buffer = reinterpret_cast<PFN_vkBeginCommandBuffer>(
        next_get_device_proc_addr(*device, "vkBeginCommandBuffer"));
    context.end_command_buffer = reinterpret_cast<PFN_vkEndCommandBuffer>(
        next_get_device_proc_addr(*device, "vkEndCommandBuffer"));
    context.cmd_begin_render_pass = reinterpret_cast<PFN_vkCmdBeginRenderPass>(
        next_get_device_proc_addr(*device, "vkCmdBeginRenderPass"));
    context.cmd_end_render_pass = reinterpret_cast<PFN_vkCmdEndRenderPass>(
        next_get_device_proc_addr(*device, "vkCmdEndRenderPass"));
    context.cmd_clear_attachments = reinterpret_cast<PFN_vkCmdClearAttachments>(
        next_get_device_proc_addr(*device, "vkCmdClearAttachments"));
    context.create_semaphore = reinterpret_cast<PFN_vkCreateSemaphore>(
        next_get_device_proc_addr(*device, "vkCreateSemaphore"));
    context.destroy_semaphore = reinterpret_cast<PFN_vkDestroySemaphore>(
        next_get_device_proc_addr(*device, "vkDestroySemaphore"));
    context.create_fence = reinterpret_cast<PFN_vkCreateFence>(
        next_get_device_proc_addr(*device, "vkCreateFence"));
    context.destroy_fence = reinterpret_cast<PFN_vkDestroyFence>(
        next_get_device_proc_addr(*device, "vkDestroyFence"));
    context.get_fence_status = reinterpret_cast<PFN_vkGetFenceStatus>(
        next_get_device_proc_addr(*device, "vkGetFenceStatus"));
    context.reset_fences = reinterpret_cast<PFN_vkResetFences>(
        next_get_device_proc_addr(*device, "vkResetFences"));
    context.wait_for_fences = reinterpret_cast<PFN_vkWaitForFences>(
        next_get_device_proc_addr(*device, "vkWaitForFences"));
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
    // Only resolve the present-wait entry point when the app enabled the
    // extension; calling it otherwise would be undefined.
    if (present_wait_requested) {
        context.wait_for_present_khr = reinterpret_cast<PFN_vkWaitForPresentKHR>(
            next_get_device_proc_addr(*device, "vkWaitForPresentKHR"));
    }
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
    context.present_wait_advertised = present_wait_advertised;
    context.present_wait_requested = present_wait_requested;
    context.present_id_advertised = present_id_advertised;
    context.present_id_requested = present_id_requested;

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
    DeviceContext device_context{};
    bool has_device_context = false;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_destroy_device = it->second.destroy_device;
            device_context = it->second;
            has_device_context = true;
        }
    }

    // Stop the present-wait thread before tearing the device down so it cannot
    // call into a device that is about to be destroyed.
    display_latency_stop_device(device);

    if (has_device_context && device_context.device_wait_idle) {
        device_context.device_wait_idle(device);
    }

    if (has_device_context) {
        std::lock_guard lock(g_mutex);
        for (auto& entry : g_swapchains) {
            if (entry.second.device == device) {
                destroy_overlay_resources(device_context, entry.second);
            }
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
        if (create_info) {
            context.image_format = create_info->imageFormat;
            context.image_extent = create_info->imageExtent;
            context.image_array_layers = create_info->imageArrayLayers;
            context.image_usage = create_info->imageUsage;
        }
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
    DeviceContext device_context{};
    bool has_device_context = false;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next_destroy_swapchain = it->second.destroy_swapchain_khr;
            device_context = it->second;
            has_device_context = true;
        }
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end() && has_device_context) {
            destroy_overlay_resources(device_context, swapchain_it->second);
        }
    }

    // Quiesce the present-wait thread before the driver destroys the swapchain,
    // so no wait is ever in flight across the destroy.
    display_latency_stop_device(device);

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
    DeviceContext device_context{};
    QueueContext queue_context{};
    bool has_device_context = false;
    bool has_queue_context = false;
    {
        std::lock_guard lock(g_mutex);
        auto queue_it = g_queues.find(queue);
        if (queue_it != g_queues.end()) {
            queue_context = queue_it->second;
            has_queue_context = true;
            device = queue_it->second.device;
            auto device_it = g_devices.find(device);
            if (device_it != g_devices.end()) {
                next_queue_present = device_it->second.queue_present_khr;
                device_context = device_it->second;
                has_device_context = true;
            }
        }
    }

    if (!next_queue_present) {
        return VK_ERROR_INITIALIZATION_FAILED;
    }

    struct PresentObservation {
        VkSwapchainKHR swapchain = VK_NULL_HANDLE;
        uint64_t present_count = 0;
        uint64_t present_frametime_us = 0;
        uint64_t overlay_fps = 0;
        uint64_t vulkan_present_id = 0;
        uint64_t injected_present_id = 0;
    };
    std::vector<PresentObservation> observations;
    VkSemaphore overlay_signal = VK_NULL_HANDLE;
    const uint64_t present_time_us = now_monotonic_us();
    // Whether to stamp our own present IDs this present. The app must enable
    // present_id + present_wait, both opt-in env flags must be set, and we must
    // be able to place our struct without duplicating one: either the app chained
    // none, or it chained one at the head (vkd3d's zero-ID struct) which we
    // substitute. A present-id struct buried deeper in the chain we leave alone.
    const bool chain_has_present_id =
        present_info_has_present_id_struct(present_info);
    const auto* present_chain_head = present_info
        ? reinterpret_cast<const VkBaseInStructure*>(present_info->pNext)
        : nullptr;
    const bool present_id_at_head = present_chain_head
        && present_chain_head->sType == VK_STRUCTURE_TYPE_PRESENT_ID_KHR;
    const bool want_inject = inject_present_id_enabled()
        && display_latency_enabled()
        && has_device_context
        && device_context.present_id_requested
        && device_context.present_wait_requested
        && device_context.wait_for_present_khr
        && (!chain_has_present_id || present_id_at_head);
    if (present_info && present_info->pSwapchains) {
        observations.reserve(present_info->swapchainCount);
        std::lock_guard lock(g_mutex);
        for (uint32_t i = 0; i < present_info->swapchainCount; ++i) {
            VkSwapchainKHR swapchain = present_info->pSwapchains[i];
            const uint64_t vulkan_present_id =
                present_id_for_swapchain(present_info, i);
            PresentObservation observation{};
            observation.swapchain = swapchain;
            observation.vulkan_present_id = vulkan_present_id;
            auto swapchain_it = g_swapchains.find(swapchain);
            if (swapchain_it != g_swapchains.end()) {
                auto& swapchain_context = swapchain_it->second;
                observation.present_count = ++swapchain_context.present_count;
                if (vulkan_present_id) {
                    swapchain_context.last_vulkan_present_id =
                        vulkan_present_id;
                } else if (want_inject) {
                    // Strictly increasing per swapchain, as the spec requires.
                    observation.injected_present_id =
                        ++swapchain_context.last_injected_present_id;
                }
                if (swapchain_context.last_present_us
                    && present_time_us > swapchain_context.last_present_us) {
                    observation.present_frametime_us =
                        present_time_us - swapchain_context.last_present_us;
                }
                swapchain_context.last_present_us = present_time_us;
                if (overlay_enabled() && observation.present_frametime_us) {
                    observation.overlay_fps =
                        median_fps_from_present_frametime_locked(
                            swapchain_context,
                            observation.present_frametime_us);
                }
                if (overlay_enabled() && has_device_context && has_queue_context
                    && present_info->swapchainCount == 1
                    && present_info->pImageIndices) {
                    const std::string overlay_text =
                        cached_overlay_text(
                            observation.overlay_fps,
                            present_time_us);
                    if (!overlay_text.empty()) {
                        overlay_signal = submit_overlay_locked(
                            device_context,
                            swapchain,
                            swapchain_context,
                            queue_context,
                            queue,
                            present_info->pImageIndices[i],
                            present_info,
                            overlay_text);
                    }
                }
            }
            observations.push_back(observation);
        }
    }

    // Present-id array must outlive the present call (the driver reads it).
    std::vector<uint64_t> injected_present_ids;
    bool any_injected = false;
    if (want_inject && present_info) {
        injected_present_ids.assign(present_info->swapchainCount, 0);
        for (size_t i = 0;
             i < observations.size() && i < injected_present_ids.size();
             ++i) {
            injected_present_ids[i] = observations[i].injected_present_id;
            if (injected_present_ids[i]) {
                any_injected = true;
            }
        }
    }

    VkPresentInfoKHR modified_present_info{};
    VkPresentIdKHR present_id_info{};
    const VkPresentInfoKHR* next_present_info = present_info;
    if (present_info && (overlay_signal != VK_NULL_HANDLE || any_injected)) {
        modified_present_info = *present_info;
        if (overlay_signal != VK_NULL_HANDLE) {
            modified_present_info.waitSemaphoreCount = 1;
            modified_present_info.pWaitSemaphores = &overlay_signal;
        }
        if (any_injected) {
            present_id_info.sType = VK_STRUCTURE_TYPE_PRESENT_ID_KHR;
            // Replace the app's head present-id struct (skip past it), else
            // prepend, preserving the rest of the pNext chain behind ours.
            present_id_info.pNext = present_id_at_head
                ? present_chain_head->pNext
                : present_info->pNext;
            present_id_info.swapchainCount = present_info->swapchainCount;
            present_id_info.pPresentIds = injected_present_ids.data();
            modified_present_info.pNext = &present_id_info;
        }
        next_present_info = &modified_present_info;
    }

    VkResult result = next_queue_present(queue, next_present_info);
    for (const PresentObservation& observation : observations) {
        VkSwapchainKHR swapchain = observation.swapchain;
        const uint64_t present_count = observation.present_count;
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
        if (observation.present_frametime_us) {
            send_present_pacing_sample(
                device,
                swapchain,
                observation.present_frametime_us,
                present_count);
        }
        // Opt-in present->scanout tail. Waits on the app's own present ID when it
        // supplied one, else on the ID we injected this present (inject path).
        const uint64_t effective_present_id =
            observation.injected_present_id
                ? observation.injected_present_id
                : observation.vulkan_present_id;
        if (display_latency_enabled() && has_device_context
            && device_context.present_wait_requested
            && device_context.present_id_requested
            && device_context.wait_for_present_khr
            && effective_present_id) {
            display_latency_enqueue(
                device,
                device_context.wait_for_present_khr,
                swapchain,
                effective_present_id,
                present_time_us);
        }
        if (inject_present_id_enabled()
            && should_report_counter(present_count)) {
            send_inject_debug_event(
                device,
                swapchain,
                present_count,
                chain_has_present_id,
                present_id_at_head,
                want_inject,
                observation.injected_present_id);
        }
        if (observation.overlay_fps) {
            write_overlay_text_file(observation.overlay_fps, present_time_us);
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
    observe_latency_marker(device, swapchain, marker_info);
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
