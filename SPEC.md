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
`build.sh` and `run.sh` (see §5). By convention the top level holds one per language
(`C/`, `CPP/`, `D/`, `Rust/`). Extra variants may be nested (`Rust/epoll/`) or siblings
(`Rust-epoll/`). All are benchmarked independently and side by side.

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

`std.parallelism`, `std.concurrency`, and `std::thread` are standard library and therefore
allowed. The line is: **the language's own distribution, plus the OS.** Anything that
requires a package manager to fetch is out.

Every implementation MUST record its exact toolchain version and build flags in its
`README.md`, and `harness/doctor.sh` records them again at benchmark time.

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
`Transfer-Encoding: chunked` request bodies are **not required** to be supported; an
implementation that does not support them MUST reject a chunked `POST /echo` with 411 or
501 rather than hang.

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
Servers MUST exit with status 0 on `SIGINT`/`SIGTERM`.

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

Every implementation directory MUST contain exactly these three things:

```
<impl-dir>/
├── build.sh     # executable; builds the server. Exit 0 on success. Idempotent.
├── run.sh       # executable; execs the server in the foreground. Honours PORT/HOST.
└── README.md    # language, toolchain version, build flags, design, known limitations
```

- `build.sh` MUST be safe to run repeatedly and MUST place the final binary at
  `<impl-dir>/bin/server` (any path is fine, but the harness records its size, so put it
  somewhere predictable — the convention is `bin/server`).
- `run.sh` MUST `exec` the server so that the process the harness starts *is* the server.
  It MUST NOT background, daemonise, or fork-and-wait.
- Builds MUST be optimised releases (e.g. `-O2`, `cargo build --release`,
  `dub build --build=release`). Debug builds are not comparable and MUST NOT be submitted.
- `build.sh` MUST NOT require network access.
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
7. 411 on `Content-Length`-less `POST /echo`; 411 or 501 on chunked `POST /echo`.
8. 431 on a >16 KiB header block.
9. 413 on a >1 MiB body.
10. 400 on a malformed request line.
11. A 64 KiB `POST /echo` round-trip, byte-exact.
12. No `Date`/`Server`/unexpected headers in any response.

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
