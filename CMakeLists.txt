cmake_minimum_required(VERSION 3.14)
project(Sample LANGUAGES C CXX VERSION 1.0.0)

cmake_policy(SET CMP0077 NEW)
set(CMAKE_POLICY_DEFAULT_CMP0077 NEW)

set(CMAKE_BUILD_TYPE Debug)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

list(APPEND CMAKE_MODULE_PATH ${CMAKE_CURRENT_LIST_DIR}/cmake)

set(BUILD_DWARFDUMP OFF)
set(LIBASSERT_USE_EXTERNAL_CPPTRACE ON)
set(LIBASSERT_USE_EXTERNAL_MAGIC_ENUM ON)
set(CPPTRACE_USE_EXTERNAL_LIBDWARF ON)
set(CPPTRACE_GET_SYMBOLS_WITH_LIBDWARF ON)

# System dependencies installed via apt:
#   sudo apt install libavutil-dev libavcodec-dev libavformat-dev \
#                    libswscale-dev libsdl2-dev pkg-config
find_package(PkgConfig REQUIRED)
pkg_check_modules(FFmpeg REQUIRED IMPORTED_TARGET
  libavutil libavcodec libavformat libswscale)
find_package(SDL2 REQUIRED)

include_directories(third_party/yuv/include)
include_directories(third_party/spdlog/include)
include_directories(third_party/argparse/include)
include_directories(third_party/cpptrace/include)
include_directories(third_party/libassert/include)
include_directories(third_party/magic_enum/include)
include_directories(third_party/libdwarf-lite/include)

add_subdirectory(third_party/yuv)
add_subdirectory(third_party/spdlog)
add_subdirectory(third_party/libdwarf-lite)
add_subdirectory(third_party/cpptrace)
add_subdirectory(third_party/libassert)

add_executable(${PROJECT_NAME} main.cpp)
target_precompile_headers(${PROJECT_NAME} PRIVATE pch.hpp)
target_link_libraries(${PROJECT_NAME}
  spdlog::spdlog
  libassert::assert
  yuv
  SDL2::SDL2
  PkgConfig::FFmpeg)

add_executable(yuvtool experiment/yuvtool.cpp)
target_include_directories(yuvtool PRIVATE ${CMAKE_CURRENT_LIST_DIR})
target_precompile_headers(yuvtool PRIVATE pch.hpp)
target_link_libraries(yuvtool
  spdlog::spdlog
  libassert::assert
  yuv
  SDL2::SDL2
  PkgConfig::FFmpeg)

# Use env `CMAKE_PRINT_ENV` to print all variables
if(DEFINED ENV{CMAKE_PRINT_ENV})
  message(STATUS "Dump all CMake variables:")
  get_cmake_property(VARIABLE_NAMES VARIABLES)
  foreach (VARIABLE_NAME ${VARIABLE_NAMES})
    message(" - ${VARIABLE_NAME}: ${${VARIABLE_NAME}}")
  endforeach()
endif()
