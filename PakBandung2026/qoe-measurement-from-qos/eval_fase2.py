#!/usr/bin/env python3
"""
eval_fase2.py — ukur akurasi Fase 2 terhadap label sebenarnya
---------------------------------------------------------------
Membandingkan kelas yang dikeluarkan loop real-time dengan label P.1203 yang
dihitung dari telemetri klien pada sesi yang sama.

Ini pengukuran akurasi Fase 2 yang sesungguhnya. Membandingkan keluaran dengan
jadwal tc hanya proksi: tahap "unshaped" belum tentu berarti Excellent, karena
setelah bandwidth sempit pemutar masih perlu waktu mengisi buffer dan menaikkan
kualitas. Justru pada periode itulah throughput tinggi tidak berarti kualitas
tinggi, dan hanya label sebenarnya yang dapat menilainya.

Penyelarasannya kebalikan dari align_qos.py. Loop real-time bekerja dalam waktu
dinding, sedangkan label dibentuk dalam waktu media, sehingga tiap window
inferensi dipetakan ke waktu media lalu dicocokkan ke window label.

Pakai:
  python eval_fase2.py --infer inferensi_realtime.csv \\
      --meta client_metadata.json --labels FASE2_v4_labels.csv
  python eval_fase2.py --self-test
"""
import argparse
import csv
import os
import sys
from collections import Counter

KELAS = ["Excellent", "Good", "Degraded", "Critical"]


def wall_ke_media(t, ps, stalls, dur):
    """Kebalikan dari media_to_wall: petakan waktu dinding ke waktu media.

    wall(m) = ps + m + jumlah stall yang posisinya < m, sehingga pemetaan
    baliknya dicari dengan menelusuri stall secara berurutan. Stall pada posisi
    nol adalah buffering awal yang sudah tercakup di ps dan tidak dihitung.
    """
    sisa = t - ps
    if sisa < 0:
        return None
    m = 0.0
    for pos, d in sorted((p, d) for p, d in stalls if p > 0):
        # waktu dinding saat mencapai posisi stall ini
        if sisa <= pos - m + 1e-9:
            return m + sisa
        sisa -= (pos - m)
        m = pos
        if sisa <= d:                 # masih di dalam stall: media tidak maju
            return m
        sisa -= d
    m += sisa
    return m if m <= dur + 1e-6 else None


def macro_f1(pasang, kelas_hadir):
    """macro-F1 tanpa sklearn, agar sebanding langsung dengan evaluasi luring.

    Akurasi mentah tidak sebanding dengan macro-F1 yang dilaporkan train_compare:
    pada sesi yang didominasi satu kelas, akurasi bisa tinggi sementara macro-F1
    rendah. Keduanya perlu dihitung atas prediksi yang sama.
    """
    f1s = []
    for k in kelas_hadir:
        tp = sum(1 for _, _, p, t in pasang if p == k and t == k)
        fp = sum(1 for _, _, p, t in pasang if p == k and t != k)
        fn = sum(1 for _, _, p, t in pasang if p != k and t == k)
        pr = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * pr * rc / (pr + rc) if pr + rc else 0.0)
    return sum(f1s) / len(f1s) if f1s else 0.0


def muat_infer(path):
    """Ambil blok TERAKHIR saja bila berkas memuat beberapa sesi."""
    baris = list(csv.DictReader(open(path, encoding="utf-8")))
    if not baris:
        return []
    mulai = 0
    for i, r in enumerate(baris):
        if int(r["window"]) == 0:
            mulai = i
    return baris[mulai:]


def self_test():
    ps = 1000.0
    stalls = [(0.0, 0.5), (30.0, 4.0), (100.0, 2.0)]
    dur = 200.0
    # titik sebelum stall pertama: media = dinding
    assert abs(wall_ke_media(1010.0, ps, stalls, dur) - 10.0) < 1e-6
    # tepat di dalam stall pada posisi 30: media berhenti di 30
    assert abs(wall_ke_media(1032.0, ps, stalls, dur) - 30.0) < 1e-6
    # setelah stall 4 detik: media = dinding - 4
    assert abs(wall_ke_media(1044.0, ps, stalls, dur) - 40.0) < 1e-6
    # setelah kedua stall: media = dinding - 6
    assert abs(wall_ke_media(1116.0, ps, stalls, dur) - 110.0) < 1e-6
    print("  [OK] pemetaan waktu dinding ke waktu media, termasuk di dalam stall")

    assert wall_ke_media(999.0, ps, stalls, dur) is None
    assert wall_ke_media(1300.0, ps, stalls, dur) is None
    print("  [OK] titik di luar rentang pemutaran ditolak")

    # blok terakhir diambil bila berkas memuat beberapa sesi
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        w = csv.DictWriter(f, fieldnames=["window", "t_epoch", "kelas"])
        w.writeheader()
        for i in range(3):
            w.writerow({"window": i, "t_epoch": 1000 + i, "kelas": "Good"})
        for i in range(4):
            w.writerow({"window": i, "t_epoch": 2000 + i, "kelas": "Excellent"})
        nama = f.name
    got = muat_infer(nama)
    os.unlink(nama)
    assert len(got) == 4 and all(r["kelas"] == "Excellent" for r in got), got
    print(f"  [OK] hanya sesi terakhir diambil ({len(got)} window dari 7 baris)")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Akurasi Fase 2 vs label sebenarnya")
    ap.add_argument("--infer", default="inferensi_realtime.csv")
    ap.add_argument("--meta", default="client_metadata.json")
    ap.add_argument("--labels", required=False,
                    help="berkas *_labels.csv dari label_from_metadata.py")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--out-json", default=None,
                    help="tulis ringkasan sbg JSON agar dapat diagregasi")
    ap.add_argument("--quiet", action="store_true",
                    help="sembunyikan tabel per window")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)
    if not a.labels:
        ap.error("--labels wajib (jalankan label_from_metadata.py lebih dulu)")
    for p in (a.infer, a.meta, a.labels):
        if not os.path.exists(p):
            sys.exit(f"tidak ketemu: {p}")

    import json
    meta = json.load(open(a.meta, encoding="utf-8"))
    ps = meta["playback_start_epoch"]
    dur = float(meta["media_duration"])
    stalls = [(float(s["position"]), float(s["duration"])) for s in meta.get("stalls", [])]
    benar = {int(r["window_index"]): r["label"]
             for r in csv.DictReader(open(a.labels, encoding="utf-8"))}
    inf = muat_infer(a.infer)

    print(f"sesi   : media {dur:.1f}s, {len(stalls)} stall, "
          f"{len(benar)} window label")
    print(f"inferensi: {len(inf)} window (sesi terakhir dari {a.infer})\n")

    pasang = []
    for r in inf:
        if r["kelas"] in ("-", "TANPA-DATA"):
            continue
        # tengah window inferensi, agar tidak jatuh tepat di batas
        t = float(r["t_epoch"]) - a.window / 2
        m = wall_ke_media(t, ps, stalls, dur)
        if m is None:
            continue
        w = int(m // a.window)
        if w in benar:
            pasang.append((int(r["window"]), w, r["kelas"], benar[w]))

    if not pasang:
        sys.exit("tidak ada window yang dapat dipasangkan; periksa apakah "
                 "client_metadata.json berasal dari sesi yang sama")

    if not a.quiet:
        print(f"{'w_infer':>8}{'w_label':>9}{'prediksi':<12}{'sebenarnya':<12}  cocok")
        print("-" * 52)
        for wi, wl, pred, akt in pasang:
            print(f"{wi:>8}{wl:>9}{pred:<12}{akt:<12}"
                  f"  {'ya' if pred == akt else 'TIDAK'}")

    cocok = sum(1 for _, _, p, t2 in pasang if p == t2)
    hadir_semua = sorted({t2 for _, _, _, t2 in pasang} | {p for _, _, p, _ in pasang},
                         key=lambda k: KELAS.index(k) if k in KELAS else 9)
    mf1 = macro_f1(pasang, hadir_semua)
    print(f"\nakurasi : {cocok}/{len(pasang)} = {cocok/len(pasang)*100:.1f}%")
    print(f"macro-F1: {mf1:.3f}  (sebanding dgn angka luring train_compare)")

    # jarak antar kelas: salah satu tingkat jauh lebih ringan drpd salah tiga
    idx = {k: i for i, k in enumerate(KELAS)}
    jarak = [abs(idx[p] - idx[t2]) for _, _, p, t2 in pasang
             if p in idx and t2 in idx]
    if jarak:
        print(f"dalam 1 tingkat: {sum(1 for j in jarak if j <= 1)}/{len(jarak)} = "
              f"{sum(1 for j in jarak if j <= 1)/len(jarak)*100:.1f}%")
        print(f"rata-rata jarak kelas: {sum(jarak)/len(jarak):.2f} tingkat")

    print(f"\ndistribusi prediksi : {dict(Counter(p for _, _, p, _ in pasang))}")
    print(f"distribusi sebenarnya: {dict(Counter(t2 for _, _, _, t2 in pasang))}")

    hadir = [k for k in KELAS if any(t2 == k for _, _, _, t2 in pasang)]
    if hadir:
        print(f"\nconfusion (baris=sebenarnya, kolom=prediksi)")
        print(f"  {'':<11}" + "".join(f"{k[:9]:>11}" for k in hadir))
        for ak in hadir:
            baris = [sum(1 for _, _, p, t2 in pasang if t2 == ak and p == pk)
                     for pk in hadir]
            print(f"  {ak:<11}" + "".join(f"{x:>11}" for x in baris))

    if a.out_json:
        # Akurasi tebakan mayoritas disertakan karena itulah pembanding yang
        # tepat untuk sesi tunggal: bila model tidak melampauinya, akurasi
        # mentahnya menyesatkan tanpa konteks.
        akt_c = Counter(t2 for _, _, _, t2 in pasang)
        mayoritas = max(akt_c.values()) / len(pasang) if akt_c else 0.0
        # Confusion disimpan penuh agar agregasi lintas sesi dapat menghitung
        # macro-F1 gabungan, bukan merata-rata macro-F1 per sesi yang bias.
        conf = {}
        for _, _, p, t2 in pasang:
            conf.setdefault(t2, {}).setdefault(p, 0)
            conf[t2][p] += 1
        ringkas = {
            "n": len(pasang),
            "akurasi": round(cocok / len(pasang), 4),
            "macro_f1": round(mf1, 4),
            "confusion": conf,
            "akurasi_mayoritas": round(mayoritas, 4),
            "dalam_1_tingkat": round(sum(1 for j in jarak if j <= 1) / len(jarak), 4)
                               if jarak else None,
            "jarak_rata2": round(sum(jarak) / len(jarak), 4) if jarak else None,
            "jarak_2_plus": sum(1 for j in jarak if j >= 2) if jarak else None,
            "dist_sebenarnya": dict(akt_c),
            "dist_prediksi": dict(Counter(p for _, _, p, _ in pasang)),
        }
        with open(a.out_json, "w", encoding="utf-8") as f:
            json.dump(ringkas, f, indent=2)
        print(f"\n-> {a.out_json}")


if __name__ == "__main__":
    main()