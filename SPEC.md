# HTTP-All — Specification

**Status: DRAFT v0.1.0** — this is the normative contract. Every implementation in this
repository MUST satisfy it. Anything not stated here is an implementation choice, and the
choices are exactly what this project compares.

- Platform: **Unix / Linux, x86-64** (only target for now).
- Protocol: **HTTP/1.1**.
- Dependencies: **standard library only**. No third-party HTTP server, client, or
  framework. See §2.
- Benchmark harness: `wrk` (or `wrk2`). See `docs/methodology.md`.

---

## 1. What is being compared

Each implementation must serve the same four endpoints, emit **byte-identical** responses,
and expose the same build/run interface. Given that, the differences that remain are the
interesting ones: throughput, latency distribution, memory, CPU cost per request, and
source-level complexity.

The comparison unit is an **implementation directory**: any directory containing
`build.sh`, `run.sh`, and `impl.env` (see §5). By convention the top level holds one per
language (`C/`, `CPP/`, `D/`, `Rust/`, `TS/`). Extra variants may be nested (`Rust/epoll/`)
or siblings (`Rust-epoll/`). All are benchmarked independently and side by side.

### 1.1 Tiers, and why results are never ranked across them

The four compiled languages have no HTTP server in their standard library, so they must
hand-roll one. Some runtimes do ship one — `node:http` is a complete, tuned HTTP/1.1
implementation in C++. Letting one implementation use it while another hand-rolls is not a
comparison between languages, it is a comparison between a runtime's HTTP stack and
somebody's hand-written parser.

So the rule is structural rather than a judgement call: **every implementation declares its
HTTP layer in `impl.env`** (see §5), and results are grouped and reported by that
declaration.

| `HTTP_LAYER` | Meaning |
|---|---|
| `hand-rolled` | the implementation parses HTTP/1.1 itself, over the runtime's raw sockets (`node:net`, `TcpListener`, `std.socket`, …) |
| `runtime` | the implementation delegates HTTP parsing and framing to a facility in its own language distribution (`node:http`) |

Within a tier, like is compared with like, and the ranking is meaningful. Across tiers it is
not, and the results tables MUST NOT mix them (§7). Both tiers are interesting: the gap
between a runtime's built-in server and a hand-rolled one, in the same language, is one of
the more useful numbers this repository can produce — it just has to be labelled.

---

## 2. Dependency rule (stdlib only)

An implementation MUST NOT link or vendor a third-party HTTP library, web framework, or
event-loop library.

| Language | Allowed | Not allowed |
|---|---|---|
| C | libc, POSIX/pthreads, Linux syscalls | libmicrohttpd, libevent, libuv, Mongoose, civetweb |
| C++ | C++ standard library, POSIX/pthreads | Boost.Asio/Beast, cpp-httplib, Drogon, Crow, Pistache |
| D | `core.*`, `std.*` (incl. `std.socket`, `std.concurrency`) | `vibe.d`, `serve-d` deps, `arsd` |
| Rust | `std` | `tokio`, `hyper`, `axum`, `actix`, `tiny_http`, `mio` |
| TypeScript (Node, Bun) | the runtime's built-ins: `node:net`, `node:buffer`, and `node:http` only in the `runtime` tier (§1.1) | `express`, `fastify`, `koa`, `hono`, `undici`, `bun:serve` outside the `runtime` tier |

`std.parallelism`, `std.concurrency`, and `std::thread` are standard library and therefore
allowed. The line is: **the language's own distribution, plus the OS.** Anything that
requires a package manager to fetch is out.

### 2.1 Toolchain versus runtime dependency

A compiler, a type checker, and type declarations — `gcc`, `tsc`, `@types/node` — are
**toolchain**, exactly as `gcc` is for C. They are installed as a documented prerequisite
and their versions are recorded in `meta.json`, but `build.sh` MUST NOT install them and
MUST NOT need the network.

A **runtime dependency** is code the server loads while serving a request. That MUST come
from the language distribution or the OS. Nothing fetched by a package manager may be
loaded at runtime.

For TypeScript the clean path therefore has no `package.json` at all: Node 23.6+ strips type
annotations natively, so `node src/server.ts` runs with zero dependencies. Type stripping is
erasable-syntax only — no `enum`, no `namespace`, no decorators. `@types/node` remains
useful for editor support and is still just toolchain.

Every implementation MUST record its exact toolchain version and build flags in its
`README.md`, and `harness/doctor.sh` records them again at benchmark time.

### 2.2 Adding a language

The repository is not limited to the languages in the table above. A different language is
welcome, and adding one requires **no changes to `harness/`**:

1. Add a row to the table in §2 — the point of the row is to state, on the record, which of
   the language's own modules count as standard library. That is a judgement the reviewer
   needs to be able to see, so it is prose in the spec rather than a table in the code.
2. Create `<Language>/` with `build.sh`, `run.sh`, `impl.env`, and `README.md` (§5).
3. Declare `SOURCE_EXT` and `TOOLCHAIN_CMD` in `impl.env`, which is how the harness learns
   the language's file extensions and compiler without being told about the language.

Two consequences of the stdlib-only rule are worth stating, because they shape which
languages can appear where:

- A language whose standard library already contains an HTTP server (Go's `net/http`,
  Java's `jdk.httpserver`, Python's `http.server`) can only enter the `runtime` tier unless
  its author also writes a parser over raw sockets. It cannot enter `hand-rolled` for free.
- A language whose standard library has no HTTP server at all (Erlang/Elixir, Ruby ≥ 3.0
  after WEBrick's removal, C#) has no `runtime` option. It must hand-roll, or it cannot
  participate under §2.

Both are honest outcomes of a stdlib-only rule, not gaps to be papered over. If a
`framework` tier is ever added — third-party HTTP libraries permitted, so Express, Gin,
Ktor, and Phoenix can be compared — it would sit alongside these two and would still never
be ranked against them (§1.1).

---

## 3. Protocol contract

### 3.1 Endpoints

| Method | Path | Status | `Content-Type` | Body (exact bytes) |
|---|---|---|---|---|
| GET | `/` | 200 | `text/plain; charset=utf-8` | `Hello, World!\n` (14 bytes) |
| GET | `/json` | 200 | `application/json` | `{"message":"Hello, World!"}` (27 bytes) |
| GET | `/health` | 200 | `text/plain; charset=utf-8` | `OK` (2 bytes) |
| POST | `/echo` | 200 | `application/octet-stream` | the request body, unchanged |
| HEAD | any listed path | 200 | as above | none (see §3.6) |
| any | unknown path | 404 | `text/plain; charset=utf-8` | `Not Found\n` |
| wrong method on a known path | → | 405 | `text/plain; charset=utf-8` | `Method Not Allowed\n` |
| malformed request | → | 400 | `text/plain; charset=utf-8` | `Bad Request\n` |
| POST `/echo` without `Content-Length` | → | 411 | `text/plain; charset=utf-8` | `Length Required\n` |
| body larger than 1 MiB | → | 413 | `text/plain; charset=utf-8` | `Payload Too Large\n` |
| header block larger than 16 KiB | → | 431 | `text/plain; charset=utf-8` | `Request Header Fields Too Large\n` |
| version other than HTTP/1.0 or HTTP/1.1 | → | 505 | `text/plain; charset=utf-8` | `HTTP Version Not Supported\n` |

Routing is by **path only**. The query string is stripped at the first `?` and ignored:
`GET /json?x=1` is the same as `GET /json`. Trailing slashes are *not* normalised:
`GET /health/` is 404.

Allowed methods per path:

| Path | Allowed |
|---|---|
| `/`, `/json`, `/health` | `GET` |
| `/echo` | `POST` |

### 3.2 Exact response bytes

To make responses comparable, the byte sequence on the wire is fixed. Header order is
normative. A response is exactly:

```
HTTP/1.1 <code> <reason>\r\n
Content-Type: <type>\r\n
Content-Length: <len>\r\n
[Allow: <methods>\r\n]          <- only on 405
Connection: <keep-alive|close>\r\n
\r\n
<body bytes>
```

Reason phrases MUST be exactly: `OK`, `Bad Request`, `Not Found`, `Method Not Allowed`,
`Length Required`, `Payload Too Large`, `Request Header Fields Too Large`,
`HTTP Version Not Supported`, `Internal Server Error`.

**No other headers.** No `Date`, no `Server`, no `X-Powered-By`. Implementations that add
headers change `bytes/request` and are not comparable; this is a hard requirement for
benchmarks. (Rationale: `Date` alone is ~37 bytes on every response and dominates the
bytes-read figure for a 14-byte body.)

Example, `GET /`:

```
HTTP/1.1 200 OK\r\n
Content-Type: text/plain; charset=utf-8\r\n
Content-Length: 14\r\n
Connection: keep-alive\r\n
\r\n
Hello, World!\n
```

### 3.3 Connections and framing

- Responses MUST always use `Content-Length`. **Chunked responses are forbidden.**
- HTTP/1.1 requests are persistent by default; the server MUST serve multiple sequential
  requests on one connection.
- `Connection: close` on the request MUST be honoured, and the response MUST carry
  `Connection: close`, after which the server closes the socket.
- `Connection: keep-alive` on an HTTP/1.0 request MAY be honoured; if it is not, respond
  with `Connection: close`.
- HTTP/1.0 goes out with `Connection: close` unless the client asked for keep-alive.
- A response with status 400, 411, 413, 431, or 505 MUST close the connection
  (`Connection: close`), because the framing of the stream can no longer be trusted.
- Request bodies MUST be consumed (or the connection closed) before the next request is
  parsed; otherwise pipelined bytes are misread as a request line.
- Request pipelining MAY be supported. It is never used by the benchmark harness, and it
  is not tested for conformance beyond "does not corrupt the stream".

### 3.4 Request limits

| Limit | Value | Violation |
|---|---|---|
| Request line + headers (up to and including the terminating `\r\n\r\n`) | 16384 bytes | 431 |
| Request body | 1048576 bytes (1 MiB) | 413 |
| Header line without terminating CRLF | counted against the 16 KiB limit | 431 |

Case-insensitive header names. Multiple `Content-Length` headers → 400.
`Transfer-Encoding` together with `Content-Length` → 400.
`Transfer-Encoding: chunked` request bodies MAY be supported. An implementation that does
**not** support them MUST reject a chunked `POST /echo` with 411 or 501 rather than hang. An
implementation that **does** support them MUST decode correctly and echo the decoded body —
a chunked `POST /echo` of `hello` returns `200` with `hello`, not a rejection. Both are
conformant; the harness accepts either, and distinguishes them by the response.

### 3.5 Out of scope

No TLS, no HTTP/2, no WebSocket, no compression, no static file serving, no cookies, no
request logging in the hot path, no routing beyond the four paths above, no configuration
files.

### 3.6 HEAD (optional)

`HEAD` is not implemented by any implementation in the baseline and is **not** part of the
benchmark. If an implementation chooses to support it, it MUST return the same status and
headers as the corresponding `GET` with no body, and must send `Content-Length` as the
length the body *would* have had.

---

## 4. Runtime contract

### 4.1 Configuration

Configuration is by environment variable. No command-line flags are required, no config
file is read.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | TCP port to listen on |
| `HOST` | `127.0.0.1` | IPv4 address to bind |
| `VERBOSE` | unset | if set, per-request logging is allowed (never used for benchmarks) |

IPv6, `SO_REUSEPORT`, multiple listeners, and worker-process counts are implementation
choices. If an implementation needs tuning (thread count, event-loop shards), it MUST read
it from an environment variable too and document it.

### 4.2 Startup handshake

The harness needs to know when the socket is accepting. An implementation MUST, as its
first action after `listen()` succeeds, write this line to **stdout** and flush:

```
LISTENING <host>:<port>\n
```

Nothing else may be written to stdout before it. It MUST bind and listen before printing.
Servers MUST exit with status 0 on `SIGINT`/`SIGTERM`. An unhandled signal does not satisfy
this: runtimes that die of the signal instead — Node exits 143 on an unhandled `SIGTERM` —
must install a handler.

### 4.3 Performance posture

- No artificial delays, sleeps, busy-wait polling, or request coalescing.
- No per-request `fork()`, no per-request `malloc` of the response body where avoidable
  (implementations may differ here — that is the point — but it must be a real
  implementation, not a deliberately hobbled one).
- `TCP_NODELAY` is a legitimate optimisation and is expected.
- If an implementation caches or precomputes response bytes, it MUST be able to produce
  them for the full 1 MiB body limit and the response MUST remain byte-exact.

---

## 5. Build and run interface

Every implementation directory MUST contain exactly these four things:

```
<impl-dir>/
├── build.sh     # executable; builds the server. Exit 0 on success. Idempotent.
├── run.sh       # executable; execs the server in the foreground. Honours PORT/HOST.
├── impl.env     # machine-readable declaration; see below
└── README.md     # language, toolchain version, build flags, design, known limitations
```

### 5.1 `impl.env`

A shell-commentable `KEY=value` file read by the harness and copied into the results. Four
keys, all required:

```
# Which tier this implementation belongs to (SPEC.md 1.1). Required.
HTTP_LAYER=hand-rolled          # hand-rolled | runtime

# The runtime and version these numbers describe. Required.
RUNTIME=go 1.24.0

# Comma-separated source extensions for the LOC count (SPEC.md 7.1). Required.
# Without this the harness has no language table to fall back on and would
# report LOC as 0, which reads as "this implementation costs nothing".
SOURCE_EXT=go

# A command whose first output line is recorded as the toolchain version and
# checked to exist by harness/doctor.sh. Required.
TOOLCHAIN_CMD=go version
```

`HTTP_LAYER=runtime` is only legitimate for an implementation that delegates HTTP parsing
and framing to its own language distribution. Claiming `hand-rolled` while delegating is a
misreport, and the review will check the source.

These four keys are the *only* place the harness learns anything about a language. There is
no language table in `harness/`: adding a language is a new directory with an `impl.env`, not
an edit to the harness.

### 5.2 Rules

- `build.sh` MUST be safe to run repeatedly. Compiled implementations MUST place the final
  binary at `<impl-dir>/bin/server`; the harness records its size. For an interpreted or JIT
  runtime `build.sh` MAY be a type check or a no-op, `bin/server` does not exist, and the
  `binary` column in the results is recorded as blank — it is not comparable across
  interpreted and compiled implementations.
- `run.sh` MUST `exec` the server so that the process the harness starts *is* the server.
  It MUST NOT background, daemonise, or fork-and-wait. For an interpreted runtime `exec`ing
  the interpreter is correct, e.g. `exec node src/server.ts`.
- Builds MUST be optimised releases (e.g. `-O2`, `cargo build --release`,
  `dub build --build=release`). Debug builds are not comparable and MUST NOT be submitted.
  For a JIT runtime the analogous requirement is that no development-only instrumentation,
  source-map generation, or type checking runs inside the request path.
- `build.sh` MUST NOT require network access (§2.1).
- Source lives wherever the implementation likes; `src/`, `Source/`, `Include/` are all
  fine.

The harness does, in order: `build.sh`, then `PORT=$port HOST=127.0.0.1 ./run.sh`, then
waits for the `LISTENING` line, then runs the load generator against it, then `SIGTERM`s
the server.

---

## 6. Conformance

No number is published for an implementation that fails conformance. `harness/conformance.py`
checks, over raw sockets and with byte-exact comparison:

1. Every row of the §3.1 endpoint table, headers and body compared byte-for-byte.
2. HTTP/1.1 keep-alive: two sequential requests on one socket.
3. `Connection: close` handling.
4. Query-string stripping (`/?x=1`, `/json?x=1`).
5. 404 on unknown path, unknown method, and `/health/`.
6. 405 with the correct `Allow` value on each wrong-method combination.
7. 411 on `Content-Length`-less `POST /echo`; on a chunked `POST /echo`, either a clean
   rejection (411 or 501) or a correct decode-and-echo — both are conformant (§3.4).
8. 431 on a >16 KiB header block.
9. 413 on a >1 MiB body.
10. 400 on a malformed request line.
11. A 64 KiB `POST /echo` round-trip, byte-exact.
12. No `Date`/`Server`/unexpected headers in any response.
13. The presence and validity of `impl.env` (§5.1): all four keys, and `HTTP_LAYER` one of
    the two permitted values.

Run it with `make conform` or `harness/conformance.py --impl <dir>`.

---

## 7. Results

A benchmark result is only valid alongside the environment it ran in. Every published
number MUST come with the machine metadata the harness records in
`results/<run-id>/meta.json`: CPU model, core count, governor, kernel, toolchain versions,
build flags, commit, pinning, generator and generator version.

Numbers are reported as the **median of N runs** (default 3), with min/max spread, and any
scenario whose runs spread by more than 5% is flagged as unstable. See
`docs/methodology.md`.

### 7.1 Grouping

Results MUST be grouped by `HTTP_LAYER` (§1.1) and MUST NOT be ranked across groups. A table
that places a `runtime` implementation next to a `hand-rolled` one without saying so is a
misreport, not a result.

Two further caveats belong next to the table rather than in a footnote:

- **Peak RSS is not comparable across runtimes.** A JIT runtime's baseline — tens of MiB
  before a single request is served — dwarfs the server's own allocation. Compare RSS only
  within a runtime, and prefer `CPU ms/req` when comparing across them.
- **The `binary` column is blank for interpreted implementations** (§5.2). Do not average it
  across a mixed table.
