// PenguinBurner NVAPI latency shim — a drop-in proxy nvapi64.dll.
//
// nvapi64.dll exposes exactly one export, `nvapi_QueryInterface(id)`, which the
// app uses to look up every NVAPI function by a 32-bit id. This DLL re-exports
// only that symbol, forwards every call to the *real* (dxvk-nvapi) nvapi64.dll,
// and swaps in its own wrappers for the Reflex latency-marker functions so we
// can emit the same per-frame marker stream the dxvk-nvapi trace produced — at
// marker rate instead of full trace, with NO fork of dxvk-nvapi and NO
// per-Proton rebuild. The launcher fronts system32\nvapi64.dll with this proxy
// and parks the real dxvk-nvapi next to us as nvapi64-pb.dll (kept refreshed
// across Proton re-syncs), which is what we forward to.
//
// Markers under frame generation are dropped at vkd3d's owner-gate before they
// reach Vulkan, so a Vulkan layer can't see them; tapping here at the NVAPI ABI
// is *above* the owner-gate, so they survive. NVAPI is a stable public ABI, so
// the ids/struct offsets below never change across dxvk-nvapi/Proton versions.
//
// Output is written to stderr in the exact format
// overlay/telemetry/nvapi_marker_bridge.py already parses (`_MARKER_LOG_RE`), so
// nothing downstream changes — the launcher already dup2's the game's stderr
// onto the marker FIFO the bridge drains.
//
// ABI constants verified against dxvk-nvapi external/nvapi/{nvapi.h,
// nvapi_interface.h}: see NV_LATENCY_MARKER_PARAMS_V1 (size 88; version@0,
// frameID@8, markerType@16) and the id table.

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>

namespace {

// ---- minimal NVAPI ABI (exact layouts from dxvk-nvapi external/nvapi/nvapi.h)
using NvU32 = unsigned int;
using NvU64 = unsigned long long;
using NvU8 = unsigned char;
using NvAPI_Status = int;             // NVAPI_OK == 0
using NV_LATENCY_MARKER_TYPE = int;   // enum, 4 bytes

// Function ids (nvapi_interface.h).
constexpr NvU32 kIdSetSleepMode = 0xac1ca9e0;
constexpr NvU32 kIdGetLatency = 0x1a587f9c;
constexpr NvU32 kIdSetLatencyMarker = 0xd9984c05;
constexpr NvU32 kIdAsyncFrameMarker = 0x13c98f73;

// NV_LATENCY_MARKER_PARAMS_V1 — size 88.
struct NvLatencyMarkerParams {
    NvU32 version;                  // @0
    NvU64 frameID;                  // @8
    NV_LATENCY_MARKER_TYPE markerType;  // @16
    NvU64 rsvd0;                    // @24
    NvU8 rsvd[56];                  // @32
};

// NV_ASYNC_FRAME_MARKER_PARAMS_V1 (D3D12_SetAsyncFrameMarker, FG present path).
struct NvAsyncFrameMarkerParams {
    NvU32 version;                  // @0
    NvU64 frameID;                  // @8
    NV_LATENCY_MARKER_TYPE markerType;  // @16
    NvU64 presentFrameID;           // @24
    NvU8 vendorInternal;            // @32
    NvU8 rsvd[55];                  // @33
};

using PfnQueryInterface = void*(__cdecl*)(NvU32 id);
using PfnSetLatencyMarker = NvAPI_Status(__cdecl*)(void* dev, NvLatencyMarkerParams* p);
using PfnAsyncFrameMarker = NvAPI_Status(__cdecl*)(void* queue, NvAsyncFrameMarkerParams* p);
using PfnGetLatency = NvAPI_Status(__cdecl*)(void* dev, void* p);
using PfnSetSleepMode = NvAPI_Status(__cdecl*)(void* dev, void* p);

// ---- shared state ------------------------------------------------------------
HMODULE g_self = nullptr;
HMODULE g_real = nullptr;
PfnQueryInterface g_real_qi = nullptr;
volatile PfnSetLatencyMarker g_real_set_marker = nullptr;
volatile PfnAsyncFrameMarker g_real_async_marker = nullptr;
volatile PfnGetLatency g_real_get_latency = nullptr;
volatile PfnSetSleepMode g_real_set_sleep = nullptr;
INIT_ONCE g_init_once = INIT_ONCE_STATIC_INIT;
CRITICAL_SECTION g_emit_lock;
double g_qpc_to_us = 0.0;
HANDLE g_out = INVALID_HANDLE_VALUE;
DWORD g_pid = 0;

// Only the marker types the bridge consumes; others are dropped to keep volume
// low (the bridge maps unknown names to None anyway).
const char* marker_name(NV_LATENCY_MARKER_TYPE type) {
    switch (type) {
        case 0: return "SIMULATION_START";
        case 5: return "PRESENT_END";
        case 6: return "INPUT_SAMPLE";
        case 11: return "OUT_OF_BAND_PRESENT_START";
        case 12: return "OUT_OF_BAND_PRESENT_END";
        default: return nullptr;
    }
}

NvU64 qpc_us() {
    LARGE_INTEGER counter;
    QueryPerformanceCounter(&counter);
    return static_cast<NvU64>(static_cast<double>(counter.QuadPart) * g_qpc_to_us);
}

void emit_raw(const char* buf, int len) {
    if (g_out == INVALID_HANDLE_VALUE || len <= 0) {
        return;
    }
    EnterCriticalSection(&g_emit_lock);
    DWORD written = 0;
    WriteFile(g_out, buf, static_cast<DWORD>(len), &written, nullptr);
    LeaveCriticalSection(&g_emit_lock);
}

void emit_marker(NvU64 frame_id, NV_LATENCY_MARKER_TYPE type) {
    const char* name = marker_name(type);
    if (!name) {
        return;
    }
    char buf[192];
    int len = std::snprintf(
        buf, sizeof(buf),
        "0.0:%x:0:latency-marker:pb:qpcUs=%llu frameID=%llu markerType=%s\n",
        static_cast<unsigned>(g_pid),
        static_cast<unsigned long long>(qpc_us()),
        static_cast<unsigned long long>(frame_id), name);
    emit_raw(buf, len);
}

// ---- init: open output + locate/self-refresh/load the real nvapi64.dll -------
void open_output() {
    const char* path = std::getenv("PENGUIN_BURNER_SHIM_OUTPUT");
    if (path && path[0]) {
        HANDLE handle = CreateFileA(
            path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
            OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (handle != INVALID_HANDLE_VALUE) {
            g_out = handle;
            return;
        }
    }
    g_out = GetStdHandle(STD_ERROR_HANDLE);
}

// We are system32\nvapi64.dll. The launcher parked the real dxvk-nvapi next to
// us under a distinct basename (nvapi64-pb.dll) so we can load it without the
// loader handing us back our own module on a same-name load. The launcher also
// keeps that sidecar refreshed across Proton re-syncs, so we just load it.
bool load_real() {
    char sidecar[MAX_PATH];
    const char* override_path = std::getenv("PENGUIN_BURNER_SHIM_REAL");
    if (override_path && override_path[0]) {
        g_real = LoadLibraryA(override_path);
    }
    if (!g_real) {
        char sysdir[MAX_PATH];
        UINT n = GetSystemDirectoryA(sysdir, MAX_PATH);
        if (n == 0 || n >= MAX_PATH) {
            return false;
        }
        std::snprintf(sidecar, sizeof(sidecar), "%s\\nvapi64-pb.dll", sysdir);
        g_real = LoadLibraryA(sidecar);
    }
    if (!g_real) {
        return false;
    }
    g_real_qi = reinterpret_cast<PfnQueryInterface>(
        GetProcAddress(g_real, "nvapi_QueryInterface"));
    return g_real_qi != nullptr;
}

BOOL CALLBACK init_once(PINIT_ONCE, PVOID, PVOID*) {
    LARGE_INTEGER freq;
    QueryPerformanceFrequency(&freq);
    g_qpc_to_us = 1000000.0 / static_cast<double>(freq.QuadPart);
    g_pid = GetCurrentProcessId();
    open_output();
    bool ok = load_real();
    char banner[256];
    int len = std::snprintf(
        banner, sizeof(banner),
        "[pb-nvapi-shim] init pid=%u real=%s qi=%p\n",
        static_cast<unsigned>(g_pid), ok ? "ok" : "MISSING",
        reinterpret_cast<void*>(g_real_qi));
    emit_raw(banner, len);
    return TRUE;
}

void ensure_init() {
    InitOnceExecuteOnce(&g_init_once, init_once, nullptr, nullptr);
}

// ---- tapped wrappers (addresses handed to the app via QueryInterface) --------
NvAPI_Status __cdecl wrap_set_latency_marker(void* dev, NvLatencyMarkerParams* p) {
    if (p) {
        emit_marker(p->frameID, p->markerType);
    }
    PfnSetLatencyMarker real = g_real_set_marker;
    return real ? real(dev, p) : 0;
}

NvAPI_Status __cdecl wrap_async_frame_marker(void* queue, NvAsyncFrameMarkerParams* p) {
    if (p) {
        emit_marker(p->frameID, p->markerType);
    }
    PfnAsyncFrameMarker real = g_real_async_marker;
    return real ? real(queue, p) : 0;
}

NvAPI_Status __cdecl wrap_get_latency(void* dev, void* p) {
    PfnGetLatency real = g_real_get_latency;
    return real ? real(dev, p) : 0;
}

NvAPI_Status __cdecl wrap_set_sleep_mode(void* dev, void* p) {
    PfnSetSleepMode real = g_real_set_sleep;
    return real ? real(dev, p) : 0;
}

}  // namespace

// ---- the only export: forward everything, wrap the latency ids ---------------
extern "C" void* __cdecl nvapi_QueryInterface(NvU32 id) {
    ensure_init();
    if (!g_real_qi) {
        return nullptr;
    }
    void* real = g_real_qi(id);
    if (!real) {
        return real;
    }
    switch (id) {
        case kIdSetLatencyMarker:
            g_real_set_marker = reinterpret_cast<PfnSetLatencyMarker>(real);
            return reinterpret_cast<void*>(&wrap_set_latency_marker);
        case kIdAsyncFrameMarker:
            g_real_async_marker = reinterpret_cast<PfnAsyncFrameMarker>(real);
            return reinterpret_cast<void*>(&wrap_async_frame_marker);
        case kIdGetLatency:
            g_real_get_latency = reinterpret_cast<PfnGetLatency>(real);
            return reinterpret_cast<void*>(&wrap_get_latency);
        case kIdSetSleepMode:
            g_real_set_sleep = reinterpret_cast<PfnSetSleepMode>(real);
            return reinterpret_cast<void*>(&wrap_set_sleep_mode);
        default:
            return real;
    }
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = inst;
        DisableThreadLibraryCalls(inst);
        InitializeCriticalSection(&g_emit_lock);
    }
    return TRUE;
}
