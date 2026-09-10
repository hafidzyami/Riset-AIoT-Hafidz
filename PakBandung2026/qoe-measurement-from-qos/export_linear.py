#!/usr/bin/env python3
"""
export_linear.py — ekspor Logistic Regression ke Python murni tanpa dependensi
--------------------------------------------------------------------------------
Model hasil pelatihan disimpan sebagai pipeline joblib yang memerlukan
scikit-learn, numpy, dan joblib saat dimuat. Untuk perangkat edge, model linear
dapat diterjemahkan menjadi perkalian dan penjumlahan biasa.

Kunci penyederhanaannya: StandardScaler DILIPAT ke dalam bobot, sehingga tidak
perlu disertakan sama sekali. Bila z = W((x - mu) / s) + b, maka dengan
W' = W / s dan b' = b - W' . mu diperoleh z = W'x + b' yang setara persis.
Akibatnya berkas hasil ekspor hanya memuat satu matriks bobot dan satu vektor
bias, dan inferensinya cukup satu perkalian titik per kelas lalu argmax.

Manfaatnya untuk deployment:
  - tidak ada dependensi scikit-learn, numpy, maupun joblib
  - waktu muat mendekati nol
  - berkasnya dapat dibaca manusia sehingga bobot tiap fitur dapat diperiksa

Pakai:
  python export_linear.py --model model_v4/LogReg.joblib --out logreg_standalone.py
  python export_linear.py --self-test
"""
import argparse
import os
import sys

FITUR_DEFAULT = ["throughput_mean", "throughput_std", "throughput_min",
                 "throughput_max", "total_bytes", "total_packets", "active_flows",
                 "jitter_mean", "jitter_p95", "reorder_rate", "reorder_count",
                 "rtt_mean", "rtt_p95", "rtt_std"]


def bongkar(est):
    """Kembalikan (linear, scaler_atau_None); menerima pipeline maupun estimator."""
    if hasattr(est, "steps"):
        lin = sca = None
        for _, langkah in est.steps:
            if hasattr(langkah, "coef_"):
                lin = langkah
            elif hasattr(langkah, "mean_") and hasattr(langkah, "scale_"):
                sca = langkah
        if lin is None:
            raise RuntimeError("pipeline tidak memuat model linear (coef_)")
        return lin, sca
    if hasattr(est, "coef_"):
        return est, None
    raise RuntimeError("model bukan linear; export_linear.py hanya untuk coef_")


def lipat(lin, sca):
    """Lipat StandardScaler ke dalam bobot. Kembalikan (W_efektif, b_efektif).

    Bekerja untuk kasus biner (coef_ berbentuk 1 x n) maupun multikelas
    (n_kelas x n). Pada kasus biner, scikit-learn menyimpan satu baris bobot dan
    keputusan diambil dari tanda skor; di sini dibuat dua baris eksplisit agar
    jalur argmax berlaku seragam untuk semua jumlah kelas.
    """
    W = [list(map(float, baris)) for baris in lin.coef_]
    b = [float(x) for x in lin.intercept_]
    if sca is not None:
        mu = [float(x) for x in sca.mean_]
        s = [float(x) if float(x) != 0.0 else 1.0 for x in sca.scale_]
        W = [[w / sk for w, sk in zip(baris, s)] for baris in W]
        b = [bi - sum(w * m for w, m in zip(baris, mu))
             for bi, baris in zip(b, W)]
    if len(W) == 1:                      # biner -> dua baris agar argmax seragam
        W = [[-w for w in W[0]], W[0]]
        b = [-b[0], b[0]]
    return W, b


def tulis_kode(W, b, kelas, fitur):
    # Paksa ke str dan float biasa. Bila y bertipe array numpy, classes_ berisi
    # np.str_ yang repr-nya "np.str_('A')" sehingga kode hasil ekspor justru
    # menuntut numpy, padahal seluruh tujuannya adalah bebas dependensi.
    kelas = [str(k) for k in kelas]
    fitur = [str(t) for t in fitur]
    n = len(fitur)
    baris_w = ",\n".join(
        "    [" + ", ".join(f"{w:.10g}" for w in baris) + "]" for baris in W)
    return f'''#!/usr/bin/env python3
"""
logreg_standalone.py — model QoE linear hasil ekspor otomatis
---------------------------------------------------------------
Dihasilkan oleh export_linear.py. TIDAK memerlukan scikit-learn, numpy, maupun
joblib. Cukup interpreter Python.

Penskalaan (StandardScaler) sudah DILIPAT ke dalam bobot, sehingga masukan
dipakai apa adanya tanpa normalisasi terlebih dahulu.

Urutan fitur (wajib sama dengan saat pelatihan):
{chr(10).join(f"  [{i}] {t}" for i, t in enumerate(fitur))}

Jumlah kelas : {len(kelas)}
Jumlah fitur : {n}
"""

FITUR = {fitur!r}
KELAS = {list(kelas)!r}

# Baris ke-k adalah bobot untuk KELAS[k]; penskalaan sudah terlipat di sini.
BOBOT = [
{baris_w},
]
BIAS = [{", ".join(f"{x:.10g}" for x in b)}]


def skor(x):
    """x = list {n} angka sesuai urutan FITUR -> skor tiap kelas.

    Panjang masukan DIPERIKSA. Tanpa pemeriksaan ini, zip() memotong secara
    senyap: masukan 7 angka pada model 14 fitur akan menghasilkan jawaban yang
    tampak wajar tetapi hanya memakai separuh bobot. Kesalahan seperti itu tidak
    memunculkan error sama sekali dan hanya terlihat sebagai akurasi yang buruk.
    """
    if len(x) != len(FITUR):
        raise ValueError(
            f"model ini mengharapkan {{len(FITUR)}} fitur ({{', '.join(FITUR)}}), "
            f"tetapi menerima {{len(x)}}")
    return [sum(w * xi for w, xi in zip(baris, x)) + bi
            for baris, bi in zip(BOBOT, BIAS)]


def predict(x):
    """x = list {n} angka sesuai urutan FITUR -> kelas QoE (str)."""
    s = skor(x)
    return KELAS[s.index(max(s))]


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 1 + len(FITUR):
        print(predict([float(v) for v in sys.argv[1:]]))
    else:
        print("pakai: python logreg_standalone.py " + " ".join(FITUR))
'''


def self_test():
    import time

    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    rng = np.random.default_rng(0)
    kelas4 = ["Excellent", "Good", "Degraded", "Critical"]

    for n_kelas, nama in ((4, "multikelas"), (2, "biner")):
        n, nf = 800, len(FITUR_DEFAULT)
        X = np.zeros((n, nf))
        y = np.empty(n, dtype=object)
        for i in range(n):
            k = (kelas4 if n_kelas == 4 else ["Layak", "Tidak"])[i % n_kelas]
            pusat = 1.0 + (i % n_kelas) * 2.5
            # skala antar-fitur dibuat sangat berbeda agar pelipatan scaler
            # benar-benar teruji, bukan lolos karena datanya sudah seragam
            X[i] = rng.normal(pusat, 0.4, nf) * np.linspace(1, 5e5, nf)
            y[i] = k
        est = make_pipeline(StandardScaler(),
                            LogisticRegression(max_iter=3000)).fit(X, y)

        lin, sca = bongkar(est)
        W, b = lipat(lin, sca)
        kode = tulis_kode(W, b, list(lin.classes_), FITUR_DEFAULT)
        ruang = {}
        exec(compile(kode, "logreg_standalone.py", "exec"), ruang)
        pred = ruang["predict"]

        sama = sum(1 for i in range(n) if pred(list(X[i])) == est.predict([X[i]])[0])
        assert sama == n, f"{nama}: hanya {sama}/{n} identik"
        print(f"  [OK] {nama}: {sama}/{n} prediksi IDENTIK dengan pipeline sklearn")

        if n_kelas == 4:
            t0 = time.perf_counter()
            for i in range(3000):
                pred(list(X[i % n]))
            t_pure = (time.perf_counter() - t0) / 3000 * 1e6
            t0 = time.perf_counter()
            for i in range(3000):
                est.predict([X[i % n]])
            t_skl = (time.perf_counter() - t0) / 3000 * 1e6
            print(f"  [OK] latensi: python murni {t_pure:.1f} us vs sklearn "
                  f"{t_skl:.1f} us ({t_skl/t_pure:.0f}x lebih cepat)")
            # Periksa pernyataan IMPOR, bukan kemunculan kata. Nama pustaka
            # memang disebut di docstring justru untuk menyatakan bahwa ia
            # tidak dibutuhkan.
            impor = [ln.strip() for ln in kode.split("\n")
                     if ln.strip().startswith(("import ", "from "))]
            assert all("sys" in ln for ln in impor), impor
            print(f"  [OK] kode bebas dependensi, satu-satunya impor: "
                  f"{impor} ({len(kode)} byte)")

    # tanpa scaler pun harus tetap benar
    X = rng.normal(0, 1, (300, 6))
    y = np.array(["A", "B", "C"] * 100)
    est2 = LogisticRegression(max_iter=2000).fit(X, y)
    W2, b2 = lipat(*bongkar(est2))
    ruang2 = {}
    exec(compile(tulis_kode(W2, b2, list(est2.classes_), [f"f{i}" for i in range(6)]),
                 "x.py", "exec"), ruang2)
    sama = sum(1 for i in range(300)
               if ruang2["predict"](list(X[i])) == est2.predict([X[i]])[0])
    assert sama == 300, sama
    print(f"  [OK] estimator tanpa scaler: {sama}/300 identik")

    # masukan berpanjang salah harus DITOLAK, bukan dipotong senyap
    try:
        ruang2["predict"]([1.0, 2.0, 3.0])       # model butuh 6
        raise AssertionError("masukan 3 fitur pd model 6 fitur seharusnya ditolak")
    except ValueError as e:
        assert "6 fitur" in str(e), str(e)
    print("  [OK] masukan berpanjang salah ditolak, tidak dipotong senyap")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Ekspor model linear ke Python murni")
    ap.add_argument("--model", default="model_v4/LogReg.joblib")
    ap.add_argument("--out", default="logreg_standalone.py")
    ap.add_argument("--features", default=",".join(FITUR_DEFAULT),
                    help="urutan fitur, dipisah koma; harus sama dgn saat pelatihan")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not os.path.exists(a.model):
        sys.exit(f"model tidak ketemu: {a.model}")

    import joblib
    est = joblib.load(a.model)
    lin, sca = bongkar(est)
    fitur = [s.strip() for s in a.features.split(",") if s.strip()]
    n_harap = len(lin.coef_[0])
    if len(fitur) != n_harap:
        sys.exit(f"model mengharapkan {n_harap} fitur, tetapi {len(fitur)} nama "
                 f"diberikan lewat --features")

    W, b = lipat(lin, sca)
    kode = tulis_kode(W, b, list(lin.classes_), fitur)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(kode)

    lama = os.path.getsize(a.model) / 1024
    baru = os.path.getsize(a.out) / 1024
    print(f"{a.model} ({lama:.1f} KB) -> {a.out} ({baru:.1f} KB)")
    print(f"  {len(W)} kelas x {n_harap} fitur = {len(W)*n_harap} bobot + "
          f"{len(b)} bias")
    print(f"  kelas: {', '.join(map(str, lin.classes_))}")
    print(f"  penskalaan {'DILIPAT ke bobot' if sca is not None else 'tidak dipakai'}")
    print("\nPakai di Fase 2:")
    print(f"  sudo python3 infer_realtime.py --model {a.out} --pure")


if __name__ == "__main__":
    main()