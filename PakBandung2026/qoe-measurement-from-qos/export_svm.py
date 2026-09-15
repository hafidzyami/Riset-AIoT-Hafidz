#!/usr/bin/env python3
"""
export_svm.py — ekspor SVM RBF ke Python mandiri tanpa scikit-learn
----------------------------------------------------------------------
Berbeda dengan model linear, SVM berkernel tidak dapat diringkas menjadi satu
vektor bobot. Keputusannya menuntut evaluasi kernel terhadap SETIAP support
vector, sehingga yang diekspor adalah seluruh support vector beserta koefisien
dualnya.

Konsekuensinya harus dinyatakan terus terang dan diukur, bukan diasumsikan:

  - Berkas hasil ekspor BESAR, sebanding dengan jumlah support vector dikali
    jumlah fitur. Ia tidak akan mendekati 2 KB seperti ekspor model linear.
  - Python murni kemungkinan LEBIH LAMBAT daripada scikit-learn, karena
    scikit-learn menjalankan evaluasi kernel di C. Ekspor ini menghapus
    dependensi, bukan mempercepat.

Karena itu kode yang dihasilkan memakai numpy bila tersedia dan jatuh ke Python
murni bila tidak. Keduanya menghasilkan prediksi yang sama; yang berbeda hanya
kecepatannya. Jalankan --self-test untuk melihat selisihnya pada mesin ini.

Penskalaan tidak dapat dilipat ke dalam bobot seperti pada model linear, karena
jarak Euclidean pada kernel RBF tidak kekal terhadap penskalaan. Rerata dan skala
karena itu disimpan dan diterapkan pada masukan.

Pakai:
  python export_svm.py --model model_v5/SVM_RBF.joblib --out svm_standalone.py
  python export_svm.py --self-test
"""
import argparse
import os
import sys

FITUR_DEFAULT = ["throughput_mean", "throughput_std", "throughput_min",
                 "throughput_max", "total_bytes", "total_packets", "active_flows",
                 "jitter_mean", "jitter_p95", "reorder_rate", "reorder_count",
                 "rtt_mean", "rtt_p95", "rtt_std"]


def bongkar(est):
    """Kembalikan (svc, scaler_atau_None); menerima pipeline maupun estimator."""
    if hasattr(est, "steps"):
        svc = sca = None
        for _, langkah in est.steps:
            if hasattr(langkah, "support_vectors_"):
                svc = langkah
            elif hasattr(langkah, "mean_") and hasattr(langkah, "scale_"):
                sca = langkah
        if svc is None:
            raise RuntimeError("pipeline tidak memuat SVC (support_vectors_)")
        return svc, sca
    if hasattr(est, "support_vectors_"):
        return est, None
    raise RuntimeError("model bukan SVC berkernel; untuk model linear pakai "
                       "export_linear.py")


def ambil_gamma(svc, n_fitur, X_contoh=None):
    """Nilai gamma efektif. sklearn menyimpannya di _gamma setelah fit."""
    g = getattr(svc, "_gamma", None)
    if g is not None:
        return float(g)
    raise RuntimeError("gamma efektif tidak ditemukan; model mungkin belum di-fit")


def tulis_kode(svc, sca, fitur, kelas):
    import numpy as np

    sv = np.asarray(svc.support_vectors_, dtype=float)
    dual = np.asarray(svc.dual_coef_, dtype=float)
    inter = np.asarray(svc.intercept_, dtype=float)
    nsup = [int(x) for x in svc.n_support_]
    gamma = ambil_gamma(svc, sv.shape[1])
    kelas = [str(k) for k in kelas]
    fitur = [str(t) for t in fitur]

    mean = [float(x) for x in sca.mean_] if sca is not None else [0.0] * len(fitur)
    skala = ([float(x) if float(x) != 0 else 1.0 for x in sca.scale_]
             if sca is not None else [1.0] * len(fitur))

    def baris_float(v, lebar=10):
        return "[" + ", ".join(f"{x:.{lebar}g}" for x in v) + "]"

    sv_txt = ",\n".join("    " + baris_float(r) for r in sv)
    dual_txt = ",\n".join("    " + baris_float(r) for r in dual)

    return f'''#!/usr/bin/env python3
"""
svm_standalone.py — model QoE SVM RBF hasil ekspor otomatis
-------------------------------------------------------------
Dihasilkan oleh export_svm.py. TIDAK memerlukan scikit-learn maupun joblib.
numpy dipakai bila tersedia karena jauh lebih cepat, tetapi tidak wajib.

Penskalaan TIDAK dilipat ke dalam bobot, karena jarak Euclidean pada kernel RBF
tidak kekal terhadap penskalaan. Rerata dan skala diterapkan pada masukan.

Keputusan multikelas memakai skema one-versus-one dengan pemungutan suara, sama
seperti libsvm yang dipakai scikit-learn.

Urutan fitur (wajib sama dengan saat pelatihan):
{chr(10).join(f"  [{i}] {t}" for i, t in enumerate(fitur))}

Jumlah kelas          : {len(kelas)}
Jumlah fitur          : {len(fitur)}
Jumlah support vector : {sv.shape[0]}
"""

try:
    import numpy as _np
except ImportError:
    _np = None

FITUR = {fitur!r}
KELAS = {kelas!r}
GAMMA = {gamma:.12g}
N_SUPPORT = {nsup!r}
MEAN = {baris_float(mean, 12)}
SKALA = {baris_float(skala, 12)}
INTERCEPT = {baris_float(inter, 12)}

# Support vector, sudah dalam ruang TERSKALA (mean 0, simpangan baku 1).
SV = [
{sv_txt},
]

# Koefisien dual, berbentuk (jumlah_kelas - 1) x jumlah_support_vector.
DUAL = [
{dual_txt},
]

_MULAI = [sum(N_SUPPORT[:i]) for i in range(len(N_SUPPORT))]
_SV_NP = _np.asarray(SV) if _np is not None else None
_DUAL_NP = _np.asarray(DUAL) if _np is not None else None
_MEAN_NP = _np.asarray(MEAN) if _np is not None else None
_SKALA_NP = _np.asarray(SKALA) if _np is not None else None


def _kernel_np(xs):
    d = _SV_NP - xs
    return _np.exp(-GAMMA * _np.einsum("ij,ij->i", d, d))


def _kernel_murni(xs):
    out = []
    for sv in SV:
        s = 0.0
        for a, b in zip(sv, xs):
            d = a - b
            s += d * d
        out.append(2.718281828459045 ** (-GAMMA * s))
    return out


def skor(x):
    """x = list {len(fitur)} angka sesuai urutan FITUR -> skor tiap pasangan kelas.

    Panjang masukan DIPERIKSA. Tanpa pemeriksaan ini, masukan yang terlalu pendek
    akan dipotong senyap oleh zip() dan menghasilkan jawaban yang tampak wajar.
    """
    if len(x) != len(FITUR):
        raise ValueError(
            f"model ini mengharapkan {{len(FITUR)}} fitur ({{', '.join(FITUR)}}), "
            f"tetapi menerima {{len(x)}}")
    if _np is not None:
        xs = (_np.asarray(x, dtype=float) - _MEAN_NP) / _SKALA_NP
        k = _kernel_np(xs)
    else:
        xs = [(v - m) / s for v, m, s in zip(x, MEAN, SKALA)]
        k = _kernel_murni(xs)

    n = len(KELAS)
    out, p = [], 0
    for i in range(n):
        for j in range(i + 1, n):
            ai, bi = _MULAI[i], _MULAI[i] + N_SUPPORT[i]
            aj, bj = _MULAI[j], _MULAI[j] + N_SUPPORT[j]
            if _np is not None:
                s = float(_DUAL_NP[j - 1, ai:bi] @ k[ai:bi]
                          + _DUAL_NP[i, aj:bj] @ k[aj:bj])
            else:
                s = sum(DUAL[j - 1][t] * k[t] for t in range(ai, bi))
                s += sum(DUAL[i][t] * k[t] for t in range(aj, bj))
            out.append(s + INTERCEPT[p])
            p += 1
    return out


def predict(x):
    """x = list {len(fitur)} angka sesuai urutan FITUR -> kelas QoE (str)."""
    d = skor(x)
    n = len(KELAS)
    suara = [0] * n
    p = 0
    for i in range(n):
        for j in range(i + 1, n):
            if d[p] > 0:
                suara[i] += 1
            else:
                suara[j] += 1
            p += 1
    return KELAS[suara.index(max(suara))]


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 1 + len(FITUR):
        print(predict([float(v) for v in sys.argv[1:]]))
    else:
        print("pakai: python svm_standalone.py " + " ".join(FITUR))
'''


def self_test():
    import tempfile
    import time

    import numpy as np
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC

    rng = np.random.default_rng(0)
    kelas4 = ["Excellent", "Good", "Degraded", "Critical"]
    F = FITUR_DEFAULT
    n, nf = 900, len(F)
    X = np.zeros((n, nf))
    y = np.empty(n, dtype=object)
    for i in range(n):
        k = kelas4[i % 4]
        pusat = 1.0 + (i % 4) * 2.0
        # skala antar-fitur sangat berbeda, supaya penanganan penskalaan teruji
        X[i] = rng.normal(pusat, 0.5, nf) * np.linspace(1, 5e5, nf)
        y[i] = k
    est = make_pipeline(StandardScaler(),
                        SVC(kernel="rbf", C=100, class_weight="balanced")).fit(X, y)
    svc, sca = bongkar(est)
    kode = tulis_kode(svc, sca, F, list(svc.classes_))

    d = tempfile.mkdtemp()
    p = os.path.join(d, "svm_standalone.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write(kode)
    sys.path.insert(0, d)
    import importlib
    mod = importlib.import_module("svm_standalone")

    sama = sum(1 for i in range(n) if mod.predict(list(X[i])) == est.predict([X[i]])[0])
    assert sama == n, f"hanya {sama}/{n} identik"
    print(f"  [OK] {sama}/{n} prediksi IDENTIK dengan pipeline sklearn "
          f"({len(svc.support_vectors_)} support vector)")

    # jalur Python murni harus memberi hasil sama dgn jalur numpy
    np_asli = mod._np
    mod._np = None
    sama2 = sum(1 for i in range(200) if mod.predict(list(X[i]))
                == est.predict([X[i]])[0])
    mod._np = np_asli
    assert sama2 == 200, sama2
    print("  [OK] jalur Python murni dan jalur numpy memberi hasil sama")

    # masukan berpanjang salah harus ditolak
    try:
        mod.predict([1.0, 2.0])
        raise AssertionError("masukan 2 fitur seharusnya ditolak")
    except ValueError as e:
        assert f"{len(F)} fitur" in str(e)
    print("  [OK] masukan berpanjang salah ditolak, tidak dipotong senyap")

    # ukuran dan latensi, diukur bukan diasumsikan
    kb = os.path.getsize(p) / 1024
    print(f"  [OK] ukuran berkas {kb:.1f} KB untuk {len(svc.support_vectors_)} "
          f"support vector x {nf} fitur")

    def ukur(fn, ulang=300):
        t0 = time.perf_counter()
        for i in range(ulang):
            fn(list(X[i % n]))
        return (time.perf_counter() - t0) / ulang * 1e3

    t_np = ukur(mod.predict)
    t_skl = ukur(lambda v: est.predict([v])[0])
    mod._np = None
    t_pure = ukur(mod.predict, 60)
    mod._np = np_asli
    print(f"  [OK] latensi per prediksi: numpy {t_np:.3f} ms, "
          f"python murni {t_pure:.3f} ms, sklearn {t_skl:.3f} ms")
    if t_pure > t_skl:
        print(f"       Python murni {t_pure/t_skl:.1f}x LEBIH LAMBAT dari sklearn, "
              f"sesuai perkiraan: evaluasi kernel dijalankan di C oleh sklearn.")
    assert "sklearn" not in [ln.split()[1] for ln in kode.split("\n")
                             if ln.strip().startswith("import ")]
    print("  [OK] kode tidak mengimpor sklearn maupun joblib")
    sys.path.remove(d)
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Ekspor SVM RBF ke Python mandiri")
    ap.add_argument("--model", default="model_v5/SVM_RBF.joblib")
    ap.add_argument("--out", default="svm_standalone.py")
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
    svc, sca = bongkar(est)
    fitur = [s.strip() for s in a.features.split(",") if s.strip()]
    n_harap = svc.support_vectors_.shape[1]
    if len(fitur) != n_harap:
        sys.exit(f"model mengharapkan {n_harap} fitur, tetapi {len(fitur)} nama "
                 f"diberikan lewat --features")

    kode = tulis_kode(svc, sca, fitur, list(svc.classes_))
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(kode)

    lama = os.path.getsize(a.model) / 1024
    baru = os.path.getsize(a.out) / 1024
    n_sv = svc.support_vectors_.shape[0]
    print(f"{a.model} ({lama:.1f} KB) -> {a.out} ({baru:.1f} KB)")
    print(f"  {n_sv} support vector x {n_harap} fitur")
    print(f"  {len(svc.classes_)} kelas: {', '.join(map(str, svc.classes_))}")
    print(f"  gamma {ambil_gamma(svc, n_harap):.6g}, "
          f"{len(svc.intercept_)} pasangan one-versus-one")
    print(f"  penskalaan {'disimpan terpisah' if sca is not None else 'tidak dipakai'}")
    print("\nCatatan: berkas ini jauh lebih besar daripada ekspor model linear, dan")
    print("Python murni kemungkinan lebih lambat daripada scikit-learn karena")
    print("evaluasi kernel dijalankan di C. Ukur di perangkat sasaran sebelum")
    print("memutuskan, misalnya dengan bench_inference.py.")


if __name__ == "__main__":
    main()