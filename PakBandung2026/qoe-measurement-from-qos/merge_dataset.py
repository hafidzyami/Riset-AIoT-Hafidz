#!/usr/bin/env python3
"""
merge_dataset.py — gabungkan fitur QoS (X) dan label QoE (y) menjadi dataset training
--------------------------------------------------------------------------------------
Menggabungkan *_qos_aligned.csv dengan *_labels.csv berdasarkan (run_id, window_index).

ATURAN PENTING yang ditegakkan di sini:
  1. Hanya fitur JARINGAN yang menjadi X. Kolom mean_o22 dan stall_s berasal dari
     P.1203/telemetri klien -> data leakage bila dipakai sebagai fitur, sekaligus
     melanggar premis "inferensi dari sisi jaringan saja". Keduanya dibuang.
  2. Window 0 dibuang secara bawaan: fase startup mengisi buffer secepat mungkin,
     pola trafiknya berbeda dari kondisi mapan dan labelnya didominasi buffering awal.
  3. run_id dipertahankan sebagai kunci GROUP-SPLIT saat training (cegah data leakage
     antar window dari run yang sama).

Pakai:
  python merge_dataset.py --dir . --out dataset.csv
  python merge_dataset.py --dir hasil/ --keep-window0 --out dataset.csv

Uji tanpa data nyata:
  python merge_dataset.py --self-test
"""
import argparse
import csv
import glob
import os
import sys
from collections import Counter

# Fitur jaringan yang boleh menjadi X (tersedia juga saat Fase 2 / inference)
FEATURES = ["throughput_mean", "throughput_std", "throughput_min", "throughput_max",
            "jitter_mean", "jitter_p95", "reorder_rate", "reorder_count",
            "rtt_mean", "rtt_p95", "rtt_std",
            "total_bytes", "total_packets", "active_flows"]

# Kolom yang HARAM menjadi fitur (turunan label / sisi klien)
TERLARANG = {"mean_o22", "stall_s", "t_start", "t_media_start",
             "t_wall_start", "t_wall_end"}

# Metadata (BUKAN fitur): dibawa agar window parsial bisa disaring belakangan.
# Window terakhir tiap run selalu parsial, sehingga total_bytes/total_packets-nya
# rendah karena window-nya pendek, bukan karena trafiknya sedikit.
META = ["n_samples", "rtt_samples"]

OUT_FIELDS = ["run_id", "window_index"] + META + FEATURES + ["label"]


def kolom_keluaran(baris_qos):
    """Daftar kolom keluaran, termasuk fitur tambahan yang tidak dikenal.

    Daftar FEATURES bersifat tetap dan cocok untuk set fitur baku. Set fitur
    eksperimental seperti multiskala menambahkan kolom baru, dan tanpa
    penanganan ini kolom-kolom itu akan hilang senyap saat penggabungan
    sehingga eksperimennya tampak tidak berpengaruh.
    """
    dikenal = set(OUT_FIELDS) | TERLARANG
    ekstra = [k for k in baris_qos if k not in dikenal]
    return (["run_id", "window_index"] + META + FEATURES + sorted(ekstra) + ["label"],
            sorted(ekstra))


def baca_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def gabung(qos_rows, label_rows, keep_window0=False, min_samples=0):
    """Inner join pada (run_id, window_index). Kembalikan (baris, statistik)."""
    lab = {(r["run_id"], int(r["window_index"])): r for r in label_rows}
    out, stat = [], Counter()
    for q in qos_rows:
        key = (q["run_id"], int(q["window_index"]))
        l = lab.get(key)
        if l is None:
            stat["tanpa_label"] += 1
            continue
        if key[1] == 0 and not keep_window0:
            stat["window0_dibuang"] += 1
            continue
        if min_samples and int(q.get("n_samples", 0) or 0) < min_samples:
            stat["parsial_dibuang"] += 1
            continue
        row = {"run_id": key[0], "window_index": key[1], "label": l["label"]}
        for f in META + FEATURES:
            row[f] = q.get(f, "")
        # Kolom fitur di luar daftar baku ikut dibawa, agar set fitur
        # eksperimental (mis. multiskala) tidak hilang senyap. Kolom terlarang
        # tetap disaring karena merupakan turunan label atau sisi klien.
        for f in q:
            if f not in row and f not in TERLARANG:
                row[f] = q[f]
        out.append(row)
        stat["tergabung"] += 1
    stat["label_tanpa_qos"] = len(lab) - stat["tergabung"] - stat["window0_dibuang"]
    return out, stat


def self_test():
    def q(w, tp, ns="10"):
        return {"run_id": "R1", "window_index": str(w), "throughput_mean": str(tp),
                "throughput_std": "2", "throughput_min": "0", "throughput_max": "9",
                "jitter_mean": "1.5", "jitter_p95": "4.2", "reorder_rate": "0.8",
                "reorder_count": "7",
                "rtt_mean": "150.2", "rtt_p95": "180.0", "rtt_std": "12.3",
                "rtt_samples": "9", "total_bytes": "50", "total_packets": "5",
                "active_flows": "1", "n_samples": ns}
    qos = [q(0, 20), q(1, 5), q(6, 0, "11")]
    lab = [{"run_id": "R1", "window_index": "0", "mean_o22": "4.3", "stall_s": "1.9", "label": "Critical"},
           {"run_id": "R1", "window_index": "1", "mean_o22": "4.4", "stall_s": "0", "label": "Excellent"}]

    rows, st = gabung(qos, lab)
    assert len(rows) == 1 and rows[0]["window_index"] == 1, rows
    assert st["window0_dibuang"] == 1, st
    assert st["tanpa_label"] == 1, st            # window 6 tak punya label -> dibuang
    print("  [OK] inner join; window 0 dibuang; window tanpa label dibuang")

    kolom = set(rows[0].keys())
    assert not (kolom & TERLARANG), kolom & TERLARANG
    assert "mean_o22" not in kolom and "stall_s" not in kolom
    assert "label" in kolom and "run_id" in kolom
    print("  [OK] kolom bocor (mean_o22, stall_s) TIDAK ikut terbawa")

    q3 = [dict(qos[1]), dict(qos[1])]
    q3[1]["window_index"] = "2"; q3[1]["n_samples"] = "4"
    lab3 = lab + [{"run_id": "R1", "window_index": "2", "mean_o22": "4", "stall_s": "0", "label": "Good"}]
    r3, s3 = gabung(q3, lab3, min_samples=8)
    assert len(r3) == 1 and s3["parsial_dibuang"] == 1, (r3, s3)
    print("  [OK] --min-samples menyaring window parsial")
    assert "n_samples" in rows[0], rows[0].keys()
    print("  [OK] n_samples terbawa sbg METADATA (bukan fitur)")

    rows2, st2 = gabung(qos, lab, keep_window0=True)
    assert len(rows2) == 2, rows2
    print("  [OK] --keep-window0 mempertahankan window 0")

    assert set(FEATURES).issubset(set(rows[0].keys()))
    print(f"  [OK] {len(FEATURES)} fitur jaringan terbawa: {', '.join(FEATURES[:3])}, ...")

    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Gabungkan fitur QoS dan label QoE")
    ap.add_argument("--dir", default=".", help="folder berisi *_qos_aligned.csv dan *_labels.csv")
    ap.add_argument("--aligned-suffix", default="",
                    help="akhiran tambahan pada nama berkas fitur, mis. _wall_dasar. "
                         "Dipakai agar hasil ablasi dapat digabung tanpa menimpa "
                         "dataset utama")
    ap.add_argument("--out", default="dataset.csv")
    ap.add_argument("--keep-window0", action="store_true",
                    help="pertahankan window 0 (bawaan: dibuang, transien startup)")
    ap.add_argument("--min-samples", type=int, default=0,
                    help="buang window dengan cuplikan < N (mis. 8) agar window parsial tersaring")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)

    pola = f"*_qos_aligned{a.aligned_suffix}.csv"
    qos_files = sorted(glob.glob(os.path.join(a.dir, pola)))
    if a.aligned_suffix:
        # tanpa penyaringan ini, pola "*_qos_aligned.csv" juga cocok dgn berkas
        # berakhiran lain saat suffix kosong, dan sebaliknya
        qos_files = [p for p in qos_files
                     if os.path.basename(p).endswith(f"_qos_aligned{a.aligned_suffix}.csv")]
    else:
        qos_files = [p for p in qos_files
                     if os.path.basename(p).endswith("_qos_aligned.csv")]
    lab_files = sorted(glob.glob(os.path.join(a.dir, "*_labels.csv")))
    if not qos_files or not lab_files:
        sys.exit(f"tidak ketemu {pola} / *_labels.csv di {a.dir}")

    qos_rows = [r for p in qos_files for r in baca_csv(p)]
    lab_rows = [r for p in lab_files for r in baca_csv(p)]
    rows, st = gabung(qos_rows, lab_rows, a.keep_window0, a.min_samples)

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        fields, ekstra = kolom_keluaran(rows[0])
        if ekstra:
            print(f"  fitur tambahan terdeteksi ({len(ekstra)}): {', '.join(ekstra)}")
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)

    runs = sorted({r["run_id"] for r in rows})
    dist = Counter(r["label"] for r in rows)
    print(f"{len(qos_files)} berkas QoS + {len(lab_files)} berkas label -> {a.out}")
    print(f"  sampel      : {len(rows)} window dari {len(runs)} run")
    print(f"  distribusi  : {dict(dist)}")
    print(f"  dibuang     : window0={st['window0_dibuang']}, parsial={st['parsial_dibuang']}, "
          f"tanpa label={st['tanpa_label']}, label tanpa QoS={max(0, st['label_tanpa_qos'])}")
    print(f"  fitur (X)   : {', '.join(FEATURES)}")
    print(f"  target (y)  : label   |   kunci group-split: run_id")
    if len(runs) < 10:
        print("  CATATAN: unit independen = jumlah RUN, bukan jumlah window. "
              f"Saat ini {len(runs)} run.")


if __name__ == "__main__":
    main()