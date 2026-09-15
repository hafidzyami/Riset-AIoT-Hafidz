#!/usr/bin/env python3
"""
latih_final.py — latih model terpilih pada SELURUH data, untuk deployment
----------------------------------------------------------------------------
Berbeda dengan train_compare.py yang membandingkan model lewat validasi silang,
skrip ini melatih SATU model pada seluruh data untuk dipakai di Fase 2.

Pemisahan ini disengaja. Angka yang dilaporkan di naskah harus berasal dari
validasi silang, sedangkan model yang diterapkan sebaiknya memakai seluruh data
yang ada. Mencampur keduanya, yaitu melaporkan skor model yang dilatih pada
seluruh data, adalah kebocoran yang jelas.

Penyetelan hiperparameter memakai group-split per run_id, sehingga window dari
run yang sama tidak terpecah antara latih dan validasi.

Pakai:
  python latih_final.py --dataset hasil_v5/dataset_ms.csv --features lengkap \\
      --model "SVM RBF" --out-dir model_v5
  python latih_final.py --dataset hasil_v5/dataset_ms.csv --features lengkap --semua
  python latih_final.py --self-test
"""
import argparse
import csv
import json
import os
import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")

DASAR = ["throughput_mean", "throughput_std", "throughput_min", "throughput_max",
         "total_bytes", "total_packets", "active_flows"]
JR = ["jitter_mean", "jitter_p95", "reorder_rate", "reorder_count"]
RTT = ["rtt_mean", "rtt_p95", "rtt_std"]
MS = ["tp_slot_akhir", "tp_pendek_mean", "tp_pendek_std", "tp_pendek_max",
      "tp_delta_prev", "tp_rasio_prev", "pkt_delta_prev",
      "tp_kumulatif", "tp_rasio_kumulatif", "bytes_kumulatif", "rasio_diam"]
SET_FITUR = {"dasar": DASAR, "jr": DASAR + JR, "rtt": DASAR + RTT,
             "semua": DASAR + JR + RTT, "multiskala": DASAR + MS,
             "lengkap": DASAR + JR + RTT + MS}

GRID = {
    "SVM RBF": {"C": [10, 100, 1000], "gamma": ["scale", 0.05, 0.1]},
    "RandomForest": {"n_estimators": [200], "max_depth": [None, 20],
                     "min_samples_leaf": [1, 3]},
    "LogReg": {"C": [1, 10, 100]},
    "DecisionTree": {"max_depth": [10, 20, None], "min_samples_leaf": [5, 20]},
}
NAMA_BERKAS = {"SVM RBF": "SVM_RBF", "RandomForest": "RandomForest",
               "LogReg": "LogReg", "DecisionTree": "DecisionTree"}


def buat(nama, par, seed=0):
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC
    from sklearn.tree import DecisionTreeClassifier
    if nama == "SVM RBF":
        return make_pipeline(StandardScaler(),
                             SVC(kernel="rbf", class_weight="balanced", **par))
    if nama == "LogReg":
        return make_pipeline(StandardScaler(),
                             LogisticRegression(max_iter=4000,
                                                class_weight="balanced", **par))
    if nama == "RandomForest":
        return RandomForestClassifier(class_weight="balanced", random_state=seed,
                                      n_jobs=-1, **par)
    if nama == "DecisionTree":
        return DecisionTreeClassifier(class_weight="balanced", random_state=seed,
                                      **par)
    raise ValueError(nama)


def kombinasi(grid):
    import itertools
    kunci = list(grid)
    for nilai in itertools.product(*[grid[k] for k in kunci]):
        yield dict(zip(kunci, nilai))


def muat(path, fitur):
    baris = [r for r in csv.DictReader(open(path, encoding="utf-8"))
             if float(r["throughput_mean"]) >= 0.01]
    if not baris:
        raise RuntimeError("dataset kosong setelah penyaringan")
    kurang = [c for c in fitur if c not in baris[0]]
    if kurang:
        raise RuntimeError(f"kolom fitur tidak ada di dataset: {kurang}")
    X = np.array([[float(r[c]) for c in fitur] for r in baris])
    for i, c in enumerate(fitur):
        if c.startswith("rtt"):
            X[:, i] = np.clip(X[:, i], 0, 2000)
    y = np.array([r["label"] for r in baris])
    g = np.array([r["run_id"] for r in baris])
    return X, y, g


def setel(nama, X, y, g, folds=4, seed=0):
    """Pilih hiperparameter lewat group-split per run_id."""
    from sklearn.metrics import f1_score
    unik = np.array(sorted(set(g)))
    rng = np.random.default_rng(seed)
    rng.shuffle(unik)
    bagian = np.array_split(unik, folds)
    terbaik, skor_terbaik = None, -1.0
    for par in kombinasi(GRID[nama]):
        sk = []
        for b in bagian:
            te = np.isin(g, b)
            m = buat(nama, par, seed).fit(X[~te], y[~te])
            sk.append(f1_score(y[te], m.predict(X[te]), average="macro"))
        s = float(np.mean(sk))
        if s > skor_terbaik:
            terbaik, skor_terbaik = par, s
    return terbaik, skor_terbaik


def self_test():
    rng = np.random.default_rng(0)
    K = ["Excellent", "Good", "Degraded", "Critical"]
    n_run, per_run = 24, 20
    rows = []
    for r in range(n_run):
        for w in range(per_run):
            k = K[(r + w) % 4]
            b = {"Excellent": 4.0, "Good": 1.2, "Degraded": 0.6, "Critical": 0.15}[k]
            tp = abs(rng.normal(b, b * 0.2))
            rows.append((f"CONT_s{r}_bola_X", w, k, tp))

    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "ds.csv")
    with open(p, "w", newline="", encoding="utf-8") as f:
        w_ = csv.writer(f)
        w_.writerow(["run_id", "window_index", "label"] + DASAR)
        for rid, wi, k, tp in rows:
            w_.writerow([rid, wi, k, round(tp, 5), round(tp * .4, 5), 0,
                         round(tp * 2, 5), int(tp * 1.25e6), int(tp * 830), 3])

    X, y, g = muat(p, DASAR)
    assert X.shape == (len(rows), 7) and len(set(g)) == n_run
    print(f"  [OK] muat: {X.shape[0]} window, {len(set(g))} run, {X.shape[1]} fitur")

    # kolom hilang harus ditolak dgn pesan jelas, bukan KeyError mentah
    try:
        muat(p, DASAR + ["tp_kumulatif"])
        raise AssertionError("kolom hilang seharusnya ditolak")
    except RuntimeError as e:
        assert "tp_kumulatif" in str(e)
    print("  [OK] kolom fitur yang tidak ada ditolak dgn pesan yang menyebutkannya")

    par, s = setel("DecisionTree", X, y, g, folds=3)
    assert par in list(kombinasi(GRID["DecisionTree"])) and 0 <= s <= 1
    print(f"  [OK] penyetelan memilih {par} dgn macro-F1 {s:.3f}")

    # lipatan penyetelan HARUS memisahkan per run, bukan per window
    unik = np.array(sorted(set(g)))
    rng2 = np.random.default_rng(0)
    rng2.shuffle(unik)
    bagian = np.array_split(unik, 3)
    for b in bagian:
        te = np.isin(g, b)
        assert len(set(g[te]) & set(g[~te])) == 0
    assert sorted(x for b in bagian for x in b) == sorted(set(g))
    print("  [OK] lipatan penyetelan memisahkan per run, tiap run muncul sekali")

    m = buat("DecisionTree", par).fit(X, y)
    assert set(m.predict(X[:20])) <= set(K)
    print("  [OK] model terlatih menghasilkan kelas yang sah")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Latih model final untuk deployment")
    ap.add_argument("--dataset", default="hasil_v5/dataset_ms.csv")
    ap.add_argument("--features", default="lengkap", choices=sorted(SET_FITUR))
    ap.add_argument("--model", default="SVM RBF", choices=sorted(GRID))
    ap.add_argument("--semua", action="store_true",
                    help="latih keempat model, bukan hanya satu")
    ap.add_argument("--out-dir", default="model_v5")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not os.path.exists(a.dataset):
        sys.exit(f"dataset tidak ketemu: {a.dataset}")

    import joblib
    fitur = SET_FITUR[a.features]
    try:
        X, y, g = muat(a.dataset, fitur)
    except RuntimeError as e:
        sys.exit(str(e))
    os.makedirs(a.out_dir, exist_ok=True)
    print(f"data    : {len(y)} window, {len(set(g))} run, {len(fitur)} fitur "
          f"({a.features})")
    print(f"keluaran: {a.out_dir}/\n")

    daftar = sorted(GRID) if a.semua else [a.model]
    ringkas = {}
    for nama in daftar:
        print(f"  {nama} ...", end="", flush=True)
        par, s = setel(nama, X, y, g, a.folds)
        m = buat(nama, par).fit(X, y)
        p = os.path.join(a.out_dir, f"{NAMA_BERKAS[nama]}.joblib")
        joblib.dump(m, p)
        kb = os.path.getsize(p) / 1024
        print(f" macro-F1 penyetelan {s:.3f} | {kb:.1f} KB | {par}")
        ringkas[nama] = {"params": {k: str(v) for k, v in par.items()},
                         "f1_penyetelan": round(s, 4), "ukuran_kb": round(kb, 1),
                         "berkas": os.path.basename(p)}

    meta = {"dataset": os.path.basename(a.dataset), "features": a.features,
            "n_fitur": len(fitur), "fitur": fitur, "n_window": len(y),
            "n_run": len(set(g)), "model": ringkas}
    with open(os.path.join(a.out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print(f"\n-> {a.out_dir}/meta.json")
    print("\nCATATAN: skor di atas berasal dari penyetelan dan TIDAK boleh")
    print("dilaporkan sebagai hasil. Angka untuk naskah berasal dari")
    print("stats_terkoreksi.py, yang memakai protokol validasi silang penuh.")
    if "SVM RBF" in daftar:
        print(f"\nEkspor untuk Fase 2:")
        print(f"  python export_svm.py --model {a.out_dir}/SVM_RBF.joblib "
              f"--out svm_standalone.py --features \"{','.join(fitur)}\"")


if __name__ == "__main__":
    main()