#!/usr/bin/env python3
"""
logreg_standalone.py — model QoE linear hasil ekspor otomatis
---------------------------------------------------------------
Dihasilkan oleh export_linear.py. TIDAK memerlukan scikit-learn, numpy, maupun
joblib. Cukup interpreter Python.

Penskalaan (StandardScaler) sudah DILIPAT ke dalam bobot, sehingga masukan
dipakai apa adanya tanpa normalisasi terlebih dahulu.

Urutan fitur (wajib sama dengan saat pelatihan):
  [0] throughput_mean
  [1] throughput_std
  [2] throughput_min
  [3] throughput_max
  [4] total_bytes
  [5] total_packets
  [6] active_flows

Jumlah kelas : 4
Jumlah fitur : 7
"""

FITUR = ['throughput_mean', 'throughput_std', 'throughput_min', 'throughput_max', 'total_bytes', 'total_packets', 'active_flows']
KELAS = ['Critical', 'Degraded', 'Excellent', 'Good']

# Baris ke-k adalah bobot untuk KELAS[k]; penskalaan sudah terlipat di sini.
BOBOT = [
    [-13.85427127, 1.344035663, 1.390014983, -0.02151674683, 3.273120366e-06, 0.002276697347, 0.383232666],
    [0.6153382493, 0.06108344112, -0.5544155703, -0.02302806012, 6.563897151e-08, -0.0005379081397, 0.1843864488],
    [7.82659746, -0.663449476, -0.3068565092, -0.00060535308, -1.947289272e-06, -0.0009745370333, -0.4810483846],
    [5.412335557, -0.7416696282, -0.5287429033, 0.04515016003, -1.391470066e-06, -0.0007642521738, -0.0865707302],
]
BIAS = [1.392344079, 0.3418136267, -1.346051732, -0.388105974]


def skor(x):
    """x = list 7 angka sesuai urutan FITUR -> skor tiap kelas.

    Panjang masukan DIPERIKSA. Tanpa pemeriksaan ini, zip() memotong secara
    senyap: masukan 7 angka pada model 14 fitur akan menghasilkan jawaban yang
    tampak wajar tetapi hanya memakai separuh bobot. Kesalahan seperti itu tidak
    memunculkan error sama sekali dan hanya terlihat sebagai akurasi yang buruk.
    """
    if len(x) != len(FITUR):
        raise ValueError(
            f"model ini mengharapkan {len(FITUR)} fitur ({', '.join(FITUR)}), "
            f"tetapi menerima {len(x)}")
    return [sum(w * xi for w, xi in zip(baris, x)) + bi
            for baris, bi in zip(BOBOT, BIAS)]


def predict(x):
    """x = list 7 angka sesuai urutan FITUR -> kelas QoE (str)."""
    s = skor(x)
    return KELAS[s.index(max(s))]


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 1 + len(FITUR):
        print(predict([float(v) for v in sys.argv[1:]]))
    else:
        print("pakai: python logreg_standalone.py " + " ".join(FITUR))
