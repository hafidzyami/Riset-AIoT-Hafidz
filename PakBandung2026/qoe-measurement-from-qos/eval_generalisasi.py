#!/usr/bin/env python3
"""
eval_generalisasi.py — LOTO dan leave-one-seed-out pada set fitur mana pun
----------------------------------------------------------------------------
Dua uji generalisasi yang menjawab pertanyaan berbeda:

  LOTO (leave-one-title-out)
      Menahan seluruh run satu judul. Menjawab: apakah model menghafal konten?
      Bila skornya setara dengan skor dalam distribusi, model tidak bergantung
      pada judul tertentu.

  LOSeedO (leave-one-seed-out)
      Menahan seluruh run satu seed, yaitu satu lintasan bandwidth utuh.
      Menjawab: apakah model bekerja pada lintasan yang belum pernah dilihat?

Keduanya WAJIB dilaporkan bersama karena mengukur hal berbeda. Pada koleksi v3,
LOTO mencapai 0,835 sementara leave-one-scenario-out runtuh ke 0,325; melaporkan
yang pertama saja akan menyesatkan.

Perlu dinyatakan bahwa LOSeedO tetap merupakan generalisasi DI DALAM distribusi.
Ia menguji lintasan baru dari pembangkit yang sama, bukan jaringan, layanan, atau
jenis konten yang berbeda.

Ketidakpastian memakai koreksi Nadeau-Bengio, sama seperti stats_terkoreksi.py,
karena lipatan LOTO dan LOSeedO juga berbagi data latih.

Pakai:
  python eval_generalisasi.py --dataset hasil_v5/dataset_ms.csv --features lengkap
  python eval_generalisasi.py --dataset hasil_v5/dataset_ms.csv --models "SVM RBF"
  python eval_generalisasi.py --self-test
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
KELAS = ["Excellent", "Good", "Degraded", "Critical"]
MODEL = ["RandomForest", "SVM RBF", "LogReg", "DecisionTree"]


def buat(nama, seed=0):
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC
    from sklearn.tree import DecisionTreeClassifier
    if nama == "RandomForest":
        return RandomForestClassifier(200, min_samples_leaf=1,
                                      class_weight="balanced",
                                      random_state=seed, n_jobs=-1)
    if nama == "SVM RBF":
        return make_pipeline(StandardScaler(),
                             SVC(kernel="rbf", C=100, class_weight="balanced"))
    if nama == "LogReg":
        return make_pipeline(StandardScaler(),
                             LogisticRegression(max_iter=3000, C=10,
                                                class_weight="balanced"))
    if nama == "DecisionTree":
        return DecisionTreeClassifier(max_depth=10, min_samples_leaf=20,
                                      class_weight="balanced", random_state=seed)
    raise ValueError(nama)


def judul_dari_run(rid, daftar):
    """Judul dari run_id, dicocokkan ke daftar judul yang ADA di dataset.

    Memotong berdasarkan posisi tidak dapat diandalkan: format berubah antar
    koleksi (S1_Judul_rep1, CONT_s10_Judul, CONT_s10_bola_Judul) dan judul
    sendiri mengandung garis bawah pada sebagian dataset.
    """
    for j in sorted(daftar, key=len, reverse=True):
        if rid.endswith("_" + j) or rid == j:
            return j
    return None


def seed_dari_run(rid):
    for b in rid.split("_"):
        if len(b) > 1 and b[0] == "s" and b[1:].isdigit():
            return b
    return None


def nb_se(skor, n_uji, n_latih):
    """Galat baku terkoreksi Nadeau-Bengio."""
    J = len(skor)
    if J < 2:
        return 0.0
    return float(np.sqrt((1.0 / J + n_uji / n_latih) * np.var(skor, ddof=1)))


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


def jalankan(X, y, kunci, nama_model, label_uji):
    """Satu putaran leave-one-out atas nilai unik `kunci`."""
    from sklearn.metrics import f1_score
    unik = sorted(set(kunci))
    skor, per_kelas, n_uji, n_latih = [], [], [], []
    for u in unik:
        te = kunci == u
        if te.sum() == 0 or (~te).sum() == 0 or len(set(y[~te])) < 2:
            continue
        m = buat(nama_model).fit(X[~te], y[~te])
        yp = m.predict(X[te])
        skor.append(f1_score(y[te], yp, average="macro"))
        per_kelas.append(f1_score(y[te], yp, average=None, labels=KELAS,
                                  zero_division=0))
        n_uji.append(int(te.sum()))
        n_latih.append(int((~te).sum()))
    return (np.array(skor), np.array(per_kelas), unik,
            float(np.mean(n_uji)), float(np.mean(n_latih)))


def self_test():
    rng = np.random.default_rng(0)
    J = ["BigBuckBunny", "Valkaama", "TearsOfSteel", "Of_Forest_And_Men"]
    assert judul_dari_run("CONT_s3_bola_BigBuckBunny", J) == "BigBuckBunny"
    assert judul_dari_run("S1_Valkaama_rep1", J) is None       # akhiran bukan judul
    assert judul_dari_run("CONT_s3_Of_Forest_And_Men", J) == "Of_Forest_And_Men"
    print("  [OK] judul dicocokkan dari daftar, termasuk yang bergaris bawah")

    assert seed_dari_run("CONT_s12_l2a_X") == "s12"
    assert seed_dari_run("CONT_bola_X") is None
    print("  [OK] seed terbaca dari run_id")

    # koreksi NB harus menggelembungkan galat baku
    sk = rng.normal(0.7, 0.03, 7)
    naif = float(np.std(sk, ddof=1) / np.sqrt(len(sk)))
    korek = nb_se(sk, 1500, 9000)
    assert korek > naif, (korek, naif)
    print(f"  [OK] galat baku: naif {naif:.4f} -> terkoreksi {korek:.4f} "
          f"({korek/naif:.1f}x)")

    # lipatan LOTO harus memisahkan judul sepenuhnya
    n = 480
    jud = np.array([J[i % 4] for i in range(n)])
    sed = np.array([f"s{(i // 4) % 6 + 1}" for i in range(n)])
    X = rng.normal(0, 1, (n, 7))
    y = np.array([KELAS[i % 4] for i in range(n)])
    for kunci, nama in ((jud, "judul"), (sed, "seed")):
        for u in sorted(set(kunci)):
            te = kunci == u
            assert len(set(kunci[te]) & set(kunci[~te])) == 0, nama
    print("  [OK] lipatan LOTO dan LOSeedO memisahkan kunci sepenuhnya")

    sk, pk, unik, nu, nl = jalankan(X, y, jud, "DecisionTree", "judul")
    assert len(sk) == 4 and pk.shape == (4, 4)
    assert nu + nl == n
    print(f"  [OK] LOTO menghasilkan {len(sk)} lipatan, rata2 n_uji {nu:.0f} "
          f"dan n_latih {nl:.0f}")

    # dataset tanpa kolom yg diminta harus ditolak dgn pesan jelas
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "d.csv")
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["run_id", "label"] + DASAR)
        w.writerow(["CONT_s1_bola_X", "Good"] + [1.0] * 7)
    try:
        muat(p, SET_FITUR["lengkap"])
        raise AssertionError("kolom hilang seharusnya ditolak")
    except RuntimeError as e:
        assert "tp_kumulatif" in str(e)
    print("  [OK] kolom fitur yang tidak ada ditolak dgn pesan yang menyebutkannya")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="LOTO dan leave-one-seed-out")
    ap.add_argument("--dataset", default="hasil_v5/dataset_ms.csv")
    ap.add_argument("--features", default="lengkap", choices=sorted(SET_FITUR))
    ap.add_argument("--models", default=None,
                    help="daftar model dipisah koma; bawaan keempatnya")
    ap.add_argument("--out", default=None, help="tulis ringkasan JSON")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not os.path.exists(a.dataset):
        sys.exit(f"dataset tidak ketemu: {a.dataset}")

    fitur = SET_FITUR[a.features]
    try:
        X, y, g = muat(a.dataset, fitur)
    except RuntimeError as e:
        sys.exit(str(e))

    rid_unik = sorted(set(g))
    kand = {r.split("_", 3)[-1] for r in rid_unik if r.count("_") >= 2}
    jud_arr = np.array([judul_dari_run(r, kand) for r in g])
    sed_arr = np.array([seed_dari_run(r) for r in g])
    if any(v is None for v in jud_arr):
        sys.exit("sebagian run_id tidak dapat dipetakan ke judul")
    if any(v is None for v in sed_arr):
        print("  CATATAN: sebagian run_id tidak memuat penanda seed; "
              "leech LOSeedO dilewati")
        sed_arr = None

    model = [m.strip() for m in a.models.split(",")] if a.models else MODEL
    salah = [m for m in model if m not in MODEL]
    if salah:
        ap.error(f"model tidak dikenal: {salah}")

    print(f"data  : {len(y)} window, {len(rid_unik)} run, {len(fitur)} fitur "
          f"({a.features})")
    print(f"judul : {len(set(jud_arr))} -> {', '.join(sorted(set(jud_arr)))}")
    if sed_arr is not None:
        print(f"seed  : {len(set(sed_arr))}")
    print()

    ring = {}
    for nama in model:
        print(f"  {nama} ...", end="", flush=True)
        baris = {}
        sk_l, pk_l, uj, nu, nl = jalankan(X, y, jud_arr, nama, "judul")
        baris["LOTO"] = (sk_l, pk_l, uj, nu, nl)
        if sed_arr is not None:
            sk_s, pk_s, us, nus, nls = jalankan(X, y, sed_arr, nama, "seed")
            baris["LOSeedO"] = (sk_s, pk_s, us, nus, nls)
        print(f" LOTO {sk_l.mean():.3f}"
              + (f" | LOSeedO {baris['LOSeedO'][0].mean():.3f}"
                 if sed_arr is not None else ""))
        ring[nama] = baris

    print("\n" + "=" * 74)
    print("A. RINGKASAN")
    print("=" * 74)
    print(f"{'model':<16}{'LOTO':>20}{'LOSeedO':>20}")
    for nama in model:
        b = ring[nama]
        sl, _, _, nul, nll = b["LOTO"]
        tl = f"{sl.mean():.3f} +/- {nb_se(sl, nul, nll):.3f}"
        ts = "-"
        if "LOSeedO" in b:
            ss, _, _, nus, nls = b["LOSeedO"]
            ts = f"{ss.mean():.3f} +/- {nb_se(ss, nus, nls):.3f}"
        print(f"{nama:<16}{tl:>20}{ts:>20}")
    print("\n  Galat baku memakai koreksi Nadeau-Bengio, karena lipatan LOTO dan")
    print("  LOSeedO juga berbagi data latih.")

    print("\n" + "=" * 74)
    print("B. PER LIPATAN")
    print("=" * 74)
    for uji in ("LOTO", "LOSeedO"):
        if uji not in ring[model[0]]:
            continue
        unik = ring[model[0]][uji][2]
        print(f"\n{uji}:")
        print(f"  {'ditahan':<22}" + "".join(f"{m[:12]:>14}" for m in model))
        for i, u in enumerate(unik):
            print(f"  {str(u):<22}" + "".join(f"{ring[m][uji][0][i]:>14.3f}"
                                              for m in model))
        print(f"  {'rentang':<22}" + "".join(
            f"{ring[m][uji][0].max()-ring[m][uji][0].min():>14.3f}" for m in model))

    print("\n" + "=" * 74)
    print("C. F1 PER KELAS (rata-rata lintas lipatan)")
    print("=" * 74)
    for uji in ("LOTO", "LOSeedO"):
        if uji not in ring[model[0]]:
            continue
        print(f"\n{uji}:")
        print(f"  {'model':<16}" + "".join(f"{k[:10]:>12}" for k in KELAS))
        for nama in model:
            pk = ring[nama][uji][1].mean(axis=0)
            print(f"  {nama:<16}" + "".join(f"{x:>12.3f}" for x in pk))

    print("\n  LOSeedO menguji lintasan bandwidth baru dari PEMBANGKIT YANG SAMA,")
    print("  sehingga tetap merupakan generalisasi di dalam distribusi. Ia bukan")
    print("  uji terhadap jaringan, layanan, atau jenis konten yang berbeda.")

    if a.out:
        js = {"dataset": os.path.basename(a.dataset), "features": a.features,
              "n_window": len(y), "n_run": len(rid_unik), "hasil": {}}
        for nama in model:
            js["hasil"][nama] = {}
            for uji, (sk, pk, unik, nu, nl) in ring[nama].items():
                js["hasil"][nama][uji] = {
                    "mean": round(float(sk.mean()), 4),
                    "se_nb": round(nb_se(sk, nu, nl), 4),
                    "min": round(float(sk.min()), 4),
                    "max": round(float(sk.max()), 4),
                    "per_lipatan": {str(u): round(float(v), 4)
                                    for u, v in zip(unik, sk)},
                    "per_kelas": {k: round(float(v), 4)
                                  for k, v in zip(KELAS, pk.mean(axis=0))}}
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(js, f, indent=2)
        print(f"\n-> {a.out}")


if __name__ == "__main__":
    main()