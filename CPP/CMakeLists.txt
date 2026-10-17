# CPP/CMakeLists.txt — the C++ hand-rolled HTTP server.
#
# CMake builds the server into build/ (build/server). build.sh then installs it
# to bin/server, which is where run.sh execs from and where the harness stats
# the binary size (SPEC.md 5.2). Run it with:
#
#     cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
#     cmake --build build --parallel
#
# C++23 over POSIX sockets and pthreads only. No third-party HTTP, framework or
# event-loop library (SPEC.md 2).

cmake_minimum_required(VERSION 3.16)

project(http_all_cpp LANGUAGES CXX)

# Release by default: only an optimised build is comparable (SPEC.md 5.2).
# The guard keeps multi-config generators (Ninja Multi-Config, VS) untouched.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
      set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF) # -std=c++23, not -std=gnu++23

# Emit compile_commands.json into the build tree so clangd/editors can resolve
# includes. build.sh copies it to CPP/ (clangd looks beside the sources, not in
# build/).
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# pthreads: allowed as standard library (SPEC.md 2), and needed by std::thread.
# Often expands to nothing here — on glibc >= 2.34 pthreads live in libc, so
# FindThreads legitimately finds no extra flags (verified: std::thread links
# without -pthread on glibc 2.44). Kept because it is what makes this work on
# musl and older glibc.
find_package(Threads REQUIRED)

# Add new translation units here as you write them.
add_executable(server
    Source/Server.cpp
    Source/Http.cpp
)

target_include_directories(server PRIVATE Source)
target_link_libraries(server PRIVATE Threads::Threads)
target_compile_options(server PRIVATE -Wall -Wextra)

# No RUNTIME_OUTPUT_DIRECTORY override on purpose: the binary lands at build/server,
# CMake's normal location, and build.sh copies it to bin/server (SPEC.md 5.2).
