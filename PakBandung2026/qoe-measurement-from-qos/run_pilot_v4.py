#!/usr/bin/env python3
"""
run_pilot_v4.py — jalankan SELURUH matriks koleksi v4 tanpa tekan Enter
-------------------------------------------------------------------------
Memanggil run_one_v4.py berulang untuk tiap kombinasi seed dan judul, lalu
menggabungkan dataset dan meringkas distribusi kelas per run.

Sifat yang penting untuk pekerjaan panjang:
  - dapat dilanjutkan: run yang sudah punya berkas hasil akan dilewati
  - satu run gagal tidak menghentikan sisanya
  - coba ulang sekali bila run gagal
  - perkiraan waktu sisa ditampilkan

Pakai:
  # pilot: 4 seed x 2 judul = 8 run, sekitar 50 menit
  python run_pilot_v4.py --seeds 1,2,3,4 --titles BigBuckBunny,Valkaama \\
      --bola-seeds 3,4 --fresh

  # koleksi penuh: 14 seed x 7 judul = 98 run, sekitar 12 jam
  python run_pilot_v4.py --seeds 1-14 --titles semua --bola-seeds 8-14

  python run_pilot_v4.py --seeds 1,2 --titles BigBuckBunny --dry-run
  python run_pilot_v4.py --self-test
"""
import argparse
import csv
import glob
import os
import shutil
import subprocess
import sys
import time
from collections import Counter

JUDUL_SEMUA = ["BigBuckBunny", "ElephantsDream", "OfForestAndMen",
               "RedBullPlayStreets", "TearsOfSteel", "TheSwissAccount", "Valkaama"]


def urai_angka(teks):
    """'1,2,3' atau '1-14' atau '1,3-5' -> [1,2,3] / [1..14] / [1,3,4,5]."""
    out = []
    for bagian in teks.split(","):
        bagian = bagian.strip()
        if not bagian:
            continue
        if "-" in bagian:
            a, b = bagian.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(bagian))
    return sorted(set(out))


def urai_judul(teks):
    if teks.strip().lower() in ("semua", "all"):
        return list(JUDUL_SEMUA)
    out = []
    for t in teks.split(","):
        t = t.strip()
        if t:
            if t not in JUDUL_SEMUA:
                raise ValueError(f"judul tidak dikenal: {t}")
            out.append(t)
    return out


def rencana(seeds, judul, bola):
    """[(seed, judul, abr)] terurut judul lalu seed.

    Mode ABR dibagi per SEED, bukan per judul, supaya tiap judul mengalami
    kedua mode. Bila dibagi per judul, mode ABR akan tertukar dengan identitas
    konten saat analisis dan pengaruh keduanya tak dapat dipisahkan.
    """
    return [(s, t, "bola" if s in bola else "dynamic") for t in judul for s in seeds]


def sudah_ada(results, seed, judul):
    """True bila run ini sudah menghasilkan berkas fitur selaras."""
    p = os.path.join(results, f"CONT_s{seed}_{judul}_qos_aligned.csv")
    return os.path.exists(p) and os.path.getsize(p) > 200


def ringkas(results):
    """Distribusi kelas per run, dibaca dari berkas label."""
    baris = []
    for p in sorted(glob.glob(os.path.join(results, "CONT_*_labels.csv"))):
        rid = os.path.basename(p).replace("_labels.csv", "")
        try:
            d = list(csv.DictReader(open(p, encoding="utf-8")))
        except OSError:
            continue
        c = Counter(r["label"] for r in d if int(r["window_index"]) > 0)
        baris.append((rid, len(d), c))
    return baris


def self_test():
    assert urai_angka("1,2,3") == [1, 2, 3]
    assert urai_angka("1-5") == [1, 2, 3, 4, 5]
    assert urai_angka("1,3-5,9") == [1, 3, 4, 5, 9]
    assert urai_angka("2, 1 ,2") == [1, 2]
    print("  [OK] penguraian rentang angka: koma, tanda hubung, dan campuran")

    assert urai_judul("semua") == JUDUL_SEMUA
    assert urai_judul("BigBuckBunny,Valkaama") == ["BigBuckBunny", "Valkaama"]
    try:
        urai_judul("Salah")
        raise AssertionError("judul salah seharusnya ditolak")
    except ValueError:
        pass
    print(f"  [OK] penguraian judul, {len(JUDUL_SEMUA)} judul terdaftar")

    r = rencana([1, 2, 3, 4], ["BigBuckBunny", "Valkaama"], {3, 4})
    assert len(r) == 8
    assert r[0] == (1, "BigBuckBunny", "dynamic")
    assert r[2] == (3, "BigBuckBunny", "bola")
    print(f"  [OK] rencana pilot: {len(r)} run")

    # tiap judul harus mengalami KEDUA mode ABR
    per_judul = {}
    for s, t, m in r:
        per_judul.setdefault(t, set()).add(m)
    assert all(v == {"dynamic", "bola"} for v in per_judul.values()), per_judul
    print("  [OK] tiap judul mengalami dynamic maupun bola "
          "(mode ABR tidak tertukar dgn identitas konten)")

    penuh = rencana(list(range(1, 15)), JUDUL_SEMUA, set(range(8, 15)))
    assert len(penuh) == 98
    n_bola = sum(1 for _, _, m in penuh if m == "bola")
    assert n_bola == 49, n_bola
    print(f"  [OK] koleksi penuh: {len(penuh)} run, {n_bola} bola dan "
          f"{len(penuh)-n_bola} dynamic (terbagi rata)")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Jalankan seluruh matriks koleksi v4")
    ap.add_argument("--seeds", default="1,2,3,4", help="mis. 1,2,3,4 atau 1-14")
    ap.add_argument("--titles", default="BigBuckBunny,Valkaama",
                    help="dipisah koma, atau 'semua'")
    ap.add_argument("--bola-seeds", default="3,4",
                    help="seed yang memakai BOLA; sisanya dynamic")
    ap.add_argument("--results", default="hasil_v4")
    ap.add_argument("--duration", type=float, default=300)
    ap.add_argument("--fresh", action="store_true",
                    help="hapus folder hasil lebih dulu, mulai dari nol")
    ap.add_argument("--retry", type=int, default=1, help="percobaan ulang per run")
    ap.add_argument("--extra", default="",
                    help="argumen tambahan utk run_one_v4.py, mis. '--bg-max-mbps 0.8'")
    ap.add_argument("--no-merge", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)

    seeds = urai_angka(a.seeds)
    judul = urai_judul(a.titles)
    bola = set(urai_angka(a.bola_seeds)) if a.bola_seeds.strip() else set()
    daftar = rencana(seeds, judul, bola)
    ekstra = a.extra.split() if a.extra.strip() else []

    if a.fresh and os.path.isdir(a.results) and not a.dry_run:
        print(f">> --fresh: menghapus {a.results}/")
        shutil.rmtree(a.results)
    os.makedirs(a.results, exist_ok=True)

    lewati = [x for x in daftar if sudah_ada(a.results, x[0], x[1])]
    kerja = [x for x in daftar if x not in lewati]
    # satu run kira-kira durasi pemutaran + jeda muka + jeda akhir + overhead
    per_run = a.duration + 75
    print(f">> {len(daftar)} run direncanakan ({len(judul)} judul x {len(seeds)} seed)")
    print(f"   sudah ada : {len(lewati)}")
    print(f"   dijalankan: {len(kerja)}  (~{len(kerja)*per_run/3600:.1f} jam)")
    print(f"   mode ABR  : bola pada seed {sorted(bola) if bola else '(tidak ada)'}\n")

    if a.dry_run:
        for i, (s, t, m) in enumerate(daftar, 1):
            tag = "LEWATI" if (s, t, m) in lewati else "jalan "
            print(f"  [{i:>3}/{len(daftar)}] {tag}  seed {s:<3} {t:<20} abr={m}")
        return

    t0 = time.time()
    ok = gagal = 0
    for i, (s, t, m) in enumerate(kerja, 1):
        sisa = (len(kerja) - i + 1) * per_run
        print(f"[{i}/{len(kerja)}] seed {s} {t} abr={m}   "
              f"(perkiraan sisa {sisa/3600:.1f} jam)")
        cmd = [sys.executable, "run_one_v4.py", "--seed", str(s), "--title", t,
               "--abr", m, "--duration", str(a.duration),
               "--results", a.results] + ekstra
        berhasil = False
        for coba in range(1 + max(0, a.retry)):
            if coba:
                print(f"    coba ulang ke-{coba} ...")
                time.sleep(10)
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=a.duration + 600)
            for ln in (r.stdout or "").strip().split("\n"):
                if any(k in ln for k in ("verifikasi", "label:", "selaras:",
                                         "OK:", "PERINGATAN", "harness GAGAL")):
                    print(f"    {ln.strip()}")
            if r.returncode == 0 and sudah_ada(a.results, s, t):
                berhasil = True
                break
            pesan = (r.stderr or "").strip().split("\n")
            if pesan and pesan[-1]:
                print(f"    GAGAL: {pesan[-1][:150]}")
        if berhasil:
            ok += 1
        else:
            gagal += 1
            print("    dilewati, lanjut ke run berikutnya")

    lama = time.time() - t0
    print(f"\n>> selesai: {ok} berhasil, {gagal} gagal, {lama/3600:.2f} jam")

    if not a.no_merge and ok:
        print("\n== Menggabungkan dataset ==")
        subprocess.run([sys.executable, "merge_dataset.py", "--dir", a.results,
                        "--out", os.path.join(a.results, "dataset.csv")])

    baris = ringkas(a.results)
    if baris:
        print(f"\n== Distribusi kelas per run (tanpa window 0) ==")
        print(f"{'run':<34}{'win':>5}  distribusi")
        print("-" * 92)
        tot = Counter()
        for rid, n, c in baris:
            tot.update(c)
            print(f"{rid:<34}{n:>5}  {dict(c)}")
        n = sum(tot.values())
        print(f"\nTOTAL: {dict(tot)}")
        if n:
            print("proporsi: " + ", ".join(f"{k} {v/n*100:.1f}%"
                                           for k, v in tot.most_common()))
            kurang = [k for k in ("Excellent", "Good", "Degraded", "Critical")
                      if tot.get(k, 0) / n < 0.10]
            if kurang:
                print(f"\nPERHATIAN: kelas {kurang} di bawah 10 persen. Pertimbangkan")
                print("menggeser rentang bandwidth di tc_continuous.py sebelum koleksi penuh.")


if __name__ == "__main__":
    main()