#!/usr/bin/env python3
"""
fitur_vicrypt.py — set fitur gaya ViCrypt, dibatasi oleh apa yang sensor rekam
================================================================================
Implementasi ulang struktur fitur ViCrypt (Wassermann, Seufert, Casas, Gang, Li,
IEEE TNSM 17(4):2007-2023, 2020) sebagai baseline karya terdahulu.

INI BUKAN REPRODUKSI SETIA, DAN PERBEDAANNYA HARUS DILAPORKAN.

ViCrypt menghitung 69 fitur per jendela dari tiga metrik tingkat paket: jumlah
paket, UKURAN TIAP PAKET, dan INTER-ARRIVAL TIME TIAP PAKET. Sensor pada
penelitian ini mengakumulasi byte dan paket per aliran pada laju 10 Hz, dan
TIDAK menyimpan ukuran paket individual, IAT individual, maupun pemisahan arah
uplink dan downlink.

Akibatnya 42 dari 69 fitur per jendela tidak dapat dihitung. Tabel di bawah
harus disalin apa adanya ke bagian keterbatasan naskah.

  Kelompok ViCrypt        Asli  Di sini  Yang hilang dan mengapa
  ----------------------  ----  -------  --------------------------------------
  Pencacah                  18        4  arah uplink/downlink tidak dipisah
                                         sensor; rasio TCP/UDP tidak dicatat
  Berbasis waktu             9        3  hanya total, bukan per arah
  Throughput                 6        2  hanya total, bukan per arah
  Regresi kumulatif          4        2  hanya total, bukan per arah
  Distribusi                32       16  momen dihitung atas VOLUME PER SLOT
                                         100 ms, bukan atas ukuran paket dan
                                         IAT individual
  ----------------------  ----  -------  --------------------------------------
  Total per jendela         69       27

Tiga jendela ViCrypt dipertahankan utuh: slot saat ini, trend window berisi t
slot terakhir, dan session window sejak sesi dimulai. Struktur inilah yang
ViCrypt sebut sebagai kontribusi utamanya, dan analisis kepentingan fitur mereka
menemukan bahwa **session window adalah kelompok terpenting**, bahkan menyamai
atau melampaui seluruh 208 fitur bila dipakai sendirian. Kelompok itu justru
yang sepenuhnya dapat dihitung di sini.

PENYIMPANGAN LAIN YANG HARUS DILAPORKAN

  Panjang slot. ViCrypt memakai 1 detik dan menyatakan telah menguji sampai 5
  detik tanpa perubahan berarti. Penelitian ini memakai 10 detik, mengikuti
  granularitas pelabelan P.1203-nya, sehingga BERADA DI LUAR rentang yang
  mereka uji.

  Target. ViCrypt memprediksi stalling, initial delay, resolusi, dan bitrate
  rata-rata. Di sini fiturnya dipakai untuk memprediksi kelas QoE turunan
  P.1203. Jadi yang dibandingkan adalah SET FITUR, bukan sistemnya.

Momen distribusi dihitung dengan pembaruan daring Pebay, sama seperti ViCrypt,
supaya sifat memori konstannya terjaga. Uji mandiri memverifikasi hasilnya
identik dengan perhitungan batch.

Pakai:
  python fitur_vicrypt.py --dir hasil_v5 --out-suffix _vc
  python merge_dataset.py --dir hasil_v5 --aligned-suffix _vc --out hasil_v5/dataset_vc.csv
  python fitur_vicrypt.py --self-test
"""
import argparse
import csv
import glob
import json
import math
import os
import sys

JENDELA = ("slot", "trend", "sesi")
DASAR = ["n_paket", "n_byte", "n_slot_aktif", "rasio_aktif",
         "t_ke_pertama", "t_stlh_terakhir", "durasi_burst",
         "tp_rata", "tp_burst", "reg_slope", "reg_intercept"]
MOMEN = ["mean", "var", "std", "cvar", "skew", "kurt", "min", "maks"]


def nama_fitur(jendela=JENDELA):
    """Daftar kolom yang dihasilkan, terurut dan stabil."""
    out = []
    for w in jendela:
        out += [f"vc_{w}_{k}" for k in DASAR]
        for m in ("vol", "pkt"):
            out += [f"vc_{w}_{m}_{s}" for s in MOMEN]
    return out + ["vc_indeks_slot"]


class Momen:
    """Momen daring sampai orde empat, mengikuti Pebay (2008).

    ViCrypt memakai perumusan ini agar ekstraksi fitur berjalan dengan memori
    konstan, dan itu salah satu klaim utamanya. Dipertahankan di sini supaya
    sifat tersebut ikut terwakili, bukan sekadar hasil angkanya.
    """

    __slots__ = ("n", "mean", "m2", "m3", "m4", "mn", "mx")

    def __init__(self):
        self.n = 0
        self.mean = 0.0
        self.m2 = self.m3 = self.m4 = 0.0
        self.mn = None
        self.mx = None

    def tambah(self, x):
        x = float(x)
        n1 = self.n
        self.n += 1
        d = x - self.mean
        dn = d / self.n
        dn2 = dn * dn
        t = d * dn * n1
        self.mean += dn
        self.m4 += (t * dn2 * (self.n * self.n - 3 * self.n + 3)
                    + 6 * dn2 * self.m2 - 4 * dn * self.m3)
        self.m3 += t * dn * (self.n - 2) - 3 * dn * self.m2
        self.m2 += t
        if self.mn is None or x < self.mn:
            self.mn = x
        if self.mx is None or x > self.mx:
            self.mx = x

    def hasil(self):
        if self.n == 0:
            return {k: 0.0 for k in MOMEN}
        var = self.m2 / (self.n - 1) if self.n > 1 else 0.0
        std = math.sqrt(var)
        return {
            "mean": self.mean,
            "var": var,
            "std": std,
            "cvar": std / self.mean if abs(self.mean) > 1e-12 else 0.0,
            "skew": (math.sqrt(self.n / (self.m2 ** 3)) * self.m3
                     if self.m2 > 1e-12 else 0.0),
            "kurt": (self.n * self.m4 / (self.m2 * self.m2) - 3.0
                     if self.m2 > 1e-12 else 0.0),
            "min": self.mn,
            "maks": self.mx,
        }


class Regresi:
    """Regresi linear daring atas trafik kumulatif terhadap waktu.

    ViCrypt memakai perumusan berbasis kovarians agar slope dan intercept dapat
    diperbarui tiap paket tanpa menyimpan riwayat.
    """

    __slots__ = ("n", "kum", "meanT", "meanS", "varT", "covTS", "t0")

    def __init__(self):
        self.n = 0
        self.kum = 0.0
        self.meanT = self.meanS = 0.0
        self.varT = self.covTS = 0.0
        self.t0 = None

    def tambah(self, t, ukuran):
        if self.t0 is None:
            self.t0 = t
        self.n += 1
        self.kum += ukuran
        dt = (t - self.t0) - self.meanT
        ds = self.kum - self.meanS
        self.varT += ((self.n - 1) / self.n * dt * dt - self.varT) / self.n
        self.covTS += ((self.n - 1) / self.n * dt * ds - self.covTS) / self.n
        self.meanT += dt / self.n
        self.meanS += ds / self.n

    def hasil(self):
        if self.n < 2 or self.varT <= 1e-12:
            return 0.0, 0.0
        slope = self.covTS / self.varT
        return slope, self.meanS - slope * self.meanT


def ambil(r, *nama):
    for n in nama:
        if n in r and r[n] != "":
            return r[n]
    raise KeyError(nama[0])


def fitur_jendela(cuplikan, t_awal, t_akhir):
    """27 fitur dari daftar cuplikan 100 ms dalam satu jendela."""
    f = {k: 0.0 for k in DASAR}
    mv, mp = Momen(), Momen()
    reg = Regresi()
    n_byte = n_pkt = n_aktif = 0
    t_pertama = t_terakhir = None
    for t, dt, b, p in cuplikan:
        n_byte += b
        n_pkt += p
        mv.tambah(b)
        mp.tambah(p)
        reg.tambah(t, b)
        if b > 0:
            n_aktif += 1
            if t_pertama is None:
                t_pertama = t
            t_terakhir = t
    n = len(cuplikan)
    durasi = max(t_akhir - t_awal, 1e-9)
    burst = (t_terakhir - t_pertama) if (t_pertama is not None
                                         and t_terakhir > t_pertama) else 0.0
    slope, inter = reg.hasil()
    f.update({
        "n_paket": n_pkt, "n_byte": n_byte, "n_slot_aktif": n_aktif,
        "rasio_aktif": n_aktif / n if n else 0.0,
        "t_ke_pertama": (t_pertama - t_awal) if t_pertama is not None else durasi,
        "t_stlh_terakhir": (t_akhir - t_terakhir) if t_terakhir is not None else durasi,
        "durasi_burst": burst,
        "tp_rata": n_byte * 8.0 / (durasi * 1e6),
        "tp_burst": (n_byte * 8.0 / (burst * 1e6)) if burst > 0 else 0.0,
        "reg_slope": slope, "reg_intercept": inter,
    })
    out = {k: round(float(v), 6) for k, v in f.items()}
    for pre, m in (("vol", mv), ("pkt", mp)):
        for k, v in m.hasil().items():
            out[f"{pre}_{k}"] = round(float(v), 6)
    return out


def proses_run(meta, cuplikan, panjang=10.0, trend=3):
    """Bentuk window waktu media, lalu hitung tiga jendela ViCrypt per window."""
    ps = float(meta["playback_start_epoch"])
    stalls = sorted(meta.get("stalls", []), key=lambda s: s["position"])
    dur = float(meta.get("media_duration", 0))

    def dinding(m):
        return ps + m + sum(s["duration"] for s in stalls if s["position"] < m)

    smp = []
    for r in cuplikan:
        smp.append((float(ambil(r, "t_epoch")), float(ambil(r, "dt")),
                    int(ambil(r, "delta_bytes", "bytes")),
                    int(ambil(r, "delta_packets", "packets"))))
    smp.sort()

    # Jumlah window HARUS sama dgn align_qos.py, yang membentuk window ekor
    # parsial. Memakai pembagian bulat menghasilkan satu window lebih sedikit
    # per run, sehingga dataset baseline dan dataset utama tidak berisi window
    # yang sama dan perbandingannya tidak lagi berpasangan.
    n_win = math.ceil(dur / panjang)
    baris = []
    for i in range(n_win):
        a, b = dinding(i * panjang), dinding((i + 1) * panjang)
        ia = dinding(max(0, i - trend + 1) * panjang)
        s0 = dinding(0.0)
        sel = {
            "slot": [x for x in smp if a <= x[0] < b],
            "trend": [x for x in smp if ia <= x[0] < b],
            "sesi": [x for x in smp if s0 <= x[0] < b],
        }
        rentang = {"slot": (a, b), "trend": (ia, b), "sesi": (s0, b)}
        # throughput_mean sbg RERATA per cuplikan, sama seperti align_qos.py,
        # bukan total dibagi durasi. Dipakai hanya sbg kunci penyaringan window
        # tanpa trafik, supaya ketiga dataset menyaring baris yg sama persis.
        mb = [(x[2] * 8.0) / (x[1] * 1e6) for x in sel["slot"] if x[1] > 0]
        row = {"run_id": meta["run_id"], "window_index": i,
               "throughput_mean": round(sum(mb) / len(mb), 6) if mb else 0.0}
        for w in JENDELA:
            for k, v in fitur_jendela(sel[w], *rentang[w]).items():
                row[f"vc_{w}_{k}"] = v
        row["vc_indeks_slot"] = i
        baris.append(row)
    return baris


def self_test():
    import random
    import tempfile

    # momen daring harus identik dgn perhitungan batch
    rng = random.Random(0)
    data = [rng.gauss(500, 120) for _ in range(400)] + [3000.0, 1.0]
    m = Momen()
    for x in data:
        m.tambah(x)
    h = m.hasil()
    n = len(data)
    mean = sum(data) / n
    var = sum((x - mean) ** 2 for x in data) / (n - 1)
    m2 = sum((x - mean) ** 2 for x in data)
    m3 = sum((x - mean) ** 3 for x in data)
    m4 = sum((x - mean) ** 4 for x in data)
    assert abs(h["mean"] - mean) < 1e-6, (h["mean"], mean)
    assert abs(h["var"] - var) < 1e-6, (h["var"], var)
    assert abs(h["skew"] - math.sqrt(n / m2 ** 3) * m3) < 1e-6
    assert abs(h["kurt"] - (n * m4 / (m2 * m2) - 3.0)) < 1e-6
    assert h["min"] == min(data) and h["maks"] == max(data)
    print(f"  [OK] momen daring Pebay identik dgn batch sampai orde empat "
          f"(skew {h['skew']:.4f}, kurt {h['kurt']:.4f})")

    # regresi daring harus memulihkan slope yg diketahui
    reg = Regresi()
    for i in range(200):
        reg.tambah(1000.0 + i * 0.1, 5000)          # 5000 byte tiap 0,1 s
    sl, _ = reg.hasil()
    assert abs(sl - 50000.0) / 50000.0 < 0.02, sl   # 5000/0,1 = 50 kB/s
    print(f"  [OK] regresi daring memulihkan slope {sl:,.0f} byte/detik")

    # jumlah fitur harus sesuai tabel keterbatasan: 27 per jendela
    per = len(DASAR) + 2 * len(MOMEN)
    assert per == 27, per
    kol = nama_fitur()
    assert len(kol) == 3 * 27 + 1 == 82, len(kol)
    assert "throughput_mean" not in kol, "kolom penyaring bukan bagian set fitur"
    assert len(set(kol)) == len(kol), "kolom duplikat"
    print(f"  [OK] {per} fitur per jendela x 3 jendela + 1 = {len(kol)} kolom "
          f"(ViCrypt asli: 69 x 3 + 1 = 208)")

    # ketiga jendela harus BERBEDA isinya, kalau sama berarti tdk ada informasi baru
    ps = 1_700_000_000.0
    meta = {"run_id": "CONT_s1_bola_X", "playback_start_epoch": ps,
            "media_duration": 60.0, "stalls": [{"position": 0, "duration": 0.4}]}
    cup = []
    for i in range(600):
        t = ps + i * 0.1
        b = 90_000 if i < 300 else 9_000        # separuh awal cepat, separuh lambat
        cup.append({"t_epoch": t, "dt": 0.1, "delta_bytes": b, "delta_packets": b // 1400})
    baris = proses_run(meta, cup)
    assert len(baris) == 6, len(baris)
    r = baris[5]
    assert r["vc_slot_tp_rata"] < r["vc_sesi_tp_rata"], (r["vc_slot_tp_rata"],
                                                         r["vc_sesi_tp_rata"])
    print(f"  [OK] jendela sesi mengingat masa lalu: slot "
          f"{r['vc_slot_tp_rata']:.2f} Mbps vs sesi {r['vc_sesi_tp_rata']:.2f} Mbps")

    r0, r5 = baris[0], baris[5]
    assert r5["vc_sesi_n_byte"] > r0["vc_sesi_n_byte"]
    assert r5["vc_indeks_slot"] == 5
    assert "throughput_mean" in r5 and r5["throughput_mean"] > 0
    print("  [OK] jendela sesi kumulatif, indeks slot bertambah, "
          "throughput_mean ikut dipancarkan")

    # trend window harus mencakup tepat t window, bukan seluruh sesi
    assert r5["vc_trend_n_byte"] < r5["vc_sesi_n_byte"]
    assert r5["vc_trend_n_byte"] > r5["vc_slot_n_byte"]
    print("  [OK] trend window berada di antara slot dan sesi")

    # window tanpa trafik tidak boleh menghasilkan NaN atau pembagian nol
    kosong = fitur_jendela([(0.0, 0.1, 0, 0)] * 10, 0.0, 1.0)
    assert all(isinstance(v, float) and not math.isnan(v) and not math.isinf(v)
               for v in kosong.values()), kosong
    assert kosong["tp_burst"] == 0.0 and kosong["rasio_aktif"] == 0.0
    print("  [OK] window tanpa trafik menghasilkan nol, bukan NaN atau inf")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Set fitur gaya ViCrypt (terbatas sensor)")
    ap.add_argument("--dir", default="hasil_v5")
    ap.add_argument("--out-suffix", default="_vc")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--trend", type=int, default=3,
                    help="jumlah window pada trend window; ViCrypt memakai 3")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)

    metas = sorted(glob.glob(os.path.join(a.dir, "*_client_metadata.json")))
    if not metas:
        sys.exit(f"tidak ada *_client_metadata.json di {a.dir}")
    n_ok = n_lewat = 0
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
        baris = proses_run(meta, cup, a.window, a.trend)
        if not baris:
            n_lewat += 1
            continue
        out = os.path.join(a.dir, f"{rid}_qos_aligned{a.out_suffix}.csv")
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["run_id", "window_index",
                                              "throughput_mean"] + nama_fitur())
            w.writeheader()
            w.writerows(baris)
        n_ok += 1

    print(f"\n{n_ok} run diproses, {n_lewat} dilewati")
    print(f"jendela     : slot {a.window}s, trend {a.trend} window, sesi kumulatif")
    print(f"fitur       : {len(nama_fitur())} kolom "
          f"(ViCrypt asli 208; lihat tabel keterbatasan di kepala berkas)")
    print(f"akhiran     : {a.out_suffix}")
    print(f"\nGabungkan:")
    print(f"  python merge_dataset.py --dir {a.dir} --aligned-suffix {a.out_suffix} "
          f"--out {a.dir}/dataset{a.out_suffix}.csv")


if __name__ == "__main__":
    main()