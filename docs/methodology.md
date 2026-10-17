# Methodology

The goal is numbers that would survive someone else re-running them. That means fixing the
machine state, pinning the processes, using a load generator with real latency accounting,
and publishing enough metadata that a disagreement can be traced to a cause.

## Machine state

Benchmarks run on **bare metal Linux**. Virtualised CI numbers are noise and are not
published.

Before every run the harness (`harness/doctor.sh`) records and, where possible, checks:

- CPU model, physical/logical core count, SMT state
- kernel version, distribution
- `scaling_governor` for every CPU — **must be `performance`** on the CPUs under test
- whether the CPUs under test are shared with anything else (honest answer: on a laptop,
  probably)
- NUMA topology, if relevant

`scaling_governor` defaults to `schedutil` on most desktops, which ramps frequency in
response to load. Under a 30-second benchmark that produces a slow ramp at the start and
inflates run-to-run variance. The harness refuses to produce a *publishable* result unless
the governor is `performance`:

```sh
sudo cpupower frequency-set -g performance    # or, per-cpu:
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
```

With `--unsafe` the harness will still run and mark the result `"publishable": false`.

Turbo/boost should be disabled when comparing across machines or over long sessions; when
comparing two implementations on the same machine in the same session, leaving it on is
acceptable as long as both are measured the same way and it is recorded.

## Process pinning

Server and load generator must not share a core — otherwise the generator steals cycles
from the thing being measured, and the ratio of that theft differs per implementation.

Defaults, overridable by environment variable:

| | Variable | Default (8 logical CPUs) |
|---|---|---|
| Server | `SERVER_CPUS` | `0-3` |
| Load generator | `LOAD_CPUS` | `4-7` |

Applied with `taskset -c`. Thread counts are then chosen to fit: `wrk -t4` on `4-7`, and
an implementation should keep its worker count within `SERVER_CPUS` (documented in its
`README.md`).

Loopback only (`127.0.0.1`). Nothing leaves the machine, so NIC and switch behaviour are
removed from the measurement. This also means absolute numbers are not comparable to
network-based benchmarks — only to other runs in this repository.

## Load generation

`wrk` (or `wrk2`) is the harness's generator. `harness/bench.sh` auto-detects, preferring
`wrk`. Neither is installed by default on Arch; see `harness/doctor.sh` output for the
install command.

### Closed-loop: max throughput

Default mode. `wrk -t4 -c64 -d30s --latency`. Connections are held open (this requires
keep-alive, see `SPEC.md` §3.3), each connection sends the next request as soon as the
previous response arrives. This measures **maximum sustainable throughput** and the
latency *at that throughput*.

The number to report is `Requests/sec`. Latency percentiles from a closed-loop run are
published too, but must be labelled: they describe queueing at saturation, not behaviour
at a target rate. A server that is merely good at queueing looks good here.

### Open-loop: fixed rate

`WRK_RATE=<n>` switches to `wrk2 -R <n> -L`, which attempts a fixed request rate and
reports a corrected latency histogram. This is the mode that answers "what latency does
this server give at 50k rps", and it is the only honest way to compare tail latency
between implementations. It is not the default because it needs a rate chosen per
machine, and because a rate above what an implementation can serve produces coordinated
omission, which corrupts the histogram.

Use it deliberately, at rates the slowest implementation can hold (typically 50–70% of the
closed-loop peak), and state the rate in the results.

### Connections sweep

Throughput and tail latency at 64 connections say little about connection-setup cost or
behaviour at 512 connections. `CONNS` can be varied to sweep:

```sh
harness/bench.sh --impl C --conns 8
harness/bench.sh --impl C --conns 64
harness/bench.sh --impl C --conns 256
```

Publish a sweep, not a single point, whenever the ranking between two implementations
depends on it.

## Scenarios

| Name | Request | What it isolates |
|---|---|---|
| `plaintext` | `GET /` | the whole path: accept, parse, route, 14-byte body |
| `json` | `GET /json` | header/content-type handling, 27-byte body |
| `health` | `GET /health` | minimum response size, routing floor |
| `echo` | `POST /echo` with a 1 KiB body | request-body read/write path, buffering |

All four are fixed in `harness/scenarios.tsv`. Bodies are byte-identical across
implementations, so `bytes read` and `bytes/request` are directly comparable.

Warmup is a discarded 10-second run per scenario per implementation, so the first measured
run is not the one paying for lazy allocation, TLB warming, or CPU frequency ramp.

## What is recorded

Per run, in `results/<run-id>/`:

- `meta.json` — machine, toolchain, build flags, commit, pinning, generator version,
  `publishable` flag
- `runs.jsonl` — one line per (impl, scenario, run): wall time, peak RSS (`VmHWM`), CPU
  ticks (`/proc/<pid>/stat` utime+stime), binary size, non-comment line count
- `*.wrk` — raw, unparsed generator output, kept so any number can be re-derived
- `summary.csv` — parsed metrics, one row per (impl, scenario), median across runs
- `RESULTS.md` — the markdown table, for pasting into `README.md`

Derived metrics:

- **Requests/sec** — median of N.
- **Latency p50/p75/p90/p99** — median of N per percentile.
- **Peak RSS** — `VmHWM` after the run. This is the high-water mark of resident memory;
  it includes the page cache touched by *sending* data, so compare like with like.
- **CPU ms/request** — `(utime+stime)/CLK_TCK / requests`. This is the most machine-independent
  efficiency figure in the set: it says how much CPU the server burns per request,
  independent of how many cores it had. Prefer it when comparing across machines.
- **Binary size** and **non-comment LOC** — the cost axis. A 4-core event loop that is
  twice as fast and 40 lines longer is a different trade-off from one 900 lines longer.

## Tiers

Some runtimes ship an HTTP server and some do not. `node:http` is a tuned HTTP/1.1
implementation; Rust's `std` gives you a `TcpListener` and nothing else. Measuring both in
one table would attribute the runtime's HTTP stack to the language.

So every implementation declares `HTTP_LAYER` in `impl.env` (`SPEC.md` 5.1), results are
grouped by it (§7.1), and **the two groups are never ranked against each other**:

- **`hand-rolled`** — the implementation parses HTTP/1.1 itself over raw sockets. This is
  the group where a language-vs-language ranking is meaningful, because every entry wrote
  the same layer by hand.
- **`runtime`** — parsing and framing come from the language distribution. Useful, and
  worth measuring, but the number describes the runtime's HTTP stack.

The gap between the two groups *in the same language* is one of the more interesting results
this repository can produce: put `TS/hand-rolled/` and `TS/runtime/` next to each other and
you have measured what the runtime's built-in server is worth. It just has to be labelled as
that, rather than folded into a ranking.

Two metrics need care once more than one runtime is involved:

- **Peak RSS is not comparable across runtimes.** A JIT runtime's baseline is tens of MiB
  before it serves a request; a C server's high-water mark is a few MiB total. Compare RSS
  within a runtime, and use `CPU ms/req` when comparing across them.
- **`binary` is blank for interpreted implementations** and must not be averaged into a
  compiled column.
- **`LOC` is contributor-declared**, from `SOURCE_EXT` in `impl.env`, and the counter strips
  only `//`, `/* */`, `*`, and `#` comments. Treat it as an order-of-magnitude cost signal,
  not a measurement, and never as a tiebreaker.

## Languages other than the five

Nothing in `harness/` knows any language. A contribution in Go, Zig, Java, Elixir, or
anything else needs a directory with `build.sh`, `run.sh`, `impl.env`, and `README.md`, plus
a row in the `SPEC.md` §2 table. No harness edit, no extension list to update.

The stdlib-only rule does constrain where such a contribution can land (`SPEC.md` §2.2): a
language that ships an HTTP server in its standard library can only enter the `runtime` tier
without its author also writing a parser, and a language with no standard-library HTTP server
has no `runtime` option at all.

## Rules for a fair comparison

1. Conformance must pass. A failed implementation gets no numbers.
2. Same machine, same session, same governor, same pinning, same generator version.
3. Optimised release builds only. Record the flags.
4. Identical response bytes (`SPEC.md` §3.2) — otherwise throughput is comparing payload sizes.
5. Identical payload sizes for `echo`.
6. Do not benchmark while doing anything else. No builds, no browsers, no containers.
7. Report the median of ≥3 runs and the spread. If spread > 5%, the result is unstable —
   say so instead of publishing it.
8. Publish the raw generator output alongside the summary.
9. If an implementation is changed, all of its numbers are invalidated and it is re-run.
10. Never compare numbers from two different machines as if they were one ranking.
11. Never rank across tiers. A `runtime` entry and a `hand-rolled` entry answer different
    questions (see Tiers above).

## Threats to validity

Stated once, here, rather than caveated in every table:

- **Loopback is not a network.** No NIC, no driver, no real RTT. It mostly measures the
  syscall and scheduler path.
- **Thread-per-connection vs event loop is not a language property.** A C implementation
  using `epoll` and a Rust implementation using threads differ for reasons unrelated to C
  and Rust. This is why variants are benchmarked separately and labelled.
- **The 2026 laptop CPU in this repo is a shared, thermally-limited part.** Absolute
  numbers move with ambient temperature. Rankings are more stable than numbers.
- **StdBuf, allocator, and default thread counts differ** across language runtimes, and
  tuning them is part of the implementation.
- **Small-body benchmarks are dominated by syscalls and the accept path**, not by the
  language. That is a finding, not a flaw, but it must not be presented as "language X is
  N times faster".
