# ---------------------------------------------------------------------------
# T_Dubber :: cpp_accelerator
#
# Audio Normalizer & Noise Gate -- a single, dependency-free C++20 CLI tool.
#
#   cmake -S . -B build
#   cmake --build build --config Release
#   ./build/normalizer <input.wav> <output.wav> <noise_threshold> <target_gain>
#
# Design intent: no external libraries (no libsndfile, no FFTW). The tool only
# needs to parse a RIFF/WAVE header, stream 16-bit PCM through a gate+gain
# kernel, and write the header back out -- so the standard library is enough.
# ---------------------------------------------------------------------------

cmake_minimum_required(VERSION 3.16)

project(t_dubber_normalizer
        VERSION 1.0.0
        DESCRIPTION "Audio normalizer and noise gate for the T_Dubber pipeline"
        LANGUAGES CXX)

# This tool is measured in MB/s, so an optimised build is the default.
# A caller can still override it with -DCMAKE_BUILD_TYPE=Debug.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
    set_property(CACHE CMAKE_BUILD_TYPE PROPERTY STRINGS
                 Debug Release RelWithDebInfo MinSizeRel)
endif()

# Strict ISO C++20: no compiler dialect extensions, so the same source builds
# with MSVC, GCC and Clang (the pipeline runs on Windows and on the render
# farm's Linux boxes).
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(normalizer normalizer.cpp)

# Warnings are the cheapest tests we own.
if(MSVC)
    target_compile_options(normalizer PRIVATE /W4 /permissive- /Zc:__cplusplus)
else()
    target_compile_options(normalizer PRIVATE -Wall -Wextra -Wpedantic)
endif()

# Optional: squeeze out the last few percent on a machine that will never run
# a different CPU. Off by default because the farm boxes are heterogeneous.
option(NORMALIZER_NATIVE "Optimise for the build machine (-march=native)" OFF)
if(NORMALIZER_NATIVE)
    if(MSVC)
        target_compile_options(normalizer PRIVATE /arch:AVX2)
    else()
        target_compile_options(normalizer PRIVATE -march=native)
    endif()
endif()

include(GNUInstallDirs)
install(TARGETS normalizer RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
