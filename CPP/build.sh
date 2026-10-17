#!/usr/bin/env bash
# Build the C++ server. Idempotent, offline, optimised release (SPEC.md 5.2).
set -euo pipefail
cd "$(dirname "$0")"

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel

# SPEC.md 5.2: the harness stats <impl-dir>/bin/server, and run.sh execs it.
# CMake put the binary at build/server; install it where the contract expects.
mkdir -p bin
cp -f build/server bin/server

# clangd looks for compile_commands.json beside the sources, not inside build/.
cp -f build/compile_commands.json compile_commands.json
