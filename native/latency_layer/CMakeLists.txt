cmake_minimum_required(VERSION 3.20)

project(penguinburner_latency_layer LANGUAGES CXX)

enable_testing()

include(GNUInstallDirs)

find_path(VULKAN_INCLUDE_DIR vulkan/vulkan.h REQUIRED)

add_library(penguinburner_latency_layer SHARED
  src/penguinburner_latency_layer.cpp
)

target_include_directories(penguinburner_latency_layer PRIVATE
  "${VULKAN_INCLUDE_DIR}"
)

target_compile_features(penguinburner_latency_layer PRIVATE cxx_std_17)

if(CMAKE_SIZEOF_VOID_P EQUAL 8)
  set(PB_LAYER_LIBRARY_ARCH "64")
else()
  set(PB_LAYER_LIBRARY_ARCH "32")
endif()

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
