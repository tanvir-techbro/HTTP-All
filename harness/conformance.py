#!/usr/bin/env python3
"""
conformance.py -- byte-exact protocol conformance suite.

An implementation that fails this gets no published benchmark numbers: the
throughput comparison is only meaningful if every server is emitting the same
bytes (SPEC.md 3.2, 6).

Usage:
    harness/conformance.py --impl C              # build, run, test, stop
    harness/conformance.py --host 127.0.0.1 --port 8080
    harness/conformance.py --impl C --verbose
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- expectations, straight from SPEC.md 3.1 ---------------------------------

REASON = {
    200: b"OK",
    400: b"Bad Request",
    404: b"Not Found",
    405: b"Method Not Allowed",
    411: b"Length Required",
    413: b"Payload Too Large",
    431: b"Request Header Fields Too Large",
    500: b"Internal Server Error",
    505: b"HTTP Version Not Supported",
}

TEXT = b"text/plain; charset=utf-8"

BODY_ROOT = b"Hello, World!\n"
BODY_JSON = b'{"message":"Hello, World!"}'
BODY_HEALTH = b"OK"
BODY_404 = b"Not Found\n"
BODY_405 = b"Method Not Allowed\n"
BODY_400 = b"Bad Request\n"
BODY_411 = b"Length Required\n"
BODY_413 = b"Payload Too Large\n"
BODY_431 = b"Request Header Fields Too Large\n"
BODY_505 = b"HTTP Version Not Supported\n"

MAX_HEADER_BYTES = 16384
MAX_BODY_BYTES = 1024 * 1024

ALLOWED_HEADERS = {b"content-type", b"content-length", b"connection", b"allow"}

# --- results bookkeeping -----------------------------------------------------

PASS, FAIL, WARN = "pass", "fail", "warn"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    if status == FAIL:
        mark = "FAIL"
    elif status == WARN:
        mark = "WARN"
    else:
        mark = " ok "
    print(f"  [{mark}] {name}" + (f"\n         {detail}" if detail else ""))


def check(name: str, condition: bool, detail: str = "") -> bool:
    record(name, PASS if condition else FAIL, detail if not condition else "")
    return condition


# --- raw HTTP client ---------------------------------------------------------


class ProtocolError(Exception):
    pass


class Conn:
    """Minimal HTTP/1.1 client that keeps the socket raw so the exact bytes are
    visible -- this is the whole point of the suite."""

    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

    def send(self, data: bytes, ignore_errors: bool = False) -> None:
        try:
            self.sock.sendall(data)
        except (BrokenPipeError, ConnectionResetError):
            if not ignore_errors:
                raise

    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ProtocolError("connection closed before the response was complete")
        self.buf += chunk

    def read_response(self) -> tuple[bytes, list[tuple[bytes, bytes]], bytes, bytes]:
        """Returns (status_line, headers, body, head). `head` is the exact header
        block as received, without the terminating CRLFCRLF."""
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, _, rest = self.buf.partition(b"\r\n\r\n")
        self.buf = rest

        lines = head.split(b"\r\n")
        status = lines[0]
        headers = []
        for line in lines[1:]:
            name, sep, value = line.partition(b":")
            if not sep:
                raise ProtocolError(f"malformed header line: {line!r}")
            headers.append((name.strip(), value.strip()))

        length = None
        for name, value in headers:
            if name.lower() == b"content-length":
                try:
                    length = int(value)
                except ValueError:
                    raise ProtocolError(f"non-numeric Content-Length: {value!r}")
        if length is None:
            raise ProtocolError("response has no Content-Length (chunked is forbidden)")

        while len(self.buf) < length:
            self._fill()
        body = self.buf[:length]
        self.buf = self.buf[length:]
        return status, headers, body, head

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def build_request(
    method: bytes,
    path: bytes,
    host: str,
    port: int,
    version: bytes = b"HTTP/1.1",
    extra: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
    content_length: int | None = None,
) -> bytes:
    lines = [method + b" " + path + b" " + version]
    headers = [(b"Host", f"{host}:{port}".encode())]
    headers.extend(extra or [])
    if content_length is not None:
        headers.append((b"Content-Length", str(content_length).encode()))
    elif body or method == b"POST":
        # POST always frames its body, even when the body is empty: a POST with
        # no Content-Length at all is the 411 case (SPEC.md 3.1), not an empty
        # body, and the two must not be confused by a test.
        headers.append((b"Content-Length", str(len(body)).encode()))
    for name, value in headers:
        lines.append(name + b": " + value)
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def expected_head(
    code: int,
    content_type: bytes,
    length: int,
    connection: bytes,
    allow: bytes | None = None,
) -> bytes:
    """The exact header block a response must have (SPEC.md 3.2)."""
    head = (
        b"HTTP/1.1 %d %s\r\n" % (code, REASON[code])
        + b"Content-Type: " + content_type + b"\r\n"
        + b"Content-Length: %d\r\n" % length
    )
    if allow is not None:
        head += b"Allow: " + allow + b"\r\n"
    head += b"Connection: " + connection + b"\r\n"
    return head.rstrip(b"\r\n")


def simple(host: str, port: int, method: bytes, path: bytes, **kw):
    conn = Conn(host, port)
    try:
        conn.send(build_request(method, path, host, port, **kw))
        return conn.read_response()
    finally:
        conn.close()


def assert_only_allowed_headers(name: str, headers) -> None:
    odd = [n for n, _ in headers if n.lower() not in ALLOWED_HEADERS]
    if odd:
        record(name, FAIL, f"unexpected headers: {[h.decode('latin1') for h in odd]}")
    else:
        record(name, PASS)


# --- individual tests --------------------------------------------------------


def test_exact(host, port, name, method, path, code, ctype, body, allow=None,
               connection=b"keep-alive", extra=None) -> None:
    status, headers, got, head = simple(
        host, port, method, path, extra=extra, body=(body if method == b"POST" else b"")
    )
    want = expected_head(code, ctype, len(body), connection, allow)
    assert_only_allowed_headers(f"{name}: header set", headers)
    if head != want:
        record(name, FAIL, f"headers differ\n         want: {want!r}\n         got:  {head!r}")
        return
    if status != b"HTTP/1.1 %d %s" % (code, REASON[code]):
        record(name, FAIL, f"status line want {code} {REASON[code]!r}, got {status!r}")
        return
    if got != body:
        record(name, FAIL, f"body want {body!r}, got {got!r}")
        return
    record(name, PASS)


def test_keep_alive(host, port) -> None:
    conn = Conn(host, port)
    try:
        for i, path in enumerate((b"/", b"/json", b"/health")):
            conn.send(build_request(b"GET", path, host, port))
            status, _, body, _ = conn.read_response()
            want = {b"/": BODY_ROOT, b"/json": BODY_JSON, b"/health": BODY_HEALTH}[path]
            if status != b"HTTP/1.1 200 OK" or body != want:
                record("keep-alive: three sequential requests", FAIL,
                       f"request {i + 1} on {path!r}: {status!r} {body!r}")
                return
        record("keep-alive: three sequential requests", PASS)
    except (ProtocolError, OSError) as exc:
        record("keep-alive: three sequential requests", FAIL, str(exc))
    finally:
        conn.close()


def test_pipelining(host, port) -> None:
    # MAY be supported (SPEC.md 3.3); informational only.
    conn = Conn(host, port)
    try:
        conn.send(
            build_request(b"GET", b"/", host, port)
            + build_request(b"GET", b"/health", host, port)
        )
        _, _, first, _ = conn.read_response()
        _, _, second, _ = conn.read_response()
        ok = first == BODY_ROOT and second == BODY_HEALTH
        record("pipelining: two requests in one write (optional)", PASS if ok else WARN,
               "" if ok else "pipelining not supported; allowed by SPEC.md 3.3")
    except (ProtocolError, OSError) as exc:
        record("pipelining: two requests in one write (optional)", WARN, str(exc))
    finally:
        conn.close()


def test_close(host, port) -> None:
    status, headers, body, head = simple(
        host, port, b"GET", b"/", extra=[(b"Connection", b"close")]
    )
    want = expected_head(200, TEXT, len(BODY_ROOT), b"close")
    if head != want or body != BODY_ROOT:
        record("Connection: close is honoured", FAIL, f"want {want!r}, got {head!r}")
        return
    # And the server must actually close.
    conn = Conn(host, port)
    try:
        conn.send(build_request(b"GET", b"/", host, port, extra=[(b"Connection", b"close")]))
        conn.read_response()
        conn.sock.settimeout(2.0)
        closed = conn.sock.recv(1) == b""
        record("Connection: close closes the socket", PASS if closed else FAIL,
               "" if closed else "server kept the socket open")
    except (ProtocolError, OSError) as exc:
        record("Connection: close closes the socket", PASS if not isinstance(exc, ProtocolError) else FAIL,
               str(exc))
    finally:
        conn.close()


def test_query_string(host, port) -> None:
    for path in (b"/?x=1", b"/json?x=1&y=2", b"/health?"):
        status, _, body, _ = simple(host, port, b"GET", path)
        want = {b"/?x=1": BODY_ROOT, b"/json?x=1&y=2": BODY_JSON, b"/health?": BODY_HEALTH}[path]
        if status != b"HTTP/1.1 200 OK" or body != want:
            record("query strings are stripped", FAIL, f"{path!r} -> {status!r} {body!r}")
            return
    record("query strings are stripped", PASS)


def test_trailing_slash(host, port) -> None:
    status, _, body, _ = simple(host, port, b"GET", b"/health/")
    ok = status == b"HTTP/1.1 404 Not Found" and body == BODY_404
    record("trailing slash is not normalised", PASS if ok else FAIL,
           "" if ok else f"/health/ -> {status!r} {body!r}")


def test_echo_large(host, port) -> None:
    payload = bytes(range(256)) * 256  # 64 KiB, byte-exact round trip
    total = b""
    conn = Conn(host, port)
    try:
        conn.send(build_request(b"POST", b"/echo", host, port, body=payload))
        status, headers, body, _ = conn.read_response()
        if status != b"HTTP/1.1 200 OK":
            record("POST /echo round-trips 64 KiB", FAIL, f"status {status!r}")
            return
        if headers[0][1] != b"application/octet-stream":
            record("POST /echo round-trips 64 KiB", FAIL, f"Content-Type {headers[0][1]!r}")
            return
        if body != payload:
            record("POST /echo round-trips 64 KiB", FAIL,
                   f"body differs ({len(body)} bytes vs {len(payload)})")
            return
        # The connection must still be usable afterwards.
        conn.send(build_request(b"GET", b"/health", host, port))
        status, _, body, _ = conn.read_response()
        if status != b"HTTP/1.1 200 OK" or body != BODY_HEALTH:
            record("POST /echo round-trips 64 KiB", FAIL, "connection unusable afterwards")
            return
        record("POST /echo round-trips 64 KiB", PASS)
    except (ProtocolError, OSError) as exc:
        record("POST /echo round-trips 64 KiB", FAIL, str(exc))
    finally:
        conn.close()


def test_oversized_header(host, port) -> None:
    # More than 16 KiB of headers must be answered with 431 and a close.
    filler = b"X-Pad: " + b"a" * 96 + b"\r\n"
    payload = b"GET / HTTP/1.1\r\nHost: x\r\n" + filler * 200 + b"\r\n"
    assert len(payload) > MAX_HEADER_BYTES
    conn = Conn(host, port)
    try:
        conn.send(payload, ignore_errors=True)
        status, headers, body, head = conn.read_response()
        want = expected_head(431, TEXT, len(BODY_431), b"close")
        ok = head == want and body == BODY_431
        record("oversized header block -> 431", PASS if ok else FAIL,
               "" if ok else f"want {want!r}, got {head!r}")
    except (ProtocolError, OSError) as exc:
        record("oversized header block -> 431", FAIL, str(exc))
    finally:
        conn.close()


def test_oversized_body(host, port) -> None:
    # Declared body above 1 MiB must be refused before it is read.
    conn = Conn(host, port)
    try:
        conn.send(build_request(b"POST", b"/echo", host, port,
                                content_length=MAX_BODY_BYTES + 1))
        status, headers, body, head = conn.read_response()
        want = expected_head(413, TEXT, len(BODY_413), b"close")
        ok = head == want and body == BODY_413
        record("body over 1 MiB -> 413", PASS if ok else FAIL,
               "" if ok else f"want {want!r}, got {head!r}")
    except (ProtocolError, OSError) as exc:
        record("body over 1 MiB -> 413", FAIL, str(exc))
    finally:
        conn.close()


def test_no_content_length(host, port) -> None:
    conn = Conn(host, port)
    try:
        # POST /echo with no Content-Length and no Transfer-Encoding: there is
        # no body to echo, so the framing is unusable -- 411.
        conn.send(b"POST /echo HTTP/1.1\r\nHost: %s:%d\r\n\r\n" % (host.encode(), port))
        status, _, body, head = conn.read_response()
        want = expected_head(411, TEXT, len(BODY_411), b"close")
        ok = head == want and body == BODY_411
        record("POST /echo without Content-Length -> 411", PASS if ok else FAIL,
               "" if ok else f"want {want!r}, got {head!r}")
    except (ProtocolError, OSError) as exc:
        record("POST /echo without Content-Length -> 411", FAIL, str(exc))
    finally:
        conn.close()


def test_chunked(host, port) -> None:
    # SPEC.md 3.4: supporting chunked bodies is optional. Rejecting cleanly
    # (411/501/400) and decoding correctly (200 with the decoded body) are both
    # conformant -- what is not allowed is hanging or mis-framing. node:http
    # decodes chunked for free, so a "must reject" test would fail a
    # spec-compliant server.
    payload = b"hello"
    conn = Conn(host, port)
    try:
        conn.send(
            b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n0\r\n\r\n"
        )
        status, _, body, _ = conn.read_response()
        code = int(status.split(b" ")[1])
        if code == 200:
            ok = body == payload
            record("chunked request body: decoded or cleanly rejected",
                   PASS if ok else FAIL,
                   "" if ok else f"200 but body was {body!r}, expected {payload!r}")
        elif code in (411, 501, 400):
            record("chunked request body: decoded or cleanly rejected", PASS,
                   f"rejected with {code}; allowed by SPEC.md 3.4")
        else:
            record("chunked request body: decoded or cleanly rejected", FAIL,
                   f"got {status!r} (want 200-with-body, or 411/501/400)")
    except (ProtocolError, OSError) as exc:
        record("chunked request body: decoded or cleanly rejected", FAIL, str(exc))
    finally:
        conn.close()


def test_malformed(host, port) -> None:
    conn = Conn(host, port)
    try:
        conn.send(b"NOT A VALID REQUEST LINE\r\n\r\n")
        status, _, body, head = conn.read_response()
        code = int(status.split(b" ")[1])
        ok = code == 400 and body == BODY_400
        record("malformed request line -> 400", PASS if ok else FAIL,
               "" if ok else f"got {status!r} {body!r}")
    except (ProtocolError, OSError) as exc:
        record("malformed request line -> 400", FAIL, str(exc))
    finally:
        conn.close()


def test_http10(host, port) -> None:
    # Not benchmarked; lenient. Must not hang, and must close (SPEC.md 3.3).
    status, _, body, head = simple(host, port, b"GET", b"/", version=b"HTTP/1.0")
    code = int(status.split(b" ")[1])
    if code == 200 and body == BODY_ROOT:
        record("HTTP/1.0 is handled (optional)", PASS if b"close" in head else WARN,
               "" if b"close" in head else "responded without Connection: close")
    elif code == 505:
        record("HTTP/1.0 is handled (optional)", PASS, "rejected with 505")
    else:
        record("HTTP/1.0 is handled (optional)", WARN, f"got {status!r}")


def validate_impl_env(impl_dir: Path) -> None:
    """SPEC.md 5.1 / 6.13: every implementation declares its tier and runtime."""
    env_path = impl_dir / "impl.env"
    if not env_path.exists():
        record("impl.env present and valid", FAIL,
               "no impl.env — see SPEC.md 5.1 for the required keys")
        return

    values: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.split("#")[0].strip()

    layer = values.get("HTTP_LAYER")
    runtime = values.get("RUNTIME")
    source_ext = values.get("SOURCE_EXT")
    toolchain = values.get("TOOLCHAIN_CMD")

    missing = [k for k, v in (("RUNTIME", runtime), ("SOURCE_EXT", source_ext),
                              ("TOOLCHAIN_CMD", toolchain)) if not v]
    if layer not in ("hand-rolled", "runtime"):
        record("impl.env present and valid", FAIL,
               f"HTTP_LAYER={layer!r} (must be 'hand-rolled' or 'runtime')")
    elif missing:
        # No harness default exists for these: without SOURCE_EXT the LOC column
        # reports 0, which reads as "this implementation costs nothing".
        record("impl.env present and valid", FAIL, f"missing: {', '.join(missing)}")
    else:
        record("impl.env present and valid", PASS,
               f"{layer} tier, runtime {runtime}, source {source_ext}")


def run_suite(host: str, port: int, impl_dir: Path | None = None) -> bool:
    if impl_dir is None:
        print(f"\n\x1b[1mConformance against {host}:{port}\x1b[0m\n")
    else:
        print(f"\n\x1b[1m=== {impl_dir.name} ===\x1b[0m\n")
        validate_impl_env(impl_dir)

    def guard(fn, *args, **kwargs):
        """A test that raises is a failure, not a traceback: a server that dies
        mid-suite must be reported as such, with the remaining tests still run."""
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - deliberate: report, don't crash
            record(getattr(fn, "__name__", str(fn)), FAIL, f"{type(exc).__name__}: {exc}")

    guard(test_exact, host, port, "GET /", b"GET", b"/", 200, TEXT, BODY_ROOT)
    guard(test_exact, host, port, "GET /json", b"GET", b"/json", 200, b"application/json", BODY_JSON)
    guard(test_exact, host, port, "GET /health", b"GET", b"/health", 200, TEXT, BODY_HEALTH)

    guard(test_exact, host, port, "POST /echo echoes the body", b"POST", b"/echo", 200,
          b"application/octet-stream", b"0123456789")
    guard(test_exact, host, port, "POST /echo with empty body", b"POST", b"/echo", 200,
          b"application/octet-stream", b"")

    guard(test_exact, host, port, "unknown path -> 404", b"GET", b"/nope", 404, TEXT, BODY_404)
    guard(test_exact, host, port, "PUT / -> 405 Allow: GET", b"PUT", b"/", 405, TEXT, BODY_405,
          allow=b"GET")
    guard(test_exact, host, port, "GET /echo -> 405 Allow: POST", b"GET", b"/echo", 405, TEXT,
          BODY_405, allow=b"POST")
    guard(test_exact, host, port, "POST /health -> 405 Allow: GET", b"POST", b"/health", 405, TEXT,
          BODY_405, allow=b"GET")

    for fn in (test_keep_alive, test_pipelining, test_close, test_query_string,
               test_trailing_slash, test_echo_large, test_oversized_header,
               test_oversized_body, test_no_content_length, test_chunked,
               test_malformed, test_http10):
        guard(fn, host, port)

    failed = [r for r in results if r[1] == FAIL]
    warned = [r for r in results if r[1] == WARN]
    print()
    if failed:
        print(f"\x1b[31m{len(failed)} failed\x1b[0m, {len(results) - len(failed) - len(warned)} passed, "
              f"{len(warned)} warnings")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
        return False
    print(f"\x1b[32mall {len(results) - len(warned)} required tests passed\x1b[0m"
          + (f" ({len(warned)} warnings)" if warned else ""))
    return True


# --- running against an implementation directory -----------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_impl(impl_dir: Path, verbose: bool):
    build = impl_dir / "build.sh"
    run = impl_dir / "run.sh"
    if not build.exists() or not run.exists():
        sys.exit(f"{impl_dir} must contain build.sh and run.sh (SPEC.md 5)")

    print(f"\n\x1b[1mBuilding {impl_dir.name}\x1b[0m")
    rc = subprocess.run(["./build.sh"], cwd=impl_dir).returncode
    if rc != 0:
        sys.exit(f"build.sh failed with status {rc}")

    port = free_port()
    env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1"}
    proc = subprocess.Popen(
        ["./run.sh"], cwd=impl_dir, env=env,
        stdout=subprocess.PIPE, stderr=None if verbose else subprocess.DEVNULL,
        text=True, bufsize=1, start_new_session=True,
    )

    ready = threading.Event()
    lines: list[str] = []

    def pump():
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.append(line.rstrip("\n"))
            if line.startswith("LISTENING "):
                ready.set()

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if ready.is_set():
            return proc, port
        if proc.poll() is not None:
            sys.exit(f"server exited with status {proc.returncode} before LISTENING; "
                     f"output: {lines!r}")
        time.sleep(0.02)
    proc.kill()
    sys.exit("timed out waiting for the LISTENING handshake (SPEC.md 4.2)")


def stop_impl(proc) -> None:
    if proc is None:
        return
    # The harness starts run.sh, which execs the server, so the process group is
    # the server.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--impl", help="implementation directory, relative to the repo root")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--verbose", action="store_true", help="show server stderr")
    args = ap.parse_args()

    proc = None
    try:
        if args.impl:
            impl_dir = (ROOT / args.impl).resolve()
            if not impl_dir.is_dir():
                sys.exit(f"no such implementation directory: {impl_dir}")
            proc, port = start_impl(impl_dir, args.verbose)
            passed = run_suite(args.host, port, impl_dir=impl_dir)
        else:
            passed = run_suite(args.host, args.port)
        return 0 if passed else 1
    finally:
        stop_impl(proc)


if __name__ == "__main__":
    sys.exit(main())
