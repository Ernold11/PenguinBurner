cmake_minimum_required(VERSION 3.20)

project(penguinburner_latency_layer LANGUAGES CXX)

include(GNUInstallDirs)

find_path(VULKAN_INCLUDE_DIR vulkan/vulkan.h REQUIRED)

add_library(penguinburner_latency_layer SHARED
  src/penguinburner_latency_layer.cpp
)

target_include_directories(penguinburner_latency_layer PRIVATE
  "${VULKAN_INCLUDE_DIR}"
)

target_compile_features(penguinburner_latency_layer PRIVATE cxx_std_17)

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(penguinburner_latency_layer PRIVATE
    -Wall
    -Wextra
    -Wpedantic
    -fvisibility=hidden
  )
endif()

set_target_properties(penguinburner_latency_layer PROPERTIES
  OUTPUT_NAME "VkLayer_penguinburner_latency"
)

# Unit test for the pure per-frame emit-decision logic (no Vulkan device needed).
# Enabled by default; run with `ctest` or the built binary directly.
enable_testing()
add_executable(test_latency_emit_plan test/test_latency_emit_plan.cpp)
target_compile_features(test_latency_emit_plan PRIVATE cxx_std_17)
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(test_latency_emit_plan PRIVATE -Wall -Wextra -Wpedantic)
endif()
add_test(NAME latency_emit_plan COMMAND test_latency_emit_plan)

set(PB_LAYER_LIBRARY_PATH "./libVkLayer_penguinburner_latency.so")
configure_file(
  VkLayer_PENGUINBURNER_latency.json.in
  "${CMAKE_CURRENT_BINARY_DIR}/VkLayer_PENGUINBURNER_latency.json"
  @ONLY
)

set(PB_LAYER_LIBRARY_PATH
  "${CMAKE_INSTALL_FULL_LIBDIR}/libVkLayer_penguinburner_latency.so"
)
configure_file(
  VkLayer_PENGUINBURNER_latency.json.in
  "${CMAKE_CURRENT_BINARY_DIR}/install/VkLayer_PENGUINBURNER_latency.json"
  @ONLY
)

install(TARGETS penguinburner_latency_layer
  LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}"
)

install(
  FILES "${CMAKE_CURRENT_BINARY_DIR}/install/VkLayer_PENGUINBURNER_latency.json"
  DESTINATION "${CMAKE_INSTALL_DATADIR}/vulkan/implicit_layer.d"
)
