-- wrk/wrk2 script for the `echo` scenario.
--
-- The body is fixed here, in one place, so that every implementation is asked to
-- echo exactly the same 1 KiB. Never change the size without re-running every
-- implementation: bytes/request and CPU/request are only comparable if the
-- payload is (docs/methodology.md).
--
-- Note: wrk requires a script to send a request body, which is why this exists
-- instead of a flag.

wrk.method = "POST"
wrk.body = string.rep("a", 1024)
wrk.headers["Content-Type"] = "application/octet-stream"
