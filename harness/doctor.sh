#!/usr/bin/env bash
# doctor.sh — environment preflight.
#
# Checks the machine state that makes benchmark numbers meaningful, and reports
# the toolchains each implementation will build with. Read-only: it changes
# nothing and exits non-zero only if benchmarks cannot run at all.
#
# Usage: harness/doctor.sh [--strict]

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

STRICT=0
[[ "${1:-}" == "--strict" ]] && STRICT=1

PROBLEMS=0

title "Machine"
printf '  %-22s %s\n' "kernel" "$(uname -srm)"
printf '  %-22s %s\n' "distribution" "$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || echo unknown)"
printf '  %-22s %s\n' "logical CPUs" "$(nproc)"
printf '  %-22s %s\n' "CPU" "$(awk -F: '/^model name/ { gsub(/^ +/, "", $2); print $2; exit }' /proc/cpuinfo)"

# --- governor ---------------------------------------------------------------
# schedutil/ondemand ramp frequency with load, which inflates variance under a
# 30s benchmark. See docs/methodology.md.
GOV_UNIQ="$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null \
  | sort -u | tr '\n' ',')"
if [[ -z "$GOV_UNIQ" ]]; then
  warn "no cpufreq governor exposed (VM?) — frequency behaviour is unknown"
else
  printf '  %-22s %s\n' "governor" "${GOV_UNIQ%,}"
  if [[ "$GOV_UNIQ" != "performance," ]]; then
    warn "governor is '${GOV_UNIQ%,}', not 'performance' — results will not be publishable"
    echo "           fix: sudo cpupower frequency-set -g performance"
    echo "           or:  echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
    PROBLEMS=$((PROBLEMS + 1))
  else
    ok "governor is 'performance'"
  fi
fi

# --- CPU set / pinning ------------------------------------------------------
title "Pinning"
printf '  %-22s %s\n' "server CPUs" "$SERVER_CPUS"
printf '  %-22s %s\n' "load CPUs" "$LOAD_CPUS"
if ! command -v taskset >/dev/null 2>&1; then
  fail "taskset is required for core pinning (util-linux)"
fi
for pair in "server:$SERVER_CPUS" "load:$LOAD_CPUS"; do
  label=${pair%%:*}; want=${pair#*:}
  missing="$(cpus_available "$want")"
  if [[ -n "$missing" ]]; then
    warn "$label CPUs $want are not available here (missing: $missing)"
    PROBLEMS=$((PROBLEMS + 1))
  else
    ok "$label CPUs $want available"
  fi
done

# --- load generator ---------------------------------------------------------
title "Load generator"
if detect_generator; then
  printf '  %-22s %s\n' "generator" "$GENERATOR"
  printf '  %-22s %s\n' "path" "$WRK"
  if [[ "$GENERATOR" == "wrk2" ]]; then
    warn "only wrk2 found; closed-loop runs will use a high fixed rate (-R 1000000)"
  fi
  ok "$GENERATOR is available"
else
  warn "neither wrk nor wrk2 found — benchmarks cannot run"
  generator_install_hint
  PROBLEMS=$((PROBLEMS + 1))
fi

# --- harness runtime --------------------------------------------------------
title "Harness requirements"
for t in make python3; do
  if command -v "$t" >/dev/null 2>&1; then
    printf '  %-22s %s\n' "$t" "$("$t" --version 2>&1 | head -1)"
  else
    warn "$t is required"
    PROBLEMS=$((PROBLEMS + 1))
  fi
done

# --- toolchains and implementations -----------------------------------------
title "Implementations"
mapfile -t IMPLS < <(discover_impls)
if [[ ${#IMPLS[@]} -eq 0 ]]; then
  warn "no implementation directories found (need <dir>/build.sh)"
  PROBLEMS=$((PROBLEMS + 1))
fi

declare -A SEEN_TOOL
for impl in "${IMPLS[@]:-}"; do
  [[ -n "$impl" ]] || continue
  # The toolchain each implementation will use, best-effort by file extension.
  tool="?"
  if find "$ROOT_DIR/$impl" -name '*.rs' -print -quit 2>/dev/null | grep -q .; then
    tool="rustc"; [[ -f "$ROOT_DIR/$impl/Cargo.toml" ]] && tool="cargo"
  elif find "$ROOT_DIR/$impl" -name '*.d' -print -quit 2>/dev/null | grep -q .; then
    for c in dub ldc2 dmd; do command -v "$c" >/dev/null 2>&1 && { tool="$c"; break; }; done
  elif find "$ROOT_DIR/$impl" \( -name '*.c' -o -name '*.cc' -o -name '*.cpp' \) -print -quit 2>/dev/null | grep -q .; then
    tool="${CC:-cc}"
  fi

  if [[ -f "$ROOT_DIR/$impl/build.sh" && ! -x "$ROOT_DIR/$impl/build.sh" ]]; then
    warn "$impl/build.sh is not executable"
    PROBLEMS=$((PROBLEMS + 1))
  fi
  if [[ -f "$ROOT_DIR/$impl/run.sh" && ! -x "$ROOT_DIR/$impl/run.sh" ]]; then
    warn "$impl/run.sh is not executable"
    PROBLEMS=$((PROBLEMS + 1))
  fi
  [[ -f "$ROOT_DIR/$impl/run.sh" ]] || { warn "$impl has no run.sh"; PROBLEMS=$((PROBLEMS + 1)); }

  printf '  %-22s %s\n' "$impl" "$tool"
  if [[ "$tool" != "?" && -z "${SEEN_TOOL[$tool]:-}" ]]; then
    SEEN_TOOL[$tool]=1
    if command -v "$tool" >/dev/null 2>&1; then
      printf '    %-20s %s\n' "" "$("$tool" --version 2>&1 | head -1)"
    else
      warn "$tool not found (needed by $impl)"
      PROBLEMS=$((PROBLEMS + 1))
    fi
  fi
done

# --- summary ----------------------------------------------------------------
title "Summary"
if [[ $PROBLEMS -eq 0 ]]; then
  ok "environment is ready; benchmarks will be marked publishable"
  exit 0
fi

warn "$PROBLEMS issue(s) found"
if [[ $STRICT -eq 1 ]]; then
  exit 1
fi
echo
echo "Benchmarks can still run, but results will be marked \"publishable\": false."
exit 0
