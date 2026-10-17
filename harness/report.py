#!/usr/bin/env python3
"""
report.py -- turn a results directory into summary.csv and RESULTS.md.

Reads the raw generator output rather than trusting anything pre-parsed, so any
number in the table can be re-derived from the files kept alongside it.

Usage:
    harness/report.py --dir results/20260101T120000-abc1234
    harness/report.py --dir results/latest --update-readme
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# wrk / wrk2 output shapes:
#   Requests/sec:  41234.56
#   1234567 requests in 30.05s, 123.45MB read
#   Transfer/sec:     1.23MB
#      50%    1.23ms          <- latency distribution (--latency / -L)
#    Latency   1.23ms  ...    <- thread stats line, must not match
RE_RPS = re.compile(r"^Requests/sec:\s+([\d.]+)", re.M)
RE_REQUESTS = re.compile(r"^\s*(\d+)\s+requests in\s+([\d.]+)(m?s|us)", re.M)
RE_TRANSFER = re.compile(r"^Transfer/sec:\s+([\d.]+)(\w+)", re.M)
RE_PCT = re.compile(r"^\s*(\d+(?:\.\d+)?)%\s+([\d.]+)(us|ms|s)\s*$", re.M)

PERCENTILES = ["50", "75", "90", "99"]


def to_ms(value: str, unit: str) -> float:
    factor = {"us": 1e-3, "ms": 1.0, "s": 1e3}[unit]
    return float(value) * factor


def parse_wrk(text: str) -> dict:
    out: dict[str, float] = {}

    m = RE_RPS.search(text)
    if m:
        out["rps"] = float(m.group(1))

    m = RE_REQUESTS.search(text)
    if m:
        out["requests"] = int(m.group(1))
        unit = m.group(3)
        seconds = float(m.group(2)) * (60 if unit == "m" else 1)
        if unit == "us":
            seconds /= 1e6
        out["seconds"] = seconds

    m = RE_TRANSFER.search(text)
    if m:
        out["transfer"] = float(m.group(1))

    for pct, value, unit in RE_PCT.findall(text):
        key = f"p{pct}"
        if key in ("p50", "p75", "p90", "p99") and key not in out:
            out[key] = to_ms(value, unit)

    return out


def load_runs(out_dir: Path):
    """(impl, scenario) -> {run number -> measurement}, and the rejected runs.

    A run the generator could not complete is excluded rather than averaged in as
    a zero; its reason is returned so the report can say so out loud."""
    runs: dict[tuple[str, str], dict[int, dict]] = defaultdict(dict)
    rejected: dict[tuple[str, str], list[str]] = defaultdict(list)
    with open(out_dir / "runs.jsonl") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            wrk_path = out_dir / row["output"]
            parsed = (
                parse_wrk(wrk_path.read_text(errors="replace"))
                if wrk_path.exists()
                else {}
            )
            row.update(parsed)

            key = (row["impl"], row["scenario"])
            reason = (row.get("error") or "").strip()
            if not reason and "rps" not in parsed:
                reason = "no throughput in the load generator output"
            if reason:
                rejected[key].append(f"run {row.get('run', '?')}: {reason}")
                continue

            requests = parsed.get("requests")
            ticks = row.get("cpu_ticks") or 0
            clk = row.get("clk_tck") or 100
            if requests:
                row["cpu_ms_per_req"] = (ticks / clk) * 1000.0 / requests
                row["us_per_req"] = (ticks / clk) * 1e6 / requests
            runs[key][row["run"]] = row
    return runs, rejected


def median(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def spread(values: list[float]) -> float:
    """(max - min) / median, in percent. The stability check from SPEC.md 7."""
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return 0.0
    med = statistics.median(values)
    if med == 0:
        return 0.0
    return (max(values) - min(values)) / med * 100.0


def summarise(runs) -> list[dict]:
    rows = []
    for (impl, scenario), by_run in sorted(runs.items()):
        measurements = list(by_run.values())
        rpss = [m.get("rps") for m in measurements]
        row = {
            "impl": impl,
            "scenario": scenario,
            "http_layer": measurements[0].get("http_layer") or "hand-rolled",
            "runtime": measurements[0].get("runtime") or "unknown",
            "runs": len(measurements),
            "rps": median(rpss),
            "rps_spread_pct": spread(rpss),
            "peak_rss_kb": median([m.get("peak_rss_kb") for m in measurements]),
            "cpu_ms_per_req": median([m.get("cpu_ms_per_req") for m in measurements]),
            "binary_bytes": measurements[0].get("binary_bytes"),
            "loc": measurements[0].get("loc"),
            "connections": measurements[0].get("connections"),
        }
        for pct in PERCENTILES:
            row[f"p{pct}"] = median([m.get(f"p{pct}") for m in measurements])
        rows.append(row)
    return rows


def fmt(value, spec="", suffix="", dash="—"):
    if value is None:
        return dash
    return f"{value:{spec}}{suffix}"


def human_bytes(n) -> str:
    if not n:
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n / 1:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} GiB"


TIER_TITLE = {
    "hand-rolled": (
        "Hand-rolled HTTP",
        "each implementation parses HTTP/1.1 itself "
        "over the runtime's raw sockets — this is the ranking that compares "
        "languages",
    ),
    "runtime": (
        "Runtime-provided HTTP",
        "HTTP parsing and framing come from the runtime's "
        "own distribution (e.g. `node:http`). Compared within this group only, and "
        "**not** against the group above — the gap between the two groups is what "
        "the runtime buys, which is a different question from which language is "
        "faster",
    ),
}


def tier_table(rows: list[dict]) -> list[str]:
    lines = [
        "| impl | runtime | scenario | req/s | p50 | p99 | peak RSS | CPU ms/req | "
        "binary | LOC | spread |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| `{r['impl']}` | {r['runtime']} | {r['scenario']} "
            f"| {fmt(r['rps'], ',.0f')} "
            f"| {fmt(r['p50'], '.2f', ' ms')} "
            f"| {fmt(r['p99'], '.2f', ' ms')} "
            f"| {fmt(r['peak_rss_kb'] / 1024 if r['peak_rss_kb'] else None, ',.1f', ' MiB')} "
            f"| {fmt(r['cpu_ms_per_req'], '.4f')} "
            f"| {human_bytes(r['binary_bytes'])} "
            f"| {fmt(r['loc'], ',.0f')} "
            f"| {fmt(r['rps_spread_pct'], '.1f', '%')} |"
        )
    return lines


def markdown(rows: list[dict], meta: dict, rejected: dict | None = None) -> str:
    lines = []
    unstable = [r for r in rows if (r["rps_spread_pct"] or 0) > 5.0]

    lines.append(
        f"Measured {meta.get('started', '?')} on "
        f"{meta.get('cpu_model', 'unknown CPU')} "
        f"({meta.get('logical_cpus', '?')} logical CPUs), "
        f"kernel {meta.get('kernel', '?')}."
    )
    lines.append("")
    lines.append(f"- commit `{meta.get('commit') or 'working tree'}`")
    lines.append(f"- governor(s): {', '.join(meta.get('governors') or ['unknown'])}")
    pin = meta.get("pinning") or {}
    lines.append(f"- pinning: server `{pin.get('server')}`, load `{pin.get('load')}`")
    gen = meta.get("generator") or {}
    rate = f" at {gen['rate']} req/s" if gen.get("rate") else ""
    lines.append(
        f"- generator: {gen.get('name')} — {gen.get('version') or 'version unknown'}{rate}"
    )
    params = meta.get("params") or {}
    lines.append(
        f"- {params.get('runs')} runs per cell, {params.get('duration')} each, "
        f"{params.get('connections')} connections, "
        f"{params.get('warmup_s')}s warmup discarded"
    )
    if meta.get("publishable"):
        lines.append("- environment marked **publishable**")
    else:
        lines.append(
            "- ⚠️ **not publishable** — the machine was not in a comparable state "
            "(see `meta.json`)"
        )
    lines.append("")
    if unstable:
        lines.append(
            f"⚠️ {len(unstable)} cell(s) varied by more than 5% across runs and are "
            "not stable enough to rank: "
            + ", ".join(f"`{r['impl']}/{r['scenario']}`" for r in unstable)
        )
        lines.append("")
    if rejected:
        total = sum(len(v) for v in rejected.values())
        lines.append(f"⚠️ {total} run(s) were rejected and excluded from the medians:")
        for (impl, scenario), reasons in sorted(rejected.items()):
            lines.append(f"- `{impl}/{scenario}` — {'; '.join(reasons)}")
        lines.append("")

    # Grouped by tier and never ranked across tiers (SPEC.md 1.1, 7.1).
    for tier in ("hand-rolled", "runtime"):
        tier_rows = [r for r in rows if r["http_layer"] == tier]
        if not tier_rows:
            continue
        title, blurb = TIER_TITLE[tier]
        lines.append(f"### {title}")
        lines.append("")
        lines.append(blurb)
        lines.append("")
        lines.extend(tier_table(tier_rows))
        lines.append("")

    other = [r for r in rows if r["http_layer"] not in TIER_TITLE]
    if other:
        lines.append("### Unclassified")
        lines.append("")
        lines.append(
            "These declare no valid `HTTP_LAYER` and cannot be grouped; treat them "
            "as unreliable (SPEC.md 5.1)."
        )
        lines.append("")
        lines.extend(tier_table(other))
        lines.append("")

    lines.append(
        "`req/s` is the median of the runs and describes **closed-loop maximum "
        "throughput**; the p50/p99 columns describe queueing at that saturation "
        "point, not latency at a target rate. `CPU ms/req` is the most portable "
        "number here — it is how much CPU the server spends per request, "
        "independent of how many cores it had. Peak RSS is not comparable across "
        "runtimes, and `binary` is blank for interpreted implementations. See "
        "`docs/methodology.md`."
    )
    return "\n".join(lines)


def rel(path: Path) -> str:
    """Path relative to the repo when it is inside it; absolute otherwise
    (`--out /tmp/...` is legitimate)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def update_readme(table: str) -> bool:
    path = ROOT / "README.md"
    text = path.read_text()
    start = "<!-- BENCH-TABLE:START"
    end = "<!-- BENCH-TABLE:END -->"
    i = text.find(start)
    j = text.find(end)
    if i == -1 or j == -1:
        print("  README.md has no BENCH-TABLE markers; skipping", file=sys.stderr)
        return False
    i_line_end = text.index("-->", i) + len("-->")
    new = text[:i_line_end] + "\n" + table + "\n\n" + text[j:]
    path.write_text(new)
    print(f"  updated {rel(path)}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dir", required=True, help="results directory")
    ap.add_argument(
        "--update-readme",
        action="store_true",
        help="splice the table into README.md between the BENCH-TABLE markers",
    )
    args = ap.parse_args()

    out_dir = Path(args.dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    if not (out_dir / "runs.jsonl").exists():
        print(f"no runs.jsonl in {out_dir}", file=sys.stderr)
        return 1

    meta = {}
    if (out_dir / "meta.json").exists():
        meta = json.loads((out_dir / "meta.json").read_text())

    runs, rejected = load_runs(out_dir)
    rows = summarise(runs)
    rejected_n = sum(len(v) for v in rejected.values())

    # summary.csv — one row per (impl, scenario), medians across runs.
    fields = [
        "impl",
        "http_layer",
        "runtime",
        "scenario",
        "runs",
        "connections",
        "rps",
        "rps_spread_pct",
        "p50",
        "p75",
        "p90",
        "p99",
        "peak_rss_kb",
        "cpu_ms_per_req",
        "binary_bytes",
        "loc",
    ]
    csv_path = out_dir / "summary.csv"
    with open(csv_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    k: (
                        ""
                        if row.get(k) is None
                        else round(row[k], 4)
                        if isinstance(row.get(k), float)
                        else row.get(k)
                    )
                    for k in fields
                }
            )

    table = markdown(rows, meta, rejected)
    (out_dir / "RESULTS.md").write_text(f"# Results — {out_dir.name}\n\n{table}\n")

    note = f", {rejected_n} run(s) rejected" if rejected_n else ""
    print(f"  {len(rows)} cells{note} -> {rel(csv_path)}")
    if args.update_readme:
        update_readme(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
