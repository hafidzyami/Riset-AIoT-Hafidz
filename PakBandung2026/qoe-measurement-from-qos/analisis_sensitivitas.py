#!/usr/bin/env python3
"""
analisis_sensitivitas.py — dua analisis wajib untuk naskah
------------------------------------------------------------
A. SENSITIVITAS AMBANG STALLING
   Ambang stall 1,0 detik adalah parameter yang ditetapkan penelitian ini, bukan
   ketentuan ITU-T. Bahwa satu label pernah ditentukan oleh selisih 14 milidetik
   menunjukkan perlunya menguji apakah kesimpulan bergantung pada angka itu.
   Analisis ini melabeli ulang seluruh run pada beberapa ambang lalu melatih
   model yang sama, sehingga terlihat apakah performanya bergeser.

B. GRANULARITAS KELAS
   Pembagian empat kelas juga keputusan desain. Analisis ini membandingkan skema
   empat, tiga, dan dua kelas pada data yang sama.

Pakai:
  # A: butuh relabel dulu (lihat petunjuk yang dicetak skrip)
  python analisis_sensitivitas.py --mode ambang --dirs hasil_v3,sens_t05,sens_t20 \\
      --labels 1.0,0.5,2.0
  # B: langsung dari satu dataset
  python analisis_sensitivitas.py --mode granularitas --dataset hasil_v3/dataset.csv
  python analisis_sensitivitas.py --self-test
"""
import argparse
import csv
import os
import sys
import warnings
from collections import Counter

import numpy as np

warnings.filterwarnings("ignore")

from sklearn.metrics import f1_score
from sklearn.model_selection import StratifiedGroupKFold, cross_val_predict
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier

FITUR = ["throughput_mean", "throughput_std", "throughput_min", "throughput_max",
         "total_bytes", "total_packets", "active_flows"]

SKEMA = {
    "4 kelas (asli)": {"Excellent": "Excellent", "Good": "Good",
                       "Degraded": "Degraded", "Critical": "Critical"},
    "3 kelas: Exc / Good / Buruk": {"Excellent": "Excellent", "Good": "Good",
                                    "Degraded": "Buruk", "Critical": "Buruk"},
    "3 kelas: Baik / Degr / Crit": {"Excellent": "Baik", "Good": "Baik",
                                    "Degraded": "Degraded", "Critical": "Critical"},
    "2 kelas: Layak / Tidak": {"Excellent": "Layak", "Good": "Layak",
                               "Degraded": "Tidak", "Critical": "Tidak"},
}


def muat(path, min_tp=0.01):
    d = [r for r in csv.DictReader(open(path, encoding="utf-8"))
         if float(r["throughput_mean"]) >= min_tp]
    X = np.array([[float(r[f]) for f in FITUR] for r in d])
    y = np.array([r["label"] for r in d])
    g = np.array([r["run_id"] for r in d])
    return X, y, g


def buat_model(nama, seed):
    if nama == "tree":
        return DecisionTreeClassifier(max_depth=10, min_samples_leaf=20,
                                      class_weight="balanced", random_state=seed)
    return make_pipeline(StandardScaler(),
                         SVC(kernel="rbf", C=100, class_weight="balanced"))


def evaluasi(X, y, g, nama_model="tree", ulangan=5):
    sk = []
    for s in range(ulangan):
        cv = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=s)
        yp = cross_val_predict(buat_model(nama_model, s), X, y, groups=g, cv=cv, n_jobs=-1)
        sk.append(f1_score(y, yp, average="macro"))
    return float(np.mean(sk)), float(np.std(sk))


# ---------------------------------------------------------------- A
def mode_ambang(dirs, labels, ulangan):
    print("=== A. SENSITIVITAS AMBANG STALLING ===")
    print("Model: DecisionTree (7 fitur), group-split per run, "
          f"{ulangan} ulangan\n")
    print(f"{'ambang':>8}{'n window':>10}{'macro-F1':>17}  distribusi kelas")
    print("-" * 88)
    hasil = []
    for d, lab in zip(dirs, labels):
        p = os.path.join(d, "dataset.csv") if os.path.isdir(d) else d
        if not os.path.exists(p):
            print(f"{lab:>8}  LEWATI, tidak ketemu: {p}")
            continue
        X, y, g = muat(p)
        m, sd = evaluasi(X, y, g, "tree", ulangan)
        hasil.append((lab, m, sd))
        dist = {str(k): int(v) for k, v in sorted(Counter(y).items())}
        print(f"{lab:>8}{len(y):>10}{f'{m:.3f} +/- {sd:.3f}':>17}  {dist}")

    if len(hasil) >= 2:
        nilai = [h[1] for h in hasil]
        sd_maks = max(h[2] for h in hasil)
        rentang = max(nilai) - min(nilai)
        print(f"\n  rentang macro-F1 antar ambang : {rentang:.3f}")
        print(f"  simpangan baku terbesar       : {sd_maks:.3f}")
        if rentang <= 2 * sd_maks:
            print("  -> perbedaan MASIH DALAM DERAU. Kesimpulan TIDAK bergantung")
            print("     pada pemilihan ambang, dan itulah yang perlu dilaporkan.")
        else:
            print("  -> perbedaan MELAMPAUI derau. Pemilihan ambang berpengaruh")
            print("     dan wajib dijustifikasi secara eksplisit di naskah.")


# ---------------------------------------------------------------- B
def mode_granularitas(dataset, ulangan):
    X, y4, g = muat(dataset)
    print("=== B. GRANULARITAS KELAS ===")
    print(f"Data: {len(y4)} window, {len(set(g))} run, 7 fitur, "
          f"{ulangan} ulangan\n")
    print(f"{'skema':<30}{'model':<7}{'macro-F1':>17}  distribusi")
    print("-" * 92)
    for nama, peta in SKEMA.items():
        y = np.array([peta[v] for v in y4])
        dist = {str(k): int(v) for k, v in sorted(Counter(y).items())}
        for mo in ("tree", "svm"):
            m, sd = evaluasi(X, y, g, mo, ulangan)
            tail = f"  {dist}" if mo == "tree" else ""
            print(f"{nama:<30}{mo:<7}{f'{m:.3f} +/- {sd:.3f}':>17}{tail}")

    print("\n  Catatan: menyusutkan jumlah kelas menaikkan macro-F1 karena batas")
    print("  yang paling sering keliru ikut dihapus, bukan karena model membaik.")
    print("  Sajikan 4 kelas sebagai hasil utama dan sisanya sebagai analisis.")


# ---------------------------------------------------------------- uji
def self_test():
    rng = np.random.default_rng(0)
    n = 400
    X = np.zeros((n, 7))
    y = np.empty(n, dtype=object)
    g = np.empty(n, dtype=object)
    K = ["Excellent", "Good", "Degraded", "Critical"]
    for i in range(n):
        k = K[i % 4]
        c = {"Excellent": 4.0, "Good": 0.6, "Degraded": 0.33, "Critical": 0.13}[k]
        X[i] = rng.normal(c, c * 0.15, 7)
        y[i], g[i] = k, f"R{i % 20}"
    m4, _ = evaluasi(X, y, g, "tree", 2)
    y2 = np.array([SKEMA["2 kelas: Layak / Tidak"][v] for v in y])
    m2, _ = evaluasi(X, y2, g, "tree", 2)
    assert m2 >= m4 - 0.01, (m4, m2)
    print(f"  [OK] evaluasi berjalan: 4 kelas {m4:.3f}, 2 kelas {m2:.3f}")
    for nama, peta in SKEMA.items():
        assert set(peta) == set(K), nama
        print(f"  [OK] skema '{nama}' -> {len(set(peta.values()))} kelas")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Analisis sensitivitas dan granularitas")
    ap.add_argument("--mode", choices=["ambang", "granularitas"], default="granularitas")
    ap.add_argument("--dataset", default="hasil_v3/dataset.csv")
    ap.add_argument("--dirs", default="", help="untuk mode ambang, dipisah koma")
    ap.add_argument("--labels", default="", help="label tiap folder, mis. 1.0,0.5,2.0")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)

    if a.mode == "granularitas":
        if not os.path.exists(a.dataset):
            sys.exit(f"tidak ketemu: {a.dataset}")
        mode_granularitas(a.dataset, a.repeats)
    else:
        dirs = [s.strip() for s in a.dirs.split(",") if s.strip()]
        labs = [s.strip() for s in a.labels.split(",") if s.strip()] or dirs
        if not dirs:
            print("Mode ambang butuh dataset yang sudah dilabeli ulang. Jalankan dulu:\n")
            print("  python relabel_all.py --dir hasil_v3 --out-dir sens_t05 "
                  "--stall-threshold 0.5 --merge")
            print("  python relabel_all.py --dir hasil_v3 --out-dir sens_t20 "
                  "--stall-threshold 2.0 --merge\n")
            print("Lalu:\n")
            print("  python analisis_sensitivitas.py --mode ambang "
                  "--dirs hasil_v3,sens_t05,sens_t20 --labels 1.0,0.5,2.0")
            sys.exit(1)
        mode_ambang(dirs, labs, a.repeats)


if __name__ == "__main__":
    main()