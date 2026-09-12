#!/usr/bin/env python3
"""
stats_terkoreksi.py — statistik yang sah untuk perbandingan model
--------------------------------------------------------------------
Menggantikan pelaporan simpangan baku atas lima ulangan, yang tidak sah sebagai
estimasi ketidakpastian.

Tiga masalah pada pelaporan lama, dan penanganannya di sini:

  1. Simpangan baku antar-ulangan MEREMEHKAN varians sebenarnya, karena himpunan
     latih antar-lipatan saling bertumpang tindih sehingga skornya berkorelasi
     positif. Nadeau dan Bengio (2003) menurunkan koreksi yang menggelembungkan
     varians dengan faktor (1/J + n_uji/n_latih) menggantikan 1/J. Bengio dan
     Grandvalet (2004) membuktikan tidak ada penduga takbias universal bagi
     varians validasi silang k-lipat, sehingga koreksi ini memang keharusan,
     bukan pilihan.
  2. Peringkat model TIDAK PERNAH diuji. Selisih seperti +0,019 atau -0,009
     dilaporkan seolah bermakna tanpa uji berpasangan. Di sini dipakai uji-t
     berpasangan terkoreksi Nadeau-Bengio, dikoreksi ganda dengan Holm, plus uji
     Friedman dan pasca-uji Nemenyi untuk membandingkan seluruh model sekaligus.
  3. Unit independensi adalah RUN, bukan window. Selang kepercayaan karena itu
     dihitung dengan bootstrap yang mengambil ulang run, bukan window.

Pakai:
  python stats_terkoreksi.py --dataset hasil_v4/dataset.csv --out stats_v4
  python stats_terkoreksi.py --dataset hasil_v4/dataset.csv --repeats 10
  python stats_terkoreksi.py --self-test
"""
import argparse
import csv
import itertools
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
# Sebelas fitur multi-cakupan dari fitur_lanjutan.py: cuplikan terakhir, jendela
# geser pendek, selisih terhadap window sebelumnya, dan kumulatif sejak sesi
# dimulai. Meniru pendekatan multi-cakupan ViCrypt, bukan reproduksi persis.
MS = ["tp_slot_akhir", "tp_pendek_mean", "tp_pendek_std", "tp_pendek_max",
      "tp_delta_prev", "tp_rasio_prev", "pkt_delta_prev",
      "tp_kumulatif", "tp_rasio_kumulatif", "bytes_kumulatif", "rasio_diam"]

SET_FITUR = {"dasar": DASAR, "jr": DASAR + JR, "rtt": DASAR + RTT,
             "semua": DASAR + JR + RTT, "multiskala": DASAR + MS}

# Nilai kritis rentang terstudentisasi untuk uji Nemenyi pada alpha 0,05,
# derajat bebas tak hingga, dibagi akar dua sesuai rumus baku Demsar (2006).
Q_NEMENYI_05 = {2: 1.960, 3: 2.343, 4: 2.569, 5: 2.728, 6: 2.850,
                7: 2.949, 8: 3.031, 9: 3.102, 10: 3.164}


# ------------------------------------------------------------------ statistik
def nb_ttest(a, b, n_uji, n_latih):
    """Uji-t berpasangan terkoreksi Nadeau-Bengio.

    Uji-t biasa memakai varians/J sebagai ragam rata-rata selisih. Itu
    anti-konservatif di sini karena lipatan berbagi data latih. Koreksinya
    mengganti 1/J dengan (1/J + n_uji/n_latih).
    """
    d = np.asarray(a, float) - np.asarray(b, float)
    J = len(d)
    if J < 2:
        return 0.0, 1.0, 0.0
    m = float(d.mean())
    s2 = float(d.var(ddof=1))
    if s2 <= 0:
        return (0.0, 1.0, m) if m == 0 else (np.inf * np.sign(m), 0.0, m)
    korek = 1.0 / J + n_uji / n_latih
    t = m / np.sqrt(korek * s2)
    from scipy import stats as sps
    p = float(2 * (1 - sps.t.cdf(abs(t), J - 1)))
    return float(t), p, m


def holm(pvals):
    """Koreksi Holm-Bonferroni; kembalikan p terkoreksi pada urutan semula."""
    m = len(pvals)
    urut = sorted(range(m), key=lambda i: pvals[i])
    keluar = [0.0] * m
    sebelum = 0.0
    for pos, i in enumerate(urut):
        v = min(1.0, (m - pos) * pvals[i])
        v = max(v, sebelum)          # jaga kemonotonan
        keluar[i] = v
        sebelum = v
    return keluar


def nemenyi_cd(k, n, alpha="0.05"):
    """Critical difference Nemenyi: q * sqrt(k(k+1)/(6n))."""
    q = Q_NEMENYI_05.get(k)
    return None if q is None else q * np.sqrt(k * (k + 1) / (6.0 * n))


def bootstrap_grup(y, yp, grup, n_boot=2000, seed=0):
    """Selang kepercayaan macro-F1 dengan mengambil ulang RUN, bukan window.

    Window di dalam satu run sangat mirip satu sama lain, sehingga bootstrap
    per window akan menganggap 2717 pengamatan independen padahal hanya ada 98.
    Selangnya akan jauh terlalu sempit.
    """
    from sklearn.metrics import f1_score
    rng = np.random.default_rng(seed)
    unik = np.unique(grup)
    idx_per = {g: np.where(grup == g)[0] for g in unik}
    skor = []
    for _ in range(n_boot):
        pilih = rng.choice(unik, len(unik), replace=True)
        idx = np.concatenate([idx_per[g] for g in pilih])
        if len(np.unique(y[idx])) < 2:
            continue
        skor.append(f1_score(y[idx], yp[idx], average="macro"))
    if not skor:
        return (np.nan, np.nan)
    return (float(np.percentile(skor, 2.5)), float(np.percentile(skor, 97.5)))


# ------------------------------------------------------------------ model
def buat_model(nama, seed):
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC
    from sklearn.tree import DecisionTreeClassifier
    if nama == "RandomForest":
        return RandomForestClassifier(200, min_samples_leaf=1, class_weight="balanced",
                                      random_state=seed, n_jobs=-1)
    if nama == "SVM RBF":
        return make_pipeline(StandardScaler(), SVC(kernel="rbf", C=100,
                                                   class_weight="balanced"))
    if nama == "LogReg":
        return make_pipeline(StandardScaler(),
                             LogisticRegression(max_iter=3000, C=10,
                                                class_weight="balanced"))
    if nama == "DecisionTree":
        return DecisionTreeClassifier(max_depth=10, min_samples_leaf=20,
                                      class_weight="balanced", random_state=seed)
    raise ValueError(nama)


MODEL = ["RandomForest", "SVM RBF", "LogReg", "DecisionTree"]


def lipatan_per_run(g, folds, seed):
    """Pembagian lipatan yang ditentukan HANYA oleh run_id.

    StratifiedGroupKFold ikut memperhatikan label, sehingga dua dataset dengan
    label sedikit berbeda akan mendapat pembagian berbeda dan skornya tidak lagi
    berpasangan. Untuk perbandingan A/B, keanggotaan lipatan harus identik.
    """
    unik = np.array(sorted(set(g)))
    rng = np.random.default_rng(seed)
    rng.shuffle(unik)
    bagian = np.array_split(unik, folds)
    for b in bagian:
        te = np.isin(g, b)
        yield np.where(~te)[0], np.where(te)[0]


def f1_per_kelas(y, yp, kelas):
    """F1 tiap kelas tanpa sklearn, agar urutannya terjamin."""
    out = []
    for k in kelas:
        tp = int(np.sum((yp == k) & (y == k)))
        fp = int(np.sum((yp == k) & (y != k)))
        fn = int(np.sum((yp != k) & (y == k)))
        pr = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        out.append(2 * pr * rc / (pr + rc) if pr + rc else 0.0)
    return np.array(out)


def skor_per_lipatan(X, y, g, nama, repeats, folds, seed0=0, per_run=False,
                     kelas=None):
    """Kembalikan (skor per lipatan, rata2 n_uji, rata2 n_latih, prediksi CV)."""
    from sklearn.metrics import f1_score
    from sklearn.model_selection import StratifiedGroupKFold
    skor, n_uji, n_latih = [], [], []
    per_kelas = []
    pred_terakhir = np.empty(len(y), dtype=object)
    for r in range(repeats):
        pembagi = (lipatan_per_run(g, folds, seed0 + r) if per_run
                   else StratifiedGroupKFold(folds, shuffle=True,
                                             random_state=seed0 + r).split(X, y, groups=g))
        for tr, te in pembagi:
            m = buat_model(nama, seed0 + r).fit(X[tr], y[tr])
            yp = m.predict(X[te])
            skor.append(f1_score(y[te], yp, average="macro"))
            if kelas is not None:
                per_kelas.append(f1_per_kelas(y[te], yp, kelas))
            n_uji.append(len(te))
            n_latih.append(len(tr))
            if r == 0:
                pred_terakhir[te] = yp
    if kelas is not None:
        return (np.array(skor), float(np.mean(n_uji)), float(np.mean(n_latih)),
                pred_terakhir, np.array(per_kelas))
    return (np.array(skor), float(np.mean(n_uji)), float(np.mean(n_latih)),
            pred_terakhir)


def muat(path, fitur):
    baris = [r for r in csv.DictReader(open(path, encoding="utf-8"))
             if float(r["throughput_mean"]) >= 0.01]
    X = np.array([[float(r[c]) for c in fitur] for r in baris])
    for i, c in enumerate(fitur):
        if c.startswith("rtt"):
            X[:, i] = np.clip(X[:, i], 0, 2000)
    y = np.array([r["label"] for r in baris])
    g = np.array([r["run_id"] for r in baris])
    return X, y, g


# ------------------------------------------------------------------ uji
def self_test():
    # koreksi NB harus MENGGELEMBUNGKAN p dibandingkan uji-t biasa
    from scipy import stats as sps
    rng = np.random.default_rng(0)
    a = rng.normal(0.70, 0.010, 25)
    b = a - 0.004 + rng.normal(0, 0.004, 25)
    t_nb, p_nb, m = nb_ttest(a, b, n_uji=540, n_latih=2160)
    d = a - b
    t_biasa = d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))
    p_biasa = 2 * (1 - sps.t.cdf(abs(t_biasa), len(d) - 1))
    assert p_nb > p_biasa, (p_nb, p_biasa)
    print(f"  [OK] koreksi NB melonggarkan p: biasa {p_biasa:.2e} -> "
          f"terkoreksi {p_nb:.4f} (selisih rata2 {m:+.4f})")

    # faktor koreksi harus sesuai rumus
    J, nu, nl = 25, 540, 2160
    korek = 1 / J + nu / nl
    assert abs(korek - (0.04 + 0.25)) < 1e-9
    print(f"  [OK] faktor koreksi 1/J + n_uji/n_latih = {korek:.4f}, "
          f"yaitu {korek*J:.2f}x varians naif")

    # Holm monoton dan tidak menurunkan p
    ps = [0.001, 0.04, 0.03, 0.5]
    hp = holm(ps)
    assert all(h >= p for h, p in zip(hp, ps))
    assert hp[0] <= hp[2] <= hp[1] <= hp[3], hp
    print(f"  [OK] Holm: {['%.3f' % x for x in hp]} dari {ps}")

    # selisih nol harus memberi p = 1
    _, p0, m0 = nb_ttest([0.7] * 10, [0.7] * 10, 500, 2000)
    assert p0 == 1.0 and m0 == 0.0
    print("  [OK] selisih nol menghasilkan p = 1")

    # pembagian lipatan per run harus IDENTIK untuk dua label berbeda
    g_uji = np.repeat([f"r{i}" for i in range(20)], 5)
    y1 = np.array(["A", "B"] * 50)
    y2 = np.array(["A"] * 60 + ["B"] * 40)
    f1 = [tuple(sorted(set(g_uji[te]))) for _, te in lipatan_per_run(g_uji, 5, 0)]
    f2 = [tuple(sorted(set(g_uji[te]))) for _, te in lipatan_per_run(g_uji, 5, 0)]
    assert f1 == f2 and len(f1) == 5
    semua = sorted(x for b in f1 for x in b)
    assert semua == sorted(set(g_uji)), "tiap run harus muncul tepat sekali"
    print(f"  [OK] pembagian per run deterministik, {len(f1)} lipatan menutupi "
          f"{len(semua)} run tanpa tumpang tindih")

    yv = np.array(["A", "A", "B", "B"])
    pv = np.array(["A", "B", "B", "B"])
    fk = f1_per_kelas(yv, pv, ["A", "B"])
    assert abs(fk[0] - 2 / 3) < 1e-9 and abs(fk[1] - 0.8) < 1e-9, fk
    print(f"  [OK] F1 per kelas: A {fk[0]:.3f}, B {fk[1]:.3f}")

    assert len(SET_FITUR["multiskala"]) == 18, len(SET_FITUR["multiskala"])
    assert SET_FITUR["multiskala"][:7] == DASAR
    assert not (set(MS) & set(JR + RTT)), "fitur multiskala tidak boleh tumpang tindih"
    print(f"  [OK] set multiskala: 7 dasar + {len(MS)} multi-cakupan = "
          f"{len(SET_FITUR['multiskala'])} fitur")

    cd = nemenyi_cd(4, 25)
    assert 0.5 < cd < 1.5, cd
    print(f"  [OK] critical difference Nemenyi utk 4 model, 25 lipatan: {cd:.3f}")

    # Bootstrap berkelompok harus LEBIH LEBAR daripada bootstrap per window.
    # Data uji dibuat dengan laju galat yang BERBEDA antar-run, karena itulah
    # kondisi nyatanya: sebagian run memang lebih sulit. Bila galat tersebar
    # merata, mengambil ulang run tidak menambah varians dan perbedaan kedua
    # bootstrap tidak muncul.
    from sklearn.metrics import f1_score
    n_run, per_run = 30, 30
    g = np.repeat([f"r{i}" for i in range(n_run)], per_run)
    y = np.array(["A", "B"] * (n_run * per_run // 2))
    yp = y.copy()
    for i in range(n_run):
        laju = 0.03 if i % 3 else 0.45      # sepertiga run jauh lebih sulit
        blok = np.arange(i * per_run, (i + 1) * per_run)
        salah = rng.choice(blok, int(len(blok) * laju), replace=False)
        yp[salah] = np.where(yp[salah] == "A", "B", "A")
    lo_g, hi_g = bootstrap_grup(y, yp, g, n_boot=600, seed=1)
    rng2 = np.random.default_rng(1)
    sk = []
    for _ in range(600):
        idx = rng2.choice(len(y), len(y), replace=True)
        if len(np.unique(y[idx])) > 1:
            sk.append(f1_score(y[idx], yp[idx], average="macro"))
    lo_w, hi_w = np.percentile(sk, 2.5), np.percentile(sk, 97.5)
    assert (hi_g - lo_g) > 1.5 * (hi_w - lo_w), ((lo_g, hi_g), (lo_w, hi_w))
    print(f"  [OK] galat berkelompok per run: selang berkelompok "
          f"[{lo_g:.3f}, {hi_g:.3f}] lebar {hi_g-lo_g:.3f}, "
          f"per window [{lo_w:.3f}, {hi_w:.3f}] lebar {hi_w-lo_w:.3f}")
    print(f"       -> bootstrap per window {((hi_g-lo_g)/(hi_w-lo_w)):.1f}x terlalu sempit")
    print("\nSEMUA UJI LULUS")
    return True


def jalankan_perbandingan(a, fitur, Xa, ya, ga, kelas=("Excellent", "Good",
                                                        "Degraded", "Critical")):
    """Bandingkan dua konfigurasi pada lipatan yang sama, dgn rincian per kelas.

    Yang berbeda dapat berupa datasetnya, set fiturnya, atau keduanya sekaligus.
    Lipatan tetap ditentukan hanya oleh run_id sehingga skornya berpasangan.
    """
    fitur_b = SET_FITUR[a.compare_features] if a.compare_features else fitur
    Xb, yb, gb = muat(a.compare_dataset, fitur_b)
    J = a.repeats * a.folds
    print(f"A: {os.path.basename(a.dataset)} -> {len(ya)} window, {len(set(ga))} run, "
          f"{len(fitur)} fitur ({a.features})")
    print(f"B: {os.path.basename(a.compare_dataset)} -> {len(yb)} window, "
          f"{len(set(gb))} run, {len(fitur_b)} fitur "
          f"({a.compare_features or a.features})")
    if set(ga) != set(gb):
        print("  PERINGATAN: himpunan run kedua dataset tidak sama persis")
    print(f"protokol: {a.folds} lipatan x {a.repeats} ulangan, pembagian ditentukan "
          f"HANYA oleh run_id\n")

    kelas = [k for k in kelas if k in set(ya) and k in set(yb)]
    for m in MODEL:
        print(f"  {m} ...", end="", flush=True)
        sa, nu, nl, _, pka = skor_per_lipatan(Xa, ya, ga, m, a.repeats, a.folds,
                                              per_run=True, kelas=kelas)
        sb, _, _, _, pkb = skor_per_lipatan(Xb, yb, gb, m, a.repeats, a.folds,
                                            per_run=True, kelas=kelas)
        # Urutan (b, a) DISENGAJA agar selisih positif berarti B lebih tinggi,
        # sesuai judul kolom "B - A". Urutan terbalik pernah membuat penurunan
        # dilaporkan sebagai kenaikan.
        t, p, d = nb_ttest(sb, sa, nu, nl)
        print(f" A {sa.mean():.4f}  B {sb.mean():.4f}  selisih {d:+.4f}")

        ps, ds = [], []
        for i, k in enumerate(kelas):
            tt, pp, dd = nb_ttest(pkb[:, i], pka[:, i], nu, nl)
            ps.append(pp)
            ds.append((k, pka[:, i].mean(), pkb[:, i].mean(), dd))
        hp = holm(ps + [p])
        print(f"    {'kelas':<12}{'A':>9}{'B':>9}{'B - A':>10}{'p Holm':>10}  putusan")
        for (k, ma, mb, dd), ph in zip(ds, hp):
            vonis = ("TURUN" if dd < 0 else "NAIK") if ph < 0.05 else "dalam derau"
            print(f"    {k:<12}{ma:>9.4f}{mb:>9.4f}{dd:>+10.4f}{ph:>10.4f}  {vonis}")
        print(f"    {'macro-F1':<12}{sa.mean():>9.4f}{sb.mean():>9.4f}"
              f"{d:>+10.4f}{hp[-1]:>10.4f}  "
              f"{'BERBEDA' if hp[-1] < 0.05 else 'dalam derau'}\n")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Statistik terkoreksi utk perbandingan model")
    ap.add_argument("--dataset", default="hasil_v4/dataset.csv")
    ap.add_argument("--features", default="semua", choices=sorted(SET_FITUR))
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--out", default=None, help="tulis ringkasan JSON")
    ap.add_argument("--compare-dataset", default=None,
                    help="dataset kedua untuk dibandingkan langsung, dgn rincian "
                         "F1 per kelas. Lipatan ditentukan hanya oleh run_id "
                         "sehingga skor kedua dataset benar-benar berpasangan")
    ap.add_argument("--compare-label", default="B", help="nama dataset kedua")
    ap.add_argument("--compare-features", default=None, choices=sorted(SET_FITUR),
                    help="set fitur untuk dataset kedua; default sama dgn --features. "
                         "Dipakai saat membandingkan SET FITUR yang berbeda, "
                         "bukan hanya dataset yang berbeda")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not os.path.exists(a.dataset):
        sys.exit(f"tidak ketemu: {a.dataset}")

    fitur = SET_FITUR[a.features]
    X, y, g = muat(a.dataset, fitur)

    if a.compare_dataset:
        jalankan_perbandingan(a, fitur, X, y, g)
        return

    J = a.repeats * a.folds
    print(f"data    : {len(y)} window, {len(set(g))} run, {len(fitur)} fitur "
          f"({a.features})")
    print(f"protokol: {a.folds} lipatan x {a.repeats} ulangan = {J} skor per model\n")

    hasil, n_uji, n_latih = {}, None, None
    for m in MODEL:
        print(f"  melatih {m} ...", end="", flush=True)
        sk, nu, nl, yp = skor_per_lipatan(X, y, g, m, a.repeats, a.folds)
        hasil[m] = {"skor": sk, "pred": yp}
        n_uji, n_latih = nu, nl
        print(f" rata2 {sk.mean():.4f}")

    korek = 1.0 / J + n_uji / n_latih
    print(f"\nfaktor koreksi Nadeau-Bengio: 1/{J} + {n_uji:.0f}/{n_latih:.0f} = "
          f"{korek:.4f}  ({korek*J:.1f}x varians naif)\n")

    # --- titik dan selang ---
    print("=" * 76)
    print("A. SKOR TITIK DAN SELANG KEPERCAYAAN")
    print("=" * 76)
    print(f"{'model':<16}{'macro-F1':>10}{'SE naif':>10}{'SE terkoreksi':>15}"
          f"{'rasio':>8}{'CI95 bootstrap run':>24}")
    ci = {}
    for m in MODEL:
        sk = hasil[m]["skor"]
        # SE naif = SD/akar(J), yaitu yang tersirat bila SD antar-ulangan
        # diperlakukan sebagai ketidakpastian. SE terkoreksi memakai faktor NB.
        se_naif = sk.std(ddof=1) / np.sqrt(J)
        se_nb = np.sqrt(korek * sk.var(ddof=1))
        lo, hi = bootstrap_grup(y, hasil[m]["pred"], g, a.n_boot)
        ci[m] = (lo, hi)
        print(f"{m:<16}{sk.mean():>10.4f}{se_naif:>10.4f}{se_nb:>15.4f}"
              f"{se_nb/se_naif:>7.1f}x{f'[{lo:.3f}, {hi:.3f}]':>24}")
    print(f"\n  SE naif mengandaikan {J} skor saling bebas, padahal himpunan latihnya")
    print(f"  bertumpang tindih. SE terkoreksi {np.sqrt(korek*J):.1f} kali lebih besar,")
    print("  dan itulah yang sah dipakai untuk pengujian maupun pelaporan.")

    # --- uji berpasangan ---
    print("\n" + "=" * 76)
    print("B. UJI BERPASANGAN TERKOREKSI (Nadeau-Bengio + Holm)")
    print("=" * 76)
    pasangan = list(itertools.combinations(MODEL, 2))
    baris = []
    for m1, m2 in pasangan:
        t, p, d = nb_ttest(hasil[m1]["skor"], hasil[m2]["skor"], n_uji, n_latih)
        baris.append([m1, m2, d, t, p])
    hp = holm([b[4] for b in baris])
    print(f"{'perbandingan':<34}{'selisih':>10}{'t':>8}{'p mentah':>11}"
          f"{'p Holm':>10}  putusan")
    for (m1, m2, d, t, p), ph in zip(baris, hp):
        vonis = "BERBEDA" if ph < 0.05 else "tidak berbeda"
        print(f"{m1+' vs '+m2:<34}{d:>+10.4f}{t:>8.2f}{p:>11.4f}{ph:>10.4f}  {vonis}")

    # --- Friedman + Nemenyi ---
    print("\n" + "=" * 76)
    print("C. PERBANDINGAN SELURUH MODEL (Friedman + Nemenyi)")
    print("=" * 76)
    from scipy import stats as sps
    mat = np.array([hasil[m]["skor"] for m in MODEL])          # model x lipatan
    stat, pfr = sps.friedmanchisquare(*mat)
    print(f"  Friedman: chi2 = {stat:.2f}, p = {pfr:.3e}")
    # peringkat rata-rata (1 = terbaik)
    rank = np.zeros(len(MODEL))
    for j in range(mat.shape[1]):
        urut = np.argsort(-mat[:, j])
        for pos, i in enumerate(urut):
            rank[i] += pos + 1
    rank /= mat.shape[1]
    cd = nemenyi_cd(len(MODEL), mat.shape[1])
    print(f"  Critical difference (alpha 0,05): {cd:.3f}\n")
    print(f"  {'model':<16}{'peringkat rata2':>18}")
    for m, r in sorted(zip(MODEL, rank), key=lambda x: x[1]):
        print(f"  {m:<16}{r:>18.2f}")
    print(f"\n  Pasangan dgn selisih peringkat < {cd:.3f} tidak dapat dipisahkan:")
    tak_pisah = [f"{m1} dan {m2}" for (m1, r1), (m2, r2)
                 in itertools.combinations(list(zip(MODEL, rank)), 2)
                 if abs(r1 - r2) < cd]
    for x in (tak_pisah or ["(tidak ada)"]):
        print(f"    {x}")

    # --- kontribusi set fitur ---
    print("\n" + "=" * 76)
    print("D. KONTRIBUSI SET FITUR (7 dasar vs 14 semua), diuji berpasangan")
    print("=" * 76)
    Xd, _, _ = muat(a.dataset, DASAR)
    baris0 = next(csv.DictReader(open(a.dataset, encoding="utf-8")))
    punya14 = all(str(baris0.get(c, "")).strip() != "" for c in JR + RTT)
    if not punya14:
        print("\n  Bagian D dan E DILEWATI: dataset ini hanya memuat tujuh fitur")
        print("  dasar, sehingga perbandingan 7 lawan 14 tidak dapat dijalankan.")
        Xs = None
    else:
        Xs, _, _ = muat(a.dataset, DASAR + JR + RTT)
    baris2 = []
    for m in (MODEL if Xs is not None else []):
        sd, nu, nl, _ = skor_per_lipatan(Xd, y, g, m, a.repeats, a.folds)
        ss, _, _, _ = skor_per_lipatan(Xs, y, g, m, a.repeats, a.folds)
        t, p, d = nb_ttest(ss, sd, nu, nl)
        baris2.append([m, sd.mean(), ss.mean(), d, p])
    hp2 = holm([b[4] for b in baris2]) if baris2 else []
    if baris2:
        print(f"{'model':<16}{'7 fitur':>10}{'14 fitur':>11}{'selisih':>10}"
              f"{'p Holm':>10}  putusan")
    for (m, a7, a14, d, p), ph in zip(baris2, hp2):
        vonis = "MEMBANTU" if (ph < 0.05 and d > 0) else (
            "MERUGIKAN" if (ph < 0.05 and d < 0) else "dalam derau")
        print(f"{m:<16}{a7:>10.4f}{a14:>11.4f}{d:>+10.4f}{ph:>10.4f}  {vonis}")

    # --- kontribusi fitur dipisah per mode ABR ---
    seed_num = np.array([int(x.split("_")[1][1:]) if x.split("_")[1][1:].isdigit()
                         else 0 for x in g])
    abr = np.where(seed_num >= 8, "bola", "dynamic")
    baris3 = []
    if len(set(abr)) == 2 and Xs is not None:
        print("\n" + "=" * 76)
        print("E. KONTRIBUSI FITUR PER MODE ABR, diuji berpasangan")
        print("=" * 76)
        for mode in ("dynamic", "bola"):
            msk = abr == mode
            for m in ("RandomForest",):
                sd, nu2, nl2, _ = skor_per_lipatan(Xd[msk], y[msk], g[msk], m,
                                                   a.repeats, a.folds)
                ss, _, _, _ = skor_per_lipatan(Xs[msk], y[msk], g[msk], m,
                                               a.repeats, a.folds)
                t, p, d = nb_ttest(ss, sd, nu2, nl2)
                baris3.append([mode, m, int(msk.sum()), sd.mean(), ss.mean(), d, p])
        hp3 = holm([b[6] for b in baris3])
        print(f"{'mode ABR':<12}{'n':>7}{'7 fitur':>10}{'14 fitur':>11}"
              f"{'selisih':>10}{'p Holm':>10}  putusan")
        for (mode, m, n3, a7, a14, d, p), ph in zip(baris3, hp3):
            vonis = "MEMBANTU" if (ph < 0.05 and d > 0) else "dalam derau"
            print(f"{mode:<12}{n3:>7}{a7:>10.4f}{a14:>11.4f}{d:>+10.4f}"
                  f"{ph:>10.4f}  {vonis}")

    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        ring = {"n_window": len(y), "n_run": len(set(g)), "fitur": a.features,
                "J": J, "faktor_koreksi": korek,
                "model": {m: {"mean": float(hasil[m]["skor"].mean()),
                              "sd_naif": float(hasil[m]["skor"].std(ddof=1)),
                              "se_nb": float(np.sqrt(korek * hasil[m]["skor"].var(ddof=1))),
                              "ci95": ci[m]} for m in MODEL},
                "pasangan": [{"a": m1, "b": m2, "selisih": d, "p_holm": ph}
                             for (m1, m2, d, _, _), ph in zip(baris, hp)],
                "friedman_p": float(pfr), "nemenyi_cd": float(cd),
                "fitur_uji": [{"model": m, "f7": a7, "f14": a14, "selisih": d,
                               "p_holm": ph}
                              for (m, a7, a14, d, _), ph in zip(baris2, hp2)],
                "abr_uji": [{"mode": x[0], "n": x[2], "f7": x[3], "f14": x[4],
                             "selisih": x[5], "p_holm": ph}
                            for x, ph in zip(baris3, holm([b[6] for b in baris3]))]
                           if baris3 else []}
        with open(a.out if a.out.endswith(".json") else a.out + ".json",
                  "w", encoding="utf-8") as f:
            json.dump(ring, f, indent=2)
        print(f"\n-> {a.out if a.out.endswith('.json') else a.out + '.json'}")


if __name__ == "__main__":
    main()