// Telemetry socket JSON emitters, flow snapshots, extension probes.
#include "latency_layer_internal.h"

namespace pblayer {

bool layer_enabled() {
    return std::getenv(kEnableEnv) || std::getenv(kMasterEnableEnv);
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
// overlay/telemetry/receiver.py MARKER_TIMING_QUALITIES.
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
        "\"session_id\":\"%s\","
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
        telemetry_session_id(),
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
    if (!layer_enabled()) {
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
    if (!layer_enabled()) {
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

}  // namespace pblayer
