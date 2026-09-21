#!/usr/bin/env python3
"""
fitur_requet.py — set fitur berbasis chunk gaya Requet, dibatasi oleh sensor
================================================================================
Implementasi ulang struktur fitur Requet (Gutterman, Guo, Arora, Gilliland,
Wang, Wu, Katz-Bassett, Zussman; ACM MMSys 2019 dan ACM TOMM 16(2s), 2020)
sebagai baseline karya terdahulu.

INI BUKAN REPRODUKSI SETIA. Penyimpangannya LEBIH BESAR daripada pada ViCrypt,
dan seluruhnya merugikan Requet. Tabel di bawah harus disalin apa adanya ke
bagian keterbatasan naskah, karena tanpa itu perbandingannya menyesatkan.

TIGA PENYIMPANGAN YANG MERUGIKAN REQUET

1. DETEKSI GET TIDAK MUNGKIN, DIGANTI DETEKSI JEDA IDLE

   Requet mengenali paket uplink berpayload di atas 300 byte sebagai permintaan
   HTTP GET, lalu menjumlahkan payload downlink sampai GET berikutnya. Itu
   menuntut UKURAN PAKET INDIVIDUAL arah uplink, yang tidak direkam sensor ini.

   Requet sendiri menyebut dua pendekatan identifikasi chunk, dan yang kedua
   adalah memakai periode idle, dengan contoh 900 ms untuk trafik Netflix.
   Varian itulah yang dipakai di sini. Konsekuensinya batas chunk lebih kabur,
   dan chunk yang saling menyusul rapat dapat tergabung menjadi satu.

2. TIDAK ADA CHUNK AUDIO, DAN ITU JUSTRU FITUR TERKUAT REQUET

   Requet memisahkan chunk audio dari chunk video lewat DetectAV, dan
   menyatakan statistik chunk audio berkorelasi kuat dengan video state.
   Mereka mencontohkan bahwa jumlah chunk audio per jendela 20 detik adalah
   fitur penting, dan bahwa tanpa mengetahui video state sulit membedakan
   resolusi tinggi saat buffer menurun dari resolusi rendah saat buffer naik.

   Enam dari tujuh judul pada penelitian ini TIDAK MEMILIKI TREK AUDIO. Jadi
   60 dari 120 fitur Requet tidak ada, dan yang hilang justru separuh terkuatnya.
   Ini keterbatasan konten, bukan keterbatasan sensor, dan tidak dapat diperbaiki
   tanpa mengganti konten uji.

3. DETEKSI BERJALAN ATAS TRAFIK AGREGAT, BUKAN PER ALIRAN

   Requet mendeteksi chunk PER ALIRAN IP, memisahkan berdasarkan alamat, port,
   dan protokol. Cuplikan pada penelitian ini menjumlahkan byte lintas seluruh
   aliran, sehingga trafik video dan trafik latar tercampur dalam satu deret.

   Akibatnya terukur dan tidak samar: pada koleksi v5 dgn ambang idle 0,3 detik,
   laju deteksi BERBANDING TERBALIK dengan bandwidth. Seed berbandwidth tinggi
   menghasilkan median 16 chunk per 200 detik, sementara seed berbandwidth
   rendah menghasilkan 39. Penyebabnya trafik latar mengisi jeda antar segmen
   ketika throughput tinggi, sehingga batas chunk menghilang.

   Ini artefak instrumentasi, bukan sifat metode Requet, dan tidak dapat
   diperbaiki dengan menyetel ambang idle. Memperbaikinya menuntut cuplikan per
   aliran, yang berarti mengubah agen dan mengulang seluruh koleksi.

4. AMBANG UKURAN CHUNK DIKALIBRASI ULANG

   Requet membuang chunk di bawah 80 KB sebagai trafik latar, dikalibrasi untuk
   YouTube. Ladder pada penelitian ini memuat rung 45 kbps, yang pada segmen 4
   detik menghasilkan sekitar 22 KB. Ambang 80 KB akan membuang SELURUH chunk
   resolusi rendah. Ambang bawaan di sini 8 KB, dan nilainya dapat diatur.

  Kelompok fitur Requet     Asli  Di sini  Keterangan
  ------------------------  ----  -------  ------------------------------------
  Jendela video 10..200 s     60       60  utuh: jumlah chunk, rata-rata ukuran,
                                           rata-rata download time x 20 jendela
  Jendela audio 10..200 s     60        0  konten tanpa trek audio
  Metrik chunk terakhir        7        5  TTFB dan protokol tidak terukur
  ------------------------  ----  -------  ------------------------------------
  Total                      127       65

TTFB tidak dapat dihitung karena menuntut waktu permintaan GET. Requet sendiri
mencatat TTFB bermedian 0,05 detik dan menyederhanakan hubungannya menjadi
slack time = chunk duration - download time, sehingga ketiadaannya berdampak
kecil pada metrik lain.

Pakai:
  python fitur_requet.py --dir hasil_v5 --out-suffix _rq
  python merge_dataset.py --dir hasil_v5 --aligned-suffix _rq --out hasil_v5/dataset_rq.csv
  python fitur_requet.py --self-test
"""
import argparse
import csv
import glob
import json
import math
import os
import sys

# Requet memakai 20 jendela: 10, 20, ..., 200 detik ke belakang.
JENDELA = [10 * i for i in range(1, 21)]
PER_JENDELA = ["n_chunk", "ukuran_rata", "unduh_rata"]
TERAKHIR = ["ukuran", "durasi", "unduh", "slack", "laju_efektif"]


def nama_fitur():
    out = []
    for w in JENDELA:
        out += [f"rq_{k}_{w}s" for k in PER_JENDELA]
    return out + [f"rq_akhir_{k}" for k in TERAKHIR]


def ambil(r, *nama):
    for n in nama:
        if n in r and r[n] != "":
            return r[n]
    raise KeyError(nama[0])


def deteksi_chunk(smp, idle=0.9, min_byte=8192):
    """Chunk sebagai ledakan downlink yang dipisahkan periode idle.

    Ini varian (ii) yang disebut Requet, bukan deteksi GET yang mereka pakai.
    Sebuah chunk dimulai pada cuplikan bertrafik pertama setelah jeda, dan
    berakhir ketika trafik diam selama `idle` detik atau lebih.

    Mengembalikan daftar dict berisi: mulai, akhir, ukuran, unduh, slack,
    durasi, laju_efektif. Definisinya mengikuti Requet, dengan slack sebagai
    jeda sampai chunk berikutnya dan durasi sebagai selang antar awal chunk.
    """
    chunks = []
    aktif = None
    diam = 0.0
    for t, dt, b, _p in smp:
        if b > 0:
            if aktif is None:
                aktif = {"mulai": t, "akhir": t, "ukuran": 0}
            aktif["ukuran"] += b
            aktif["akhir"] = t
            diam = 0.0
        elif aktif is not None:
            diam += dt
            if diam >= idle:
                chunks.append(aktif)
                aktif = None
                diam = 0.0
    if aktif is not None:
        chunks.append(aktif)

    # Chunk terlalu kecil dianggap trafik latar, mengikuti gagasan DetectAV,
    # tetapi dgn ambang yg dikalibrasi ulang untuk ladder penelitian ini.
    chunks = [c for c in chunks if c["ukuran"] >= min_byte]

    out = []
    for i, c in enumerate(chunks):
        unduh = max(c["akhir"] - c["mulai"], 1e-9)
        if i + 1 < len(chunks):
            durasi = chunks[i + 1]["mulai"] - c["mulai"]
            slack = chunks[i + 1]["mulai"] - c["akhir"]
        else:
            durasi, slack = unduh, 0.0
        out.append({"mulai": c["mulai"], "akhir": c["akhir"],
                    "ukuran": c["ukuran"], "unduh": unduh,
                    "slack": max(slack, 0.0), "durasi": max(durasi, unduh),
                    "laju_efektif": c["ukuran"] * 8.0 / (max(durasi, unduh) * 1e6)})
    return out


def fitur_pada(chunks, t_now):
    """60 fitur jendela + 5 metrik chunk terakhir, pada saat t_now."""
    f = {}
    for w in JENDELA:
        sel = [c for c in chunks if t_now - w <= c["akhir"] <= t_now]
        n = len(sel)
        f[f"rq_n_chunk_{w}s"] = n
        f[f"rq_ukuran_rata_{w}s"] = round(sum(c["ukuran"] for c in sel) / n, 3) if n else 0.0
        f[f"rq_unduh_rata_{w}s"] = round(sum(c["unduh"] for c in sel) / n, 6) if n else 0.0
    lalu = [c for c in chunks if c["akhir"] <= t_now]
    if lalu:
        c = lalu[-1]
        for k in TERAKHIR:
            f[f"rq_akhir_{k}"] = round(float(c[k]), 6)
    else:
        for k in TERAKHIR:
            f[f"rq_akhir_{k}"] = 0.0
    return f


def proses_run(meta, cuplikan, panjang=10.0, idle=0.9, min_byte=8192):
    ps = float(meta["playback_start_epoch"])
    stalls = sorted(meta.get("stalls", []), key=lambda s: s["position"])
    dur = float(meta.get("media_duration", 0))

    def dinding(m):
        return ps + m + sum(s["duration"] for s in stalls if s["position"] < m)

    smp = sorted((float(ambil(r, "t_epoch")), float(ambil(r, "dt")),
                  int(ambil(r, "delta_bytes", "bytes")),
                  int(ambil(r, "delta_packets", "packets"))) for r in cuplikan)
    chunks = deteksi_chunk(smp, idle, min_byte)

    baris = []
    # Jumlah window HARUS sama dgn align_qos.py, yang membentuk window ekor
    # parsial. Memakai pembagian bulat menghasilkan satu window lebih sedikit
    # per run, sehingga dataset baseline dan dataset utama tidak berisi window
    # yang sama dan perbandingannya tidak lagi berpasangan.
    for i in range(math.ceil(dur / panjang)):
        t_now = dinding((i + 1) * panjang)
        # throughput_mean sbg RERATA per cuplikan, sama seperti align_qos.py.
        # Dipakai hanya sbg kunci penyaringan window tanpa trafik, supaya
        # ketiga dataset menyaring baris yg sama persis.
        a = dinding(i * panjang)
        mb = [(x[2] * 8.0) / (x[1] * 1e6) for x in smp if a <= x[0] < t_now
              and x[1] > 0]
        row = {"run_id": meta["run_id"], "window_index": i,
               "throughput_mean": round(sum(mb) / len(mb), 6) if mb else 0.0}
        row.update(fitur_pada(chunks, t_now))
        baris.append(row)
    return baris, chunks


def self_test():
    # trafik bercorak chunk: 1,5 detik mengunduh, 2,5 detik diam, berulang
    t0 = 1_700_000_000.0
    smp = []
    t = t0
    for siklus in range(12):
        for _ in range(15):
            smp.append((t, 0.1, 120_000, 90)); t += 0.1
        for _ in range(25):
            smp.append((t, 0.1, 0, 0)); t += 0.1
    ch = deteksi_chunk(smp)
    assert len(ch) == 12, len(ch)
    print(f"  [OK] {len(ch)} chunk terdeteksi dari 12 siklus unduh-diam")

    c = ch[0]
    assert abs(c["ukuran"] - 15 * 120_000) < 1, c["ukuran"]
    assert abs(c["unduh"] - 1.4) < 0.15, c["unduh"]
    assert abs(c["durasi"] - 4.0) < 0.15, c["durasi"]
    assert abs(c["slack"] - 2.6) < 0.15, c["slack"]
    # hubungan Requet: durasi = TTFB + unduh + slack, dgn TTFB tidak terukur
    assert abs(c["durasi"] - (c["unduh"] + c["slack"])) < 0.05
    print(f"  [OK] ukuran {c['ukuran']:,} B, unduh {c['unduh']:.2f}s, "
          f"slack {c['slack']:.2f}s, durasi = unduh + slack")

    # ambang idle menentukan penggabungan; jeda 0,5 s TIDAK memisahkan chunk
    rapat = []
    t = t0
    for _ in range(3):
        for _ in range(10):
            rapat.append((t, 0.1, 120_000, 90)); t += 0.1
        for _ in range(5):
            rapat.append((t, 0.1, 0, 0)); t += 0.1
    assert len(deteksi_chunk(rapat, idle=0.9)) == 1
    assert len(deteksi_chunk(rapat, idle=0.4)) == 3
    print("  [OK] jeda 0,5s menggabungkan chunk pd ambang 0,9s, memisah pd 0,4s")
    print("       (inilah kekaburan yg timbul krn deteksi GET tidak tersedia)")

    # ambang ukuran membuang ledakan kecil, dan 80 KB milik Requet terlalu besar
    kecil = [(t0 + i * 0.1, 0.1, 2_000 if i < 5 else 0, 2) for i in range(40)]
    assert len(deteksi_chunk(kecil, min_byte=8192)) == 1      # 10 KB lolos
    assert len(deteksi_chunk(kecil, min_byte=80 * 1024)) == 0  # ambang Requet
    print("  [OK] rung rendah (10 KB) lolos ambang 8 KB tetapi DIBUANG ambang "
          "80 KB milik Requet")

    # jumlah fitur sesuai tabel keterbatasan
    kol = nama_fitur()
    assert len(kol) == 20 * 3 + 5 == 65, len(kol)
    assert "throughput_mean" not in kol, "kolom penyaring bukan bagian set fitur"
    assert len(set(kol)) == len(kol)
    print(f"  [OK] {len(kol)} kolom (Requet asli 127; 60 fitur audio tidak ada)")

    # jendela 200 detik harus memuat lebih banyak chunk drpd jendela 10 detik
    f = fitur_pada(ch, t0 + 48.0)
    assert f["rq_n_chunk_200s"] >= f["rq_n_chunk_10s"]
    assert f["rq_n_chunk_200s"] == 12, f["rq_n_chunk_200s"]
    print(f"  [OK] jendela bertingkat: 10s memuat {f['rq_n_chunk_10s']} chunk, "
          f"200s memuat {f['rq_n_chunk_200s']}")

    # tanpa chunk sama sekali, seluruh fitur nol dan bukan NaN
    kosong = fitur_pada([], t0)
    assert all(v == 0 or v == 0.0 for v in kosong.values())
    print("  [OK] tanpa chunk seluruh fitur nol, bukan NaN")

    meta = {"run_id": "CONT_s1_bola_X", "playback_start_epoch": t0,
            "media_duration": 40.0, "stalls": [{"position": 0, "duration": 0.3}]}
    baris, _ = proses_run(meta, [{"t_epoch": a, "dt": b, "delta_bytes": c,
                                  "delta_packets": d} for a, b, c, d in smp])
    assert len(baris) == 4 and baris[-1]["rq_n_chunk_200s"] > 0
    assert all("throughput_mean" in b for b in baris)
    assert baris[0]["throughput_mean"] > 0
    print(f"  [OK] {len(baris)} window terbentuk dari media 40 detik")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Set fitur berbasis chunk gaya Requet")
    ap.add_argument("--dir", default="hasil_v5")
    ap.add_argument("--out-suffix", default="_rq")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--idle", type=float, default=0.9,
                    help="jeda pemisah chunk; 0.9 mengikuti contoh Netflix yg "
                         "disebut Requet sbg pendekatan alternatif")
    ap.add_argument("--min-byte", type=int, default=8192,
                    help="ambang ukuran chunk; Requet memakai 80 KB untuk "
                         "YouTube, terlalu besar untuk ladder penelitian ini")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)

    metas = sorted(glob.glob(os.path.join(a.dir, "*_client_metadata.json")))
    if not metas:
        sys.exit(f"tidak ada *_client_metadata.json di {a.dir}")
    n_ok = n_lewat = 0
    tot_chunk = 0
    for pm in metas:
        rid = os.path.basename(pm).replace("_client_metadata.json", "")
        ps = os.path.join(a.dir, f"{rid}_qos_samples.csv")
        if not os.path.exists(ps):
            n_lewat += 1
            continue
        meta = json.load(open(pm, encoding="utf-8"))
        meta.setdefault("run_id", rid)
        if not meta.get("playback_start_epoch"):
            n_lewat += 1
            continue
        cup = list(csv.DictReader(open(ps, encoding="utf-8")))
        baris, chunks = proses_run(meta, cup, a.window, a.idle, a.min_byte)
        if not baris:
            n_lewat += 1
            continue
        tot_chunk += len(chunks)
        out = os.path.join(a.dir, f"{rid}_qos_aligned{a.out_suffix}.csv")
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["run_id", "window_index",
                                              "throughput_mean"] + nama_fitur())
            w.writeheader()
            w.writerows(baris)
        n_ok += 1

    print(f"\n{n_ok} run diproses, {n_lewat} dilewati")
    print(f"chunk       : {tot_chunk:,} total, rata-rata {tot_chunk/max(n_ok,1):.1f} per run")
    print(f"deteksi     : jeda idle {a.idle}s, ambang ukuran {a.min_byte:,} B")
    print(f"fitur       : {len(nama_fitur())} kolom "
          f"(Requet asli 127; 60 fitur audio tidak ada pd konten ini)")
    print(f"\nPERIKSA jumlah chunk per run. Konten bersegmen 4 detik pada sesi")
    print(f"300 detik semestinya menghasilkan sekitar 75 chunk bila tiap segmen")
    print(f"terdeteksi terpisah. Angka yang jauh lebih kecil berarti chunk")
    print(f"tergabung, dan ambang --idle perlu diturunkan.")
    print(f"\nGabungkan:")
    print(f"  python merge_dataset.py --dir {a.dir} --aligned-suffix {a.out_suffix} "
          f"--out {a.dir}/dataset{a.out_suffix}.csv")


if __name__ == "__main__":
    main()