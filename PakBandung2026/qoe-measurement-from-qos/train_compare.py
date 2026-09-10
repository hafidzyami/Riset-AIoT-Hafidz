#!/usr/bin/env python3
"""
train_compare.py — bandingkan model QoE dari sisi AKURASI, UKURAN, dan LATENSI
-------------------------------------------------------------------------------
Menjawab pertanyaan yang relevan untuk deployment edge: bukan sekadar model mana
yang paling akurat, tetapi model mana yang memberi akurasi memadai dengan biaya
memori dan waktu inferensi yang masuk akal di perangkat terbatas.

Metodologi:
  - group-split per run_id (window dari satu run tidak boleh terpecah)
  - NESTED CV untuk model yang disetel, agar penyetelan tidak bocor ke fold uji
  - LOTO (leave-one-title-out) untuk generalisasi ke konten yang belum dilihat
  - ukuran model = besar berkas joblib; latensi = waktu prediksi per sampel

Catatan penting: latensi yang diukur di sini adalah latensi MESIN INI. Untuk
angka RQ yang sah, jalankan bench_inference.py di Raspberry Pi 5 memakai model
yang disimpan skrip ini.

Pakai:
  python train_compare.py --dataset hasil_v3/dataset.csv --out-dir model
  python train_compare.py --dataset hasil_v3/dataset.csv --features semua
  python train_compare.py --self-test
"""
import argparse
import csv
import json
import os
import sys
import time
import warnings

import numpy as np

warnings.filterwarnings("ignore")

from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import confusion_matrix, f1_score
from sklearn.model_selection import GridSearchCV, GroupKFold, StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier

KELAS = ["Excellent", "Good", "Degraded", "Critical"]
DASAR = ["throughput_mean", "throughput_std", "throughput_min", "throughput_max",
         "total_bytes", "total_packets", "active_flows"]
JR = ["jitter_mean", "jitter_p95", "reorder_rate", "reorder_count"]
RTT = ["rtt_mean", "rtt_p95", "rtt_std"]
SET_FITUR = {"dasar": DASAR, "jr": DASAR + JR, "rtt": DASAR + RTT,
             "semua": DASAR + JR + RTT}

JUDUL_DIKENAL = ["BigBuckBunny", "ElephantsDream", "OfForestAndMen",
                 "RedBullPlayStreets", "TearsOfSteel", "TheSwissAccount", "Valkaama"]


def judul_dari_run(rid):
    """Ambil nama judul dari run_id, apa pun format penamaannya.

    Format berubah antar koleksi: "S1_BigBuckBunny_rep1" pada matriks skenario
    dan "CONT_s10_BigBuckBunny" pada koleksi bandwidth kontinu. Memakai
    rid.split("_")[1] hanya benar untuk format pertama; pada format kedua ia
    mengembalikan "s10" sehingga pengambilan manifest gagal dan pengelompokan
    leave-one-title-out berubah menjadi leave-one-seed-out tanpa disadari.
    Pencocokan terhadap daftar judul membuatnya tahan terhadap kedua format.
    """
    for t in JUDUL_DIKENAL:
        if t in rid:
            return t
    bagian = rid.split("_")
    return bagian[1] if len(bagian) > 1 else rid


def seed_dari_run(rid):
    """Ambil penanda lintasan bandwidth (sN) bila ada; jika tidak, prefiks."""
    for b in rid.split("_"):
        if len(b) > 1 and b[0] == "s" and b[1:].isdigit():
            return b
    return rid.split("_")[0]




# ---------------------------------------------------------------- SOM
# Didefinisikan di modul terpisah agar model .joblib bisa dimuat skrip lain
# (kelas di __main__ tidak portabel saat unpickle).
from som_model import SOMClassifier


# ---------------------------------------------------------------- data
def muat(path, feats, clip_rtt=2000.0, min_tp=0.01, buang_skenario=()):
    """buang_skenario: mis. ("S4A","S4B","S4C").

    S4 adalah skenario delay murni yang labelnya seragam Excellent; RTT di sana
    memisahkan skenario tanpa memisahkan kelas, sehingga ikut menjadi derau bagi
    pengklasifikasi. Skenario itu dirancang untuk VALIDASI RTT (RQ1), bukan
    sebagai data latih klasifikasi, jadi wajar diuji tanpa mereka.
    """
    baris = [r for r in csv.DictReader(open(path, encoding="utf-8"))
             if float(r["throughput_mean"]) >= min_tp
             and r["run_id"].split("_")[0] not in set(buang_skenario)]
    X = np.array([[float(r[f]) for f in feats] for r in baris])
    for i, f in enumerate(feats):
        if f.startswith("rtt"):
            X[:, i] = np.clip(X[:, i], 0, clip_rtt)
    y = np.array([r["label"] for r in baris])
    g = np.array([r["run_id"] for r in baris])
    judul = np.array([judul_dari_run(r["run_id"]) for r in baris])
    seed = np.array([seed_dari_run(r["run_id"]) for r in baris])
    return X, y, g, judul, seed


# ---------------------------------------------------------------- model
def kandidat(seed=0):
    """(nama, pipeline, grid_hyperparameter) — grid kosong = tanpa penyetelan."""
    return [
        ("Tebak mayoritas", Pipeline([("m", DummyClassifier(strategy="most_frequent"))]), {}),
        ("LogReg", Pipeline([("sc", StandardScaler()),
                             ("m", LogisticRegression(max_iter=2000,
                                                      class_weight="balanced"))]),
         {"m__C": [0.1, 1, 10]}),
        ("DecisionTree", Pipeline([("m", DecisionTreeClassifier(class_weight="balanced",
                                                               random_state=seed))]),
         {"m__max_depth": [6, 10, None], "m__min_samples_leaf": [1, 5, 20]}),
        ("RandomForest", Pipeline([("m", RandomForestClassifier(n_estimators=200,
                                                               class_weight="balanced",
                                                               random_state=seed, n_jobs=-1))]),
         {"m__max_depth": [10, None], "m__min_samples_leaf": [1, 5]}),
        ("SVM RBF", Pipeline([("sc", StandardScaler()),
                              ("m", SVC(kernel="rbf", class_weight="balanced"))]),
         {"m__C": [1, 10, 100, 300], "m__gamma": ["scale", 0.05, 0.1]}),
        ("SOM 10x10", Pipeline([("sc", StandardScaler()),
                                ("m", SOMClassifier(seed=seed))]),
         {"m__grid": [10, 15], "m__sigma": [1.0, 1.5]}),
    ]


def nested_cv(pipe, grid, X, y, g, n_luar=5, n_dalam=4, seed=None):
    """Prediksi out-of-fold dgn penyetelan HANYA di dalam fold latih.

    Bila seed diberikan, pembagian luar memakai StratifiedGroupKFold dengan
    pengacakan sehingga tiap ulangan menghasilkan komposisi fold berbeda. Itu
    memungkinkan pengukuran simpangan baku, yang penting karena selisih
    antar-model di sini kecil dan bisa tenggelam dalam derau implementasi.
    """
    yp = np.empty(len(y), dtype=object)
    k = min(n_luar, len(set(g)))
    if seed is None:
        luar = GroupKFold(n_splits=k)
    else:
        luar = StratifiedGroupKFold(n_splits=k, shuffle=True, random_state=seed)
    for tr, te in luar.split(X, y, g):
        if grid:
            gs = GridSearchCV(pipe, grid, scoring="f1_macro",
                              cv=GroupKFold(n_splits=min(n_dalam, len(set(g[tr])))),
                              n_jobs=-1)
            gs.fit(X[tr], y[tr], groups=g[tr])
            est = gs.best_estimator_
        else:
            est = pipe.fit(X[tr], y[tr])
        yp[te] = est.predict(X[te])
    return yp.astype(str)


def ukur(pipe, grid, X, y, g, out_dir, nama):
    """Latih pd SELURUH data (utk disimpan), lalu ukur ukuran & latensi."""
    import joblib
    if grid:
        gs = GridSearchCV(pipe, grid, scoring="f1_macro",
                          cv=GroupKFold(n_splits=min(4, len(set(g)))), n_jobs=-1)
        gs.fit(X, y, groups=g)
        est, par = gs.best_estimator_, gs.best_params_
    else:
        est, par = pipe.fit(X, y), {}
    p = os.path.join(out_dir, f"{nama.replace(' ', '_').replace('/', '_')}.joblib")
    joblib.dump(est, p, compress=0)
    kb = os.path.getsize(p) / 1024.0

    n = min(500, len(X))
    Xs = X[:n]
    est.predict(Xs[:5])                       # pemanasan
    t0 = time.perf_counter()
    for _ in range(3):
        est.predict(Xs)
    dt = (time.perf_counter() - t0) / 3 / n * 1e6      # mikrodetik per sampel
    return est, par, kb, dt, p


def loto(pipe, params, X, y, judul):
    """Leave-one-title-out memakai hyperparameter yang SUDAH disetel.

    Menyetel ulang di tiap fold membuat waktu jalan berkali-kali lebih lama tanpa
    mengubah kesimpulan secara berarti; parameter diambil dari penyetelan pada
    seluruh data, jadi angka LOTO di sini sedikit optimistis dan itu dicatat.
    """
    from sklearn.base import clone
    sk = []
    for t in sorted(set(judul)):
        tr, te = judul != t, judul == t
        if len(set(y[tr])) < 2:
            continue
        est = clone(pipe)
        if params:
            est.set_params(**params)
        est.fit(X[tr], y[tr])
        sk.append(f1_score(y[te], est.predict(X[te]), average="macro"))
    return float(np.mean(sk)), float(np.std(sk))


# ---------------------------------------------------------------- uji mandiri
def self_test():
    # Penamaan run_id berbeda antar koleksi. Bila judul salah diekstrak,
    # leave-one-title-out berubah menjadi leave-one-seed-out tanpa disadari
    # dan angkanya jauh lebih rendah tanpa penjelasan.
    assert judul_dari_run("S1_BigBuckBunny_rep1") == "BigBuckBunny"
    assert judul_dari_run("CONT_s10_BigBuckBunny") == "BigBuckBunny"
    assert judul_dari_run("CONT_s3_TearsOfSteel") == "TearsOfSteel"
    assert seed_dari_run("CONT_s10_BigBuckBunny") == "s10"
    assert seed_dari_run("S1_BigBuckBunny_rep1") == "S1"
    print("  [OK] judul dan seed terbaca benar dari kedua format run_id")

    rng = np.random.default_rng(0)
    n = 400
    X = np.zeros((n, 10))
    y = np.empty(n, dtype=object)
    g = np.empty(n, dtype=object)
    jd = np.empty(n, dtype=object)
    for i in range(n):
        k = KELAS[i % 4]
        tinggi = {"Excellent": 4.0, "Good": 0.6, "Degraded": 0.33, "Critical": 0.13}[k]
        X[i] = rng.normal(tinggi, tinggi * 0.1, 10)
        y[i], g[i] = k, f"R{i % 20}"
        jd[i] = f"T{i % 4}"
    for nama, pipe, grid in kandidat():
        yp = nested_cv(pipe, grid, X, y, g, n_luar=3, n_dalam=2)
        f = f1_score(y, yp, average="macro")
        assert len(yp) == n
        tanda = "OK" if (nama == "Tebak mayoritas" or f > 0.5) else "RENDAH"
        print(f"  [{tanda}] {nama:<18} macro-F1 {f:.3f}")
    print("\n  data sintetis SANGAT terpisah, jadi F1 tinggi memang diharapkan;")
    print("  yang diuji di sini adalah seluruh jalur berjalan tanpa error.")
    print("\nSEMUA UJI LULUS")
    return True


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Bandingkan model QoE (akurasi/ukuran/latensi)")
    ap.add_argument("--dataset", default="hasil_v3/dataset.csv")
    ap.add_argument("--features", default="rtt", choices=list(SET_FITUR))
    ap.add_argument("--out-dir", default="model")
    ap.add_argument("--clip-rtt", type=float, default=2000.0)
    ap.add_argument("--exclude-scenarios", default="",
                    help="mis. S4A,S4B,S4C (skenario delay murni, label seragam)")
    ap.add_argument("--repeats", type=int, default=1,
                    help="jumlah ulangan dgn pembagian & seed berbeda; >1 memberi "
                         "simpangan baku (disarankan 5 utk pelaporan)")
    ap.add_argument("--skip-loto", action="store_true", help="lewati LOTO (lebih cepat)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not os.path.exists(a.dataset):
        sys.exit(f"tidak ketemu: {a.dataset}")
    os.makedirs(a.out_dir, exist_ok=True)

    feats = SET_FITUR[a.features]
    buang = tuple(s.strip() for s in a.exclude_scenarios.split(",") if s.strip())
    X, y, g, judul, seed = muat(a.dataset, feats, a.clip_rtt, buang_skenario=buang)
    print(f"data: {len(y)} window, {len(set(g))} run, {len(set(judul))} judul, "
          f"{len(feats)} fitur ({a.features})"
          + (f" | skenario dibuang: {', '.join(buang)}" if buang else ""))
    from collections import Counter
    print(f"kelas: {dict(Counter(y))}\n")

    R = max(1, a.repeats)
    hasil = []
    for nama, _, grid in kandidat(0):
        t0 = time.perf_counter()
        print(f"  {nama} ...", end="", flush=True)
        f1s, akurs, lotos, losos, pers = [], [], [], [], []
        est = par = kb = lat = path = None
        # Penyetelan penuh dilakukan SEKALI (seed 0) lalu dipakai ulang untuk LOTO;
        # nested_cv tetap menyetel sendiri di dalam tiap fold sehingga angka utama
        # tidak bocor. Tanpa ini waktu jalan berlipat sejumlah ulangan.
        for rep in range(R):
            pipe = dict((n, p) for n, p, _ in kandidat(rep))[nama]
            if rep == 0:
                est, par, kb, lat, path = ukur(pipe, grid, X, y, g, a.out_dir, nama)
            yp = nested_cv(pipe, grid, X, y, g, seed=(None if R == 1 else rep))
            f1s.append(f1_score(y, yp, average="macro"))
            akurs.append(float((yp == y).mean()))
            pers.append(f1_score(y, yp, average=None, labels=KELAS, zero_division=0))
            if not a.skip_loto:
                lotos.append(loto(pipe, par, X, y, judul)[0])
                # Leave-one-seed-out menguji generalisasi ke LINTASAN BANDWIDTH
                # yang belum pernah dilihat. Pada koleksi bandwidth kontinu ini
                # pengganti sah bagi leave-one-scenario-out yang tidak berlaku
                # lagi, karena skenario diskret sudah tidak ada.
                if len(set(seed)) >= 3:
                    losos.append(loto(pipe, par, X, y, seed)[0])
        f1m, f1sd = float(np.mean(f1s)), float(np.std(f1s))
        per = dict(zip(KELAS, np.mean(pers, axis=0)))
        lo = (float(np.mean(lotos)), float(np.std(lotos))) if lotos else (None, None)
        ls = (float(np.mean(losos)), float(np.std(losos))) if losos else (None, None)
        print("\r", end="")
        hasil.append({"model": nama, "macro_f1": round(f1m, 4), "macro_f1_std": round(f1sd, 4),
                      "akurasi": round(float(np.mean(akurs)), 4), "ulangan": R,
                      **{f"f1_{k}": round(float(v), 4) for k, v in per.items()},
                      "loto_mean": None if lo[0] is None else round(lo[0], 4),
                      "loto_std": None if lo[1] is None else round(lo[1], 4),
                      "loso_mean": None if ls[0] is None else round(ls[0], 4),
                      "loso_std": None if ls[1] is None else round(ls[1], 4),
                      "ukuran_kb": round(kb, 1), "latensi_us": round(lat, 2),
                      "params": json.dumps(par), "berkas": os.path.basename(path)})
        print(f"  {nama:<18} F1 {f1m:.3f} +/- {f1sd:.3f} | akur {np.mean(akurs):.3f} | "
              f"{kb:>8.1f} KB | {lat:>7.2f} us | "
              f"LOTO {'-' if lo[0] is None else f'{lo[0]:.3f}'} "
              f"LOSeedO {'-' if ls[0] is None else f'{ls[0]:.3f}'}  "
              f"({time.perf_counter()-t0:.0f}s)")

    tag = a.features + ("_tanpa_" + "-".join(buang) if buang else "")
    p = os.path.join(a.out_dir, f"hasil_{tag}.csv")
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(hasil[0].keys()))
        w.writeheader()
        w.writerows(hasil)

    print(f"\n=== RINGKASAN ({R} ulangan) -> {p} ===")
    print(f"{'model':<18}{'macro-F1':>17}{'LOTO':>8}{'LOSeedO':>10}"
          f"{'ukuran KB':>11}{'latensi us':>12}")
    urut = sorted(hasil, key=lambda x: -x["macro_f1"])
    for r in urut:
        lo_txt = "-" if r["loto_mean"] is None else f"{r['loto_mean']:.3f}"
        ls_txt = "-" if r.get("loso_mean") is None else f"{r['loso_mean']:.3f}"
        f1_txt = f"{r['macro_f1']:.3f} +/- {r['macro_f1_std']:.3f}"
        print(f"{r['model']:<18}{f1_txt:>17}{lo_txt:>8}{ls_txt:>10}"
              f"{r['ukuran_kb']:>11.1f}{r['latensi_us']:>12.2f}")

    if R > 1:
        nyata = [r for r in urut if r["model"] != "Tebak mayoritas"]
        if len(nyata) >= 2:
            atas = nyata[0]
            amb = atas["macro_f1"] - atas["macro_f1_std"]
            setara = [r["model"] for r in nyata
                      if r["macro_f1"] + r["macro_f1_std"] >= amb]
            print(f"\n  Model yang TIDAK terpisah secara meyakinkan dari yang teratas")
            print(f"  (selang +/-1 simpangan baku saling tumpang tindih):")
            print(f"    {', '.join(setara)}")
            if len(setara) > 1:
                print("  -> pemilihan sebaiknya berdasarkan UKURAN dan LATENSI, bukan akurasi.")
    print("\nCATATAN: latensi di atas diukur pada MESIN INI. Untuk angka RQ, jalankan")
    print("bench_inference.py di Raspberry Pi 5 memakai model .joblib yang tersimpan.")


if __name__ == "__main__":
    main()