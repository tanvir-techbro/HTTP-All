#!/usr/bin/env bash
# bench.sh — build, run, and measure implementations.
#
# Every run gets a freshly started server, pinned away from the load generator,
# which is itself pinned. Raw generator output is kept so any number in the
# summary can be re-derived. See docs/methodology.md for why each choice is made.
#
# Usage:
#   harness/bench.sh                          # every implementation, every scenario
#   harness/bench.sh --impl C                 # one implementation
#   harness/bench.sh --impl Rust --scenario plaintext --conns 256
#
# Environment (see docs/methodology.md):
#   SERVER_CPUS=0-3 LOAD_CPUS=4-7   core pinning
#   WRK_RATE=50000                  switch to open-loop wrk2 at a fixed rate
#   WRK=/path/to/wrk                pick a specific generator binary

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# --- defaults ----------------------------------------------------------------
RUNS=3
DURATION=30s
CONNS=64
THREADS=4
WARMUP=10
UNSAFE=0
KEEP=0
OUT=""
IMPLS=()
WANT_SCENARIOS=()

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --impl DIR         implementation directory (repeatable; default: all found)
  --scenario NAME    scenario name (repeatable; default: all in scenarios.tsv)
  --runs N           measured runs per scenario (default: 3)
  --conns N          concurrent connections (default: 64)
  --threads N        generator threads (default: 4)
  --duration D       each run's duration, e.g. 30s (default: 30s)
  --warmup N         discarded warmup seconds per scenario (default: 10)
  --out DIR          results directory (default: results/<timestamp>-<commit>)
  --unsafe           run even if the environment is not publishable
  --keep             keep temporary files
  -h, --help         this message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --impl)        IMPLS+=("$2"); shift 2 ;;
    --scenario)    WANT_SCENARIOS+=("$2"); shift 2 ;;
    --runs)        RUNS="$2"; shift 2 ;;
    --conns)       CONNS="$2"; shift 2 ;;
    --threads)     THREADS="$2"; shift 2 ;;
    --duration)    DURATION="$2"; shift 2 ;;
    --warmup)      WARMUP="$2"; shift 2 ;;
    --out)         OUT="$2"; shift 2 ;;
    --unsafe)      UNSAFE=1; shift ;;
    --keep)        KEEP=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             fail "unknown option: $1 (try --help)" ;;
  esac
done

# --- preflight ---------------------------------------------------------------
title "Preflight"

detect_generator || { warn "no load generator found"; generator_install_hint; exit 1; }
GEN_VERSION="$("$WRK" --version 2>&1 | head -1 || true)"
printf '  %-20s %s\n' "generator" "$GENERATOR ($WRK)"
printf '  %-20s %s\n' "version" "$GEN_VERSION"

PUBLISHABLE=1
# A comma-terminated, sorted list of the distinct governors across all CPUs:
# "performance," when every CPU is locked, "ondemand,performance," when mixed.
GOV_UNIQ="$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null \
  | sort -u | tr '\n' ',')"
if [[ -n "$GOV_UNIQ" && "$GOV_UNIQ" != "performance," ]]; then
  PUBLISHABLE=0
  warn "CPU governor is '${GOV_UNIQ%,}', not 'performance' — results will be marked publishable: false"
  warn "fix with: sudo cpupower frequency-set -g performance"
fi
for pair in "server:$SERVER_CPUS" "load:$LOAD_CPUS"; do
  missing="$(cpus_available "${pair#*:}")"
  [[ -z "$missing" ]] || { PUBLISHABLE=0; warn "${pair%%:*} CPUs missing here: $missing"; }
done
if [[ "$PUBLISHABLE" -eq 0 && "$UNSAFE" -eq 0 ]]; then
  fail "environment is not publishable; re-run with --unsafe to benchmark anyway"
fi

# --- resolve what to run -----------------------------------------------------
if [[ ${#IMPLS[@]} -eq 0 ]]; then
  mapfile -t IMPLS < <(discover_impls)
  [[ ${#IMPLS[@]} -gt 0 ]] || fail "no implementations found (need <dir>/build.sh)"
fi
for impl in "${IMPLS[@]}"; do
  [[ -d "$ROOT_DIR/$impl" ]] || fail "no such implementation directory: $impl"
  [[ -f "$ROOT_DIR/$impl/build.sh" ]] || fail "$impl has no build.sh"
  [[ -f "$ROOT_DIR/$impl/run.sh" ]] || fail "$impl has no run.sh"
  # Results are grouped by tier, so an undeclared tier cannot be published at all
  # (SPEC.md 1.1, 5.1, 7.1).
  problem="$(impl_env_problem "$ROOT_DIR/$impl")"
  [[ -z "$problem" ]] || fail "$impl: $problem"
done

SCEN_FILE="$HARNESS_DIR/scenarios.tsv"
declare -a SCEN_NAMES=() SCEN_METHODS=() SCEN_PATHS=() SCEN_SCRIPTS=()
while IFS=$'\t' read -r name method path script; do
  [[ -z "${name// /}" || "$name" == \#* ]] && continue
  if [[ ${#WANT_SCENARIOS[@]} -gt 0 ]]; then
    keep=0
    for want in "${WANT_SCENARIOS[@]}"; do [[ "$want" == "$name" ]] && keep=1; done
    [[ $keep -eq 1 ]] || continue
  fi
  SCEN_NAMES+=("$name"); SCEN_METHODS+=("$method")
  SCEN_PATHS+=("$path"); SCEN_SCRIPTS+=("$script")
done < "$SCEN_FILE"
[[ ${#SCEN_NAMES[@]} -gt 0 ]] || fail "no scenarios selected"

# --- output directory --------------------------------------------------------
COMMIT="$(git_commit)"
if [[ -z "$OUT" ]]; then
  OUT="$ROOT_DIR/results/$(date +%Y%m%dT%H%M%S)-$COMMIT"
fi
mkdir -p "$OUT/tmp"
OUT="${OUT%/}"

title "Plan"
printf '  %-20s %s\n' "results" "${OUT#$ROOT_DIR/}"
printf '  %-20s %s\n' "implementations" "${IMPLS[*]}"
for impl in "${IMPLS[@]}"; do
  read_impl_env "$ROOT_DIR/$impl"
  printf '    %-18s %s (%s)\n' "$(impl_name "$impl")" "$HTTP_LAYER" "$RUNTIME"
done
printf '  %-20s %s\n' "scenarios" "${SCEN_NAMES[*]}"
printf '  %-20s %sx%s for %s (%ss warmup)\n' "load" "$THREADS" "$CONNS" "$DURATION" "$WARMUP"
printf '  %-20s %s\n' "runs" "$RUNS (+1 warmup)"
printf '  %-20s %s\n' "pinning" "server=$SERVER_CPUS load=$LOAD_CPUS"
printf '  %-20s %s\n' "publishable" "$PUBLISHABLE"

# --- build -------------------------------------------------------------------
title "Build"
for impl in "${IMPLS[@]}"; do
  log "building $impl"
  if ! ( cd "$ROOT_DIR/$impl" && ./build.sh ) >"$OUT/tmp/build-$(impl_name "$impl").log" 2>&1; then
    cat "$OUT/tmp/build-$(impl_name "$impl").log" >&2
    fail "build failed for $impl (full log: $OUT/tmp/build-$(impl_name "$impl").log)"
  fi
  ok "$impl built"
done

# --- helpers -----------------------------------------------------------------
S_NAME=""; S_METHOD=""; S_PATH=""; S_SCRIPT=""
RUN_RSS_KB=0; RUN_TICKS=0; RUN_ERROR=""; RUN_SOCK_ERR=0

log_run() {
  python3 - "$OUT/runs.jsonl" "$@" <<'PY'
import json, sys
path, *pairs = sys.argv[1:]
obj = {}
for pair in pairs:
    key, _, value = pair.partition("=")
    try:
        obj[key] = int(value)
    except ValueError:
        try:
            obj[key] = float(value)
        except ValueError:
            obj[key] = value
with open(path, "a") as fh:
    fh.write(json.dumps(obj) + "\n")
PY
}

# One complete measurement: fresh server, load, sample, stop.
measure() {
  local impl_name=$1 impl_dir=$2 port=$3 outfile=$4 duration=$5
  local logf="$OUT/tmp/server-$impl_name-$S_NAME.log"

  start_server "$ROOT_DIR/$impl_dir" "$port" "$logf"
  local rc=0
  wait_for_listening || rc=$?
  if [[ $rc -ne 0 ]]; then
    stop_server
    if [[ $rc -eq 2 ]]; then
      fail "server for $impl_dir exited before LISTENING; see $logf"
    fi
    fail "server for $impl_dir never printed LISTENING; see $logf"
  fi
  sleep 0.2   # let the listen backlog drain before the first sample

  local url="http://$HOST:$port$S_PATH"
  local -a args=(-t"$THREADS" -c"$CONNS" -d"$duration")
  local -a sargs=()
  [[ "$S_SCRIPT" != "-" ]] && sargs=(-s "$ROOT_DIR/$S_SCRIPT")
  if [[ "$GENERATOR" == "wrk2" ]]; then
    args+=(-R"${WRK_RATE:-1000000}" -L)
  else
    args+=(--latency)
  fi

  taskset -c "$LOAD_CPUS" "$WRK" "${args[@]}" "${sargs[@]}" "$url" >"$outfile" 2>&1 || true

  RUN_RSS_KB="$(peak_rss_kb "$SERVER_PID")"
  RUN_TICKS="$(cpu_ticks "$SERVER_PID")"
  stop_server

  # A run the generator could not complete is not a slow result, it is no result.
  # Refusing to pass it through as a silent zero is the whole point of recording it.
  RUN_ERROR=""
  RUN_SOCK_ERR="$(awk '/Socket errors:/{for(i=1;i<=NF;i++) if($i ~ /^[0-9]+$/) s+=$i} END{print s+0}' "$outfile")"
  local bad
  bad="$(awk -F': *' '/Non-2xx or 3xx responses:/{print $2+0}' "$outfile")"
  if ! grep -q '^Requests/sec:' "$outfile"; then
    RUN_ERROR="load generator produced no result (server unreachable?)"
  elif ! grep -qE '[1-9][0-9]* requests in' "$outfile"; then
    RUN_ERROR="load generator completed 0 requests"
  elif [[ "${RUN_SOCK_ERR:-0}" -gt 0 ]]; then
    RUN_ERROR="load generator reported $RUN_SOCK_ERR socket error(s); throughput is understated"
  elif [[ "${bad:-0}" -gt 0 ]]; then
    RUN_ERROR="$bad non-2xx/3xx response(s) — the load did not hit the contract response"
  fi
}

# --- measure -----------------------------------------------------------------
title "Measure"
BIN_BYTES=0
CLK="$(clk_tck)"
START_ALL=$(date +%s)

for impl in "${IMPLS[@]}"; do
  name="$(impl_name "$impl")"
  bin="$ROOT_DIR/$impl/bin/server"
  if [[ -f "$bin" ]]; then BIN_BYTES=$(stat -c %s "$bin"); else BIN_BYTES=0; fi

  # Declared by the implementation, so a new language needs no harness edit
  # (SPEC.md 5.1). LOC is meaningless without the declared extensions.
  read_impl_env "$ROOT_DIR/$impl"
  LOC="$(count_loc "$ROOT_DIR/$impl" "$SOURCE_EXT")"
  TOOLCHAIN_VERSION="unknown"
  if [[ -n "$TOOLCHAIN_CMD" ]]; then
    TOOLCHAIN_VERSION="$($TOOLCHAIN_CMD 2>&1 | head -1)" || true
  fi

  for idx in "${!SCEN_NAMES[@]}"; do
    S_NAME="${SCEN_NAMES[$idx]}"
    S_METHOD="${SCEN_METHODS[$idx]}"
    S_PATH="${SCEN_PATHS[$idx]}"
    S_SCRIPT="${SCEN_SCRIPTS[$idx]}"

    if [[ "$WARMUP" -gt 0 ]]; then
      log "$name / $S_NAME — warmup ${WARMUP}s"
      measure "$name" "$impl" "$(free_port)" "$OUT/tmp/warmup.wrk" "${WARMUP}s"
    fi

    for ((run = 1; run <= RUNS; run++)); do
      port="$(free_port)"
      outfile="$OUT/${name}__${S_NAME}__r${run}.wrk"
      log "$name / $S_NAME — run $run/$RUNS"
      measure "$name" "$impl" "$port" "$outfile" "$DURATION"
      if [[ -n "$RUN_ERROR" ]]; then
        PUBLISHABLE=0
        warn "$name / $S_NAME run $run rejected: $RUN_ERROR"
      fi
      log_run "impl=$name" "impl_dir=$impl" "scenario=$S_NAME" "run=$run" \
        "http_layer=$HTTP_LAYER" "runtime=$RUNTIME" \
        "duration_s=${DURATION%s}" "connections=$CONNS" "threads=$THREADS" \
        "peak_rss_kb=$RUN_RSS_KB" "cpu_ticks=$RUN_TICKS" "clk_tck=$CLK" \
        "binary_bytes=$BIN_BYTES" "loc=$LOC" "generator=$GENERATOR" \
        "toolchain=$TOOLCHAIN_VERSION" "socket_errors=$RUN_SOCK_ERR" "error=$RUN_ERROR" \
        "server_cpus=$SERVER_CPUS" "load_cpus=$LOAD_CPUS" "output=$(basename "$outfile")"
    done
  done
done

ELAPSED=$(( $(date +%s) - START_ALL ))

# --- metadata ----------------------------------------------------------------
export PUBLISHABLE GEN_VERSION RUNS DURATION CONNS THREADS WARMUP ELAPSED
export SERVER_CPUS LOAD_CPUS
export GOV_UNIQ
export GENERATOR WRK
python3 - "$OUT/meta.json" <<'PY'
import json, os, platform, socket, subprocess, sys, time

out = sys.argv[1]

def gov():
    try:
        names = os.listdir("/sys/devices/system/cpu")
    except OSError:
        return []
    seen = []
    for n in sorted(names):
        if not n.startswith("cpu") or not n[3:].isdigit():
            continue
        try:
            with open(f"/sys/devices/system/cpu/{n}/cpufreq/scaling_governor") as fh:
                g = fh.read().strip()
        except OSError:
            continue
        if g not in seen:
            seen.append(g)
    return seen

def cpu_model():
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"

def commit():
    try:
        return subprocess.run(["git", "-C", os.path.dirname(os.path.dirname(out)),
                               "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None

meta = {
    "run_id": os.path.basename(os.path.dirname(out)),
    "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "publishable": bool(int(os.environ.get("PUBLISHABLE", "0"))),
    "host": socket.gethostname(),
    "platform": platform.platform(),
    "kernel": platform.release(),
    "cpu_model": cpu_model(),
    "logical_cpus": os.cpu_count(),
    "governors": gov(),
    "clk_tck": os.sysconf("SC_CLK_TCK"),
    "commit": commit(),
    "pinning": {"server": os.environ.get("SERVER_CPUS"), "load": os.environ.get("LOAD_CPUS")},
    "generator": {"name": os.environ.get("GENERATOR"),
                  "path": os.environ.get("WRK"),
                  "version": os.environ.get("GEN_VERSION"),
                  "rate": os.environ.get("WRK_RATE") or None},
    "params": {"runs": int(os.environ.get("RUNS", "0")),
               "duration": os.environ.get("DURATION"),
               "connections": int(os.environ.get("CONNS", "0")),
               "threads": int(os.environ.get("THREADS", "0")),
               "warmup_s": int(os.environ.get("WARMUP", "0"))},
    "toolchains": {},   # filled from impl.env declarations below; no language table here

    "elapsed_s": int(os.environ.get("ELAPSED", "0")),
}

# Per-implementation facts, taken from the run log so the tier that each number
# belongs to travels with the results (SPEC.md 1.1, 7.1).
impls = {}
runs_path = os.path.join(os.path.dirname(out), "runs.jsonl")
if os.path.exists(runs_path):
    with open(runs_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            impls.setdefault(row["impl"], {
                "http_layer": row.get("http_layer"),
                "runtime": row.get("runtime"),
                "toolchain": row.get("toolchain"),
                "binary_bytes": row.get("binary_bytes"),
                "loc": row.get("loc"),
            })
            if row.get("toolchain") and row["toolchain"] != "unknown":
                meta["toolchains"][row["impl"]] = row["toolchain"]
meta["implementations"] = impls

with open(out, "w") as fh:
    json.dump(meta, fh, indent=2)
    fh.write("\n")
print(f"  metadata -> {out}")
PY

# --- report ------------------------------------------------------------------
title "Report"
# Deliberately no --update-readme: README is the published table, and `make results`
# is the explicit step that rewrites it. A throwaway or unpublishable run must not
# overwrite it. The table for this run is in this directory as RESULTS.md.
python3 "$HARNESS_DIR/report.py" --dir "$OUT" || \
  warn "report generation failed; raw output is kept in ${OUT#$ROOT_DIR/}"

[[ "$KEEP" -eq 1 ]] || rm -rf "$OUT/tmp"

ln -sfn "$(basename "$OUT")" "$ROOT_DIR/results/latest"

title "Done"
ok "results in ${OUT#$ROOT_DIR/} (elapsed ${ELAPSED}s)"
[[ "$PUBLISHABLE" -eq 1 ]] || warn "marked publishable: false — do not use these numbers in a comparison"
