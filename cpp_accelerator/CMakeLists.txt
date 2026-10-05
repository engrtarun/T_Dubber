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

add_executable(normalizer normalizer.cpp normalizer_kernel.cpp)

# Warnings are the cheapest tests we own.
#
# Scoped to CXX on purpose: this target also contains a NASM source, and
# CMake hands target_compile_options to every language in the target -- where
# nasm would try to parse "/W4" as a filename.
if(MSVC)
    set(NORMALIZER_WARNINGS /W4 /permissive- /Zc:__cplusplus)
else()
    set(NORMALIZER_WARNINGS -Wall -Wextra -Wpedantic)
endif()
foreach(_warn IN LISTS NORMALIZER_WARNINGS)
    target_compile_options(normalizer PRIVATE "$<$<COMPILE_LANGUAGE:CXX>:${_warn}>")
endforeach()

# ---------------------------------------------------------------------------
# Optional AVX2 kernel: kernels/normalizer_gate_gain.asm, assembled by NASM.
#
# One source serves both builds this project ships -- win64 for the local
# MSVC build, elf64 for the static Linux binary the Kaggle pack exports from
# the Dockerfile -- so NASM is the only new requirement and it is a
# BUILD-TIME one: nothing is executed at run time that was not in the object
# file already.
#
# Every part of this degrades to the original behaviour: no NASM, a non-x86
# target, or -DNORMALIZER_ASM=OFF all leave normalizer.cpp on its scalar
# loop, and the runtime CPUID probe in normalizer_kernel.cpp can still say no
# on a machine without AVX2.
# ---------------------------------------------------------------------------
option(NORMALIZER_ASM "Assemble the AVX2 gate/gain kernel with NASM" ON)
option(NORMALIZER_BUILD_TESTS "Build tests/kernel_equivalence_test" ON)

find_program(NASM_EXECUTABLE
             NAMES nasm nasm.exe
             HINTS "${CMAKE_CURRENT_SOURCE_DIR}/../tools/nasm"
                   "${CMAKE_CURRENT_SOURCE_DIR}/tools/nasm")

set(NORMALIZER_ASM_SOURCES "")
set(NORMALIZER_HAVE_ASM OFF)

if(NORMALIZER_ASM AND NASM_EXECUTABLE)
    if(MSVC)
        set(_nasm_format win64)
    elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux")
        set(_nasm_format elf64)
    endif()

    if(_nasm_format)
        # Both must be set before enable_language(), or CMake probes for an
        # assembler of its own choosing and ignores NASM_EXECUTABLE.
        set(CMAKE_ASM_NASM_OBJECT_FORMAT "${_nasm_format}")
        set(CMAKE_ASM_NASM_COMPILER "${NASM_EXECUTABLE}" CACHE FILEPATH
            "NASM assembler" FORCE)
        enable_language(ASM_NASM)

        set(NORMALIZER_ASM_SOURCES kernels/normalizer_gate_gain.asm)
        set(NORMALIZER_HAVE_ASM ON)
        message(STATUS "normalizer: AVX2 kernel ON  (${NASM_EXECUTABLE}, ${_nasm_format})")
    endif()
endif()

if(NOT NORMALIZER_HAVE_ASM)
    message(STATUS "normalizer: AVX2 kernel OFF (nasm missing, unsupported target, "
                   "or NORMALIZER_ASM=OFF) -- scalar path only")
endif()

if(NORMALIZER_HAVE_ASM)
    target_sources(normalizer PRIVATE ${NORMALIZER_ASM_SOURCES})
    target_compile_definitions(normalizer PRIVATE TD_NORMALIZER_HAVE_ASM=1)
endif()

# The equivalence test is meaningful only when there are two paths to
# compare; a scalar-only build skips it instead of pretending to pass.
if(NORMALIZER_HAVE_ASM AND NORMALIZER_BUILD_TESTS)
    add_executable(normalizer_kernel_test
                   tests/kernel_equivalence_test.cpp
                   normalizer_kernel.cpp
                   ${NORMALIZER_ASM_SOURCES})
    target_include_directories(normalizer_kernel_test PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")
    target_compile_definitions(normalizer_kernel_test PRIVATE TD_NORMALIZER_HAVE_ASM=1)
    foreach(_warn IN LISTS NORMALIZER_WARNINGS)
        target_compile_options(normalizer_kernel_test PRIVATE "$<$<COMPILE_LANGUAGE:CXX>:${_warn}>")
    endforeach()
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
