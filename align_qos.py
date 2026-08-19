#!/usr/bin/env python3
"""
align_qos.py — selaraskan cuplikan QoS (waktu dinding) dengan window label (waktu media)
-----------------------------------------------------------------------------------------
MASALAH yang dipecahkan:
  Agen QoS mencuplik menurut WAKTU DINDING. Labeler membentuk window menurut
  WAKTU MEDIA. Saat terjadi stalling, waktu media tertinggal dari waktu dinding,
  sehingga window ke-N milik agen TIDAK menunjuk potongan video yang sama dengan
  window ke-N milik labeler. Pada skenario jitter (S3) pergeserannya bisa >1 window.

PEMETAAN yang dipakai:
  wall(m) = playback_start_epoch + m + jumlah durasi stall yang posisinya < m

  Stall pada posisi 0 (buffering awal) terjadi SEBELUM pemutaran dimulai,
  sehingga sudah tercakup di playback_start_epoch dan tidak dijumlahkan lagi.

Pakai:
  python align_qos.py client_metadata.json qos_samples.csv
  python align_qos.py client_metadata.json qos_samples.csv --window 10 --out fitur.csv

Uji tanpa data nyata:
  python align_qos.py --self-test
"""
import argparse
import csv
import json
import math
import sys

FIELDS = ["run_id", "window_index", "t_media_start", "t_wall_start", "t_wall_end",
          "throughput_mean", "throughput_std", "throughput_min", "throughput_max",
          "jitter_mean", "jitter_p95", "reorder_rate", "reorder_count",
          "total_bytes", "total_packets", "active_flows", "n_samples"]


def media_to_wall(m, playback_start, stalls):
    """Ubah waktu media (detik sejak pemutaran mulai) menjadi epoch waktu dinding."""
    tambahan = sum(d for (p, d) in stalls if 0 < p < m)
    return playback_start + m + tambahan


def load_stalls(meta):
    """[(posisi_media, durasi_detik), ...]"""
    return [(float(s["position"]), float(s["duration"])) for s in meta.get("stalls", [])]


def p95(v):
    if not v:
        return 0.0
    v = sorted(v)
    i = min(len(v) - 1, int(math.ceil(0.95 * len(v))) - 1)
    return v[max(i, 0)]


def summarize(rows):
    """rows = list dict cuplikan mentah -> fitur QoS per window.

    bytes/packets/retrans bersifat KUMULATIF di kernel sehingga cuplikan sudah
    berisi selisih; jitter adalah LEVEL (EWMA) sehingga nilainya dipakai apa adanya.
    """
    if not rows:
        return None
    mbps, jit, tb, tp, tr, af = [], [], 0, 0, 0, 0
    for r in rows:
        dt = float(r["dt"])
        b = int(r["delta_bytes"])
        if dt > 0:
            mbps.append(b * 8.0 / (dt * 1e6))
        tb += b
        tp += int(r["delta_packets"])
        tr += int(r.get("delta_reorder", r.get("delta_retrans", 0)) or 0)
        jit.append(float(r.get("jitter_ns", 0) or 0) / 1e6)      # ns -> ms
        af = max(af, int(r["active_flows"]))
    if not mbps:
        return None
    n = len(mbps)
    mean = sum(mbps) / n
    var = sum((x - mean) ** 2 for x in mbps) / n
    return {
        "throughput_mean": round(mean, 6),
        "throughput_std": round(math.sqrt(var), 6),
        "throughput_min": round(min(mbps), 6),
        "throughput_max": round(max(mbps), 6),
        "jitter_mean": round(sum(jit) / len(jit), 6) if jit else 0.0,
        "jitter_p95": round(p95(jit), 6),
        # BUKAN packet loss: gabungan reordering + retransmisi
        "reorder_rate": round(tr / tp * 100.0, 6) if tp else 0.0,
        "reorder_count": tr,
        "total_bytes": tb, "total_packets": tp,
        "active_flows": af, "n_samples": n,
    }


def align(meta, samples, window=10.0):
    """Bentuk window selaras-media dari cuplikan mentah."""
    run_id = meta.get("run_id", "run")
    ps = meta.get("playback_start_epoch")
    if not ps:
        raise ValueError("client_metadata.json tidak punya 'playback_start_epoch' "
                         "(perbarui harness.js lalu rekam ulang)")
    stalls = load_stalls(meta)
    dur = float(meta.get("media_duration", 0.0))
    n_win = max(1, math.ceil(dur / window)) if dur > 0 else 1

    smp = sorted(samples, key=lambda r: float(r["t_epoch"]))
    out = []
    for w in range(n_win):
        m0, m1 = w * window, (w + 1) * window
        t0 = media_to_wall(m0, ps, stalls)
        t1 = media_to_wall(m1, ps, stalls)
        # cuplikan diberi cap pada AKHIR intervalnya; masuk window bila (t-dt, t] beririsan
        dalam = [r for r in smp
                 if float(r["t_epoch"]) > t0 and (float(r["t_epoch"]) - float(r["dt"])) < t1]
        s = summarize(dalam)
        row = {"run_id": run_id, "window_index": w,
               "t_media_start": round(m0, 1),
               "t_wall_start": round(t0, 3), "t_wall_end": round(t1, 3)}
        row.update(s or {k: 0 for k in FIELDS[5:]})
        out.append(row)
    return out


# ---------------- uji mandiri ----------------
def self_test():
    # pemetaan dasar tanpa stall
    assert media_to_wall(0, 1000.0, []) == 1000.0
    assert media_to_wall(10, 1000.0, []) == 1010.0
    print("  [OK] pemetaan media->wall tanpa stall")

    # stall awal (posisi 0) TIDAK menggeser: sudah tercakup di playback_start
    st = [(0.0, 5.0)]
    assert media_to_wall(10, 1000.0, st) == 1010.0
    print("  [OK] stall awal (posisi 0) tidak digeser dua kali")

    # stall di tengah menggeser window sesudahnya
    st = [(0.0, 2.0), (15.0, 3.0), (35.0, 4.0)]
    assert media_to_wall(10, 1000.0, st) == 1010.0          # sebelum stall pertama
    assert media_to_wall(20, 1000.0, st) == 1023.0          # +3 dari stall @15
    assert media_to_wall(40, 1000.0, st) == 1047.0          # +3 +4
    print("  [OK] stall di tengah menggeser waktu dinding dengan benar")

    # penyelarasan end-to-end: trafik ada HANYA pada window media ke-2
    ps = 1000.0
    meta = {"run_id": "T", "media_duration": 40.0, "playback_start_epoch": ps,
            "stalls": [{"position": 0.0, "duration": 2.0},
                       {"position": 15.0, "duration": 3.0}]}
    # window media 20-30 -> wall 1023-1033 (karena stall 3 detik di media 15)
    samples = []
    for t in range(1000, 1050):
        aktif = 1 if 1023 <= t < 1033 else 0
        samples.append({"run_id": "T", "t_epoch": float(t + 1), "dt": "1.0",
                        "delta_bytes": 1_250_000 * aktif, "delta_packets": 830 * aktif,
                        "delta_reorder": 4 * aktif, "jitter_ns": 2_000_000 * aktif,
                        "active_flows": aktif})
    rows = align(meta, samples, window=10.0)
    got = [r["window_index"] for r in rows if r["throughput_mean"] > 1.0]
    assert got == [2], f"window bertrafik seharusnya [2], dapat {got}"
    assert abs(rows[2]["throughput_mean"] - 10.0) < 0.2, rows[2]
    assert abs(rows[2]["jitter_mean"] - 2.0) < 0.01, rows[2]["jitter_mean"]
    assert abs(rows[2]["reorder_rate"] - 4 / 830 * 100) < 0.01, rows[2]["reorder_rate"]
    print(f"  [OK] penyelarasan: trafik jatuh tepat di window {got[0]} "
          f"({rows[2]['throughput_mean']:.2f} Mbps, jitter {rows[2]['jitter_mean']:.2f} ms, "
          f"reorder {rows[2]['reorder_rate']:.2f}%)")

    # TANPA koreksi stall, trafik akan salah jatuh ke window lain
    meta_tanpa = dict(meta); meta_tanpa["stalls"] = []
    rows2 = align(meta_tanpa, samples, window=10.0)
    got2 = [r["window_index"] for r in rows2 if r["throughput_mean"] > 1.0]
    assert got2 != [2], "seharusnya berbeda bila stall diabaikan"
    print(f"  [OK] bukti pentingnya koreksi: tanpa stall -> window {got2} (salah)")

    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Selaraskan cuplikan QoS dengan window label")
    ap.add_argument("metadata", nargs="?", help="client_metadata.json dari harness")
    ap.add_argument("samples", nargs="?", help="qos_samples.csv dari qos_agent")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--out", default=None, help="default: <run_id>_qos_aligned.csv")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not a.metadata or not a.samples:
        ap.error("butuh <client_metadata.json> dan <qos_samples.csv>")

    with open(a.metadata, encoding="utf-8") as f:
        meta = json.load(f)
    with open(a.samples, encoding="utf-8") as f:
        smp = [r for r in csv.DictReader(f)]

    run_id = meta.get("run_id", "run")
    smp = [r for r in smp if r.get("run_id") == run_id] or smp
    rows = align(meta, smp, a.window)

    out = a.out or f"{run_id}_qos_aligned.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader(); w.writerows(rows)

    kosong = sum(1 for r in rows if r["n_samples"] == 0)
    stalls = load_stalls(meta)
    geser = sum(d for (p, d) in stalls if p > 0)
    print(f"[{run_id}] {len(rows)} window -> {out}")
    print(f"  pergeseran akibat stall: {geser:.3f} detik "
          f"({geser / a.window:.2f} window)")
    if kosong:
        print(f"  PERINGATAN: {kosong} window tanpa cuplikan QoS "
              f"(agen mungkin berhenti sebelum pemutaran selesai)")


if __name__ == "__main__":
    main()