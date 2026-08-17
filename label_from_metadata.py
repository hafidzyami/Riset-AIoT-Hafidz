#!/usr/bin/env python3
"""
label_from_metadata.py
----------------------
Ubah client_metadata.json (dari harness.js) menjadi:
  1) <prefix>_p1203_input.json  -> file input P.1203 (I11/I13/I23/IGen)
  2) <prefix>_labels.csv         -> label 4-kelas per window (default 10 detik)

Label memakai O22 (kualitas video per-detik, video-only ~ setara flag --only-pv)
dari implementasi resmi ITU-T P.1203, lalu dikoreksi dengan stalling per window.

Prasyarat:
  pip install "git+https://github.com/itu-p1203/itu-p1203.git"   (paket TIDAK ada di PyPI)

Contoh:
  python label_from_metadata.py client_metadata.json --device mobile
  python label_from_metadata.py client_metadata.json --device pc --ignore-initial
"""
import argparse
import csv
import json
import math
from collections import Counter

from itu_p1203 import P1203Standalone

CLASSES = ["Excellent", "Good", "Degraded", "Critical"]


def build_p1203_report(meta, device, display=None):
    """Bangun report P.1203 dari timeline kualitas + stalling di client_metadata."""
    qt = sorted(meta.get("quality_timeline", []), key=lambda e: e["t_media"])
    dur = float(meta.get("media_duration", 0.0))
    fps = float(meta.get("fps_assumed", 24))
    codec = meta.get("codec", "h264")

    # I13: tiap interval antar-perpindahan-kualitas jadi satu "segment" P.1203
    segments = []
    for i, e in enumerate(qt):
        start = float(e["t_media"])
        end = float(qt[i + 1]["t_media"]) if i + 1 < len(qt) else dur
        d = round(end - start, 3)
        if d <= 0:          # lewati interval nol/negatif (mis. dua event di t sama)
            continue
        segments.append({
            "start": round(start, 3),
            "duration": d,
            "bitrate": round(float(e["bitrate_kbps"]), 2),   # P.1203 minta kbps
            "resolution": f'{int(e["width"])}x{int(e["height"])}',
            "fps": fps,
            "codec": codec,
        })
    # Entri pertama bisa mulai > 0 bila event kualitas pertama menyala setelah
    # pemutaran dimulai. Rekatkan ke 0 agar I13 menutupi seluruh sesi tanpa celah.
    if segments and segments[0]["start"] > 0:
        segments[0]["duration"] = round(segments[0]["duration"] + segments[0]["start"], 3)
        segments[0]["start"] = 0.0

    if not segments:        # fallback aman bila timeline kosong
        segments = [{"start": 0, "duration": max(dur, 1.0),
                     "bitrate": 500, "resolution": "640x360", "fps": fps, "codec": codec}]

    # I23: stalling [posisi_media_time, durasi_detik]
    stalls = [[round(float(s["position"]), 3), round(float(s["duration"]), 3)]
              for s in meta.get("stalls", [])]

    disp = meta.get("display", {"width": 1920, "height": 1080})
    disp_str = display or f'{int(disp["width"])}x{int(disp["height"])}'
    report = {
        "I11": {"streamId": 42, "segments": []},                       # audio dikosongkan
        "I13": {"streamId": 42, "segments": segments},
        "I23": {"streamId": 42, "stalling": stalls},
        "IGen": {"device": device,
                 "displaySize": disp_str,
                 "viewingDistance": "150cm"},
    }
    return report, dur


def classify(mean_o22, stall_s):
    """Aturan pelabelan: stalling dominan; jika mulus, pakai O22 rata-rata window."""
    if stall_s > 1.0:
        return "Critical"
    if stall_s > 0.0:
        return "Degraded"
    if mean_o22 >= 4.0:
        return "Excellent"
    if mean_o22 >= 3.0:
        return "Good"
    if mean_o22 >= 2.0:
        return "Degraded"
    return "Critical"


def main():
    ap = argparse.ArgumentParser(description="client_metadata.json -> input P.1203 + label per-window")
    ap.add_argument("input", help="path ke client_metadata.json")
    ap.add_argument("--device", default="pc", choices=["pc", "mobile", "handheld"],
                    help="konteks P.1203 IGen.device (default: pc)")
    ap.add_argument("--window", type=float, default=10.0, help="lebar window dalam detik (default: 10)")
    ap.add_argument("--out-prefix", default=None, help="prefix file output (default: run_id)")
    ap.add_argument("--display", default=None,
                    help="ganti resolusi tampilan P.1203, mis. 1280x720 (default: dari client_metadata)")
    ap.add_argument("--ignore-initial", action="store_true",
                    help="abaikan stall di posisi 0 (initial loading delay)")
    a = ap.parse_args()

    with open(a.input, encoding="utf-8") as f:
        meta = json.load(f)

    run_id = meta.get("run_id", "run")
    prefix = a.out_prefix or run_id

    report, dur = build_p1203_report(meta, a.device, a.display)
    if a.ignore_initial:
        report["I23"]["stalling"] = [s for s in report["I23"]["stalling"] if s[0] != 0]

    with open(f"{prefix}_p1203_input.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    # O22: kualitas video per-detik (video-only). Tidak terpengaruh audio kosong.
    o22 = P1203Standalone(report).calculate_pv()["video"]["O22"]

    # Windowing: window w mencakup detik [w*W, (w+1)*W) pada media-time.
    W = a.window
    n_win = max(1, math.ceil(dur / W)) if dur > 0 else 1
    rows = []
    for w in range(n_win):
        t0, t1 = w * W, (w + 1) * W
        secs = [o22[s] for s in range(int(math.floor(t0)), int(math.ceil(t1))) if 0 <= s < len(o22)]
        if not secs:
            continue
        mean_o22 = sum(secs) / len(secs)
        stall_s = sum(d for (p, d) in report["I23"]["stalling"] if t0 <= p < t1)
        rows.append({
            "run_id": run_id,
            "window_index": w,
            "t_start": round(t0, 1),
            "mean_o22": round(mean_o22, 4),
            "stall_s": round(stall_s, 3),
            "label": classify(mean_o22, stall_s),
        })

    with open(f"{prefix}_labels.csv", "w", newline="", encoding="utf-8") as f:
        wtr = csv.DictWriter(f, fieldnames=["run_id", "window_index", "t_start",
                                            "mean_o22", "stall_s", "label"])
        wtr.writeheader()
        wtr.writerows(rows)

    dist = Counter(r["label"] for r in rows)
    ctx = report["IGen"]
    print(f"[{run_id}] {len(rows)} window ({W:.0f}s) -> {prefix}_labels.csv")
    print(f"          input P.1203 -> {prefix}_p1203_input.json  ({len(report['I13']['segments'])} segmen, "
          f"{len(report['I23']['stalling'])} stall)")
    print(f"          konteks P.1203  : device={ctx['device']}, display={ctx['displaySize']}, "
          f"fps={report['I13']['segments'][0]['fps']}")
    print(f"          distribusi kelas: {dict(dist)}")


if __name__ == "__main__":
    main()