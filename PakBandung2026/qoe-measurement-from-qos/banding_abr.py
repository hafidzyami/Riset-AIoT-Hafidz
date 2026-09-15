#!/usr/bin/env python3
"""
banding_abr.py — apakah dua algoritma ABR benar-benar berperilaku berbeda?
----------------------------------------------------------------------------
Menjawab satu pertanyaan sebelum koleksi besar dijalankan: apakah menambahkan
sebuah mode ABR ke matriks benar-benar menambah titik yang berbeda, atau sekadar
menduplikasi mode yang sudah ada.

Pertanyaan ini nyata. L2A-LL dan LoL+ dirancang untuk LL-DASH dengan CMAF
berpotongan. Pada konten VOD bersegmen empat detik keduanya tetap berjalan, tetapi
bisa saja berperilaku praktis sama dengan aturan berbasis throughput. Bila demikian,
menambahkannya ke matriks hanya menghabiskan waktu koleksi tanpa menambah informasi.

Ukuran utamanya adalah KESAMAAN WAKTU MEDIA: berapa proporsi durasi pemutaran yang
menampilkan representasi sama persis, dihitung dari quality_timeline. Dua mode yang
berbeda secara bermakna seharusnya berbeda pada sebagian besar waktu.

Pakai:
  python banding_abr.py a.json b.json c.json
  python banding_abr.py --dir hasil_uji --pola "UJI_*_client_metadata.json"
  python banding_abr.py --self-test
"""
import argparse
import glob
import json
import os
import sys

# Di bawah ambang ini, dua mode dianggap tidak cukup berbeda untuk dimasukkan
# sebagai titik terpisah pada matriks koleksi.
AMBANG_DUPLIKAT = 0.85


def timeline_ke_deret(tl, durasi, langkah=0.5):
    """Ubah quality_timeline menjadi deret representasi per langkah waktu media.

    quality_timeline berisi titik perpindahan; yang dibandingkan adalah representasi
    apa yang sedang diputar pada tiap saat, sehingga perlu diisi maju.
    """
    # harness.js menulis t_media dan bitrate_kbps; alias lain diterima agar
    # tahan terhadap variasi penamaan antar-versi harness
    def waktu(x):
        for k in ("t_media", "t", "media_time"):
            if k in x:
                return float(x[k])
        return 0.0

    def rep(x):
        for k in ("bitrate_kbps", "bitrate", "representation"):
            if k in x:
                return str(x[k])
        return "?"

    titik = sorted(((waktu(x), rep(x)) for x in tl), key=lambda z: z[0])
    out, i, kini = [], 0, None
    t = 0.0
    while t < durasi:
        while i < len(titik) and titik[i][0] <= t:
            kini = titik[i][1]
            i += 1
        out.append(kini)
        t += langkah
    return out


def kesamaan(a, b):
    """Proporsi langkah waktu yang menampilkan representasi sama."""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    return sum(1 for i in range(n) if a[i] == b[i]) / n


def muat(path):
    m = json.load(open(path, encoding="utf-8"))
    mode = (m.get("abr") or {}).get("requested") or "?"
    dur = float(m.get("media_duration", 0))
    tl = m.get("quality_timeline", [])
    stalls = m.get("stalls", [])
    return {"path": os.path.basename(path), "mode": mode, "durasi": dur,
            "n_switch": len(tl), "n_stall": len(stalls),
            "stall_total": round(sum(float(s["duration"]) for s in stalls), 2),
            "deret": timeline_ke_deret(tl, dur)}


def self_test():
    # dua timeline identik
    tl = [{"t_media": 0, "bitrate_kbps": 1000}, {"t_media": 20, "bitrate_kbps": 2000}]
    a = timeline_ke_deret(tl, 40)
    b = timeline_ke_deret(tl, 40)
    assert kesamaan(a, b) == 1.0
    print(f"  [OK] timeline identik -> kesamaan {kesamaan(a, b):.2f}")

    # berbeda setelah detik ke-20
    tl2 = [{"t_media": 0, "bitrate_kbps": 1000}, {"t_media": 20, "bitrate_kbps": 500}]
    c = timeline_ke_deret(tl2, 40)
    k = kesamaan(a, c)
    assert abs(k - 0.5) < 0.05, k
    print(f"  [OK] separuh waktu berbeda -> kesamaan {k:.2f}")

    # pengisian maju: sebelum titik pertama bernilai None, sesudahnya terisi
    tl3 = [{"t_media": 10, "bitrate_kbps": 3000}]
    d = timeline_ke_deret(tl3, 20)
    assert d[0] is None and d[-1] == "3000", (d[0], d[-1])
    print("  [OK] representasi diisi maju dari titik perpindahan")

    # nama medan alias harus memberi hasil identik
    alias = [{"t": 0, "bitrate": 1000}, {"t": 20, "bitrate": 2000}]
    assert timeline_ke_deret(alias, 40) == a
    print("  [OK] alias t/bitrate memberi hasil identik dgn t_media/bitrate_kbps")

    # durasi berbeda dipotong ke yang terpendek
    e = timeline_ke_deret(tl, 100)
    assert kesamaan(a, e) == 1.0 and len(e) > len(a)
    print("  [OK] durasi tak sama dibandingkan pada bagian bertindih saja")

    # ambang duplikat
    assert 0.5 < AMBANG_DUPLIKAT < 1.0
    print(f"  [OK] ambang duplikat {AMBANG_DUPLIKAT:.2f}")
    print("\nSEMUA UJI LULUS")
    return True


def main():
    ap = argparse.ArgumentParser(description="Bandingkan perilaku algoritma ABR")
    ap.add_argument("berkas", nargs="*", help="berkas *_client_metadata.json")
    ap.add_argument("--dir", default=None)
    ap.add_argument("--pola", default="*_client_metadata.json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(0 if self_test() else 1)

    paths = list(a.berkas)
    if a.dir:
        paths += sorted(glob.glob(os.path.join(a.dir, a.pola)))
    paths = [p for p in paths if os.path.exists(p)]
    if len(paths) < 2:
        sys.exit("perlu minimal dua berkas metadata untuk dibandingkan")

    data = [muat(p) for p in paths]
    print(f"{'mode':<14}{'media':>9}{'switch':>9}{'stall':>8}{'total stall':>13}  berkas")
    for d in data:
        print(f"{d['mode']:<14}{d['durasi']:>8.1f}s{d['n_switch']:>9}{d['n_stall']:>8}"
              f"{d['stall_total']:>12.2f}s  {d['path']}")

    print(f"\n=== kesamaan waktu media antar-pasangan ===")
    print(f"{'pasangan':<30}{'kesamaan':>11}  putusan")
    duplikat = []
    for i in range(len(data)):
        for j in range(i + 1, len(data)):
            k = kesamaan(data[i]["deret"], data[j]["deret"])
            pas = f"{data[i]['mode']} vs {data[j]['mode']}"
            if k >= AMBANG_DUPLIKAT:
                duplikat.append((pas, k))
                v = "NYARIS SAMA"
            elif k >= 0.6:
                v = "mirip"
            else:
                v = "berbeda"
            print(f"{pas:<30}{k:>10.2f}  {v}")

    print()
    if duplikat:
        print("  Pasangan berikut memutar representasi yang sama pada sebagian besar")
        print("  waktu, sehingga menambahkan keduanya ke matriks koleksi hanya")
        print("  menduplikasi titik yang sama:")
        for pas, k in duplikat:
            print(f"    {pas}  ({k*100:.0f}% waktu sama)")
        print("\n  Saran: sertakan salah satu saja.")
    else:
        print("  Seluruh pasangan cukup berbeda; masing-masing menambah titik baru")
        print("  pada matriks koleksi.")

    print("\n  Catatan: perbandingan ini hanya sah bila seluruh berkas berasal dari")
    print("  KONDISI JARINGAN yang sama. Jalankan dengan seed tc yang sama dan judul")
    print("  yang sama, karena kondisi berbeda akan menghasilkan timeline berbeda")
    print("  terlepas dari algoritma ABR-nya.")


if __name__ == "__main__":
    main()