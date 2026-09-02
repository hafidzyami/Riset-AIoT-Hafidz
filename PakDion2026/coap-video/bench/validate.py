#!/usr/bin/env python3
"""
validate.py -- experimental-validity checks on the benchmark CSVs.

    python bench/validate.py results/*.csv

The two arms of each comparison run back to back against a *live* camera, so
the scene can drift between them.  A JPEG of a noisier or busier scene is
simply bigger, and a bigger body needs more blocks -- which would show up as a
protocol difference that is really a lighting difference.

These checks catch that, plus the other ways a run can be quietly invalid:

  1. body size parity     both mechanisms must have moved comparable frames
  2. no corrupt bodies    reassembly must never produce a torn JPEG
  3. block arithmetic     datagrams/frame must match the mechanism's model
  4. sample size          enough recorded frames for the percentiles to mean
                          anything

Exit status is non-zero if any check fails, so this can gate a result set.
"""

import csv
import glob
import os
import sys

SIZE_TOLERANCE_PCT = 10.0
MIN_FRAMES = 20

AXES = ("resolution", "block_size", "loss_pct", "max_payloads", "label")


def axis_of(rows):
    present = [c for c in AXES if c in rows[0]]
    for col in ("label", "max_payloads", "loss_pct", "block_size", "resolution"):
        if col in present and len({r[col] for r in rows}) > 1:
            return col
    return present[0] if present else None


def fnum(r, k, default=0.0):
    try:
        return float(r[k])
    except (TypeError, ValueError, KeyError):
        return default


def check_file(path):
    rows = [r for r in csv.DictReader(open(path, newline="")) if r.get("mechanism")]
    if not rows:
        return []
    axis = axis_of(rows)
    if axis is None:
        return []

    problems = []
    name = os.path.basename(path)

    # ---- 2. corrupt bodies, 4. sample size ------------------------------
    for r in rows:
        if fnum(r, "bad") > 0:
            problems.append(
                f"{name}: {r['mechanism']} @ {r[axis]} produced "
                f"{int(fnum(r,'bad'))} corrupt bodies -- reassembly is wrong")
        if fnum(r, "frames") < MIN_FRAMES:
            problems.append(
                f"{name}: {r['mechanism']} @ {r[axis]} recorded only "
                f"{int(fnum(r,'frames'))} frames (< {MIN_FRAMES})")

    # ---- 1. body size parity between the two arms -----------------------
    for key in dict.fromkeys(r[axis] for r in rows):
        per = {}
        for r in rows:
            if r[axis] != key:
                continue
            ok = max(fnum(r, "ok"), 1.0)
            per[r["mechanism"]] = fnum(r, "bytes") / ok
        if len(per) == 2 and all(per.values()):
            a = per.get("rfc7959")
            b = per.get("rfc9177")
            if a and b:
                diff = (b - a) / a * 100.0
                if abs(diff) > SIZE_TOLERANCE_PCT:
                    problems.append(
                        f"{name}: @ {key} the two arms carried different frame "
                        f"sizes ({a:.0f} vs {b:.0f} B/frame, {diff:+.0f}%) -- "
                        f"the scene drifted between runs, so throughput there "
                        f"is not a like-for-like comparison")

    # ---- 5. is the frame rate dominated by a handful of stalls? ---------
    # A timed-out frame contributes its whole deadline to the wall clock, so a
    # single stall can halve the reported fps while the median latency stays
    # healthy.  Where that happens, latency is the trustworthy metric and fps
    # is not.
    for r in rows:
        to = fnum(r, "timeout")
        secs = fnum(r, "secs")
        frames = fnum(r, "frames")
        lat_mean = fnum(r, "lat_mean")
        if to <= 0 or secs <= 0 or frames <= 0:
            continue
        productive = frames * lat_mean / 1000.0     # time actually transferring
        if productive < 0.5 * secs:
            problems.append(
                f"{name}: {r['mechanism']} @ {r[axis]} spent only "
                f"{productive:.1f}s of {secs:.1f}s transferring -- "
                f"{int(to)} timed-out frame(s) dominate the wall clock, so "
                f"fps={fnum(r,'fps'):.2f} understates it; use latency instead")

    # ---- 6. did the scene hold still across the whole sweep? ------------
    # Only resolution legitimately changes the body size; along every other
    # axis all rows should have seen comparable frames.  A wide spread means
    # the lighting moved during the sweep, so rows are not comparable to each
    # other (and absolute numbers are not comparable to another run).
    if axis != "resolution":
        sizes = [fnum(r, "bytes") / max(fnum(r, "ok"), 1.0) for r in rows]
        sizes = [s for s in sizes if s > 0]
        if len(sizes) > 1:
            lo, hi = min(sizes), max(sizes)
            if lo and (hi - lo) / lo > 0.25:
                problems.append(
                    f"{name}: frame size drifted {lo:.0f}..{hi:.0f} B across "
                    f"the sweep ({(hi-lo)/lo*100:.0f}%) -- the scene changed "
                    f"mid-experiment, so rows are only loosely comparable")

    # ---- 3. datagrams per frame vs the protocol model -------------------
    # RFC 7959 is lock-step: one request and one response per block, ~2N.
    # RFC 9177 bursts: one request and N responses, ~N+1.
    for r in rows:
        blk = fnum(r, "block_size")
        ok = max(fnum(r, "ok"), 1.0)
        body = fnum(r, "bytes") / ok
        dgpf = fnum(r, "dg_per_frame")
        if not (blk and body and dgpf) or fnum(r, "timeout") > 0:
            continue                      # aborted transfers skew the count
        n = body / blk
        expect = 2 * n if r["mechanism"] == "rfc7959" else n + 1
        if expect and abs(dgpf - expect) / expect > 0.35:
            problems.append(
                f"{name}: {r['mechanism']} @ {r[axis]} sent {dgpf:.1f} "
                f"datagrams/frame, model predicts {expect:.1f} "
                f"({n:.0f} blocks) -- unexpected protocol behaviour")
    return problems


def main(argv):
    paths = []
    for a in argv[1:]:
        paths.extend(sorted(glob.glob(a)) or [a])
    if not paths:
        print(__doc__)
        return 1

    all_problems = []
    for p in paths:
        if not os.path.exists(p):
            continue
        all_problems += check_file(p)

    if not all_problems:
        print(f"OK: {len(paths)} file(s) passed all validity checks")
        return 0

    print(f"{len(all_problems)} issue(s) found:\n")
    for p in all_problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
