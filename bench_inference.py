#!/usr/bin/env python3
"""
bench_inference.py — ukur latensi & memori inferensi DI RASPBERRY PI 5
-----------------------------------------------------------------------
Latensi yang diukur di laptop tidak sah untuk RQ; angka yang dilaporkan harus
berasal dari perangkat tempat sistem benar-benar berjalan.

Diukur untuk tiap model:
  - latensi per sampel pada inferensi SATU window (kasus nyata Fase 2)
  - latensi per sampel pada inferensi batch (pembanding, lebih optimistis)
  - ukuran berkas model
  - tambahan memori residen saat model dimuat

Pakai (di Raspberry Pi 5):
  python3 bench_inference.py --model-dir model --dataset dataset.csv
  python3 bench_inference.py --model-dir model --n 2000 --ulang 5
"""
import argparse
import csv
import glob
import os
import statistics as st
import sys
import time

import numpy as np

DASAR = ["throughput_mean", "throughput_std", "throughput_min", "throughput_max",
         "total_bytes", "total_packets", "active_flows"]
JR = ["jitter_mean", "jitter_p95", "reorder_rate", "reorder_count"]
RTT = ["rtt_mean", "rtt_p95", "rtt_std"]
SET_FITUR = {"dasar": DASAR, "jr": DASAR + JR, "rtt": DASAR + RTT,
             "semua": DASAR + JR + RTT}


def rss_kb():
    try:
        for ln in open("/proc/self/status"):
            if ln.startswith("VmRSS:"):
                return int(ln.split()[1])
    except Exception:
        pass
    return 0


def muat_fitur(path, feats, n):
    baris = [r for r in csv.DictReader(open(path, encoding="utf-8"))
             if float(r["throughput_mean"]) >= 0.01]
    X = np.array([[float(r[f]) for f in feats] for r in baris[:n]])
    for i, f in enumerate(feats):
        if f.startswith("rtt"):
            X[:, i] = np.clip(X[:, i], 0, 2000.0)
    return X


def main():
    ap = argparse.ArgumentParser(description="Benchmark inferensi di perangkat edge")
    ap.add_argument("--model-dir", default="model")
    ap.add_argument("--dataset", default=None,
                    help="opsional; bila tidak ada, dipakai fitur acak")
    ap.add_argument("--features", default="rtt", choices=list(SET_FITUR))
    ap.add_argument("--n", type=int, default=1000, help="jumlah sampel untuk batch")
    ap.add_argument("--ulang", type=int, default=5)
    a = ap.parse_args()

    import joblib
    import som_model  # noqa: F401  (wajib agar model SOM bisa di-unpickle)

    feats = SET_FITUR[a.features]
    if a.dataset and os.path.exists(a.dataset):
        X = muat_fitur(a.dataset, feats, a.n)
        sumber = os.path.basename(a.dataset)
    else:
        rng = np.random.default_rng(0)
        X = rng.random((a.n, len(feats))) * np.array([5, 2, 1, 20, 5e6, 4000, 2,
                                                     200, 300, 30])[:len(feats)]
        sumber = "acak"
    print(f"perangkat: {os.uname().machine} | fitur: {a.features} ({len(feats)}) "
          f"| sampel: {len(X)} dari {sumber}\n")

    berkas = sorted(glob.glob(os.path.join(a.model_dir, "*.joblib")))
    if not berkas:
        sys.exit(f"tidak ada *.joblib di {a.model_dir}")

    print(f"{'model':<20}{'ukuran KB':>11}{'RSS +KB':>9}"
          f"{'1 window us':>13}{'batch us':>10}{'p95 us':>9}")
    print("-" * 72)
    hasil = []
    for p in berkas:
        nama = os.path.basename(p).replace(".joblib", "")
        kb = os.path.getsize(p) / 1024.0
        r0 = rss_kb()
        est = joblib.load(p)
        dr = max(0, rss_kb() - r0)

        est.predict(X[:5])                              # pemanasan

        # Kasus NYATA Fase 2: satu window pada satu waktu
        satu = []
        for i in range(min(200, len(X))):
            x = X[i:i + 1]
            t0 = time.perf_counter()
            est.predict(x)
            satu.append((time.perf_counter() - t0) * 1e6)

        # Pembanding: batch (lebih optimistis, bukan pola pemakaian nyata)
        t0 = time.perf_counter()
        for _ in range(a.ulang):
            est.predict(X)
        batch = (time.perf_counter() - t0) / a.ulang / len(X) * 1e6

        med = st.median(satu)
        p95 = sorted(satu)[int(0.95 * len(satu)) - 1]
        print(f"{nama:<20}{kb:>11.1f}{dr:>9}{med:>13.1f}{batch:>10.2f}{p95:>9.1f}")
        hasil.append({"model": nama, "ukuran_kb": round(kb, 1), "rss_kb": dr,
                      "latensi_1window_us": round(med, 1),
                      "latensi_batch_us": round(batch, 2),
                      "latensi_p95_us": round(p95, 1)})

    out = os.path.join(a.model_dir, "bench_inference.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(hasil[0].keys()))
        w.writeheader()
        w.writerows(hasil)
    print(f"\n-> {out}")
    print("\nAngka '1 window' adalah yang relevan untuk Fase 2, karena inferensi")
    print("dilakukan satu window tiap 10 detik, bukan dalam batch besar.")
    print("Bandingkan dengan target rancangan: <2 ms (SOM) dan 5-25 ms (SVM).")


if __name__ == "__main__":
    main()