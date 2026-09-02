#!/usr/bin/env python3
"""
compare_baseline.py — apakah model ML MENGALAHKAN baseline naif?
------------------------------------------------------------------
Membandingkan tiga hal terhadap label sebenarnya (P.1203_full):

  1. Baseline naif   : manifest + heuristik atas QoS  -> P.1203_degraded
  2. Model ML        : HANYA fitur QoS                -> SVM RBF, group-split
  3. Tebak mayoritas : pembanding paling dasar

Inilah bukti utama untuk klaim "information gap compensation". Perlu dicatat
baseline justru memegang informasi LEBIH BANYAK (manifest), sehingga
mengalahkannya bermakna.

Pakai:
  python compare_baseline.py --dir hasil_v3
  python compare_baseline.py --dir hasil_v3 --features rtt
"""
import argparse
import csv
import os
import sys
from collections import Counter

import warnings

import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.metrics import confusion_matrix, f1_score
from sklearn.model_selection import GroupKFold, cross_val_predict
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

warnings.filterwarnings("ignore", message=".*ill-defined.*")

KELAS = ["Excellent", "Good", "Degraded", "Critical"]
DASAR = ["throughput_mean", "throughput_std", "throughput_min", "throughput_max",
         "total_bytes", "total_packets", "active_flows"]
JR = ["jitter_mean", "jitter_p95", "reorder_rate", "reorder_count"]
RTT = ["rtt_mean", "rtt_p95", "rtt_std"]
SET = {"dasar": DASAR, "jr": DASAR + JR, "rtt": DASAR + RTT, "semua": DASAR + JR + RTT}


def main():
    ap = argparse.ArgumentParser(description="Bandingkan ML vs baseline naif")
    ap.add_argument("--dir", default="hasil_v3")
    ap.add_argument("--dataset", default=None, help="default: <dir>/dataset.csv")
    ap.add_argument("--baseline", default=None, help="default: <dir>/baseline_degraded.csv")
    ap.add_argument("--features", default="rtt", choices=list(SET))
    ap.add_argument("--clip-rtt", type=float, default=2000.0,
                    help="batas RTT (ms); pencilan puluhan detik bukan RTT jalur nyata")
    a = ap.parse_args()

    fds = a.dataset or os.path.join(a.dir, "dataset.csv")
    fbl = a.baseline or os.path.join(a.dir, "baseline_degraded.csv")
    for p in (fds, fbl):
        if not os.path.exists(p):
            sys.exit(f"tidak ketemu: {p}")

    ds = list(csv.DictReader(open(fds, encoding="utf-8")))
    bl = {(r["run_id"], int(r["window_index"])): r["label_degraded"]
          for r in csv.DictReader(open(fbl, encoding="utf-8"))}

    # hanya window yang ADA di keduanya dan punya trafik teramati
    baris = []
    for r in ds:
        k = (r["run_id"], int(r["window_index"]))
        if k in bl and float(r["throughput_mean"]) >= 0.01:
            baris.append((r, bl[k]))
    if not baris:
        sys.exit("tidak ada window yang beririsan")

    feats = SET[a.features]
    X = np.array([[float(r[f]) for f in feats] for r, _ in baris])
    for i, f in enumerate(feats):
        if f.startswith("rtt"):
            X[:, i] = np.clip(X[:, i], 0, a.clip_rtt)
    y = np.array([r["label"] for r, _ in baris])
    g = np.array([r["run_id"] for r, _ in baris])
    yb = np.array([b for _, b in baris])

    n_grup = len(set(g))
    n_split = max(2, min(5, n_grup))          # GroupKFold butuh split <= jumlah grup
    print(f"n = {len(y)} window dari {n_grup} run | fitur: {a.features} ({len(feats)}) "
          f"| group-split {n_split}-fold\n")

    cv = GroupKFold(n_splits=n_split)
    yp = cross_val_predict(make_pipeline(StandardScaler(),
                                         SVC(kernel="rbf", C=10, class_weight="balanced")),
                           X, y, groups=g, cv=cv, n_jobs=-1)
    yd = cross_val_predict(DummyClassifier(strategy="most_frequent"),
                           X, y, groups=g, cv=cv)

    print(f"{'metode':<28}{'macro-F1':>10}{'akurasi':>10}")
    print("-" * 48)
    hasil = []
    for nama, pred in [("Tebak mayoritas", yd),
                       ("Baseline naif (P.1203_deg)", yb),
                       ("Model ML (QoS saja)", yp)]:
        f1 = f1_score(y, pred, average="macro", labels=sorted(set(y)), zero_division=0)
        hasil.append((nama, f1))
        print(f"{nama:<28}{f1:>10.3f}{(pred == y).mean():>10.3f}")

    d = hasil[2][1] - hasil[1][1]
    print(f"\n  selisih ML - baseline: {d:+.3f} "
          f"({'ML MENANG' if d > 0 else 'ML KALAH'})")
    if hasil[1][1] > 0:
        print(f"  peningkatan relatif  : {d / hasil[1][1] * 100:+.1f}%")

    print("\n=== F1 PER KELAS ===")
    hadir = [k for k in KELAS if k in set(y)]
    fb = f1_score(y, yb, average=None, labels=hadir, zero_division=0)
    fm = f1_score(y, yp, average=None, labels=hadir, zero_division=0)
    print(f"{'kelas':<12}{'baseline':>10}{'ML':>10}{'selisih':>10}")
    for k, b, m in zip(hadir, fb, fm):
        print(f"{k:<12}{b:>10.3f}{m:>10.3f}{m - b:>+10.3f}")

    print("\n=== DISTRIBUSI PREDIKSI ===")
    fmt = lambda c: {str(k): v for k, v in sorted(c.items())}
    print(f"  sebenarnya : {fmt(Counter(y))}")
    print(f"  baseline   : {fmt(Counter(yb))}")
    print(f"  ML         : {fmt(Counter(yp))}")

    print("\n=== CONFUSION BASELINE NAIF (baris=asli, kolom=tebakan) ===")
    cm = confusion_matrix(y, yb, labels=hadir)
    print(f"  {'':<11}" + "".join(f"{c[:9]:>11}" for c in hadir))
    for i, k in enumerate(hadir):
        print(f"  {k:<11}" + "".join(f"{cm[i][j]:>11}" for j in range(len(hadir))))


if __name__ == "__main__":
    main()