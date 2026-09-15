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


def rencana_bersilang(seeds, judul, modes):
    """Rancangan BERSILANG: tiap seed dijalankan dengan SETIAP mode ABR.

    Karena satu seed berarti satu lintasan bandwidth yang identik, menjalankannya
    dengan seluruh mode membuat lintasan menjadi variabel terkendali. Selisih yang
    tersisa antar-mode karena itu dapat diatribusikan pada algoritma ABR, bukan
    pada rezim bandwidth.

    Rancangan lama (satu mode per seed) menyisakan konfound: kelompok bola dan
    dynamic ternyata menempati rezim bandwidth berbeda, dengan throughput median
    0,410 berbanding 0,829 Mbps pada koleksi v4.

    Diurutkan judul, lalu seed, lalu mode, supaya bila koleksi terputus di tengah
    jalan, seed yang sudah berjalan sudah lengkap seluruh modenya.
    """
    return [(s, t, m) for t in judul for s in seeds for m in modes]


def nama_run(seed, judul, abr, bersilang):
    return f"CONT_s{seed}_{abr}_{judul}" if bersilang else f"CONT_s{seed}_{judul}"


def sudah_ada(results, seed, judul, abr=None, bersilang=False):
    """True bila run ini sudah menghasilkan berkas fitur selaras."""
    rid = nama_run(seed, judul, abr, bersilang)
    return (os.path.exists(os.path.join(results, f"{rid}_qos_aligned.csv"))
            and os.path.getsize(os.path.join(results, f"{rid}_qos_aligned.csv")) > 200)


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

    # rancangan bersilang
    x = rencana_bersilang([1, 2], ["BigBuckBunny"], ["throughput", "dynamic", "bola"])
    assert len(x) == 6 and x[0] == (1, "BigBuckBunny", "throughput")
    per_seed = {}
    for s_, t_, m_ in x:
        per_seed.setdefault(s_, set()).add(m_)
    assert all(v == {"throughput", "dynamic", "bola"} for v in per_seed.values())
    print("  [OK] bersilang: tiap seed mengalami SETIAP mode ABR")
    assert nama_run(3, "Valkaama", "bola", True) == "CONT_s3_bola_Valkaama"
    assert nama_run(3, "Valkaama", "bola", False) == "CONT_s3_Valkaama"
    print("  [OK] run_id memuat mode ABR hanya pada rancangan bersilang")

    # pengacakan harus reprodusibel dan tidak kehilangan satu pun run
    import random as _r
    asli = rencana_bersilang([1, 2, 3], ["BigBuckBunny", "Valkaama"],
                             ["throughput", "bola"])
    a1, a2 = list(asli), list(asli)
    _r.Random(7).shuffle(a1)
    _r.Random(7).shuffle(a2)
    assert a1 == a2 and sorted(a1) == sorted(asli) and a1 != asli
    print(f"  [OK] pengacakan reprodusibel per seed, {len(a1)} run utuh tanpa hilang")
    # tanpa pengacakan, judul mengelompok di awal; dgn pengacakan, menyebar
    posisi_awal = [i for i, (_, t, _) in enumerate(asli) if t == "BigBuckBunny"]
    posisi_acak = [i for i, (_, t, _) in enumerate(a1) if t == "BigBuckBunny"]
    assert max(posisi_awal) < min(i for i, (_, t, _) in enumerate(asli)
                                  if t == "Valkaama")
    assert max(posisi_acak) > min(i for i, (_, t, _) in enumerate(a1)
                                  if t == "Valkaama")
    print("  [OK] tanpa acak judul mengelompok, dgn acak menyebar")

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
                    help="rancangan LAMA: seed yang memakai BOLA, sisanya dynamic")
    ap.add_argument("--abr-modes", default=None,
                    help="rancangan BERSILANG: daftar mode ABR dipisah koma, mis. "
                         "throughput,dynamic,bola. Tiap seed dijalankan dengan "
                         "SETIAP mode sehingga lintasan bandwidth terkendali. "
                         "Bila diberikan, --bola-seeds diabaikan")
    ap.add_argument("--results", default="hasil_v4")
    ap.add_argument("--shuffle", type=int, default=None, metavar="SEED",
                    help="acak urutan run dengan seed tertentu. Urutan bawaan "
                         "menempatkan judul di perulangan terluar, sehingga bila "
                         "ada penyimpangan yang berkembang sepanjang waktu (mis. "
                         "laju cuplik menurun karena map eBPF menumpuk), judul "
                         "menjadi terkonfound dengan penyimpangan itu. Pengacakan "
                         "menyebarkannya merata ke seluruh judul, seed, dan mode")
    ap.add_argument("--restart-sensor", action="store_true",
                    help="muat ulang layanan sensor di RPi 5 sebelum tiap run, agar "
                         "map eBPF selalu bersih dan laju cuplik tetap. UJI DULU "
                         "secara manual bahwa perintahnya tidak meminta kata sandi, "
                         "karena bila meminta, tiap run akan menggantung")
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
    SAH = ["throughput", "dynamic", "bola", "l2a", "lolp"]
    if a.abr_modes:
        modes = [m.strip().lower() for m in a.abr_modes.split(",") if m.strip()]
        tak_dikenal = [m for m in modes if m not in SAH]
        if tak_dikenal:
            ap.error(f"mode ABR tidak dikenal: {tak_dikenal}; pilihan: {SAH}")
        daftar = rencana_bersilang(seeds, judul, modes)
        bersilang, bola = True, set()
    else:
        bola = set(urai_angka(a.bola_seeds)) if a.bola_seeds.strip() else set()
        daftar = rencana(seeds, judul, bola)
        bersilang = False
        modes = sorted({m for _, _, m in daftar})
    ekstra = a.extra.split() if a.extra.strip() else []

    if a.fresh and os.path.isdir(a.results) and not a.dry_run:
        print(f">> --fresh: menghapus {a.results}/")
        shutil.rmtree(a.results)
    os.makedirs(a.results, exist_ok=True)

    if a.shuffle is not None:
        import random as _rnd
        _rnd.Random(a.shuffle).shuffle(daftar)

    lewati = [x for x in daftar if sudah_ada(a.results, x[0], x[1], x[2], bersilang)]
    kerja = [x for x in daftar if x not in lewati]
    # satu run kira-kira durasi pemutaran + jeda muka + jeda akhir + overhead
    per_run = a.duration + 75
    if bersilang:
        print(f">> RANCANGAN BERSILANG: {len(judul)} judul x {len(seeds)} seed x "
              f"{len(modes)} mode = {len(daftar)} run")
        print(f"   mode ABR  : {', '.join(modes)}")
        print("   tiap seed dijalankan dgn SETIAP mode, sehingga lintasan bandwidth")
        print("   menjadi variabel terkendali")
    else:
        print(f">> rancangan lama: {len(daftar)} run "
              f"({len(judul)} judul x {len(seeds)} seed)")
        print(f"   mode ABR  : bola pada seed {sorted(bola) if bola else '(tidak ada)'}")
    print(f"   sudah ada : {len(lewati)}")
    print(f"   dijalankan: {len(kerja)}  (~{len(kerja)*per_run/3600:.1f} jam)")
    if a.shuffle is not None:
        print(f"   urutan    : diacak dgn seed {a.shuffle}, sehingga penyimpangan "
              f"sepanjang waktu tidak sejajar dgn judul")
    if a.restart_sensor:
        print("   sensor    : dimuat ulang sebelum tiap run agar laju cuplik tetap")
    print()

    if a.dry_run:
        for i, (s, t, m) in enumerate(daftar, 1):
            tag = "LEWATI" if (s, t, m) in lewati else "jalan "
            print(f"  [{i:>3}/{len(daftar)}] {tag}  {nama_run(s, t, m, bersilang)}")
        return

    t0 = time.time()
    ok = gagal = 0
    for i, (s, t, m) in enumerate(kerja, 1):
        sisa = (len(kerja) - i + 1) * per_run
        print(f"[{i}/{len(kerja)}] {nama_run(s, t, m, bersilang)}   "
              f"(perkiraan sisa {sisa/3600:.1f} jam)")
        cmd = ([sys.executable, "run_one_v4.py", "--seed", str(s), "--title", t,
                "--abr", m, "--duration", str(a.duration),
                "--results", a.results]
               + (["--tandai-abr"] if bersilang else [])
               + (["--restart-sensor"] if a.restart_sensor else []) + ekstra)
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
            if r.returncode == 0 and sudah_ada(a.results, s, t, m, bersilang):
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