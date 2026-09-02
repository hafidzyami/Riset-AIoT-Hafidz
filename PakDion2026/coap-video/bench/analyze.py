#!/usr/bin/env python3
"""
analyze.py -- turn the run_matrix.sh CSVs into comparison tables and plots.

    python bench/analyze.py results/e1_resolution.csv [more.csv ...]

Tables go to stdout (and to results/report.md); plots are written next to the
inputs when matplotlib is available.  Only the standard library is required for
the tables.
"""

import csv
import os
import sys
from collections import OrderedDict

BASE, TEST = "rfc7959", "rfc9177"

# metric -> (label, better direction)
METRICS = OrderedDict([
    ("fps",          ("frame rate (fps)",        "higher")),
    ("goodput_mbps", ("goodput (Mbit/s)",        "higher")),
    ("lat_p50",      ("latency p50 (ms)",        "lower")),
    ("lat_p95",      ("latency p95 (ms)",        "lower")),
    ("dg_per_frame", ("UDP datagrams / frame",   "lower")),
    ("dg_out",       ("client transmissions",    "lower")),
    ("srv_cpu_s",    ("server CPU (s)",          "lower")),
    ("bad",          ("corrupt frames",          "lower")),
    ("timeout",      ("timed-out frames",        "lower")),
])


def load(paths):
    rows = []
    for p in paths:
        with open(p, newline="") as f:
            for r in csv.DictReader(f):
                if not r.get("mechanism"):
                    continue
                r["_src"] = os.path.basename(p)
                rows.append(r)
    return rows


def num(r, k):
    try:
        return float(r[k])
    except (TypeError, ValueError, KeyError):
        return float("nan")


AXIS_LABELS = {
    "resolution":   "resolution",
    "block_size":   "block size (B)",
    "loss_pct":     "packet loss (%)",
    "max_payloads": "MAX_PAYLOADS",
    "label":        "configuration",
}


def axis_of(rows):
    """Pick the column that actually varies -- that is the x axis.

    Files differ in shape (E1-E3 sweep resolution/block/loss, E4 sweeps
    MAX_PAYLOADS, E5 sweeps a named set of timer settings), so only consider
    columns that are actually present."""
    present = [c for c in AXIS_LABELS if c in rows[0]]
    for col in ("label", "max_payloads", "loss_pct", "block_size", "resolution"):
        if col in present and len({r[col] for r in rows}) > 1:
            return col
    return present[0] if present else None


def ratio(base, test, direction):
    """Improvement factor of test over base, >1 always meaning 'test wins'.

    Returns None when the comparison is not meaningful -- notably 0 vs 0, which
    is a tie (both mechanisms perfect), not an infinite win."""
    if base != base or test != test:          # NaN
        return None
    if base == 0 and test == 0:
        return None                            # tie, e.g. zero corrupt frames
    if direction == "higher":
        return test / base if base else float("inf")
    return base / test if test else float("inf")


def fmt(v, nd=2):
    if v is None:
        return "n/a"
    if v != v:
        return "n/a"
    if v == float("inf"):
        return "inf"
    return f"{v:.{nd}f}"


def report(rows, out):
    axis = axis_of(rows)
    # keep first-seen order of the axis values
    keys, seen = [], set()
    for r in rows:
        if r[axis] not in seen:
            seen.add(r[axis])
            keys.append(r[axis])

    def pick(mech, key):
        got = [r for r in rows if r["mechanism"] == mech and r[axis] == key]
        return got[0] if got else None

    label = AXIS_LABELS.get(axis, axis)
    out.append(f"\n### Sweep over {label}\n")
    hdr = f"| metric | {' | '.join(str(k) for k in keys)} |"
    out.append(hdr)
    out.append("|" + "---|" * (len(keys) + 1))

    # Pairwise ratios only make sense when both mechanisms were measured at the
    # *same* axis value.  That is false for E4 (one mechanism throughout) and
    # for E5, where the axis is itself the configuration and each point has a
    # single mechanism.  In those cases emit a flat table instead, naming the
    # mechanism per column.
    paired = [k for k in keys if pick(BASE, k) and pick(TEST, k)]
    if not paired:
        by_key = {}
        for k in keys:
            row = next((r for r in rows if r[axis] == k), None)
            by_key[k] = row
        out.append("| *mechanism* | "
                   + " | ".join(by_key[k]["mechanism"] if by_key[k] else "n/a"
                                for k in keys) + " |")
        for m, (mlabel, _dir) in METRICS.items():
            if m not in rows[0]:
                continue
            cells = [fmt(num(by_key[k], m)) if by_key[k] else "n/a"
                     for k in keys]
            out.append(f"| {mlabel} | {' | '.join(cells)} |")
        return axis, keys

    for m, (mlabel, direction) in METRICS.items():
        if m not in rows[0]:
            continue
        for mech in (BASE, TEST):
            cells = []
            for k in keys:
                r = pick(mech, k)
                cells.append(fmt(num(r, m)) if r else "n/a")
            out.append(f"| {mlabel} - {mech} | {' | '.join(cells)} |")
        cells = []
        for k in keys:
            rb, rt = pick(BASE, k), pick(TEST, k)
            r = ratio(num(rb, m), num(rt, m), direction) if (rb and rt) else None
            if r is None:
                cells.append("tie" if rb and rt else "n/a")
            elif r == float("inf"):
                cells.append("**9177 only**")
            else:
                cells.append("**" + fmt(r) + "x**")
        out.append(f"| *{mlabel} - improvement* | {' | '.join(cells)} |")
    return axis, keys


def plot(rows, axis, keys, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib not available, skipping plots)")
        return

    wanted = [("fps", "frame rate (fps)"),
              ("lat_p50", "latency p50 (ms)"),
              ("dg_per_frame", "UDP datagrams / frame"),
              ("srv_cpu_s", "server CPU (s)"),
              ("lat_p99", "latency p99 (ms)"),
              ("timeout", "timed-out frames")]
    panels = [p for p in wanted if p[0] in rows[0]][:4]
    if not panels:
        return

    mechs = [m for m in (BASE, TEST) if any(r["mechanism"] == m for r in rows)]
    x = range(len(keys))
    fig, axes = plt.subplots(2, 2, figsize=(11, 7))
    colors = {BASE: "#c44e52", TEST: "#4c72b0"}

    for ax, (m, title) in zip(axes.flat, panels):
        for mech in mechs:
            ys = []
            for k in keys:
                got = [r for r in rows
                       if r["mechanism"] == mech and r[axis] == k]
                ys.append(num(got[0], m) if got else float("nan"))
            ax.plot(list(x), ys, "o-", label=mech, color=colors[mech], lw=2)
        ax.set_title(title, fontsize=10)
        ax.set_xticks(list(x))
        ax.set_xticklabels([str(k) for k in keys], fontsize=8)
        ax.grid(alpha=.3)
        ax.legend(fontsize=8)

    fig.suptitle("CoAP live video: RFC 7959 vs RFC 9177 - by "
                 + AXIS_LABELS.get(axis, axis))
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    print(f"plot -> {path}")


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1

    lines = ["# CoAP live video: RFC 7959 vs RFC 9177 - measured results",
             "",
             "Improvement rows are stated so that **>1x always means RFC 9177 "
             "did better**, whichever direction the metric runs in."]

    for path in argv[1:]:
        rows = load([path])
        if not rows:
            continue
        if axis_of(rows) is None:
            continue
        lines.append(f"\n## {os.path.basename(path)}")
        axis, keys = report(rows, lines)
        plot(rows, axis, keys,
             os.path.join(os.path.dirname(path) or ".",
                          os.path.basename(path).replace(".csv", ".png")))

    text = "\n".join(lines) + "\n"
    print(text)
    out = os.path.join(os.path.dirname(argv[1]) or ".", "report.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"report -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
