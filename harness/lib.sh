# Shared shell helpers for the HTTP-All harness.
# Sourced by bench.sh and doctor.sh. No side effects on source.

set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$HARNESS_DIR/.." && pwd)"

# --- logging -----------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""
fi

log()   { printf '%s[%s]%s %s\n' "$C_DIM" "$(date +%H:%M:%S)" "$C_RESET" "$*"; }
ok()    { printf '%s  ok  %s%s\n' "$C_GREEN" "$*" "$C_RESET"; }
warn()  { printf '%swarn  %s%s\n' "$C_YELLOW" "$*" "$C_RESET" >&2; }
fail()  { printf '%sfail  %s%s\n' "$C_RED" "$*" "$C_RESET" >&2; exit 1; }
title() { printf '\n%s==> %s%s\n' "$C_BOLD" "$*" "$C_RESET"; }

# --- environment -------------------------------------------------------------
# Server and load generator must not share cores; see docs/methodology.md.
SERVER_CPUS="${SERVER_CPUS:-0-3}"
LOAD_CPUS="${LOAD_CPUS:-4-7}"
HOST="${BENCH_HOST:-127.0.0.1}"

# Weakest of the available timeouts, so a hung server cannot wedge a run.
WAIT_ITERS=400      # 400 * 50ms = 20s to reach the LISTENING handshake
WAIT_STEP=0.05

# --- load generator ----------------------------------------------------------
# Prefer wrk; fall back to wrk2 (which needs a rate, see docs/methodology.md).
detect_generator() {
  if [[ -n "${WRK:-}" ]]; then
    GENERATOR="$(basename "$WRK")"
  elif command -v wrk >/dev/null 2>&1; then
    WRK="$(command -v wrk)"; GENERATOR="wrk"
  elif command -v wrk2 >/dev/null 2>&1; then
    WRK="$(command -v wrk2)"; GENERATOR="wrk2"
  else
    return 1
  fi
  export WRK GENERATOR
  return 0
}

generator_install_hint() {
  cat <<'EOF'
  wrk is required to run benchmarks. Neither wrk nor wrk2 was found.

    Arch:    paru -S wrk          (or: sudo pacman -S wrk)
    Debian:  sudo apt install wrk
    macOS:   brew install wrk
    source:  https://github.com/wg/wrk

  wrk2 (with -R rate support) if you want open-loop latency runs:
    Arch:    paru -S wrk2
    source:  https://github.com/giltene/wrk2
EOF
}

# --- process helpers ---------------------------------------------------------
# Peak RSS in kB, read from the kernel's high-water mark. Must be called while
# the process is still alive (see SPEC-adjacent notes in docs/methodology.md).
peak_rss_kb() {
  awk '/^VmHWM:/ { print $2; exit }' "/proc/$1/status" 2>/dev/null || echo 0
}

# utime+stime in clock ticks, summed over all threads of the process.
# Field offsets differ when comm contains spaces, so locate the comm token.
cpu_ticks() {
  awk '{ for (i = 1; i <= NF; i++) if ($i ~ /\)$/) { print $(i+12) + $(i+13); exit } }' \
    "/proc/$1/stat" 2>/dev/null || echo 0
}

clk_tck() { getconf CLK_TCK 2>/dev/null || echo 100; }

pid_alive() { kill -0 "$1" 2>/dev/null; }

# --- CPU set sanity ----------------------------------------------------------
cpus_available() {
  local want=$1
  python3 - "$want" <<'PY'
import os, sys
want = sys.argv[1]
allowed = set()
try:
    with open("/sys/fs/cgroup/cpuset.cpus.effective") as f:
        spec = f.read().strip()
except OSError:
    spec = ""
if not spec:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("Cpus_allowed_list:"):
                    spec = line.split(":", 1)[1].strip()
    except OSError:
        spec = ""
def expand(s):
    out = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out
avail = expand(spec) if spec else set(range(os.cpu_count() or 0))
need = expand(want)
missing = sorted(need - avail)
print(",".join(str(m) for m in missing))
PY
}

# --- server lifecycle --------------------------------------------------------
# Globals: SERVER_PID, SERVER_LOG
start_server() {
  local impl_dir=$1 port=$2
  SERVER_LOG="$3"
  : > "$SERVER_LOG"

  # Run from the impl dir, no subshell, so $! is the server process itself:
  # run.sh is required to `exec` (SPEC.md 5).
  pushd "$impl_dir" >/dev/null
  PORT="$port" HOST="$HOST" taskset -c "$SERVER_CPUS" ./run.sh >"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  popd >/dev/null
  export SERVER_PID
}

wait_for_listening() {
  local i
  for ((i = 0; i < WAIT_ITERS; i++)); do
    if grep -q '^LISTENING ' "$SERVER_LOG" 2>/dev/null; then
      return 0
    fi
    if ! pid_alive "$SERVER_PID"; then
      return 2
    fi
    sleep "$WAIT_STEP"
  done
  return 1
}

stop_server() {
  [[ -n "${SERVER_PID:-}" ]] || return 0
  if pid_alive "$SERVER_PID"; then
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    local i
    for ((i = 0; i < 100; i++)); do
      pid_alive "$SERVER_PID" || break
      sleep 0.05
    done
    pid_alive "$SERVER_PID" && kill -KILL "$SERVER_PID" 2>/dev/null || true
  fi
  wait "$SERVER_PID" 2>/dev/null || true
  SERVER_PID=""
}

free_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

# --- implementation discovery ------------------------------------------------
# An implementation is any directory containing build.sh. Depth is capped so
# nested variants (Rust/epoll) are found but source subdirectories are not.
discover_impls() {
  find "$ROOT_DIR" -mindepth 2 -maxdepth 3 -name build.sh -type f \
    -not -path "$ROOT_DIR/.git/*" \
    -not -path "$ROOT_DIR/harness/*" \
    -not -path "$ROOT_DIR/results/*" \
    -not -path "$ROOT_DIR/docs/*" \
    -printf '%h\n' 2>/dev/null | sed "s|^$ROOT_DIR/||" | sort
}

# The name a directory appears under in result tables: path separators become
# dashes, so Rust/epoll is Rust-epoll.
impl_name() { printf '%s' "${1//\//-}"; }

# --- misc --------------------------------------------------------------------
# Non-comment, non-blank line count. The "cost" axis in results.
count_loc() {
  local dir=$1
  find "$dir" -type f \
    \( -name '*.c' -o -name '*.h' -o -name '*.cc' -o -name '*.cpp' \
    -o -name '*.hpp' -o -name '*.d' -o -name '*.rs' \) \
    -not -path '*/bin/*' -not -path '*/target/*' -not -path '*/.dub/*' \
    -print0 2>/dev/null |
    xargs -0 -r awk '
      { sub(/^[ \t]+/, "") }
      /^$/ { next }
      /^\/\// { next }
      /^\/\*/ { next }
      /^\*/ { next }
      /^#/ { next }
      { n++ }
      END { print n + 0 }' |
    awk '{ t += $1 } END { print t + 0 }'
}

git_commit() { git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown"; }
